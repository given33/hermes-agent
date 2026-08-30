import json
from datetime import datetime, timezone

from hermes_cli import managed_nodes
from hermes_services.worker_channel import WorkerChannelRegistry


NOW = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)


def _registry() -> WorkerChannelRegistry:
    return WorkerChannelRegistry(
        monotonic_clock=lambda: 100.0,
        wall_clock=lambda: NOW.timestamp(),
    )


def _config(tmp_path, **extra):
    token = tmp_path / "status-token"
    token.write_text("status-token", encoding="utf-8")
    path = tmp_path / "managed-nodes.json"
    path.write_text(json.dumps({
        "nodes": [{
            "id": "legacy-metrics",
            "status_url": "https://status.example/live",
            "token_file": str(token),
            "auto_recover": False,
            **extra,
        }],
    }), encoding="utf-8")
    return path


def test_status_always_represents_three_workers_without_legacy_config(tmp_path):
    result = managed_nodes.fetch_managed_nodes(
        tmp_path / "missing.json",
        now=NOW,
        worker_registry=_registry(),
    )

    assert result["configured"] is True
    assert result["sources"] == []
    assert [node["id"] for node in result["nodes"]] == ["dbb3", "wsl", "hk"]
    assert [node["online"] for node in result["nodes"]] == [False, False, False]


def test_worker_ws_is_liveness_truth_and_legacy_payload_only_enriches_metrics(
    tmp_path,
    monkeypatch,
):
    registry = _registry()
    registry.connect(
        "dbb3-worker",
        connection_generation="dbb3-generation",
        release={"node_id": "dbb3", "commit": "a" * 40, "version": "v2"},
        runtime={"worker_ready": True, "active_tasks": 1},
    )
    registry.connect(
        "hk-worker",
        connection_generation="hk-generation",
        release={"node_id": "hk", "commit": "b" * 40, "version": "v3"},
    )
    monkeypatch.setattr(
        managed_nodes,
        "_fetch_status",
        lambda _config: {
            "timestamp": NOW.isoformat(),
            "devices": {
                "dbb3": {"sampled_at": NOW.isoformat(), "cpu_percent": 11},
                "pc": {
                    "available": True,
                    "sampled_at": NOW.isoformat(),
                    "cpu_percent": 22,
                },
            },
            # These obsolete fields must not make the disconnected WSL lane live.
            "gateways": {
                "agent": {"alive": False},
                "rainday": {"alive": True, "state": "active"},
            },
            "services": {"hermes_gateway": "active"},
            "wsl": {"gateway_running": True, "worker_ready": True},
        },
    )

    result = managed_nodes.fetch_managed_nodes(
        _config(tmp_path),
        now=NOW,
        worker_registry=registry,
    )

    assert [node["online"] for node in result["nodes"]] == [True, False, True]
    dbb3, wsl, hk = result["nodes"]
    assert dbb3["version"] == "v2"
    assert dbb3["active_tasks"] == 1
    assert dbb3["metrics"]["cpu_percent"] == 11
    assert wsl["metrics"]["cpu_percent"] == 22
    assert wsl["metrics_available"] is True
    assert wsl["gateway_state"] == "offline"
    assert hk["version"] == "v3"
    assert all("lease_id" not in node for node in result["nodes"])
    assert all("connection_generation" not in node for node in result["nodes"])


def test_legacy_source_failure_does_not_recover_connected_workers(
    tmp_path,
    monkeypatch,
):
    registry = _registry()
    for worker in ("dbb3-worker", "pc-worker", "hk-worker"):
        registry.connect(worker, connection_generation=f"{worker}-generation")
    config_path = _config(
        tmp_path,
        recovery_urls={
            "dbb3": "https://dbb3.example/recover",
            "wsl": "https://wsl.example/recover",
            "hk": "https://hk.example/recover",
        },
        auto_recover=True,
    )
    monkeypatch.setattr(
        managed_nodes,
        "_fetch_status",
        lambda _config: (_ for _ in ()).throw(OSError("metrics source unavailable")),
    )
    recovery_calls = []
    monkeypatch.setattr(
        managed_nodes,
        "_request_recovery",
        lambda *args, **kwargs: recovery_calls.append((args, kwargs)),
    )

    result = managed_nodes.fetch_managed_nodes(
        config_path,
        now=NOW,
        worker_registry=registry,
    )

    assert [node["online"] for node in result["nodes"]] == [True, True, True]
    assert result["sources"][0]["error"] == "upstream_invalid"
    assert recovery_calls == []
