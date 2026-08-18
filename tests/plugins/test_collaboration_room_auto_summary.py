"""Auto-summary scheduling contract tests.

Covers the every_turns due detection, the single-flight background
summarizer, durable result/anchor persistence, and failure handling.
"""

import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "collaboration"
    / "dashboard"
    / "plugin_api.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "collaboration_room_auto_summary",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def make_room(module, **overrides):
    room = module.create_room_record("Sum room", ["default"], "owner-a", "gen-a")
    room["conversation_id"] = "chat_room_sum1"
    room.update(overrides)
    return room


def make_conversation(terminal_turns=0, messages=None):
    hosted_turns = {
        f"turn-{index}": {"status": "completed", "created_at": 1000 + index}
        for index in range(terminal_turns)
    }
    hosted_turns["turn-running"] = {"status": "running", "created_at": 9}
    return {
        "id": "chat_room_sum1",
        "owner_id": "owner-a",
        "account_generation": "gen-a",
        "messages": messages if messages is not None else [
            {"id": "m1", "role": "user", "content": "build the deploy"},
            {"id": "m2", "role": "assistant", "name": "default", "content": "done"},
        ],
        "hosted_turns": hosted_turns,
    }


def patch_module(module, room, conversation):
    module.owner_id_from_request = lambda _request: "owner-a"
    module._account_generation_for_request = lambda _request, _owner: "gen-a"
    module._account_generation_for_owner = lambda _owner: "gen-a"
    module.load_state = lambda: {"rooms": [room]}
    module.load_single_state = lambda: {"conversations": [conversation]}
    module.save_state = lambda state, _path=None: None
    module.save_single_state = lambda state: None
    module._notify_hosted_update = lambda _cid="": 0


class TestDueDetection:
    def test_not_due_without_profile(self):
        module = load_module()
        room = make_room(module, summary_config={"every_turns": 1})
        assert module._room_summary_due(room, make_conversation(terminal_turns=5)) is False

    def test_due_after_every_turns_terminal(self):
        module = load_module()
        room = make_room(
            module,
            summary_config={"profile": "default", "every_turns": 3},
        )
        assert module._room_summary_due(room, make_conversation(terminal_turns=3)) is True
        # Running turns do not count.
        assert module._room_summary_due(room, make_conversation(terminal_turns=2)) is False

    def test_anchor_prevents_retrigger(self):
        module = load_module()
        room = make_room(
            module,
            summary_config={"profile": "default", "every_turns": 2},
            summary={"status": "success", "turn_count": 4},
        )
        assert module._room_summary_due(room, make_conversation(terminal_turns=5)) is False
        assert module._room_summary_due(room, make_conversation(terminal_turns=6)) is True

    def test_active_lease_blocks_but_stale_lease_does_not(self):
        module = load_module()
        now = int(time.time() * 1000)
        room = make_room(
            module,
            summary_config={"profile": "default", "every_turns": 1},
            summary={"status": "summarizing", "lease_started_at": now},
        )
        assert module._room_summary_due(room, make_conversation(terminal_turns=3)) is False
        room["summary"]["lease_started_at"] = now - module._ROOM_SUMMARY_STALE_LEASE_MS - 1000
        assert module._room_summary_due(room, make_conversation(terminal_turns=3)) is True

    def test_recent_failure_backs_off(self):
        module = load_module()
        now = int(time.time() * 1000)
        room = make_room(
            module,
            summary_config={"profile": "default", "every_turns": 1},
            summary={"status": "failed", "last_attempt_at": now},
        )
        assert module._room_summary_due(room, make_conversation(terminal_turns=3)) is False
        room["summary"]["last_attempt_at"] = now - module._ROOM_SUMMARY_ATTEMPT_BACKOFF_MS - 1000
        assert module._room_summary_due(room, make_conversation(terminal_turns=3)) is True


class TestBackgroundSummarizer:
    def test_success_persists_summary_and_anchor(self):
        module = load_module()
        room = make_room(module, summary_config={"profile": "default", "every_turns": 1})
        conversation = make_conversation(terminal_turns=2)
        patch_module(module, room, conversation)
        module.run_single_turn = lambda profile, prompt: " 摘要：部署已完成。 "

        module._summarize_room_async(room["id"], "chat_room_sum1", "owner-a", "gen-a")
        thread = next(iter(module._ROOM_SUMMARY_THREADS.values()), None)
        if thread is not None:
            thread.join(timeout=10)
            assert not thread.is_alive()

        summary = room["summary"]
        assert summary["status"] == "success"
        assert summary["text"] == "摘要：部署已完成。"
        assert summary["turn_count"] == 2
        assert summary["version"] == 1
        assert summary.get("lease_started_at") is None

    def test_failure_records_error_and_retries_allowed_later(self):
        module = load_module()
        room = make_room(module, summary_config={"profile": "default", "every_turns": 1})
        patch_module(module, room, make_conversation(terminal_turns=1))

        def boom(_profile, _prompt):
            raise RuntimeError("profile offline")

        module.run_single_turn = boom
        module._summarize_room_async(room["id"], "chat_room_sum1", "owner-a", "gen-a")
        thread = next(iter(module._ROOM_SUMMARY_THREADS.values()), None)
        if thread is not None:
            thread.join(timeout=10)
            assert not thread.is_alive()

        summary = room["summary"]
        assert summary["status"] == "failed"
        assert "profile offline" in summary["last_error"]
        assert summary.get("lease_started_at") is None

    def test_prompt_carries_bounded_transcript(self):
        module = load_module()
        room = make_room(module, name="研究")
        messages = [
            {"id": f"m{i}", "role": "user", "content": f"msg {i}"} for i in range(80)
        ]
        prompt = module._room_summary_prompt(room, make_conversation(messages=messages))
        assert "研究" in prompt
        assert "msg 0" not in prompt
        assert "msg 79" in prompt

    def test_read_route_triggers_when_due(self):
        module = load_module()
        room = make_room(module, summary_config={"profile": "default", "every_turns": 1})
        conversation = make_conversation(terminal_turns=2)
        patch_module(module, room, conversation)
        dispatched = []
        module._summarize_room_async = (
            lambda room_id, cid, owner, gen: dispatched.append((room_id, cid, owner, gen))
        )

        response = module.get_room_summary(room["id"], SimpleNamespace(query_params={}))
        assert dispatched == [(room["id"], "chat_room_sum1", "owner-a", "gen-a")]
        assert response["summary"]["room_id"] == room["id"]
        assert response["config"]["profile"] == "default"


class TestDurableDeletionTombstones:
    def test_finalize_records_tombstone_visible_in_list(self):
        import json as _json
        module = load_module()
        conversation = {
            "id": "chat_durable_del01",
            "owner_id": "owner-a",
            "account_generation": "gen-a",
            "delete_requested": True,
            "delete_requested_at": 123,
            "messages": [],
            "hosted_turns": {},
        }
        saved = {}
        module.load_single_state = lambda: {"conversations": [conversation]}
        module.save_single_state = lambda state: saved.update(state)
        module._remove_room_index_for_conversation = lambda _cid, _owner: None
        module._delete_runtime_session = lambda profile, sid: True
        library = SimpleNamespace()
        library.delete_conversation = lambda *a, **k: {"files": 0}
        module._file_library = lambda: library
        module.release_hosted_gateway_conversation = lambda *a, **k: 0
        module._notify_hosted_update = lambda _cid="": 0

        ok = module._finalize_pending_conversation_deletion("chat_durable_del01")
        assert ok is True
        tombstones = saved.get("deletion_tombstones") or []
        assert any(
            isinstance(item, dict) and item.get("id") == "chat_durable_del01" for item in tombstones
        )

        # The tombstone surfaces in the list response for the owning account.
        responses = {}

        def capture_list(request=None):
            responses["value"] = module.get_single_conversations(request)
            return responses["value"]

        module.owner_id_from_request = lambda _request: "owner-a"
        module._account_generation_for_owner = lambda _owner: "gen-a"
        module.load_single_state = lambda: {"conversations": [], "deletion_tombstones": tombstones}
        module.save_single_state = lambda state: None
        module.resume_unfinished_hosted_workflows = lambda owned: None
        module.reconcile_conversation_runtime_results = lambda conversation: False
        module.reconcile_stale_hosted_turns = lambda conversation: False
        module.comp_conversation_title = lambda conversation: False
        result = module.get_single_conversations(SimpleNamespace(query_params={}))
        assert "chat_durable_del01" in (result.get("deleted") or [])

    def test_tombstones_expire_and_cap(self):
        module = load_module()
        now = int(time.time() * 1000)
        state = {
            "deletion_tombstones": [
                {"id": f"old-{i}", "owner_id": "o", "account_generation": "g", "deleted_at": now - 100 * 24 * 3600 * 1000}
                for i in range(3)
            ]
            + [
                {"id": f"new-{i}", "owner_id": "o", "account_generation": "g", "deleted_at": now - i}
                for i in range(module._DELETION_TOMBSTONE_MAX + 10)
            ]
        }
        assert module._prune_deletion_tombstones(state) is True
        kept = state["deletion_tombstones"]
        assert len(kept) <= module._DELETION_TOMBSTONE_MAX
        assert all(str(item.get("id", "")).startswith("new-") for item in kept)
