from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import textwrap
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import hermes_cli.dashboard_auth.mobile_device_store as mobile_device_store
from hermes_services.internal_hooks import (
    InternalHook,
)
from tests.hermes_services.internal_hook_test_support import (
    register_test_hook as _register_internal_hook_for_tests,
    reset_test_registry as reset_internal_hooks_for_tests,
    restore_production_registry,
)


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "collaboration"
    / "dashboard"
    / "plugin_api.py"
)

IOS_DASHBOARD_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "ios-intelligence"
    / "dashboard"
    / "plugin_api.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "collaboration_hosted_event_stream_test_module",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Production plugin imports now seal the trusted registry before executing
    # plugin code.  This test-only loader deliberately reopens the injected
    # registry afterward so individual cases can install their fake observer.
    reset_internal_hooks_for_tests()
    return module


def _load_ios_dashboard_module():
    spec = importlib.util.spec_from_file_location(
        "ios_intelligence_account_deletion_recovery_test_module",
        IOS_DASHBOARD_MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind_in_memory_state(module, conversation):
    state = {"conversations": [conversation]}
    module.load_single_state = lambda: state
    module.save_single_state = lambda _state: None
    module._notify_hosted_update = lambda *_args: None
    return state


@pytest.fixture
def isolated_internal_hooks():
    reset_internal_hooks_for_tests()
    try:
        yield
    finally:
        restore_production_registry()


def test_post_persistence_hook_waits_for_successful_atomic_target_write(
    monkeypatch,
    tmp_path,
    isolated_internal_hooks,
):
    del isolated_internal_hooks
    module = _load_module()
    single_path = tmp_path / "single.json"
    observations: list[tuple[str, bool]] = []

    def observe(event, **context):
        observations.append((event["event_id"], single_path.exists()))
        assert context["store_path"] == str(single_path)
        assert event["event_id"] in single_path.read_text(encoding="utf-8")
        return event

    _register_internal_hook_for_tests(
        "after_hosted_event_persistence",
        InternalHook(
            name="durability-observer",
            source="telemetry.test",
            version="1",
            callback=observe,
        ),
    )
    conversation = {"id": "conversation-1"}
    state = {"conversations": [conversation]}
    result = module.append_hosted_event(
        conversation,
        account_generation="generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        role_stage="worker",
        event_type="turn.started",
        idempotency_key="turn-started",
    )
    assert result.appended is True
    assert observations == []

    original_write = module._atomic_write_state_document

    def install_then_fail(path, document):
        original_write(path, document)
        if Path(path) == single_path:
            raise OSError("simulated crash after replace")

    monkeypatch.setattr(module, "_atomic_write_state_document", install_then_fail)
    with pytest.raises(OSError, match="simulated crash"):
        module.save_single_state(state, path=single_path)

    assert single_path.exists() is True
    assert observations == []
    assert conversation.get("_hosted_event_persistence_pending")
    committed_after_exit = single_path.read_text(encoding="utf-8")
    assert result.event["event_id"] in committed_after_exit
    assert "_hosted_event_persistence_outbox" in committed_after_exit
    assert "_hosted_event_persistence_pending" not in committed_after_exit

    monkeypatch.setattr(module, "_atomic_write_state_document", original_write)
    reloaded = module.load_single_state(path=single_path)

    assert observations == [(result.event["event_id"], True)]
    durable_ack = single_path.read_text(encoding="utf-8")
    assert "_hosted_event_persistence_outbox" not in durable_ack
    assert "_hosted_event_persistence_acks" in durable_ack
    assert reloaded["conversations"][0]["hosted_events"][0][
        "persistence_hook_trace"
    ][0][
        "status"
    ] == "completed"
    assert any(
        entry.get("entry_type") == "hook_trace"
        and entry.get("payload", {}).get("point")
        == "after_hosted_event_persistence"
        for entry in reloaded["conversations"][0]["session_entries"]
    )
    # The original caller still carries its pre-exit staged record. A later
    # stale save must merge the durable receipt and not resurrect delivery.
    module.save_single_state(state, path=single_path)
    assert observations == [(result.event["event_id"], True)]


def test_process_exit_after_target_replace_recovers_durable_hook_outbox(
    tmp_path,
    isolated_internal_hooks,
):
    del isolated_internal_hooks
    single_path = tmp_path / "single.json"
    script = textwrap.dedent(
        f"""
        import importlib.util
        import os
        from pathlib import Path

        from hermes_services.internal_hooks import InternalHook
        from tests.hermes_services.internal_hook_test_support import (
            register_test_hook,
            reset_test_registry,
        )

        module_path = Path({str(MODULE_PATH)!r})
        target = Path({str(single_path)!r})
        spec = importlib.util.spec_from_file_location("p8_child_plugin", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reset_test_registry()
        register_test_hook(
            "after_hosted_event_persistence",
            InternalHook(
                name="child-observer",
                source="telemetry.test",
                version="1",
                callback=lambda event, **context: event,
            ),
        )
        conversation = {{"id": "conversation-1"}}
        state = {{"conversations": [conversation]}}
        module.append_hosted_event(
            conversation,
            account_generation="generation-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            role_stage="worker",
            event_type="turn.started",
            idempotency_key="turn-started",
        )
        original_write = module._atomic_write_state_document

        def exit_after_replace(path, document):
            original_write(path, document)
            if Path(path) == target:
                os._exit(73)

        module._atomic_write_state_document = exit_after_replace
        module.save_single_state(state, path=target)
        raise AssertionError("save unexpectedly returned")
        """
    )

    child = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert child.returncode == 73, child.stderr
    committed = single_path.read_text(encoding="utf-8")
    assert "_hosted_event_persistence_outbox" in committed
    assert "turn.started" in committed

    delivered: list[str] = []
    module = _load_module()
    _register_internal_hook_for_tests(
        "after_hosted_event_persistence",
        InternalHook(
            name="restarted-observer",
            source="telemetry.test",
            version="1",
            callback=lambda event, **context: delivered.append(
                context["delivery_id"]
            )
            or event,
        ),
    )
    module.load_single_state(path=single_path)

    assert len(delivered) == 1
    assert delivered[0].startswith("after_hosted_event_persistence:evt_")
    recovered = single_path.read_text(encoding="utf-8")
    assert "_hosted_event_persistence_outbox" not in recovered
    assert "_hosted_event_persistence_acks" in recovered
    module.load_single_state(path=single_path)
    assert len(delivered) == 1


def test_post_persistence_retry_uses_stable_idempotency_key_and_durable_ack(
    monkeypatch,
    tmp_path,
    isolated_internal_hooks,
):
    del isolated_internal_hooks
    module = _load_module()
    single_path = tmp_path / "single.json"
    callback_deliveries: list[str] = []
    side_effects: set[str] = set()

    def observe(event, **context):
        assert context["event_id"] == event["event_id"]
        callback_deliveries.append(context["idempotency_key"])
        side_effects.add(context["idempotency_key"])
        return event

    _register_internal_hook_for_tests(
        "after_hosted_event_persistence",
        InternalHook(
            name="idempotent-observer",
            source="telemetry.test",
            version="1",
            callback=observe,
        ),
    )
    conversation = {"id": "conversation-1"}
    state = {"conversations": [conversation]}
    result = module.append_hosted_event(
        conversation,
        account_generation="generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        role_stage="worker",
        event_type="turn.started",
        idempotency_key="turn-started",
    )
    original_write = module._atomic_write_state_document
    target_writes = 0

    def lose_first_ack(path, document):
        nonlocal target_writes
        if Path(path) == single_path:
            target_writes += 1
            if target_writes == 2:
                raise OSError("simulated exit before durable ACK")
        original_write(path, document)

    monkeypatch.setattr(module, "_atomic_write_state_document", lose_first_ack)
    module.save_single_state(state, path=single_path)

    assert callback_deliveries == [
        f"after_hosted_event_persistence:{result.event['event_id']}"
    ]
    assert len(side_effects) == 1
    assert "_hosted_event_persistence_outbox" in single_path.read_text(
        encoding="utf-8"
    )

    monkeypatch.setattr(module, "_atomic_write_state_document", original_write)
    module.load_single_state(path=single_path)

    assert len(callback_deliveries) == 2
    assert callback_deliveries[0] == callback_deliveries[1]
    assert len(side_effects) == 1
    recovered_document = single_path.read_text(encoding="utf-8")
    assert "_hosted_event_persistence_outbox" not in recovered_document
    assert "_hosted_event_persistence_acks" in recovered_document


def test_post_persistence_failure_never_rolls_back_committed_business_state(
    tmp_path,
    isolated_internal_hooks,
):
    del isolated_internal_hooks
    module = _load_module()
    single_path = tmp_path / "single.json"
    deliveries: list[str] = []

    def fail(event, **context):
        deliveries.append(context["delivery_id"])
        raise RuntimeError("telemetry sink unavailable")

    _register_internal_hook_for_tests(
        "after_hosted_event_persistence",
        InternalHook(
            name="unavailable-observer",
            source="telemetry.test",
            version="1",
            callback=fail,
        ),
    )
    conversation = {"id": "conversation-1"}
    state = {"conversations": [conversation]}
    result = module.append_hosted_event(
        conversation,
        account_generation="generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        role_stage="worker",
        event_type="turn.started",
        idempotency_key="turn-started",
    )

    module.save_single_state(state, path=single_path)

    document = single_path.read_text(encoding="utf-8")
    assert result.event["event_id"] in document
    assert "_hosted_event_persistence_outbox" in document
    assert "_hosted_event_persistence_acks" not in document
    assert deliveries == [
        f"after_hosted_event_persistence:{result.event['event_id']}"
    ]


def test_account_deletion_revokes_hook_outbox_before_recovery_delivery(
    tmp_path,
    isolated_internal_hooks,
    monkeypatch,
):
    """A deleted owner epoch can never receive a queued observer callback."""

    del isolated_internal_hooks
    module = _load_module()
    single_path = tmp_path / "single.json"
    monkeypatch.setattr(module, "single_state_path", lambda: single_path)
    deliveries: list[str] = []

    def unavailable(event, **context):
        deliveries.append(context["delivery_id"])
        raise RuntimeError("sink unavailable")

    _register_internal_hook_for_tests(
        "after_hosted_event_persistence",
        InternalHook(
            name="deletion-aware-observer",
            source="telemetry.test",
            version="1",
            callback=unavailable,
        ),
    )
    conversation = {"id": "conversation-1", "owner_id": "alice"}
    state = {"conversations": [conversation]}
    result = module.append_hosted_event(
        conversation,
        account_generation="alice-generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        role_stage="worker",
        event_type="turn.started",
        idempotency_key="turn-started",
    )

    module.save_single_state(state)
    assert deliveries == [
        f"after_hosted_event_persistence:{result.event['event_id']}"
    ]
    before_delete = json.loads(single_path.read_text(encoding="utf-8"))
    assert result.event["event_id"] in before_delete[
        "_hosted_event_persistence_outbox"
    ]
    entry = before_delete["_hosted_event_persistence_outbox"][result.event["event_id"]]
    assert entry["owner_id"] == "alice"
    assert entry["account_generation"] == "alice-generation-1"

    module.begin_owner_account_deletion(
        "alice",
        account_generation="alice-generation-1",
    )
    after_delete = json.loads(single_path.read_text(encoding="utf-8"))
    assert "_hosted_event_persistence_outbox" not in after_delete
    assert "_hosted_event_persistence_acks" not in after_delete
    assert "alice" in after_delete["account_deletion_tombstones"]

    # Repeated recovery is idempotent and cannot call the old sink again.
    module.load_single_state()
    module.load_single_state()
    assert deliveries == [
        f"after_hosted_event_persistence:{result.event['event_id']}"
    ]


def test_tombstoned_legacy_hook_record_is_fail_closed(tmp_path):
    from hermes_services.hosted_event_protocol import (
        dispatch_persisted_hosted_event_hooks,
    )

    state = {
        "account_deletion_tombstones": {
            "alice": {"alice-generation-1": {"deleted_at": 1}}
        },
        "_hosted_event_persistence_outbox": {
            "legacy-event": {
                "delivery_id": "delivery-legacy",
                "event": {
                    "event_id": "legacy-event",
                    "account_generation": "alice-generation-1",
                },
            }
        },
        "_hosted_event_persistence_acks": {
            "legacy-event": {"delivery_id": "delivery-legacy"}
        },
    }
    outcome = dispatch_persisted_hosted_event_hooks(
        state,
        store_path=str(tmp_path / "state.json"),
    )
    assert outcome.attempted_event_ids == ()
    assert "_hosted_event_persistence_outbox" not in outcome.state
    assert "_hosted_event_persistence_acks" not in outcome.state


def test_authenticated_create_is_rejected_if_deletion_begins_before_the_write(
    monkeypatch,
    tmp_path,
):
    module = _load_module()
    store = mobile_device_store.MobileDeviceStore(tmp_path / "mobile-auth.db")
    store.account_generation("owner-a", create=True)
    state = {"conversations": []}
    monkeypatch.setattr(module, "available_profiles", lambda: [{"name": "default"}])
    monkeypatch.setattr(module, "owner_id_from_request", lambda _request: "owner-a")
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    monkeypatch.setattr(mobile_device_store, "MobileDeviceStore", lambda: store)

    # Authentication has already resolved owner-a; deletion wins before the
    # route obtains the active account generation and mutates conversation state.
    store.begin_account_deletion("owner-a", "owner-scope")

    with pytest.raises(module.HTTPException) as exc_info:
        module.create_single_chat(
            module.CreateSingleConversationBody(profile="default"),
            request=object(),
        )
    assert exc_info.value.status_code == 410
    assert exc_info.value.detail == "Account deletion is in progress"
    assert state["conversations"] == []


def test_collaboration_tombstone_blocks_a_request_that_already_captured_generation(
    monkeypatch,
    tmp_path,
):
    module = _load_module()
    single_path = tmp_path / "single.json"
    monkeypatch.setattr(module, "single_state_path", lambda: single_path)
    monkeypatch.setattr(module, "available_profiles", lambda: [{"name": "default"}])
    monkeypatch.setattr(module, "owner_id_from_request", lambda _request: "alice")
    module.save_single_state({"conversations": []})
    captured_generation = "alice-generation-1"

    def generation_checked_before_deletion(_owner_id):
        # The mobile generation lookup has already succeeded. Account deletion
        # then persists its collaboration intent before this old request resumes.
        with module._STATE_LOCK:
            deletion_state = module.load_single_state()
            module._record_account_deletion_intent(
                deletion_state,
                "alice",
                captured_generation,
                deleted_at=123,
            )
            with module._collaboration_deletion_write():
                module.save_single_state(deletion_state)
        return captured_generation

    monkeypatch.setattr(
        module,
        "_account_generation_for_owner",
        generation_checked_before_deletion,
    )

    with pytest.raises(module.HTTPException) as exc_info:
        module.create_single_chat(
            module.CreateSingleConversationBody(profile="default"),
            request=object(),
        )

    assert exc_info.value.status_code == 410
    stored = module.load_single_state()
    assert stored["conversations"] == []
    assert stored[module._ACCOUNT_DELETION_TOMBSTONES_KEY]["alice"] == {
        captured_generation: {"deleted_at": 123}
    }


def test_collaboration_tombstone_allows_only_a_new_account_generation(tmp_path):
    module = _load_module()
    single_path = tmp_path / "single.json"
    module.save_single_state({"conversations": []}, path=single_path)
    deletion_state = module.load_single_state(path=single_path)
    module._record_account_deletion_intent(
        deletion_state,
        "alice",
        "generation-1",
        deleted_at=123,
    )
    with module._collaboration_deletion_write():
        module.save_single_state(deletion_state, path=single_path)

    stale = module.load_single_state(path=single_path)
    stale["conversations"].append({
        "id": "late-old",
        "owner_id": "alice",
        "account_generation": "generation-1",
    })
    with pytest.raises(module.CollaborationAccountDeletionInProgress):
        module.save_single_state(stale, path=single_path)

    fresh = module.load_single_state(path=single_path)
    fresh["conversations"].append({
        "id": "new-account",
        "owner_id": "alice",
        "account_generation": "generation-2",
    })
    module.save_single_state(fresh, path=single_path)
    assert [item["id"] for item in module.load_single_state(path=single_path)["conversations"]] == [
        "new-account"
    ]


def test_single_store_deletion_intent_is_authoritative_for_group_room_writes(
    monkeypatch,
    tmp_path,
):
    module = _load_module()
    single_path = tmp_path / "single.json"
    rooms_path = tmp_path / "rooms.json"
    monkeypatch.setattr(module, "single_state_path", lambda: single_path)
    monkeypatch.setattr(module, "state_path", lambda: rooms_path)
    module.save_single_state({"conversations": []})
    module.save_state({"rooms": []})

    module.begin_owner_account_deletion(
        "alice",
        account_generation="generation-1",
    )
    stale_rooms = module.load_state()
    stale_rooms["rooms"].append({
        "id": "late-room",
        "owner_id": "alice",
        "account_generation": "generation-1",
    })

    with pytest.raises(module.CollaborationAccountDeletionInProgress):
        module.save_state(stale_rooms)
    assert module.load_state()["rooms"] == []


def test_intelligence_intent_blocks_collaboration_write_after_boundary_exit(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    module = _load_module()
    single_path = tmp_path / "single.json"
    monkeypatch.setattr(module, "single_state_path", lambda: single_path)
    module.save_single_state({"conversations": []})

    stale = module.load_single_state()
    stale["conversations"].append({
        "id": "late-old-generation",
        "owner_id": "alice",
        "account_generation": "generation-1",
    })

    from hermes_cli.account_lifecycle import account_lifecycle_commit_guard
    from hermes_cli.ios_intelligence import IOSIntelligenceStore

    # This is the exact persisted state left when the API process exits after
    # the first database commit and before the collaboration JSON fence.
    with account_lifecycle_commit_guard():
        IOSIntelligenceStore().begin_account_deletion(
            "alice",
            "owner-scope",
            "generation-1",
        )

    with pytest.raises(module.CollaborationAccountDeletionInProgress):
        module.save_single_state(stale)
    assert module.load_single_state()["conversations"] == []


def test_subprocess_exit_restarts_account_cleanup_workers_without_old_generation_writes(
    monkeypatch,
    tmp_path,
):
    """B01: a crash after the intelligence intent stays fail-closed through recovery."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "ios_intelligence:\n  enabled: false\n",
        encoding="utf-8",
    )
    token_path = tmp_path / "captured-access-token.txt"
    child_script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path

        from hermes_cli.dashboard_auth.mobile_device_store import (
            MobileDeviceInfo,
            MobileDeviceStore,
        )
        from hermes_cli.ios_intelligence import IOSIntelligenceStore

        mobile = MobileDeviceStore()
        tokens = mobile.create_session(
            user_id="alice",
            device=MobileDeviceInfo(id="device-primary", name="Owner iPhone"),
        )
        generation = mobile.account_generation("alice", create=False)
        Path({str(token_path)!r}).write_text(tokens.access_token, encoding="utf-8")
        IOSIntelligenceStore().begin_account_deletion(
            "alice",
            "https://hermes.example|alice",
            generation,
        )
        os._exit(73)
        """
    )
    child = subprocess.run(
        [sys.executable, "-c", child_script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "HERMES_HOME": str(tmp_path)},
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert child.returncode == 73, child.stderr

    from hermes_cli.dashboard_auth.mobile_device_store import MobileDeviceStore
    from hermes_cli.ios_intelligence import IOSIntelligenceStore

    mobile = MobileDeviceStore()
    captured_access_token = token_path.read_text(encoding="utf-8")
    generation = mobile.account_generation("alice", create=False)
    assert generation
    assert mobile.verify_access(captured_access_token, touch=False) is not None
    intelligence = IOSIntelligenceStore()
    crashed_intent = intelligence.account_deletion_status("alice")
    assert crashed_intent is not None
    assert {
        key: crashed_intent[key]
        for key in (
            "owner_id",
            "owner_scope",
            "account_generation",
            "status",
            "attempts",
            "last_error",
            "completed_at",
        )
    } == {
        "owner_id": "alice",
        "owner_scope": "https://hermes.example|alice",
        "account_generation": generation,
        "status": "pending",
        "attempts": 0,
        "last_error": "",
        "completed_at": None,
    }
    assert crashed_intent["requested_at"] == pytest.approx(time.time(), abs=10)
    assert crashed_intent["updated_at"] == pytest.approx(time.time(), abs=10)

    collaboration = _load_module()
    collaboration.save_single_state({"conversations": []})
    assert collaboration._ACCOUNT_DELETION_TOMBSTONES_KEY not in (
        collaboration.load_single_state()
    )

    def old_generation_write(
        record_id: str,
        *,
        captured: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        stale = collaboration.load_single_state()
        stale["conversations"].append(
            {
                "id": record_id,
                "owner_id": "alice",
                "account_generation": generation,
            }
        )
        if captured is not None:
            captured.set()
        if release is not None:
            assert release.wait(timeout=5)
        with pytest.raises(collaboration.CollaborationAccountDeletionInProgress):
            collaboration.save_single_state(stale)
        assert all(
            item.get("id") != record_id
            for item in collaboration.load_single_state()["conversations"]
        )

    # This simulates a request that authenticated before the crashed process
    # could persist the collaboration JSON fence.
    old_generation_write("before-cleanup-restart")

    ios_dashboard = _load_ios_dashboard_module()
    app = FastAPI()
    app.include_router(ios_dashboard.router)
    writer_errors: list[BaseException] = []
    stale_write_captured = threading.Event()
    release_stale_write = threading.Event()
    writer = threading.Thread(
        target=lambda: _capture_exception(
            writer_errors,
            lambda: old_generation_write(
                "during-cleanup-restart",
                captured=stale_write_captured,
                release=release_stale_write,
            ),
        ),
    )
    writer.start()
    assert stale_write_captured.wait(timeout=5)

    with TestClient(app):
        assert ios_dashboard._SCHEDULER is not None
        assert ios_dashboard._SCHEDULER.cleanup_only is True
        release_stale_write.set()
        deadline = time.monotonic() + 10
        mobile_status = mobile.account_deletion_status("alice")
        while (
            mobile_status is None
            or mobile_status["state"]
            not in {"delivered", "no_recipients", "permanent_failure"}
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
            mobile_status = mobile.account_deletion_status("alice")
        writer.join(timeout=5)
        assert not writer.is_alive()
        assert writer_errors == []
        assert mobile_status is not None
        assert mobile_status["state"] == "no_recipients"

    # A second production lifecycle startup is idempotent and remains fenced.
    with TestClient(app):
        assert ios_dashboard._SCHEDULER is not None
        deadline = time.monotonic() + 10
        while intelligence.account_deletion_status("alice")["status"] != "complete":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        old_generation_write("after-cleanup-restart")

    assert intelligence.account_deletion_status("alice")["status"] == "complete"
    assert mobile.verify_access(captured_access_token, touch=False) is None
    assert collaboration.load_single_state()["conversations"] == []


def _capture_exception(errors: list[BaseException], callback) -> None:
    try:
        callback()
    except BaseException as exc:  # pragma: no cover - asserted by caller
        errors.append(exc)


def test_account_lifecycle_guard_serializes_write_and_deletion_commit(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    module = _load_module()
    single_path = tmp_path / "single.json"
    monkeypatch.setattr(module, "single_state_path", lambda: single_path)
    module.save_single_state({"conversations": []})

    incoming = module.load_single_state()
    incoming["conversations"].append({
        "id": "commits-before-delete",
        "owner_id": "alice",
        "account_generation": "generation-1",
    })
    entered_write = threading.Event()
    release_write = threading.Event()
    deletion_committed = threading.Event()
    original_write = module._atomic_write_state_document

    def paused_write(path, document):
        entered_write.set()
        assert release_write.wait(5)
        original_write(path, document)

    monkeypatch.setattr(module, "_atomic_write_state_document", paused_write)
    writer = threading.Thread(target=module.save_single_state, args=(incoming,))
    writer.start()
    assert entered_write.wait(5)

    from hermes_cli.account_lifecycle import account_lifecycle_commit_guard
    from hermes_cli.ios_intelligence import IOSIntelligenceStore

    def delete():
        with account_lifecycle_commit_guard():
            IOSIntelligenceStore().begin_account_deletion(
                "alice",
                "owner-scope",
                "generation-1",
            )
        deletion_committed.set()

    deleter = threading.Thread(target=delete)
    deleter.start()
    assert deletion_committed.wait(0.1) is False
    release_write.set()
    writer.join(5)
    deleter.join(5)
    assert writer.is_alive() is False
    assert deleter.is_alive() is False
    assert deletion_committed.is_set()

    late = module.load_single_state()
    late["conversations"].append({
        "id": "late-after-delete",
        "owner_id": "alice",
        "account_generation": "generation-1",
    })
    with pytest.raises(module.CollaborationAccountDeletionInProgress):
        module.save_single_state(late)


def test_behavior_eval_runtime_binding_fails_closed_on_profile_mismatch(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(
        module,
        "available_profiles",
        lambda: [{
            "name": "default",
            "provider": "provider-a",
            "model": "model-a",
        }],
    )

    binding = module._required_runtime_binding(
        "default",
        required_provider="provider-a",
        required_model="model-a",
    )
    assert binding["verified"] is True

    with pytest.raises(module.HTTPException) as exc_info:
        module._required_runtime_binding(
            "default",
            required_provider="provider-b",
            required_model="model-b",
        )
    assert exc_info.value.status_code == 409


def test_explicit_legacy_profiles_use_default_runtime_without_catalog_reappearance(
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(
        module,
        "available_profiles",
        lambda: [{
            "name": "default",
            "provider": "provider-a",
            "model": "model-a",
        }],
    )

    for profile in ("reviewer", "supervisor", "dbb3-manager"):
        binding = module._required_runtime_binding(
            profile,
            required_provider="provider-a",
            required_model="model-a",
        )
        assert binding["profile"] == profile
        assert binding["resolved_profile"] == "default"
        assert binding["verified"] is True

        route, mode, selected, artifact = module._hosted_route_parameters(
            route_metadata={"mode": "chat", "profiles": [profile]},
            requested_mode="chat",
            requested_profiles=[profile],
        )
        assert mode == "chat"
        assert selected == [profile]
        assert route["profiles"] == [profile]
        assert artifact is False


def test_local_intervention_reply_reuses_the_hosted_account_artifact_boundary(
    monkeypatch,
):
    module = _load_module()
    captured: dict[str, object] = {}
    artifact_context = {
        "root": "/srv/hermes",
        "owner_id": "alice",
        "account_generation": "alice-generation-7",
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
    }

    def runner(profile, prompt, **kwargs):
        captured.update(profile=profile, prompt=prompt, **kwargs)
        return "intervention acknowledged"

    monkeypatch.setattr(
        module,
        "_runtime_session_boundary",
        lambda *_args, **_kwargs: {"session_id": "session-1", "tip_message_id": 1},
    )
    monkeypatch.setattr(
        module,
        "_hosted_turn_cancellation_requested",
        lambda *_args: False,
    )

    result, state = module._run_local_intervention_reply(
        "conversation-1",
        "turn-1",
        profile="default",
        runtime_profile="default",
        runner=runner,
        kanban_task_id="task-1",
        runtime_session_id="session-1",
        prompt="reply to @mention",
        artifact_context=artifact_context,
    )

    assert result == "intervention acknowledged"
    assert state["status"] == "completed"
    assert captured["artifact_context"] == artifact_context
    assert captured["artifact_context"]["owner_id"] == "alice"
    assert captured["artifact_context"]["account_generation"] == "alice-generation-7"


def test_501_events_are_available_as_back_to_back_incremental_pages():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    for index in range(501):
        module.append_hosted_event(
            conversation,
            conversation_id=conversation["id"],
            turn_id="turn-1",
            role_stage="worker",
            event_type="tool.progress",
            entity_id="tool-1",
            idempotency_key=f"progress-{index}",
            payload={"entity_id": "tool-1", "index": index},
        )

    first, has_more = module._hosted_event_stream_frame(
        conversation,
        delivered_cursor=0,
        include_snapshot=True,
    )
    second, has_more_after_second = module._hosted_event_stream_frame(
        conversation,
        delivered_cursor=first["cursor"],
        include_snapshot=False,
    )

    assert len(first["events"]) == 500
    assert first["cursor"] == 500
    assert "conversation" in first
    assert has_more is True
    assert len(second["events"]) == 1
    assert second["cursor"] == 501
    assert "conversation" not in second
    assert has_more_after_second is False


def test_live_projection_reuses_captured_generation_without_reopening_auth_db(
    monkeypatch,
):
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation.update(
        {
            "owner_id": "owner@example.test",
            "account_generation": "generation-captured",
        }
    )
    module.create_hosted_turn_record(
        conversation,
        turn_id="turn-live",
        content="hello",
        title="hello",
        profiles=["default"],
        artifact_required=False,
        mode="chat",
    )
    _bind_in_memory_state(module, conversation)
    module._HOSTED_LIVE_CONVERSATIONS[conversation["id"]] = conversation
    monkeypatch.setattr(
        module,
        "_account_generation_for_owner",
        lambda _owner: (_ for _ in ()).throw(
            AssertionError("live event hot path reopened mobile auth DB")
        ),
    )

    module._publish_live_hosted_role_projection(
        conversation["id"],
        "turn-live",
        protocol_events=[
            {
                "event_type": "message.delta",
                "payload": {"text": "ok"},
                "entity_id": "message-live",
                "idempotency_key": "message-live-1",
                "occurred_at": int(time.time() * 1000),
                "role_stage": "chat",
            }
        ],
    )

    live = module._live_conversation_snapshot(
        conversation["id"],
        conversation["owner_id"],
    )
    assert live is not None
    assert live["hosted_events"][0]["account_generation"] == "generation-captured"


def test_gap_forces_authoritative_snapshot_even_for_incremental_frame():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    conversation["hosted_events"] = [
        {
            "cursor": 10,
            "sequence": 1,
            "event_type": "message.delta",
        }
    ]
    conversation["hosted_event_cursor"] = 10

    frame, _has_more = module._hosted_event_stream_frame(
        conversation,
        delivered_cursor=1,
        include_snapshot=False,
    )

    assert frame["has_gap"] is True
    assert "conversation" in frame


def test_future_hosted_cursor_returns_an_explicit_authoritative_reset():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    conversation["hosted_events"] = [{
        "cursor": 10,
        "sequence": 1,
        "event_type": "message.delta",
    }]
    conversation["hosted_event_cursor"] = 10

    frame, has_more = module._hosted_event_stream_frame(
        conversation,
        delivered_cursor=999,
        include_snapshot=False,
    )

    assert frame["cursor"] == 10
    assert frame["reset_cursor"] is True
    assert frame["reset_reason"] == "future_cursor"
    assert frame["has_gap"] is True
    assert "conversation" in frame
    assert has_more is False


def test_future_session_cursor_requires_a_zero_cursor_replay(monkeypatch):
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    module.append_session_entry(
        conversation,
        entry_type="message",
        idempotency_key="message-1",
        payload={"message_id": "message-1", "content": "hello"},
        occurred_at=100,
    )
    _bind_in_memory_state(module, conversation)
    monkeypatch.setattr(
        module, "owner_id_from_request", lambda _request: module.LOCAL_OWNER_ID
    )
    monkeypatch.setattr(
        module, "_account_generation_for_owner", lambda _owner: "generation-1"
    )

    response = module.list_conversation_session_entries(
        conversation["id"], SimpleNamespace(), cursor=999, limit=500
    )

    assert response["cursor"] == 1
    assert response["entries"] == []
    assert response["reset_cursor"] is True
    assert response["reset_reason"] == "future_cursor"


@pytest.mark.asyncio
async def test_hosted_event_stream_rejects_a_stale_expected_account_generation(
    monkeypatch,
):
    module = _load_module()

    class Request:
        query_params = {
            "cursor": "0",
            "expected_account_generation": "generation-1",
        }
        headers = {}

    monkeypatch.setattr(
        module,
        "owner_id_from_request",
        lambda _request: module.LOCAL_OWNER_ID,
    )
    monkeypatch.setattr(
        module,
        "_account_generation_for_request",
        lambda _request, _owner: "generation-2",
    )

    with pytest.raises(module.HTTPException) as raised:
        await module.stream_hosted_conversation_events("chat-stale", Request())

    assert raised.value.status_code == 409
    assert "generation changed" in raised.value.detail


@pytest.mark.asyncio
async def test_unrelated_conversation_update_does_not_busy_loop_an_idle_stream(
    monkeypatch,
):
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    _bind_in_memory_state(module, conversation)
    waits: list[int] = []

    class Request:
        query_params = {}
        headers = {}

        async def is_disconnected(self):
            return False

    def wait_for_update(
        revision: int,
        _timeout: float,
        conversation_id: str,
    ) -> int:
        waits.append(revision)
        return module._hosted_update_revision(conversation_id)

    monkeypatch.setattr(module, "owner_id_from_request", lambda _request: module.LOCAL_OWNER_ID)
    monkeypatch.setattr(module, "_wait_for_hosted_update", wait_for_update)
    response = await module.stream_hosted_conversation_events(conversation["id"], Request())
    iterator = response.body_iterator

    first = await anext(iterator)
    assert "event: conversation" in first

    # A different conversation only advances the global revision. This stream
    # must stay on its own conversation revision and emit one keepalive.
    module._HOSTED_UPDATE_REVISION += 1
    second = await anext(iterator)

    assert second == ": keepalive\n\n"
    assert waits == [0]
    await iterator.aclose()


def test_rejected_late_event_is_not_projected_into_session_history():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    terminal = module.append_hosted_event(
        conversation,
        conversation_id=conversation["id"],
        turn_id="turn-1",
        role_stage="reporter",
        event_type="turn.completed",
        entity_id="turn-1",
        idempotency_key="turn-1:completed",
    )
    entry_cursor = conversation["session_entry_cursor"]

    late = module.append_hosted_event(
        conversation,
        conversation_id=conversation["id"],
        turn_id="turn-1",
        role_stage="manager",
        event_type="role.handoff",
        entity_id="late-handoff",
        idempotency_key="turn-1:late-handoff",
        payload={"from_role": "manager", "to_role": "worker"},
    )

    assert terminal.appended is True
    assert late.appended is False
    assert conversation["session_entry_cursor"] == entry_cursor


def test_old_generation_writer_cannot_mutate_a_reused_conversation_and_turn(monkeypatch):
    module = _load_module()
    current_generation = {"value": "generation-1"}
    monkeypatch.setattr(
        module,
        "_account_generation_for_owner",
        lambda _owner: current_generation["value"],
    )
    old_conversation = module.create_single_conversation(profile="default")
    old_conversation["id"] = "chat-reused"
    old_conversation["owner_id"] = "alice"
    old_conversation["account_generation"] = "generation-1"
    module.create_hosted_turn_record(
        old_conversation,
        turn_id="turn-reused",
        content="old task",
        title="old task",
        profiles=["default"],
        artifact_required=False,
        mode="chat",
    )
    state = _bind_in_memory_state(module, old_conversation)

    current_generation["value"] = "generation-2"
    new_conversation = module.create_single_conversation(profile="default")
    new_conversation["id"] = "chat-reused"
    new_conversation["owner_id"] = "alice"
    new_conversation["account_generation"] = "generation-2"
    module.create_hosted_turn_record(
        new_conversation,
        turn_id="turn-reused",
        content="new task",
        title="new task",
        profiles=["default"],
        artifact_required=False,
        mode="chat",
    )
    state["conversations"] = [new_conversation]

    with pytest.raises(RuntimeError, match="stale hosted turn account generation"):
        module._persist_hosted_turn(
            "chat-reused",
            "turn-reused",
            patch={"status": "completed", "stage": "completed"},
            message={
                "role": "assistant",
                "name": "default",
                "content": "stale old result",
                "status": "completed",
            },
            expected_account_generation="generation-1",
        )

    assert new_conversation["hosted_turns"]["turn-reused"]["status"] == "queued"
    assert new_conversation["messages"] == []


def test_terminal_event_is_persisted_after_final_visible_message_event():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    module.create_hosted_turn_record(
        conversation,
        turn_id="turn-1",
        content="task",
        title="task",
        profiles=["default"],
        artifact_required=False,
        mode="chat",
    )
    state = {"conversations": [conversation]}
    module.load_single_state = lambda: state
    module.save_single_state = lambda _state: None
    module._notify_hosted_update = lambda *_args: None

    module._persist_hosted_turn(
        conversation["id"],
        "turn-1",
        patch={"status": "completed", "stage": "completed"},
        message={
            "role": "assistant",
            "name": "default",
            "content": "done",
            "status": "completed",
            "kind": "message",
            "meta": {"role_stage": "reporter", "message_key": "final"},
        },
        protocol_events=[{
            "role_stage": "turn",
            "event_type": "turn.completed",
            "entity_id": "turn-1",
            "idempotency_key": "turn-1-completed",
        }],
    )

    event_types = [event["event_type"] for event in conversation["hosted_events"]]
    assert event_types[-2:] == ["message.completed", "turn.completed"]


def test_repeated_role_invocations_get_distinct_entities_and_start_boundaries():
    module = _load_module()
    state = {
        "_protocol_event_index": 0,
        "_protocol_invocation_index": 0,
        "_protocol_started_entities": [],
        "_protocol_events": [],
        "_saw_message_complete": False,
    }
    all_events = []
    for invocation in range(2):
        module._queue_hosted_protocol_event(
            state,
            {"type": "request.accepted", "payload": {}},
        )
        module._queue_hosted_protocol_event(
            state,
            {"type": "message.delta", "payload": {"text": f"part-{invocation}"}},
        )
        module._queue_hosted_protocol_event(
            state,
            {"type": "message.complete", "payload": {"text": f"done-{invocation}"}},
        )
        assert state["_saw_message_complete"] is True
        all_events.extend(state.pop("_protocol_events"))
        state["_protocol_events"] = []

    conversation = module.create_single_conversation(profile="default")
    for event in all_events:
        result = module.append_hosted_event(
            conversation,
            conversation_id=conversation["id"],
            turn_id="turn-repeat",
            role_stage="worker",
            **event,
        )
        assert result.appended is True

    message_events = [
        event for event in conversation["hosted_events"]
        if event["event_type"].startswith("message.")
    ]
    assert [event["event_type"] for event in message_events] == [
        "message.started",
        "message.delta",
        "message.completed",
        "message.started",
        "message.delta",
        "message.completed",
    ]
    assert {
        event["entity_id"] for event in message_events
        if event["event_type"] == "message.completed"
    } == {"message-invocation-1", "message-invocation-2"}


def test_follow_up_is_durable_but_does_not_interrupt_active_remote_role():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    run = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-1",
        content="deploy",
        title="deploy",
        profiles=["dbb3-worker"],
        artifact_required=False,
        mode="work",
    )
    run["active_roles"] = {
        "worker:dbb3-worker": {
            "role_stage": "worker:dbb3-worker",
            "profile": "dbb3-worker",
            "execution": "remote",
            "remote_run_id": "remote-1",
        }
    }
    run["remote_runs"] = {
        "worker:dbb3-worker": {
            "id": "remote-1",
            "role_stage": "worker:dbb3-worker",
            "profile": "dbb3-worker",
            "status": "running",
        }
    }
    state = {"conversations": [conversation]}
    module.load_single_state = lambda: state
    module.save_single_state = lambda _state: None
    module._notify_hosted_update = lambda *_args: None
    module.owner_id_from_request = lambda _request: module.LOCAL_OWNER_ID

    response = module.intervene_hosted_turn(
        conversation["id"],
        "turn-1",
        module.HostedTurnInterventionBody(
            content="@Hermes Worker 完成本轮后再核对日志。",
            message_id="follow-up-1",
            delivery="follow_up",
            queue_mode="one_at_a_time",
        ),
        SimpleNamespace(),
    )

    intervention = response["hosted_turn"]["interventions"][0]
    assert intervention["delivery"] == "follow_up"
    assert intervention["queue_mode"] == "one_at_a_time"
    assert "cancel_requested" not in run["remote_runs"]["worker:dbb3-worker"]
    assert module._pending_hosted_role_intervention(
        conversation["id"],
        "turn-1",
        role_stage="worker:dbb3-worker",
        profile="dbb3-worker",
        deliveries={"steer"},
    ) is None
    assert module._pending_hosted_role_intervention(
        conversation["id"],
        "turn-1",
        role_stage="worker:dbb3-worker",
        profile="dbb3-worker",
        deliveries={"follow_up"},
    )["id"] == "follow-up-1"
    assert any(
        event["event_type"] == "intervention.queued"
        for event in conversation["hosted_events"]
    )


def test_intervention_modes_reject_unknown_values():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    module.create_hosted_turn_record(
        conversation,
        turn_id="turn-1",
        content="task",
        title="task",
        profiles=["dbb3-worker"],
        artifact_required=False,
        mode="work",
    )
    state = {"conversations": [conversation]}
    module.load_single_state = lambda: state
    module.save_single_state = lambda _state: None
    module.owner_id_from_request = lambda _request: module.LOCAL_OWNER_ID

    with pytest.raises(module.HTTPException) as raised:
        module.intervene_hosted_turn(
            conversation["id"],
            "turn-1",
            module.HostedTurnInterventionBody(
                content="@Hermes Worker check",
                delivery="later",
            ),
            SimpleNamespace(),
        )
    assert raised.value.status_code == 422


def test_all_at_once_has_independent_claim_and_completion_per_target():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    run = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-all-at-once",
        content="task",
        title="task",
        profiles=["dbb3-worker", "reviewer"],
        artifact_required=False,
        mode="work",
    )
    run["interventions"].append({
        "id": "intervention-all-at-once",
        "content": "update worker and reviewer",
        "targets": ["worker", "reviewer"],
        "target_profiles": [],
        "status": "pending",
        "delivery": "steer",
        "queue_mode": "all_at_once",
    })
    state = {"conversations": [conversation]}
    module.load_single_state = lambda: state
    module.save_single_state = lambda _state: None
    module._notify_hosted_update = lambda *_args: None

    worker = module._claim_hosted_role_intervention(
        conversation["id"],
        "turn-all-at-once",
        role_stage="worker",
        profile="dbb3-worker",
        checkpoint={"content": "worker checkpoint"},
        intervention_id="intervention-all-at-once",
        execution_owner="worker-owner",
    )
    reviewer = module._claim_hosted_role_intervention(
        conversation["id"],
        "turn-all-at-once",
        role_stage="reviewer",
        profile="reviewer",
        checkpoint={"content": "review checkpoint"},
        intervention_id="intervention-all-at-once",
        execution_owner="reviewer-owner",
    )

    assert worker is not None and worker["delivery_key"] == "role:worker"
    assert reviewer is not None and reviewer["delivery_key"] == "role:reviewer"
    assert worker["claim_token"] != reviewer["claim_token"]
    module._complete_hosted_role_intervention(
        conversation["id"],
        "turn-all-at-once",
        intervention_id="intervention-all-at-once",
        claim_token=worker["claim_token"],
        execution_owner="worker-owner",
        role_stage="worker",
        role_label="Worker",
        profile="dbb3-worker",
        reply="worker updated",
        checkpoint={"content": "worker updated"},
    )
    assert run["interventions"][0]["status"] == "processing"
    module._complete_hosted_role_intervention(
        conversation["id"],
        "turn-all-at-once",
        intervention_id="intervention-all-at-once",
        claim_token=reviewer["claim_token"],
        execution_owner="reviewer-owner",
        role_stage="reviewer",
        role_label="Reviewer",
        profile="reviewer",
        reply="reviewer updated",
        checkpoint={"content": "reviewer updated"},
    )

    assert run["interventions"][0]["status"] == "completed"
    replies = [
        message for message in conversation["messages"]
        if (message.get("meta") or {}).get("intervention_reply") is True
    ]
    assert {message["content"] for message in replies} == {
        "worker updated",
        "reviewer updated",
    }


@pytest.mark.parametrize(
    ("status", "event_type"),
    [
        ("completed", "turn.completed"),
        ("failed", "turn.failed"),
        ("cancelled", "turn.cancelled"),
    ],
)
def test_authoritative_terminal_is_automatic_incremental_and_idempotent(
    status,
    event_type,
):
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    module.create_hosted_turn_record(
        conversation,
        turn_id="turn-terminal",
        content="task",
        title="task",
        profiles=["default"],
        artifact_required=False,
        mode="chat",
    )
    _bind_in_memory_state(module, conversation)

    module._persist_hosted_turn(
        conversation["id"],
        "turn-terminal",
        patch={
            "status": status,
            "stage": status,
            "completed_at": 1234,
        },
        message={
            "role": "assistant",
            "name": "default",
            "content": f"terminal {status}",
            "status": status,
            "kind": "message",
            "meta": {
                "role_stage": "reporter",
                "message_key": "terminal-message",
            },
        },
    )
    frame, _has_more = module._hosted_event_stream_frame(
        conversation,
        delivered_cursor=0,
        include_snapshot=False,
    )

    assert "conversation" not in frame
    assert frame["events"][-1]["event_type"] == event_type
    assert frame["events"][-1]["payload"]["status"] == status
    assert [
        entry["payload"]["status"]
        for entry in conversation["session_entries"]
        if entry["entry_type"] == "terminal_state"
    ] == [status]

    event_cursor = conversation["hosted_event_cursor"]
    entry_cursor = conversation["session_entry_cursor"]
    module._persist_hosted_turn(
        conversation["id"],
        "turn-terminal",
        patch={"status": status, "stage": status},
    )
    assert conversation["hosted_event_cursor"] == event_cursor
    assert conversation["session_entry_cursor"] == entry_cursor


def test_role_error_cannot_commit_turn_terminal_before_authoritative_failure():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    module.create_hosted_turn_record(
        conversation,
        turn_id="turn-role-error",
        content="task",
        title="task",
        profiles=["default"],
        artifact_required=False,
        mode="chat",
    )
    _bind_in_memory_state(module, conversation)
    role_state = {
        "status": "failed",
        "error": "role failed",
        "content": "role failed",
        "activities": [],
        "_protocol_events": [{
            "event_type": "turn.failed",
            "entity_id": "role-error",
            "idempotency_key": "provisional-role-error",
            "payload": {"message": "role failed"},
        }],
    }

    module._persist_hosted_role_state(
        conversation["id"],
        "turn-role-error",
        profile="default",
        role_stage="worker",
        role_label="Worker",
        state=role_state,
        content_fallback="role failed",
    )
    assert not any(
        event["event_type"].startswith("turn.")
        and event["event_type"] in {"turn.completed", "turn.failed", "turn.cancelled"}
        for event in conversation["hosted_events"]
    )

    module._persist_hosted_turn(
        conversation["id"],
        "turn-role-error",
        patch={"status": "failed", "stage": "failed", "error": "role failed"},
    )
    assert [
        event["event_type"]
        for event in conversation["hosted_events"]
        if event["event_type"] in {"turn.completed", "turn.failed", "turn.cancelled"}
    ] == ["turn.failed"]


def test_stale_recovery_commits_one_failed_terminal_for_cursor_only_clients():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    run = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-stale",
        content="task",
        title="task",
        profiles=["default"],
        artifact_required=False,
        mode="chat",
    )
    run["status"] = "running"
    run["updated_at"] = 1

    assert module.reconcile_stale_hosted_turns(
        conversation,
        now_ms=module._HOSTED_TURN_STALE_AFTER_MS + 2,
    ) is True
    frame, _has_more = module._hosted_event_stream_frame(
        conversation,
        delivered_cursor=0,
        include_snapshot=False,
    )
    assert "conversation" not in frame
    assert frame["events"][-1]["event_type"] == "turn.failed"
    assert module.reconcile_stale_hosted_turns(
        conversation,
        now_ms=module._HOSTED_TURN_STALE_AFTER_MS * 2,
    ) is False
    assert sum(
        event["event_type"] == "turn.failed"
        for event in conversation["hosted_events"]
    ) == 1
    assert sum(
        entry["entry_type"] == "terminal_state"
        for entry in conversation["session_entries"]
    ) == 1


def test_competing_terminal_writers_commit_exactly_one_matching_terminal():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    module.create_hosted_turn_record(
        conversation,
        turn_id="turn-race",
        content="task",
        title="task",
        profiles=["default"],
        artifact_required=False,
        mode="chat",
    )
    _bind_in_memory_state(module, conversation)
    barrier = threading.Barrier(3)
    errors = []

    def commit(status):
        try:
            barrier.wait(timeout=5)
            module._persist_hosted_turn(
                conversation["id"],
                "turn-race",
                patch={"status": status, "stage": status, "completed_at": 99},
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    threads = [
        threading.Thread(target=commit, args=("completed",)),
        threading.Thread(target=commit, args=("failed",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    status = conversation["hosted_turns"]["turn-race"]["status"]
    terminals = [
        event for event in conversation["hosted_events"]
        if event["event_type"] in {"turn.completed", "turn.failed", "turn.cancelled"}
    ]
    assert len(terminals) == 1
    assert terminals[0]["event_type"] == f"turn.{status}"


def test_production_lifecycle_projects_lift_handoff_attachment_intervention_and_compaction():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    run = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-history",
        content="task",
        title="task",
        profiles=["dbb3-worker"],
        artifact_required=False,
        attachment_ids=["file_input"],
        mode="work",
    )
    run["status"] = "running"
    _bind_in_memory_state(module, conversation)
    module.owner_id_from_request = lambda _request: module.LOCAL_OWNER_ID

    module._persist_hosted_turn(
        conversation["id"],
        "turn-history",
        patch={"stage": "dispatching"},
    )
    module.intervene_hosted_turn(
        conversation["id"],
        "turn-history",
        module.HostedTurnInterventionBody(
            content="@Hermes Worker verify the result",
            message_id="intervention-history",
        ),
        SimpleNamespace(),
    )
    role_state = {"status": "streaming", "content": "", "activities": []}
    module._queue_hosted_protocol_event(
        role_state,
        {
            "type": "session:compress",
            "payload": {
                "session_id": "session-new",
                "old_session_id": "session-old",
                "compression_count": 2,
                "in_place": False,
            },
        },
    )
    module._persist_hosted_role_state(
        conversation["id"],
        "turn-history",
        profile="dbb3-worker",
        role_stage="worker",
        role_label="Worker",
        state=role_state,
        content_fallback="",
        visible=False,
    )

    entry_types = [entry["entry_type"] for entry in conversation["session_entries"]]
    assert "collaboration_lift" in entry_types
    assert "role_handoff" in entry_types
    assert "attachment" in entry_types
    assert "intervention" in entry_types
    assert "compaction" in entry_types


def test_malformed_attachment_size_cannot_block_session_history_or_terminal_state():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID

    module._append_attachment_session_entries(
        conversation,
        turn_id="turn-attachment",
        role_stage="worker",
        attachments=[{"id": "file-bad-size", "size": "not-an-integer"}],
        direction="output",
        occurred_at=123,
    )

    attachment = next(
        entry
        for entry in conversation["session_entries"]
        if entry["entry_type"] == "attachment"
    )
    assert attachment["payload"]["size"] == 0


def test_streaming_message_storage_is_linear_and_events_carry_bounded_deltas():
    module = _load_module()
    conversation = module.create_single_conversation(profile="default")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    module.create_hosted_turn_record(
        conversation,
        turn_id="turn-stream",
        content="task",
        title="task",
        profiles=["default"],
        artifact_required=False,
        mode="chat",
    )
    _bind_in_memory_state(module, conversation)
    for index in range(1, 41):
        content = "x" * (index * 100)
        module._persist_hosted_turn(
            conversation["id"],
            "turn-stream",
            patch={"status": "running"} if index == 1 else None,
            message={
                "role": "assistant",
                "name": "default",
                "content": content,
                "status": "streaming",
                "kind": "message",
                "meta": {
                    "role_stage": "chat",
                    "message_key": "stable-stream-message",
                },
            },
        )
    final_content = "x" * 4000
    module._persist_hosted_turn(
        conversation["id"],
        "turn-stream",
        patch={"status": "completed", "stage": "completed"},
        message={
            "role": "assistant",
            "name": "default",
            "content": final_content,
            "status": "completed",
            "kind": "message",
            "meta": {
                "role_stage": "chat",
                "message_key": "stable-stream-message",
            },
        },
    )

    stored_text = 0
    for entry in conversation["session_entries"]:
        payload = entry["payload"]
        stored_text += len(str(payload.get("content") or ""))
        stored_text += len(str(payload.get("content_delta") or ""))
    assert stored_text <= len(final_content) * 3
    deltas = [
        event for event in conversation["hosted_events"]
        if event["event_type"] == "message.delta"
    ]
    assert deltas
    assert max(len(str(event["payload"].get("content") or "")) for event in deltas) <= 100
    assert conversation["hosted_events"][-1]["event_type"] == "turn.completed"
