"""Studio group-chat completion contract tests.

Covers the collaboration plugin's agent roster management, structured
@mention routing, typing presence, invite-code membership, message
retraction, room summary/config updates, room workspace files, and the
member-access fencing added alongside the core room API.
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
        "collaboration_room_studio_completion",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    # Pydantic resolves model annotations through sys.modules; register the
    # module so RoomConfigBody-style dict[str, Any] fields build on first use.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def make_room(module, owner_id="owner-a", **overrides):
    room = module.create_room_record(
        "Studio room",
        ["default", "pc-worker"],
        owner_id,
        "gen-a",
    )
    room["conversation_id"] = "chat_room_studio1"
    room.update(overrides)
    return room


def patch_room_module(module, rooms, conversations=None, owner_id="owner-a"):
    module.owner_id_from_request = lambda _request, _owner=owner_id: _owner
    module._account_generation_for_request = (
        lambda _request, _owner_id, _owner=owner_id: "gen-a"
    )
    module._account_generation_for_owner = lambda _owner_id: "gen-a"
    module.load_state = lambda: {"rooms": list(rooms)}
    saved = {}

    def save_state(state, _path=None):
        saved["state"] = state

    module.save_state = save_state
    single_state = {"conversations": list(conversations or [])}

    def load_single_state():
        return single_state

    module.load_single_state = load_single_state
    module.save_single_state = lambda state: None
    module.available_profiles = lambda: [
        {"name": "default", "provider": "openai", "model": "gpt-test", "description": ""},
        {"name": "pc-worker", "provider": "anthropic", "model": "claude-test", "description": ""},
        {"name": "dbb3-worker", "provider": "openai", "model": "gpt-test", "description": ""},
    ]
    module._notify_hosted_update = lambda _conversation_id="": 0
    return saved


def request(query=None):
    return SimpleNamespace(query_params=query or {})


class TestMentionRouting:
    def test_agent_mention_targets_matching_profile(self):
        module = load_module()
        room = make_room(module)
        targets = module._resolve_room_mention_profiles(
            room,
            [{"type": "agent", "participantId": "pc-worker"}],
            {"default", "pc-worker"},
        )
        assert targets == ["pc-worker"]

    def test_agent_mention_matches_display_name(self):
        module = load_module()
        room = make_room(module)
        room["agents"] = [{"profile": "default", "name": "调度员", "description": ""}]
        targets = module._resolve_room_mention_profiles(
            room,
            [{"type": "agent", "participantId": "调度员"}],
            {"default", "pc-worker"},
        )
        assert targets == ["default"]

    def test_all_mention_targets_every_profile(self):
        module = load_module()
        room = make_room(module)
        targets = module._resolve_room_mention_profiles(
            room,
            [{"type": "all"}],
            {"default", "pc-worker"},
        )
        assert sorted(targets) == ["default", "pc-worker"]

    def test_unresolvable_mention_falls_back_to_all(self):
        module = load_module()
        room = make_room(module)
        targets = module._resolve_room_mention_profiles(
            room,
            [{"type": "agent", "participantId": "ghost"}],
            {"default", "pc-worker"},
        )
        assert sorted(targets) == ["default", "pc-worker"]

    def test_absent_mentions_keep_default_targeting(self):
        module = load_module()
        room = make_room(module)
        assert module._resolve_room_mention_profiles(room, None, {"default"}) is None
        assert module._resolve_room_mention_profiles(room, [], {"default"}) is None

    def test_normalized_mentions_carry_camel_case_fields(self):
        module = load_module()
        normalized = module._normalized_room_mentions(
            [{"type": "Agent", "participantId": "pc-worker", "displayName": "Worker"}]
        )
        assert normalized == [
            {
                "type": "agent",
                "participant_id": "pc-worker",
                "display_name": "Worker",
            }
        ]


class TestTypingPresence:
    def test_start_and_stop_typing(self):
        module = load_module()
        room = make_room(module)
        saved = patch_room_module(module, [room])

        module.set_room_typing(
            room["id"],
            module.RoomTypingBody(state="start", name="Given"),
            request(),
        )
        active = module._room_active_typing(room)
        assert [entry["name"] for entry in active] == ["Given"]
        assert saved["state"]["rooms"][0] is room

        module.set_room_typing(
            room["id"],
            module.RoomTypingBody(state="stop"),
            request(),
        )
        assert module._room_active_typing(room) == []

    def test_expired_typing_entries_are_pruned(self):
        module = load_module()
        room = make_room(module)
        room["typing"] = {
            "owner-a": {
                "id": "owner-a",
                "name": "Given",
                "expires_at": int(time.time() * 1000) - 1,
            }
        }
        assert module._room_active_typing(room) == []
        assert room["typing"] == {}


class TestInviteCodesAndMembership:
    def test_rotate_generates_unique_code(self):
        module = load_module()
        room = make_room(module)
        other = make_room(module, owner_id="owner-b")
        other["invite_code"] = "code0000001"
        patch_room_module(module, [room, other])

        response = module.rotate_room_invite_code(room["id"], module.RoomInviteCodeBody(), request())
        assert response["success"] is True
        assert response["invite_code"]
        assert response["invite_code"] != "code0000001"
        assert room["invite_code"] == response["invite_code"]

    def test_join_adds_member_and_is_idempotent(self):
        module = load_module()
        room = make_room(module, owner_id="owner-b")
        room["invite_code"] = "joincode01"
        patch_room_module(module, [room], owner_id="owner-a")

        response = module.join_room_by_code(module.RoomJoinBody(invite_code="joincode01"), request())
        assert response["room"]["id"] == room["id"]
        members = [m["user_id"] for m in response["members"]]
        assert "owner-a" in members and "owner-b" in members

        before = list(room["members"])
        module.join_room_by_code(module.RoomJoinBody(invite_code="joincode01"), request())
        assert room["members"] == before

    def test_join_rejects_unknown_code(self):
        module = load_module()
        patch_room_module(module, [])
        with pytest.raises(Exception) as excinfo:
            module.join_room_by_code(module.RoomJoinBody(invite_code="missing"), request())
        assert "not found" in str(excinfo.value).lower()

    def test_member_can_access_room_but_not_manage(self):
        module = load_module()
        room = make_room(module, owner_id="owner-b")
        room["members"] = [
            {"id": "m1", "user_id": "owner-a", "name": "Given", "role": "member"}
        ]
        patch_room_module(module, [room], owner_id="owner-a")

        agents = module.list_room_agents(room["id"], request())
        assert {a["profile"] for a in agents["agents"]} == {"default", "pc-worker"}

        with pytest.raises(Exception) as excinfo:
            module.add_room_agent(
                room["id"],
                module.RoomAgentBody(profile="default"),
                request(),
            )
        assert excinfo.value.status_code == 404

    def test_owner_cannot_be_removed_as_member(self):
        module = load_module()
        room = make_room(module)
        patch_room_module(module, [room])
        with pytest.raises(Exception) as excinfo:
            module.remove_room_member(room["id"], "owner-a", request())
        assert excinfo.value.status_code == 409


class TestAgentRoster:
    def test_add_update_remove_agent(self):
        module = load_module()
        room = make_room(module)
        patch_room_module(module, [room])

        added = module.add_room_agent(
            room["id"],
            module.RoomAgentBody(profile="dbb3-worker", name="执行员"),
            request(),
        )
        assert added["agent"]["profile"] == "dbb3-worker"
        assert added["agent"]["name"] == "执行员"
        assert "dbb3-worker" in room["profiles"]

        updated = module.update_room_agent(
            room["id"],
            "profile:dbb3-worker",
            module.RoomAgentUpdateBody(name="DBB3"),
            request(),
        )
        assert updated["agent"]["name"] == "DBB3"
        assert {a["profile"] for a in updated["agents"]} >= {
            "default",
            "pc-worker",
            "dbb3-worker",
        }

        removed = module.remove_room_agent(room["id"], "dbb3-worker", request())
        assert removed["success"] is True
        assert "dbb3-worker" not in room["profiles"]

    def test_add_rejects_unknown_profile(self):
        module = load_module()
        room = make_room(module)
        patch_room_module(module, [room])
        with pytest.raises(Exception) as excinfo:
            module.add_room_agent(
                room["id"],
                module.RoomAgentBody(profile="ghost-profile"),
                request(),
            )
        assert excinfo.value.status_code == 404

    def test_last_agent_cannot_be_removed(self):
        module = load_module()
        room = make_room(module)
        room["profiles"] = ["default"]
        patch_room_module(module, [room])
        with pytest.raises(Exception) as excinfo:
            module.remove_room_agent(room["id"], "default", request())
        assert excinfo.value.status_code == 409


class TestRoomConfigAndSummary:
    def test_config_update_persists_all_sections(self):
        module = load_module()
        room = make_room(module)
        patch_room_module(module, [room])

        response = module.update_room_config(
            room["id"],
            module.RoomConfigBody(
                name="研发讨论",
                workspace="research",
                summary={"profile": "default", "every_turns": 10},
                context={"trigger_tokens": 50000},
                settings={"allow_guest_agents": 1},
            ),
            request(),
        )
        assert response["room"]["name"] == "研发讨论"
        assert room["workspace"] == "research"
        assert room["summary_config"]["every_turns"] == 10
        assert room["context_config"]["trigger_tokens"] == 50000
        assert room["settings"]["allow_guest_agents"] == 1

    def test_summary_roundtrip_bumps_version(self):
        module = load_module()
        room = make_room(module)
        patch_room_module(module, [room])

        first = module.update_room_summary(
            room["id"], module.RoomSummaryBody(summary="first"), request()
        )
        assert first["summary"]["version"] == 1
        second = module.update_room_summary(
            room["id"], module.RoomSummaryBody(summary="second"), request()
        )
        assert second["summary"]["version"] == 2
        assert second["summary"]["summary"] == "second"

        state = module.get_room_summary(room["id"], request())
        assert state["summary"]["summary"] == "second"
        assert state["config"] == {}


class TestRetraction:
    def make_conversation(self, module, room):
        return {
            "id": room["conversation_id"],
            "owner_id": "owner-a",
            "account_generation": "gen-a",
            "messages": [
                {
                    "id": "msg-1",
                    "role": "user",
                    "name": "User",
                    "content": "hello world",
                    "meta": {"room_id": room["id"], "sender_id": "owner-a"},
                },
                {
                    "id": "msg-2",
                    "role": "user",
                    "name": "User",
                    "content": "from member",
                    "meta": {"room_id": room["id"], "sender_id": "owner-b"},
                },
            ],
            "hosted_turns": {},
        }

    def test_sender_retracts_own_message(self):
        module = load_module()
        room = make_room(module)
        conversation = self.make_conversation(module, room)
        patch_room_module(module, [room], [conversation])

        response = module.retract_room_message(room["id"], "msg-1", request())
        assert response["retracted"] is True
        message = conversation["messages"][0]
        assert message["meta"]["retracted"] is True
        assert message["content"] == "[已撤回]"

        replay = module.retract_room_message(room["id"], "msg-1", request())
        assert replay["replayed"] is True

    def test_member_cannot_retract_foreign_message(self):
        module = load_module()
        room = make_room(module, owner_id="owner-z")
        room["members"] = [
            {"id": "m1", "user_id": "owner-a", "name": "Given", "role": "member"}
        ]
        conversation = self.make_conversation(module, room)
        patch_room_module(module, [room], [conversation], owner_id="owner-a")

        with pytest.raises(Exception) as excinfo:
            module.retract_room_message(room["id"], "msg-2", request())
        assert excinfo.value.status_code == 403


class TestClearContext:
    def test_clear_resets_transcript_and_mailbox(self):
        module = load_module()
        room = make_room(module)
        room["mailbox"] = [{"id": "mb1"}]
        conversation = {
            "id": room["conversation_id"],
            "owner_id": "owner-a",
            "account_generation": "gen-a",
            "messages": [{"id": "m", "role": "user", "content": "x"}],
            "hosted_turns": {},
        }
        patch_room_module(module, [room], [conversation])

        response = module.clear_room_context(room["id"], request())
        assert response["success"] is True
        assert conversation["messages"] == []
        assert room["mailbox"] == []


class TestWorkspaceFiles:
    @pytest.fixture()
    def wired(self, tmp_path):
        module = load_module()
        room = make_room(module)
        patch_room_module(module, [room])
        module.conversation_files_root = lambda _conversation_id: tmp_path
        return module, room, tmp_path

    def test_write_read_list_roundtrip(self, wired):
        module, room, _tmp = wired
        module.write_room_workspace_file(
            room["id"],
            module.RoomWorkspaceWriteBody(path="notes/a.md", content="# hello"),
            request(),
        )
        listing = module.list_room_workspace_files(room["id"], request(), path="")
        assert [e["name"] for e in listing["entries"]] == ["notes"]

        listing = module.list_room_workspace_files(room["id"], request(), path="notes")
        assert listing["entries"][0]["type"] == "file"

        content = module.read_room_workspace_file(room["id"], request(), path="notes/a.md")
        assert content["content"] == "# hello"

    def test_path_traversal_is_contained(self, wired):
        module, room, tmp = wired
        module.write_room_workspace_file(
            room["id"],
            module.RoomWorkspaceWriteBody(path="safe.txt", content="ok"),
            request(),
        )
        with pytest.raises(Exception) as excinfo:
            module.read_room_workspace_file(room["id"], request(), path="../../escape")
        assert excinfo.value.status_code == 404
        assert not (tmp / "escape").exists()
        assert not (tmp.parent / "escape").exists()

    def test_delete_rename_copy(self, wired):
        module, room, _tmp = wired
        module.write_room_workspace_file(
            room["id"],
            module.RoomWorkspaceWriteBody(path="one.txt", content="1"),
            request(),
        )
        module.copy_room_workspace_path(
            room["id"],
            module.RoomWorkspaceTwoPathBody(source_path="one.txt", destination_path="two.txt"),
            request(),
        )
        module.rename_room_workspace_path(
            room["id"],
            module.RoomWorkspaceTwoPathBody(old_path="two.txt", new_path="three.txt"),
            request(),
        )
        listing = module.list_room_workspace_files(room["id"], request(), path="")
        assert sorted(e["name"] for e in listing["entries"]) == ["one.txt", "three.txt"]

        module.delete_room_workspace_path(room["id"], request(), path="one.txt")
        listing = module.list_room_workspace_files(room["id"], request(), path="")
        assert [e["name"] for e in listing["entries"]] == ["three.txt"]


class TestRoomDetailProjection:
    def test_detail_carries_agents_members_typing_summary(self):
        module = load_module()
        room = make_room(module)
        room["typing"] = {
            "owner-a": {
                "id": "owner-a",
                "name": "Given",
                "expires_at": int(time.time() * 1000) + 5_000,
            }
        }
        room["summary"] = {"text": "之前讨论了部署", "version": 3}
        patch_room_module(module, [room])

        detail = module._room_detail_response(room, {"conversations": []}, "owner-a")
        assert {a["profile"] for a in detail["agents"]} == {"default", "pc-worker"}
        assert detail["agents"][0]["provider"] == "openai"
        assert [m["role"] for m in detail["members"]] == ["owner"]
        assert [t["name"] for t in detail["typing_users"]] == ["Given"]
        assert detail["summary_state"]["summary"] == "之前讨论了部署"
        assert detail["summary_state"]["version"] == 3

    def test_room_list_summary_flags_mention_all(self):
        module = load_module()
        room = make_room(module)
        patch_room_module(module, [room])
        module._room_maps_to_deleting_conversation = lambda _room, _state: False
        module._claim_legacy_rooms_in_state = lambda *a, **k: False
        module._room_conversation_in_state = (
            lambda room_, state, owner, gen: (None, False)
        )

        response = module.get_rooms(request())
        assert len(response["rooms"]) == 1
        summary = response["rooms"][0]
        assert summary["can_mention_all"] is True
        assert summary["can_manage"] is True
        assert summary["members"][0]["role"] == "owner"
