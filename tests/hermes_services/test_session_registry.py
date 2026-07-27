from __future__ import annotations

import threading

from hermes_services.session_registry import LiveSessionRegistry


def test_registry_iteration_is_a_stable_snapshot() -> None:
    registry = LiveSessionRegistry[dict]()
    registry["one"] = {"value": 1}
    iterator = iter(registry)

    registry["two"] = {"value": 2}

    assert list(iterator) == ["one"]
    assert list(registry) == ["one", "two"]


def test_registry_lock_supports_atomic_multi_step_updates() -> None:
    registry = LiveSessionRegistry[int]()
    registry["counter"] = 0

    def increment() -> None:
        for _ in range(500):
            with registry.lock:
                registry["counter"] = registry["counter"] + 1

    workers = [threading.Thread(target=increment) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert registry["counter"] == 2_000


def test_snapshot_is_shallow_and_independent_from_membership_changes() -> None:
    value = {"state": "running"}
    registry = LiveSessionRegistry[dict]()
    registry["session"] = value

    snapshot = registry.snapshot()
    registry.clear()

    assert snapshot == {"session": value}
    assert not registry
