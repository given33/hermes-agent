from __future__ import annotations

import json

from deploy.dbb3 import dbb3_cloud_connector as connector_module


def test_timestamp_ms_rejects_non_finite_values():
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


def test_session_snapshot_cache_is_bounded_lru(tmp_path, monkeypatch):
    calls = []

    def command_runner(command, **_kwargs):
        session_id = command[command.index("--session-id") + 1]
        calls.append(session_id)
        return 0, json.dumps({"id": session_id, "model": "test-model"})

    connector = connector_module.DBB3CloudConnector(
        object(),
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
