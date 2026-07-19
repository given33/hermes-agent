import json

from hermes_cli import managed_node_recovery_watchdog as watchdog


def test_watchdog_writes_a_redacted_health_snapshot_once(tmp_path, monkeypatch):
    state_file = tmp_path / "watchdog.json"
    snapshot = {
        "configured": True,
        "nodes": [{"id": "wsl", "online": True}],
        "sources": [{"id": "home", "online": True}],
    }
    calls = []
    monkeypatch.setattr(
        watchdog,
        "fetch_managed_nodes",
        lambda path: calls.append(path) or snapshot,
    )

    result = watchdog.run_watchdog(
        tmp_path / "managed-nodes.json",
        state_file=state_file,
        once=True,
    )

    assert result == snapshot
    assert calls == [tmp_path / "managed-nodes.json"]
    assert json.loads(state_file.read_text(encoding="utf-8")) == snapshot
