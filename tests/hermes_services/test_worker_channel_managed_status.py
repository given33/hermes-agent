import pytest

from hermes_services.low_latency_protocol import ProtocolError
from hermes_services.worker_channel import (
    HEARTBEAT_TIMEOUT_SECONDS,
    WorkerChannelRegistry,
    get_worker_channel_registry,
)


def test_managed_snapshots_are_fixed_order_and_publicly_sanitized():
    monotonic = [10.0]
    wall = [1_800_000_000.0]
    registry = WorkerChannelRegistry(
        monotonic_clock=lambda: monotonic[0],
        wall_clock=lambda: wall[0],
    )
    connection = registry.connect(
        "hk-worker",
        connection_generation="private-generation",
        connector_id="hk-primary",
        expected_connector_id="hk-primary",
        runtime={"worker_ready": True, "active_tasks": 2, "ignored": "value"},
        release={
            "schema": "hermes.fabric-release.v1",
            "node_id": "hk",
            "commit": "a" * 40,
            "version": "v1.2.3",
            "ignored": "value",
        },
        metrics={
            "cpu_percent": 12.5,
            "memory_total_bytes": 1024,
            "available": True,
            "ignored": "value",
        },
    )

    snapshots = registry.managed_snapshots()

    assert [item["id"] for item in snapshots] == ["dbb3", "wsl", "hk"]
    assert [item["online"] for item in snapshots] == [False, False, True]
    hk = snapshots[-1]
    assert hk["gateway_state"] == "ready"
    assert hk["version"] == "v1.2.3"
    assert hk["active_tasks"] == 2
    assert hk["runtime"] == {"worker_ready": True, "active_tasks": 2}
    assert hk["metrics"] == {
        "cpu_percent": 12.5,
        "memory_total_bytes": 1024,
        "available": True,
    }
    assert "lease_id" not in hk
    assert "connection_generation" not in hk
    assert "private-generation" not in repr(snapshots)
    deployment = registry.deployment_snapshot("hk-worker")
    assert deployment["online"] is True
    assert deployment["connection_generation"] == "private-generation"
    assert deployment["release"]["commit"] == "a" * 40
    assert "lease_id" not in deployment

    monotonic[0] += HEARTBEAT_TIMEOUT_SECONDS + 1
    assert registry.managed_snapshots()[-1]["online"] is False

    wall[0] += 30
    assert registry.heartbeat(
        "hk-worker",
        connection.lease_id,
        runtime={"worker_ready": True, "active_tasks": 0},
    )
    refreshed = registry.managed_snapshots()[-1]
    assert refreshed["online"] is True
    assert refreshed["active_tasks"] == 0
    assert refreshed["observed_at"].startswith("2027-")


def test_worker_status_rejects_identity_mismatch_and_unbounded_payloads():
    registry = WorkerChannelRegistry()

    with pytest.raises(ProtocolError, match="node_id does not match"):
        registry.connect(
            "hk-worker",
            connection_generation="generation",
            release={"node_id": "dbb3"},
        )

    with pytest.raises(ProtocolError, match="16 KiB"):
        registry.connect(
            "dbb3-worker",
            connection_generation="generation",
            runtime={"ignored": "x" * (17 * 1024)},
        )


def test_process_registry_is_shared_without_admitting_a_dispatcher():
    assert get_worker_channel_registry() is get_worker_channel_registry()
    with pytest.raises(ProtocolError, match="only"):
        WorkerChannelRegistry().connect(
            "hermes-manager",
            connection_generation="forbidden",
        )
