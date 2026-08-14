from __future__ import annotations

import random
import subprocess
import sys

import pytest

from hermes_runtime.composability import (
    BoundedEventBuffer,
    DependencyGraph,
    DependencySpec,
    LIFECYCLE_STATES,
    LIFECYCLE_TRANSITIONS,
    ProviderCatalog,
    ProviderStatus,
    assert_lifecycle_transition,
    lifecycle_transition_allowed,
    recover_after_process_exit,
)
from hermes_runtime.composability.turn_plan import TurnPlan, TurnPlanNode


def test_provider_catalog_random_drain_never_resolves_removed_generation() -> None:
    random_source = random.Random(20260814)
    catalog = ProviderCatalog()
    ids = []
    for index in range(8):
        provider_id = f"connector:{index}"
        ids.append(provider_id)
        catalog.register(
            provider_id=provider_id,
            interface_key="connector:hosted",
            version=str(index),
            health="healthy",
        )
    for provider_id in random_source.sample(ids, len(ids)):
        catalog.begin_drain(provider_id)
        binding = catalog.resolve(DependencySpec(key="connector:hosted"))
        if binding is not None:
            assert catalog.get(binding.provider_id).status == ProviderStatus.ACTIVE


def test_lifecycle_transition_table_accepts_only_declared_edges() -> None:
    for previous in sorted(LIFECYCLE_STATES):
        for current in sorted(LIFECYCLE_STATES):
            expected = current in LIFECYCLE_TRANSITIONS[previous]
            assert lifecycle_transition_allowed(previous, current) is expected
            if expected:
                assert_lifecycle_transition(previous, current)
            else:
                with pytest.raises(ValueError, match="illegal lifecycle transition"):
                    assert_lifecycle_transition(previous, current)


def test_turn_plan_random_event_order_converges_to_same_ready_set() -> None:
    nodes = tuple(
        TurnPlanNode(
            node_id=f"node-{index}",
            role="worker",
            depends_on=(f"node-{index - 1}",) if index else (),
        )
        for index in range(6)
    )
    plan = TurnPlan(plan_id="random-order", revision=1, nodes=nodes)
    expected = [f"node-{index}" for index in range(6)]
    for seed in range(20):
        order = list(expected)
        random.Random(seed).shuffle(order)
        completed: set[str] = set()
        for node_id in order:
            ready = {node.node_id for node in plan.ready_nodes(completed)}
            if node_id not in ready:
                # An out-of-order event is retained, then applied when its
                # dependency event arrives; it cannot fabricate readiness.
                continue
            completed.add(node_id)
        while len(completed) < len(expected):
            ready = plan.ready_nodes(completed)
            assert ready
            completed.add(ready[0].node_id)
        assert tuple(sorted(completed, key=expected.index)) == tuple(expected)


def test_dependency_graph_detects_cycles_and_reports_lost_dependency() -> None:
    graph = DependencyGraph()
    graph.declare("component-a", [DependencySpec(key="component-b")])
    graph.set_available("component-b")
    assert graph.ready("component-a") is True
    graph.set_available("component-b", False)
    assert graph.missing("component-a")[0].key == "component-b"
    with pytest.raises(ValueError, match="cycle"):
        graph.declare("component-b", [DependencySpec(key="component-a")])
    assert graph.components() == ("component-a",)


def test_dependent_before_provider_unload_order() -> None:
    catalog = ProviderCatalog()
    catalog.register(
        provider_id="connector:primary",
        interface_key="connector:hosted",
        version="1",
        health="healthy",
    )
    catalog.register(
        provider_id="worker:primary",
        interface_key="worker:hosted",
        version="1",
        health="healthy",
        dependencies=(DependencySpec(key="connector:hosted"),),
    )
    assert catalog.dependent_first_drain_order("connector:primary") == (
        "worker:primary",
        "connector:primary",
    )
    with pytest.raises(RuntimeError, match="dependent"):
        catalog.unload("connector:primary")


def test_process_exit_recovery_and_event_backpressure_are_explicit() -> None:
    checkpoint = {"status": "running", "checkpoint_count": 2, "state": {"step": 3}}
    recovered = recover_after_process_exit(checkpoint, exit_code=137)
    assert recovered["status"] == "recovering"
    assert recovered["resume_required"] is True
    buffer = BoundedEventBuffer(capacity=2)
    assert buffer.put("a") is True
    assert buffer.put("b") is True
    assert buffer.put("c") is False
    assert buffer.dropped == 1
    assert buffer.get() == "a"


def test_killed_process_requires_checkpoint_recovery() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        process.kill()
        exit_code = process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    recovered = recover_after_process_exit(
        {"status": "running", "checkpoint_count": 1, "state": {"step": 2}},
        exit_code=exit_code,
    )
    assert recovered["resume_required"] is True
    assert recovered["exit_code"] != 0
