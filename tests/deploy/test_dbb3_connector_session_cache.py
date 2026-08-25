from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from deploy.dbb3 import dbb3_cloud_connector as connector_module


def test_timestamp_ms_rejects_non_finite_values():
    assert connector_module._timestamp_ms(True) is None
    assert connector_module._timestamp_ms(float("nan")) is None
    assert connector_module._timestamp_ms(float("inf")) is None
    assert connector_module._timestamp_ms(float("-inf")) is None


def test_command_runner_uses_utf8_replacement_for_windows_output(monkeypatch):
    captured = {}

    def fake_run(_command, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="完成�", stderr="")

    monkeypatch.setattr(connector_module.subprocess, "run", fake_run)

    assert connector_module.run(["hermes", "--version"]) == (0, "完成�")
    assert captured["text"] is True
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"


def test_cloud_client_rejects_malformed_list_responses():
    client = connector_module.CloudRelayClient("https://example.test", "token")

    def malformed(_path, **_kwargs):
        return {"runs": {"remote_run_id": "run-1"}}

    client._request = malformed
    try:
        client.pull_runs()
    except connector_module.ConnectorContractError as exc:
        assert exc.status == 502
    else:
        raise AssertionError("malformed runs response was accepted")


def test_cloud_client_rejects_missing_list_response_field():
    client = connector_module.CloudRelayClient("https://example.test", "token")
    client._request = lambda _path, **_kwargs: {}
    with pytest.raises(connector_module.ConnectorContractError) as error:
        client.pull_runs()
    assert error.value.status == 502


def test_activity_timing_ignores_non_finite_duration_values():
    started, completed, duration = connector_module._activity_timing(
        {"duration_ms": float("nan"), "duration_seconds": float("inf")},
        None,
    )
    assert started is None
    assert completed is None
    assert duration is None


def test_checkpoint_store_recovers_invalid_top_level_collections(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text(
        json.dumps({"version": 1, "runs": [], "cancellations": "broken"}),
        encoding="utf-8",
    )
    state = connector_module.CheckpointStore(path).load()
    assert state["runs"] == {}
    assert state["cancellations"] == {}


def test_checkpoint_run_recovers_invalid_nested_record():
    state = {"runs": {"run-1": ["corrupt"]}}
    local = connector_module._checkpoint_run(state, "run-1")
    assert local == {}
    assert state["runs"]["run-1"] is local


def test_checkpoint_cursor_is_fail_closed_and_non_negative():
    assert connector_module._checkpoint_cursor("12") == 12
    assert connector_module._checkpoint_cursor(-4) == 0
    assert connector_module._checkpoint_cursor(True) == 0
    assert connector_module._checkpoint_cursor("broken") == 0
    assert connector_module._checkpoint_cursor(float("inf")) == 0


def test_coerce_flag_handles_textual_values():
    assert connector_module._coerce_flag("true") is True
    assert connector_module._coerce_flag("false") is False
    assert connector_module._coerce_flag("unexpected") is False
    assert connector_module._coerce_flag(True) is True


def test_build_root_command_ignores_non_finite_runtime_limit():
    command = connector_module.build_root_task_command(
        {"objective": "test", "remote_run_id": "run-1", "max_runtime_seconds": float("inf")}
    )
    assert "--max-runtime" not in command


def test_content_length_parser_rejects_invalid_headers():
    assert connector_module._content_length_bytes(None) is None
    assert connector_module._content_length_bytes("12") == 12
    for value in ("broken", "-1"):
        try:
            connector_module._content_length_bytes(value)
        except connector_module.ConnectorContractError as exc:
            assert exc.status == 502
        else:
            raise AssertionError(f"invalid Content-Length accepted: {value}")


def test_main_once_returns_failure_for_transient_sync_error(tmp_path, monkeypatch):
    closed = []

    class FailingConnector:
        def __init__(self, *_args, **_kwargs):
            pass

        def sync_once(self):
            raise OSError("cloud unavailable")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(connector_module, "_load_token", lambda _path: "token")
    monkeypatch.setattr(connector_module, "DBB3CloudConnector", FailingConnector)
    assert (
        connector_module.main(
            [
                "--cloud-url",
                "https://example.test",
                "--token-file",
                str(tmp_path / "token"),
                "--once",
            ]
        )
        == 75
    )
    assert closed == [True]


def test_main_rejects_non_finite_interval():
    with pytest.raises(SystemExit):
        connector_module.main(["--cloud-url", "https://example.test", "--interval", "inf"])


def test_connector_keeps_polling_when_event_stream_is_unavailable(tmp_path):
    connector = connector_module.DBB3CloudConnector(
        object(),
        state_file=tmp_path / "checkpoint.json",
        artifact_roots=[tmp_path],
    )
    assert connector._stream_thread is None
    connector.close()


def test_connector_routes_stream_steers_to_its_owned_queue(tmp_path):
    event = {"type": "run.steer", "remote_run_id": "run-1", "message": "continue"}

    class _StreamingCloudClient:
        def _stream_events(self, wake, stop, on_steer):
            on_steer(event)

    connector = connector_module.DBB3CloudConnector(
        _StreamingCloudClient(),
        state_file=tmp_path / "checkpoint.json",
        artifact_roots=[tmp_path],
    )
    assert connector._wake_event.wait(timeout=1)
    assert connector._pending_steers == [event]
    connector.close()


def test_stream_connection_uses_bounded_socket_timeout(monkeypatch):
    client = connector_module.CloudRelayClient("https://example.test", "token")
    wake = threading.Event()
    stop = threading.Event()
    captured = {}

    def fake_urlopen(_request, *, timeout):
        captured["timeout"] = timeout
        stop.set()
        raise OSError("connect blocked")

    monkeypatch.setattr(connector_module.urllib.request, "urlopen", fake_urlopen)
    client._stream_events(wake, stop)

    assert captured["timeout"] == connector_module._STREAM_TIMEOUT_SECONDS
    assert captured["timeout"] <= 35.0


def test_stream_parser_joins_multiline_data_and_advances_last_event_id(monkeypatch):
    client = connector_module.CloudRelayClient("https://example.test", "token")
    wake = threading.Event()

    class StopAfterWait:
        def __init__(self):
            self.waits = []

        def is_set(self):
            return False

        def wait(self, timeout):
            self.waits.append(timeout)
            return True

    class Response:
        def __iter__(self):
            return iter(
                [
                    b"id: 42\n",
                    b"event: relay\n",
                    b'data: {"type":"run.created",\n',
                    b'data: "remote_run_id":"run-1"}\n',
                    b"\n",
                ]
            )

        def close(self):
            pass

    requests = []
    monkeypatch.setattr(
        connector_module.urllib.request,
        "urlopen",
        lambda request, *, timeout: (requests.append(request) or Response()),
    )
    stop = StopAfterWait()
    client._stream_events(wake, stop)

    assert wake.is_set()
    assert client._last_stream_event_id == "42"
    assert stop.waits == [1.0]

    def fail_urlopen(request, *, timeout):
        requests.append(request)
        raise OSError("offline")

    monkeypatch.setattr(connector_module.urllib.request, "urlopen", fail_urlopen)
    client._stream_events(wake, StopAfterWait())
    assert requests[-1].get_header("Last-event-id") == "42"


def test_stream_parser_accepts_bare_cr_and_split_utf8_bytes():
    events = list(
        connector_module._iter_sse_events(
            [
                b'id: 9\rdata: {"text":"' + "中".encode("utf-8")[:1],
                "中".encode("utf-8")[1:] + b'"}\r\r',
            ]
        )
    )

    assert events == [("9", "message", '{"text":"中"}')]


def test_stream_parser_yields_an_unterminated_final_event():
    events = list(
        connector_module._iter_sse_events(
            [b'id: 79\ndata: {"type":"run.created"}']
        )
    )

    assert events == [("79", "message", '{"type":"run.created"}')]


def test_stream_parser_bounds_an_undelimited_event_and_recovers(monkeypatch):
    monkeypatch.setattr(connector_module, "_MAX_SSE_EVENT_BYTES", 64)

    oversized = b"data: " + (b"x" * 128)
    normal = b'id: 78\ndata: {"type":"run.created"}\n\n'
    events = list(connector_module._iter_sse_events([oversized, b"\n", normal]))

    assert events == [("78", "message", '{"type":"run.created"}')]
    assert list(connector_module._iter_sse_events([b"x" * 128])) == []


def test_stream_parser_preserves_sse_empty_id_reset():
    events = list(
        connector_module._iter_sse_events(
            [
                b'id: 42\ndata: {"type":"run.created"}\n\n',
                b'id:\ndata: {"type":"run.terminal"}\n\n',
                b'data: {"type":"run.created"}\n\n',
            ]
        )
    )

    assert [event[0] for event in events] == ["42", "", None]


def test_stream_reconnect_drops_last_event_id_after_empty_id(monkeypatch):
    client = connector_module.CloudRelayClient("https://example.test", "token")
    requests = []

    class StopAfterResponse:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return False

        def wait(self, _timeout):
            self.waits += 1
            return self.waits >= 1

    class Response:
        def __iter__(self):
            return iter(
                [
                    b'id: 42\ndata: {"type":"run.created"}\n\n',
                    b'id:\ndata: {"type":"run.terminal"}\n\n',
                ]
            )

        def close(self):
            pass

    monkeypatch.setattr(
        connector_module.urllib.request,
        "urlopen",
        lambda request, *, timeout: (requests.append(request) or Response()),
    )
    client._stream_events(threading.Event(), StopAfterResponse())

    assert client._last_stream_event_id == ""
    assert requests[-1].get_header("Last-event-id") is None


def test_stream_reconnect_backoff_is_not_reset_by_immediate_eof(monkeypatch):
    client = connector_module.CloudRelayClient("https://example.test", "token")

    class StopAfterThreeWaits:
        def __init__(self):
            self.waits = []

        def is_set(self):
            return False

        def wait(self, timeout):
            self.waits.append(timeout)
            return len(self.waits) >= 3

    class EmptyResponse:
        def __iter__(self):
            return iter(())

        def close(self):
            pass

    monkeypatch.setattr(
        connector_module.urllib.request,
        "urlopen",
        lambda _request, *, timeout: EmptyResponse(),
    )
    stop = StopAfterThreeWaits()
    client._stream_events(threading.Event(), stop)

    assert stop.waits == [1.0, 2.0, 4.0]


def test_stream_close_race_after_connect_closes_registered_response(monkeypatch):
    client = connector_module.CloudRelayClient("https://example.test", "token")
    stop = threading.Event()

    class Response:
        closed = False

        def __iter__(self):
            return iter(())

        def close(self):
            self.closed = True

    response = Response()

    def fake_urlopen(_request, *, timeout):
        stop.set()
        return response

    monkeypatch.setattr(connector_module.urllib.request, "urlopen", fake_urlopen)
    client._stream_events(threading.Event(), stop)

    assert response.closed is True
    assert client._stream_response is None


def test_connector_close_interrupts_blocked_stream(tmp_path):
    entered = threading.Event()
    closed = threading.Event()

    class _BlockingCloudClient:
        def _stream_events(self, wake, stop):
            entered.set()
            while not closed.wait(0.01):
                pass

        def close_stream(self):
            closed.set()

    connector = connector_module.DBB3CloudConnector(
        _BlockingCloudClient(),
        state_file=tmp_path / "checkpoint.json",
        artifact_roots=[tmp_path],
    )
    try:
        assert entered.wait(timeout=1)
        started = time.monotonic()
        connector.close(timeout=1)
        elapsed = time.monotonic() - started
        assert closed.is_set()
        assert connector._stream_thread is not None
        assert not connector._stream_thread.is_alive()
        assert elapsed < 1.5
    finally:
        connector.close(timeout=0)


def test_connector_bounds_realtime_steer_wakeup_queue(tmp_path):
    connector = connector_module.DBB3CloudConnector(
        object(),
        state_file=tmp_path / "checkpoint.json",
        artifact_roots=[tmp_path],
    )
    for index in range(connector_module._MAX_PENDING_STEERS + 10):
        connector._queue_steer({"id": str(index), "remote_run_id": "run-1", "text": "continue"})

    assert len(connector._pending_steers) == connector_module._MAX_PENDING_STEERS
    assert connector._pending_steers[0]["id"] == "10"
    assert connector._pending_steers[-1]["id"] == str(connector_module._MAX_PENDING_STEERS + 9)
    connector.close()


def test_session_snapshot_cache_is_bounded_lru(tmp_path, monkeypatch):
    calls = []

    def command_runner(command, **_kwargs):
        session_id = command[command.index("--session-id") + 1]
        calls.append(session_id)
        return 0, json.dumps({"id": session_id, "model": "test-model"})

    class _FakeCloudClient:
        # The connector constructor starts a stream watcher thread against
        # the cloud client; a fake only needs the entry point to exist.
        def _stream_events(self, wake, stop):
            stop.wait()

    connector = connector_module.DBB3CloudConnector(
        _FakeCloudClient(),
        command_runner=command_runner,
        state_file=tmp_path / "checkpoint.json",
        artifact_roots=[tmp_path],
    )
    monkeypatch.setattr(connector_module, "_SESSION_CACHE_MAX", 2)
    monkeypatch.setattr(
        connector,
        "_discover_session_id",
        lambda _detail, local: local["session_id"],
    )

    def snapshot(name):
        return connector._session_snapshot(
            {},
            {
                "session_id": name,
                "remote_run_id": f"remote-{name}",
                "execution_profile": "default",
            },
            terminal=False,
        )

    snapshot("one")
    snapshot("two")
    snapshot("one")
    snapshot("three")

    assert list(connector._session_cache) == ["remote-one", "remote-three"]
    assert calls == ["one", "two", "three"]
    connector.close()
