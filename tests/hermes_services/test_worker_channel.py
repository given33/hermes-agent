import threading
import time

from hermes_services.low_latency_protocol import ProtocolError, make_event, validate_event
from hermes_services.worker_channel import HEARTBEAT_TIMEOUT_SECONDS, WorkerChannelRegistry


def test_worker_channel_fences_nodes_and_replays_without_duplicates():
    registry = WorkerChannelRegistry(max_backlog=64)
    connection = registry.connect(
        "dbb3-worker",
        connection_generation="gen-1",
        capabilities=["tools"],
        version="1",
    )
    queued = registry.publish("dbb3-worker", "worker.queued", payload={"id": "r1"})
    assert registry.replay("dbb3-worker", 0) == [queued]
    event = make_event(
        "worker.started",
        node_id="dbb3-worker",
        sequence=1,
        payload={"id": "r1"},
    )
    received, duplicate = registry.append("dbb3-worker", connection.lease_id, event)
    assert received["sequence"] == 2
    assert duplicate is False
    _, duplicate = registry.append("dbb3-worker", connection.lease_id, event)
    assert duplicate is True
    assert registry.disconnect("dbb3-worker", connection.lease_id)


def test_worker_channel_rejects_manager_and_stale_lease():
    registry = WorkerChannelRegistry()
    try:
        registry.connect("hermes-manager", connection_generation="gen-1")
    except ProtocolError:
        pass
    else:
        raise AssertionError("manager must remain server-local")
    connection = registry.connect("pc-worker", connection_generation="gen-1")
    event = make_event("worker.started", node_id="pc-worker")
    try:
        registry.append("pc-worker", "stale", event)
    except ProtocolError:
        pass
    else:
        raise AssertionError("stale worker lease must be fenced")
    assert connection.node_id == "pc-worker"


def test_worker_channel_rejects_connector_node_mismatch():
    registry = WorkerChannelRegistry()
    try:
        registry.connect(
            "dbb3-worker",
            connection_generation="gen-1",
            connector_id="pc-primary",
            expected_connector_id="dbb3-primary",
        )
    except ProtocolError as exc:
        assert "not mapped" in str(exc)
    else:
        raise AssertionError("connector must not assume another node identity")


def test_worker_channel_rejects_old_lease_heartbeat_and_event_after_replacement():
    registry = WorkerChannelRegistry()
    old = registry.connect("dbb3-worker", connection_generation="generation-1")
    current = registry.connect("dbb3-worker", connection_generation="generation-2")

    assert registry.heartbeat("dbb3-worker", old.lease_id) is False
    try:
        registry.append(
            "dbb3-worker",
            old.lease_id,
            make_event("worker.completed", node_id="dbb3-worker"),
        )
    except ProtocolError as exc:
        assert str(exc) == "stale worker lease"
    else:
        raise AssertionError("replaced worker lease accepted an event")

    received, duplicate = registry.append(
        "dbb3-worker",
        current.lease_id,
        make_event("worker.completed", node_id="dbb3-worker"),
    )
    assert received["type"] == "worker.completed"
    assert duplicate is False


def test_worker_channel_rejects_oversized_events():
    registry = WorkerChannelRegistry()
    connection = registry.connect(
        "dbb3-worker",
        connection_generation="gen-1",
        connector_id="dbb3-primary",
        expected_connector_id="dbb3-primary",
    )
    event = make_event(
        "worker.started",
        node_id="dbb3-worker",
        payload={"blob": "x" * 600_000},
    )
    try:
        registry.append("dbb3-worker", connection.lease_id, event)
    except ProtocolError as exc:
        assert "512 KiB" in str(exc)
    else:
        raise AssertionError("oversized worker event must be rejected")


def test_worker_channel_wait_wakes_without_polling_backlog():
    registry = WorkerChannelRegistry()
    registry.connect("dbb3-worker", connection_generation="gen-1")

    def publish_when_waiter_parked():
        for _ in range(100):
            if registry._changed._waiters:
                registry.publish("dbb3-worker", "worker.queued")
                return
            time.sleep(0.001)
        raise AssertionError("waiter never parked")

    publisher = threading.Thread(target=publish_when_waiter_parked)
    publisher.start()
    try:
        events = registry.wait_for_replay("dbb3-worker", 0, timeout=2)
    finally:
        publisher.join(timeout=1)

    assert [event["type"] for event in events] == ["worker.queued"]


def test_worker_channel_does_not_refresh_dead_lease_from_server_heartbeat():
    registry = WorkerChannelRegistry()
    connection = registry.connect("pc-worker", connection_generation="gen-1")

    assert registry.lease_alive(connection.node_id, connection.lease_id, timeout_seconds=1)
    connection.last_heartbeat = time.monotonic() - 2

    assert not registry.lease_alive(
        connection.node_id,
        connection.lease_id,
        timeout_seconds=1,
    )
    assert not registry.lease_alive(connection.node_id, "stale", timeout_seconds=1)


def test_worker_channel_rejects_events_after_heartbeat_timeout():
    registry = WorkerChannelRegistry()
    connection = registry.connect("pc-worker", connection_generation="gen-1")
    connection.last_heartbeat = time.monotonic() - HEARTBEAT_TIMEOUT_SECONDS - 1
    event = make_event("worker.completed", node_id="pc-worker")

    try:
        registry.append("pc-worker", connection.lease_id, event)
    except ProtocolError as exc:
        assert str(exc) == "worker lease expired"
    else:
        raise AssertionError("expired worker lease accepted an event")
    assert registry.replay("pc-worker", 0) == []


def test_expired_replay_waiter_does_not_return_new_server_event():
    registry = WorkerChannelRegistry()
    connection = registry.connect("pc-worker", connection_generation="gen-1")
    connection.last_heartbeat = time.monotonic() - HEARTBEAT_TIMEOUT_SECONDS - 1
    registry.publish("pc-worker", "worker.queued", payload={"task": {}})

    assert registry.wait_for_replay(
        "pc-worker",
        0,
        timeout=0.01,
        lease_id=connection.lease_id,
    ) == []


def test_worker_channel_accepts_event_after_fresh_heartbeat():
    registry = WorkerChannelRegistry()
    connection = registry.connect("pc-worker", connection_generation="gen-1")
    event = make_event("worker.completed", node_id="pc-worker")

    assert registry.heartbeat("pc-worker", connection.lease_id)
    received, duplicate = registry.append("pc-worker", connection.lease_id, event)

    assert received["type"] == "worker.completed"
    assert duplicate is False


def test_worker_channel_replay_waiter_exits_on_cancel_and_fence():
    registry = WorkerChannelRegistry()
    first = registry.connect("dbb3-worker", connection_generation="gen-1")
    fenced_result: list[list[dict]] = []
    fenced_waiter = threading.Thread(
        target=lambda: fenced_result.append(
            registry.wait_for_replay(
                "dbb3-worker",
                0,
                timeout=30,
                lease_id=first.lease_id,
            )
        )
    )
    fenced_waiter.start()
    for _ in range(100):
        with registry._changed:
            if registry._changed._waiters:
                break
        time.sleep(0.001)
    registry.connect("dbb3-worker", connection_generation="gen-2")
    fenced_waiter.join(timeout=1)
    assert not fenced_waiter.is_alive()
    assert fenced_result == [[]]

    second = registry.snapshot("dbb3-worker")["lease_id"]
    cancel = threading.Event()
    cancelled_result: list[list[dict]] = []
    cancelled_waiter = threading.Thread(
        target=lambda: cancelled_result.append(
            registry.wait_for_replay(
                "dbb3-worker",
                0,
                timeout=30,
                lease_id=str(second),
                cancel_event=cancel,
            )
        )
    )
    cancelled_waiter.start()
    for _ in range(100):
        with registry._changed:
            if registry._changed._waiters:
                break
        time.sleep(0.001)
    cancel.set()
    registry.wake_replay_waiters()
    cancelled_waiter.join(timeout=1)
    assert not cancelled_waiter.is_alive()
    assert cancelled_result == [[]]


def test_reconnected_worker_window_retains_delivered_event_ids():
    registry = WorkerChannelRegistry()
    first = registry.connect(
        "dbb3-worker",
        connection_generation="gen-1",
        connector_id="dbb3-primary",
        expected_connector_id="dbb3-primary",
    )
    event = make_event(
        "worker.started",
        node_id="dbb3-worker",
        sequence=1,
        event_id="delivered-once",
    )
    registry.append("dbb3-worker", first.lease_id, event)
    registry.disconnect("dbb3-worker", first.lease_id)

    second = registry.connect(
        "dbb3-worker",
        connection_generation="gen-2",
        connector_id="dbb3-primary",
        expected_connector_id="dbb3-primary",
    )
    _, duplicate = registry.append("dbb3-worker", second.lease_id, event)

    assert duplicate is True


def test_worker_sequence_hint_can_reset_below_server_replay_cursor():
    registry = WorkerChannelRegistry()
    connection = registry.connect("dbb3-worker", connection_generation="gen-1")
    for index in range(100):
        registry.publish("dbb3-worker", "worker.queued", request_id=f"run-{index}")

    event = make_event(
        "worker.started",
        node_id="dbb3-worker",
        sequence=1,
        event_id="producer-restarted",
    )
    received, duplicate = registry.append(
        "dbb3-worker",
        connection.lease_id,
        event,
    )

    assert duplicate is False
    assert received["sequence"] == 101


def test_duplicate_worker_event_ack_returns_server_canonical_sequence():
    registry = WorkerChannelRegistry()
    connection = registry.connect("pc-worker", connection_generation="gen-1")
    original = make_event(
        "worker.completed",
        node_id="pc-worker",
        sequence=1,
        event_id="canonical-event",
        payload={"status": "completed"},
    )
    stored, duplicate = registry.append("pc-worker", connection.lease_id, original)
    assert duplicate is False

    retry = make_event(
        "worker.completed",
        node_id="pc-worker",
        sequence=1,
        event_id="canonical-event",
        payload={"status": "retry-payload-must-not-rewrite"},
    )
    acknowledged, duplicate = registry.append(
        "pc-worker",
        connection.lease_id,
        retry,
    )

    assert duplicate is True
    assert acknowledged == stored
    assert acknowledged["sequence"] == 1


def test_malformed_numeric_envelope_is_a_protocol_error():
    event = make_event("worker.started", node_id="dbb3-worker")
    event["sequence"] = "not-a-number"
    try:
        validate_event(event)
    except ProtocolError as exc:
        assert "integers" in str(exc)
    else:
        raise AssertionError("malformed transport input must not escape as ValueError")


def test_protocol_rejects_lossy_or_unsafe_numeric_fields():
    for field, value in (
        ("sequence", True),
        ("cursor", 1.5),
        ("timestamp", 2**53),
    ):
        event = make_event("worker.started", node_id="dbb3-worker")
        event[field] = value
        try:
            validate_event(event)
        except ProtocolError as exc:
            assert field in str(exc)
        else:
            raise AssertionError(f"malformed {field} must be rejected")
