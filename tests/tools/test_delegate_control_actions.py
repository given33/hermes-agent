"""Behavioral coverage for live delegate_task orchestration controls."""
import json
import weakref

import pytest

import tools.delegate_tool as delegate
from agent.tool_guardrails import (
    LoopCapConfig,
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    _subagent_spawn_count,
)


class StubParent:
    pass


class StubChild:
    def __init__(self, parent=None, accepting_steer=True):
        self.steered = []
        self.accepting_steer = accepting_steer
        self.interrupted = []
        self._live_transcript_path = "/tmp/live/task-0.log"
        if parent is not None:
            self._delegate_parent_ref = weakref.ref(parent)

    def steer(self, text):
        if not self.accepting_steer:
            return False
        self.steered.append(text)
        return True

    def interrupt(self, message=None):
        self.interrupted.append(message)


def register(sid, child, **extra):
    record = {
        "subagent_id": sid,
        "parent_id": None,
        "depth": 0,
        "goal": "test goal",
        "model": "test-model",
        "started_at": 1000.0,
        "status": "running",
        "tool_count": 0,
        "agent": child,
    }
    record.update(extra)
    delegate._register_subagent(record)


def unregister(*ids):
    for sid in ids:
        delegate._unregister_subagent(sid)


def test_control_ownership_follows_direct_and_grandchild_parent_refs():
    parent = StubParent()
    child = StubChild(parent)
    grandchild = StubChild(child)
    foreign = StubChild(StubParent())
    assert delegate._is_descendant_of(child, parent)
    assert delegate._is_descendant_of(grandchild, parent)
    assert not delegate._is_descendant_of(foreign, parent)
    assert not delegate._is_descendant_of(StubChild(), parent)


def test_list_only_exposes_the_calling_spawn_tree():
    parent = StubParent()
    mine = StubChild(parent)
    foreign = StubChild(StubParent())
    register("control-list-owned", mine)
    register("control-list-foreign", foreign)
    try:
        result = json.loads(delegate._handle_control_action("list", None, None, parent))
        assert result["count"] == 1
        entry = result["subagents"][0]
        assert entry["subagent_id"] == "control-list-owned"
        assert entry["accepting_steer"] is True
        assert entry["live_transcript"] == "/tmp/live/task-0.log"
        assert "agent" not in entry
        assert "owner_transport" not in entry
    finally:
        unregister("control-list-owned", "control-list-foreign")


def test_steer_and_stop_reach_owned_child_but_refuse_foreign_child(monkeypatch):
    parent = StubParent()
    mine = StubChild(parent)
    foreign = StubChild(StubParent())
    register("control-steer-owned", mine)
    register("control-stop-owned", mine)
    register("control-steer-foreign", foreign)
    try:
        queued = json.loads(
            delegate._handle_control_action(
                "steer", "control-steer-owned", "focus on evidence", parent
            )
        )
        assert queued["status"] == "queued"
        assert mine.steered == ["focus on evidence"]

        interrupted = []
        monkeypatch.setattr(
            delegate,
            "request_hard_interrupt",
            lambda agent, reason: interrupted.append((agent, reason)) or True,
        )
        stopped = json.loads(
            delegate._handle_control_action("stop", "control-stop-owned", None, parent)
        )
        assert stopped["status"] == "interrupt_requested"
        assert interrupted[0][0] is mine

        refused = delegate._handle_control_action(
            "steer", "control-steer-foreign", "hijack", parent
        )
        assert "No live subagent" in refused
        assert foreign.steered == []
    finally:
        unregister("control-steer-owned", "control-stop-owned", "control-steer-foreign")


def test_control_actions_bypass_pause_and_spawn_cap(monkeypatch):
    parent = StubParent()
    delegate.set_spawn_paused(True)
    try:
        result = json.loads(delegate.delegate_task(action="list", parent_agent=parent))
        assert result["action"] == "list"
    finally:
        delegate.set_spawn_paused(False)

    config = ToolCallGuardrailConfig(loop_caps=LoopCapConfig(max_subagents=1))
    controller = ToolCallGuardrailController(config)
    assert controller.before_call("delegate_task", {"goal": "a"}).action == "allow"
    assert controller.before_call("delegate_task", {"goal": "b"}).action == "block"
    assert _subagent_spawn_count({"action": "list"}) == 0
    assert _subagent_spawn_count({"action": "steer", "subagent_id": "x"}) == 0
    assert _subagent_spawn_count({"action": "stop", "subagent_id": "x"}) == 0

    control_controller = ToolCallGuardrailController(config)
    assert control_controller.before_call(
        "delegate_task", {"goal": "a"}
    ).action == "allow"
    assert control_controller.before_call(
        "delegate_task", {"action": "stop", "subagent_id": "x"}
    ).action == "allow"


def test_control_entrypoint_validates_action_without_entering_spawn_path():
    assert "Unknown action" in delegate.delegate_task(
        action="pause", goal="g", parent_agent=StubParent()
    )
    assert "requires a parent agent" in delegate.delegate_task(action="list")
    assert "Provide either 'goal'" in delegate.delegate_task(
        tasks=[], goal="", parent_agent=StubParent()
    )
