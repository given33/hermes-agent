from types import SimpleNamespace
import threading

from agent import tool_executor


def _agent_with_capacity(capacity: int):
    return SimpleNamespace(
        _tool_execution_slots=threading.BoundedSemaphore(capacity)
    )


def test_abandoned_worker_restores_agent_capacity_but_keeps_process_bound(
    monkeypatch,
):
    process_slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(
        tool_executor, "_PROCESS_TOOL_EXECUTION_SLOTS", process_slots
    )
    agent = _agent_with_capacity(1)

    orphan, error = tool_executor._acquire_tool_execution_lease(agent)
    assert error == ""
    assert orphan is not None
    orphan.abandon()

    replacement, error = tool_executor._acquire_tool_execution_lease(agent)
    assert error == ""
    assert replacement is not None

    blocked, error = tool_executor._acquire_tool_execution_lease(
        _agent_with_capacity(1)
    )
    assert blocked is None
    assert error == "process tool worker limit reached"

    orphan.finish()
    replacement.finish()
    assert process_slots._value == 2
    assert agent._tool_execution_slots._value == 1


def test_agent_limit_failure_does_not_leak_process_capacity(monkeypatch):
    process_slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(
        tool_executor, "_PROCESS_TOOL_EXECUTION_SLOTS", process_slots
    )
    agent = _agent_with_capacity(1)

    first, error = tool_executor._acquire_tool_execution_lease(agent)
    assert error == ""
    assert first is not None

    blocked, error = tool_executor._acquire_tool_execution_lease(agent)
    assert blocked is None
    assert error == "agent tool worker limit reached"
    assert process_slots._value == 1

    first.finish()
    assert process_slots._value == 2


def test_finish_is_idempotent_after_abandonment(monkeypatch):
    process_slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(
        tool_executor, "_PROCESS_TOOL_EXECUTION_SLOTS", process_slots
    )
    agent = _agent_with_capacity(1)
    lease, _error = tool_executor._acquire_tool_execution_lease(agent)
    assert lease is not None

    lease.abandon()
    lease.abandon()
    lease.finish()
    lease.finish()

    assert process_slots._value == 1
    assert agent._tool_execution_slots._value == 1
