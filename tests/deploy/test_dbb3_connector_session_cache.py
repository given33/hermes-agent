from __future__ import annotations

import json

import pytest

from deploy.dbb3 import dbb3_cloud_connector as connector_module


def test_timestamp_ms_rejects_non_finite_values():
    assert connector_module._timestamp_ms(True) is None
    assert connector_module._timestamp_ms(float("nan")) is None
    assert connector_module._timestamp_ms(float("inf")) is None
    assert connector_module._timestamp_ms(float("-inf")) is None


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
    class FailingConnector:
        def __init__(self, *_args, **_kwargs):
            pass

        def sync_once(self):
            raise OSError("cloud unavailable")

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


def test_main_rejects_non_finite_interval():
    with pytest.raises(SystemExit):
        connector_module.main(["--cloud-url", "https://example.test", "--interval", "inf"])


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
