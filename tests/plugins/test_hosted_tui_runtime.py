from __future__ import annotations

import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from plugins.collaboration.dashboard.hosted_tui_runtime import (
    _GatewayProcess,
    _HostedSessionState,
    _TurnSink,
    _pool_key,
)


def _event(event_type: str, payload: dict) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": event_type,
                "session_id": "live-session",
                "payload": payload,
            },
        }
    )


def test_reader_marks_turn_idle_only_after_post_completion_session_info():
    gateway = _GatewayProcess.__new__(_GatewayProcess)
    gateway.process = SimpleNamespace(
        stdout=io.StringIO(
            "\n".join(
                (
                    _event("message.complete", {"text": "ok", "status": "complete"}),
                    _event("session.info", {"running": False}),
                    "",
                )
            )
        )
    )
    gateway._pending_lock = threading.Lock()
    gateway._session_lock = threading.Lock()
    gateway._pending = {}
    gateway._ready = threading.Event()
    gateway._closed = threading.Event()
    gateway._stderr_tail = []
    state = _HostedSessionState(
        conversation_id="conversation-1",
        live_session_id="live-session",
        stored_session_id="stored-session",
        artifact_context={},
    )
    state.current_sink = _TurnSink(None)
    gateway._sessions_by_conversation = {"conversation-1": state}
    gateway._sessions_by_live = {"live-session": state}
    gateway.live_session_id = "live-session"
    gateway.stored_session_id = "stored-session"

    gateway._read_stdout()

    assert state.message_complete_seen.is_set()
    assert state.idle_after_turn.is_set()
    assert state.latest_session_info == {
        "type": "session.info",
        "payload": {
            "running": False,
            "session_id": "stored-session",
        },
    }


def test_run_turn_waits_for_gateway_idle_ack_before_returning(monkeypatch):
    gateway = _GatewayProcess.__new__(_GatewayProcess)
    gateway.last_used = 0.0
    state = _HostedSessionState(
        conversation_id="conversation-1",
        live_session_id="live-session",
        stored_session_id="stored-session",
        artifact_context={},
    )
    state.agent_ready.set()
    gateway.ensure_session = lambda *_args, **_kwargs: state

    def rpc(method, _params, *, timeout):
        assert method == "prompt.submit"

        def finish():
            sink = state.current_sink
            assert sink is not None
            sink.accepted.wait(timeout=1)
            sink.result = "ok"
            state.message_complete_seen.set()
            sink.done.set()
            time.sleep(0.05)
            state.idle_after_turn.set()

        threading.Thread(target=finish, daemon=True).start()
        return {"status": "streaming"}

    gateway.rpc = rpc
    started = time.monotonic()

    result = gateway.run_turn(
        "hello",
        requested_session_id="",
        turn_id="turn-1",
        event_callback=None,
        cancel_check=None,
        timeout=2,
        conversation_id="conversation-1",
        artifact_context={},
    )

    assert result == "ok"
    assert time.monotonic() - started >= 0.04


def test_run_turn_returns_completed_reply_without_idle_ack(monkeypatch):
    gateway = _GatewayProcess.__new__(_GatewayProcess)
    gateway.last_used = 0.0
    state = _HostedSessionState(
        conversation_id="conversation-1",
        live_session_id="live-session",
        stored_session_id="stored-session",
        artifact_context={},
    )
    state.agent_ready.set()
    state.idle_after_turn = SimpleNamespace(wait=lambda timeout: False, clear=lambda: None)
    gateway.ensure_session = lambda *_args, **_kwargs: state

    def rpc(_method, _params, *, timeout):
        def finish():
            sink = state.current_sink
            assert sink is not None
            sink.accepted.wait(timeout=1)
            sink.result = "ok"
            sink.done.set()

        threading.Thread(target=finish, daemon=True).start()
        return {"status": "streaming"}

    gateway.rpc = rpc

    result = gateway.run_turn(
        "hello",
        requested_session_id="",
        turn_id="turn-1",
        event_callback=None,
        cancel_check=None,
        timeout=2,
        conversation_id="conversation-1",
        artifact_context={},
    )

    assert result == "ok"


def test_gateway_tracks_multiple_conversations_with_distinct_artifact_scopes():
    gateway = _GatewayProcess.__new__(_GatewayProcess)
    gateway._session_lock = threading.Lock()
    gateway._sessions_by_conversation = {}
    gateway._sessions_by_live = {}
    gateway.live_session_id = ""
    gateway.stored_session_id = ""
    responses = iter(
        (
            {"session_id": "live-a", "stored_session_id": "stored-a"},
            {"session_id": "live-b", "stored_session_id": "stored-b"},
        )
    )
    calls = []

    def rpc(method, params, *, timeout):
        calls.append((method, params, timeout))
        return next(responses)

    gateway.rpc = rpc
    scope_a = {
        "root": "/artifacts",
        "owner_id": "owner",
        "conversation_id": "conversation-a",
        "account_generation": "generation",
    }
    scope_b = {**scope_a, "conversation_id": "conversation-b"}

    state_a = gateway.ensure_session(
        "conversation-a", artifact_context=scope_a
    )
    state_b = gateway.ensure_session(
        "conversation-b", artifact_context=scope_b
    )

    assert state_a.live_session_id == "live-a"
    assert state_b.live_session_id == "live-b"
    assert gateway._sessions_by_live == {"live-a": state_a, "live-b": state_b}
    assert calls[0][1]["tool_artifact_context"] == scope_a
    assert calls[1][1]["tool_artifact_context"] == scope_b


def test_gateway_replays_session_ready_emitted_before_local_registration():
    gateway = _GatewayProcess.__new__(_GatewayProcess)
    gateway._session_lock = threading.Lock()
    gateway._sessions_by_conversation = {}
    gateway._sessions_by_live = {}
    gateway._early_session_ready = {"live-a"}
    gateway._early_session_info = {}
    gateway.live_session_id = ""
    gateway.stored_session_id = ""
    gateway.rpc = lambda _method, _params, *, timeout: {
        "session_id": "live-a",
        "stored_session_id": "stored-a",
    }

    state = gateway.ensure_session(
        "conversation-a",
        artifact_context={"conversation_id": "conversation-a"},
    )

    assert state.agent_ready.is_set()
    assert gateway._early_session_ready == set()


def test_gateway_replays_early_session_info_as_ready_boundary():
    gateway = _GatewayProcess.__new__(_GatewayProcess)
    gateway._session_lock = threading.Lock()
    gateway._sessions_by_conversation = {}
    gateway._sessions_by_live = {}
    gateway._early_session_ready = set()
    gateway._early_session_info = {
        "live-a": {
            "type": "session.info",
            "payload": {"session_id": "stored-a", "running": False},
        }
    }
    gateway.live_session_id = ""
    gateway.stored_session_id = ""
    gateway.rpc = lambda _method, _params, *, timeout: {
        "session_id": "live-a",
        "stored_session_id": "stored-a",
    }

    state = gateway.ensure_session(
        "conversation-a",
        artifact_context={"conversation_id": "conversation-a"},
    )

    assert state.agent_ready.is_set()
    assert state.latest_session_info == {
        "type": "session.info",
        "payload": {
            "session_id": "stored-a",
            "running": False,
        },
    }
    assert gateway._early_session_info == {}


def test_tool_free_hosted_session_does_not_disable_reasoning():
    gateway = _GatewayProcess.__new__(_GatewayProcess)
    gateway._session_lock = threading.Lock()
    gateway._sessions_by_conversation = {}
    gateway._sessions_by_live = {}
    gateway.live_session_id = ""
    gateway.stored_session_id = ""
    calls = []
    gateway.rpc = lambda method, params, *, timeout: calls.append(
        (method, params, timeout)
    ) or {"session_id": "live-a", "stored_session_id": "stored-a"}

    gateway.ensure_session(
        "conversation-a",
        artifact_context={"allow_tools": "0"},
    )

    assert calls[0][0] == "session.create"
    assert calls[0][1]["allow_tools"] is False
    assert "reasoning_effort" not in calls[0][1]


def test_gateway_runs_distinct_sessions_without_account_wide_serialization():
    gateway = _GatewayProcess.__new__(_GatewayProcess)
    gateway.last_used = 0.0
    states = {
        conversation_id: _HostedSessionState(
            conversation_id=conversation_id,
            live_session_id=f"live-{conversation_id}",
            stored_session_id=f"stored-{conversation_id}",
            artifact_context={},
        )
        for conversation_id in ("a", "b")
    }
    for state in states.values():
        state.agent_ready.set()
    gateway.ensure_session = lambda conversation_id, *_args, **_kwargs: states[conversation_id]
    concurrent = 0
    max_concurrent = 0
    counter_lock = threading.Lock()

    def rpc(method, params, *, timeout):
        assert method == "prompt.submit"
        state = next(
            item for item in states.values() if item.live_session_id == params["session_id"]
        )

        def finish():
            nonlocal concurrent, max_concurrent
            sink = state.current_sink
            assert sink is not None
            sink.accepted.wait(timeout=1)
            with counter_lock:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            time.sleep(0.08)
            sink.result = state.conversation_id
            state.message_complete_seen.set()
            sink.done.set()
            state.idle_after_turn.set()
            with counter_lock:
                concurrent -= 1

        threading.Thread(target=finish, daemon=True).start()
        return {"status": "streaming"}

    gateway.rpc = rpc
    results = {}
    threads = [
        threading.Thread(
            target=lambda conversation_id=conversation_id: results.setdefault(
                conversation_id,
                gateway.run_turn(
                    "hello",
                    requested_session_id="",
                    turn_id=f"turn-{conversation_id}",
                    event_callback=None,
                    cancel_check=None,
                    timeout=2,
                    conversation_id=conversation_id,
                    artifact_context={},
                ),
            )
        )
        for conversation_id in states
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert results == {"a": "a", "b": "b"}
    assert max_concurrent == 2


def test_close_session_releases_gateway_worker_and_local_routing_state():
    gateway = _GatewayProcess.__new__(_GatewayProcess)
    gateway._session_lock = threading.Lock()
    state = _HostedSessionState(
        conversation_id="conversation-1",
        live_session_id="live-session",
        stored_session_id="stored-session",
        artifact_context={},
    )
    gateway._sessions_by_conversation = {"conversation-1": state}
    gateway._sessions_by_live = {"live-session": state}
    calls = []
    gateway.rpc = lambda method, params, *, timeout: calls.append(
        (method, params, timeout)
    ) or {"closed": True}

    assert gateway.close_session("conversation-1") is True
    assert calls == [("session.close", {"session_id": "live-session"}, 10.0)]
    assert gateway._sessions_by_conversation == {}
    assert gateway._sessions_by_live == {}


def test_gateway_pool_key_is_account_scoped_not_conversation_scoped():
    assert _pool_key(
        runtime_home="/runtime",
        owner_id="owner",
        account_generation="generation",
        profile="default",
        artifact_root="/artifacts",
    ) == ("/runtime", "owner", "generation", "default", "/artifacts")


def test_real_gateway_process_serves_two_isolated_conversations(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    runtime_home = tmp_path / "runtime"
    runtime_home.mkdir()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    env = {
        **os.environ,
        "HERMES_HOME": str(runtime_home),
        "HERMES_ISO_CERTIFY_SYNTH_TURN": "1",
        "HERMES_ISO_CERTIFY_DURATION_S": "0.01",
        "HERMES_TOOL_ARTIFACT_ROOT": str(artifact_root),
        "HERMES_TOOL_ARTIFACT_OWNER": "owner",
        "HERMES_ACCOUNT_GENERATION": "generation",
    }
    gateway = _GatewayProcess(env=env, cwd=str(repo_root))
    try:
        def run(conversation_id):
            return gateway.run_turn(
                "hello",
                requested_session_id="",
                turn_id=f"turn-{conversation_id}",
                event_callback=None,
                cancel_check=None,
                timeout=20,
                conversation_id=conversation_id,
                artifact_context={
                    "root": str(artifact_root),
                    "owner_id": "owner",
                    "conversation_id": conversation_id,
                    "account_generation": "generation",
                },
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run, ("conversation-a", "conversation-b")))

        assert all(result.startswith("[synthetic heavy turn]") for result in results)
        assert set(gateway._sessions_by_conversation) == {
            "conversation-a",
            "conversation-b",
        }
        assert len(gateway._sessions_by_live) == 2
    finally:
        gateway.close()
