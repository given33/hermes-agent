from __future__ import annotations

import asyncio

import pytest

from hermes_runtime.composability.effects import (
    EffectScope,
    EffectScopeClosedError,
)
from hermes_runtime.composability.providers import (
    DependencySpec,
    ProviderCatalog,
    ProviderStatus,
)
from hermes_runtime.composability.turn_plan import (
    TurnPlan,
    TurnPlanError,
    TurnPlanNode,
)
from hermes_runtime.composability.provider_update import ProviderUpdateTransaction
from hermes_runtime.composability.prompt_metrics import PromptMetrics
from hermes_runtime.composability.resources import (
    create_temporary_workspace,
)
from hermes_runtime.composability.hosted_plan import next_ready_plan_nodes
from hermes_runtime.composability.long_task import LongTaskBudget, LongTaskController
from hermes_runtime.composability.update_strategy import (
    TransactionalUpdate,
    UpdateMode,
    classify_update,
)


def test_effect_scope_disposes_lifo_and_is_idempotent() -> None:
    calls: list[str] = []

    async def scenario() -> None:
        scope = EffectScope(owner_id="turn-1")
        first = scope.add(lambda: calls.append("first"), description="first")
        second = scope.add(lambda: calls.append("second"), description="second")
        await scope.close()
        await scope.close()
        await second.dispose()
        assert first.disposed is True

    asyncio.run(scenario())
    assert calls == ["second", "first"]


def test_sync_close_does_not_race_an_async_close() -> None:
    async def scenario() -> None:
        scope = EffectScope(owner_id="turn-1")
        started = asyncio.Event()
        release = asyncio.Event()

        async def disposer() -> None:
            started.set()
            await release.wait()

        scope.add(disposer, description="async-resource")
        closing = asyncio.create_task(scope.close())
        await started.wait()
        with pytest.raises(RuntimeError, match="already closing"):
            scope.close_sync()
        release.set()
        await closing

    asyncio.run(scenario())


def test_child_scope_closes_before_parent_resource() -> None:
    calls: list[str] = []

    async def scenario() -> None:
        parent = EffectScope(owner_id="turn-1")
        parent.add(lambda: calls.append("parent"), description="parent-resource")
        child = parent.child(owner_id="node-1")
        child.add(lambda: calls.append("child"), description="child-resource")
        await parent.close()
        assert child.closed is True

    asyncio.run(scenario())
    assert calls == ["child", "parent"]


def test_scope_rejects_registration_while_closing() -> None:
    async def scenario() -> None:
        scope = EffectScope(owner_id="turn-1")
        scope.add(lambda: None, description="resource")
        await scope.close()
        with pytest.raises(EffectScopeClosedError):
            scope.add(lambda: None, description="late")

    asyncio.run(scenario())


def test_concurrent_scope_close_waits_for_the_same_cleanup() -> None:
    calls: list[str] = []

    async def scenario() -> None:
        scope = EffectScope(owner_id="turn-1")
        started = asyncio.Event()
        release = asyncio.Event()

        async def dispose() -> None:
            started.set()
            await release.wait()
            calls.append("closed")

        scope.add(dispose, description="slow-resource")
        first = asyncio.create_task(scope.close())
        await started.wait()
        second = asyncio.create_task(scope.close())
        await asyncio.sleep(0)
        assert second.done() is False
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert calls == ["closed"]


def test_provider_catalog_binds_generation_and_drains_inflight_calls() -> None:
    catalog = ProviderCatalog()
    first = catalog.register(
        provider_id="mcp-a-v1",
        interface_key="mcp:search",
        version="1.0.0",
        health="healthy",
        capacity=2,
    )
    binding = catalog.resolve(DependencySpec(key="mcp:search"))
    assert binding is not None
    assert binding.provider_id == first.provider_id
    assert binding.generation == first.generation

    catalog.begin_call(first.provider_id)
    draining = catalog.begin_drain(first.provider_id)
    assert draining.status == ProviderStatus.DRAINING
    with pytest.raises(RuntimeError, match="not accepting"):
        catalog.begin_call(first.provider_id)
    with pytest.raises(RuntimeError, match="in-flight"):
        catalog.unload(first.provider_id)
    catalog.end_call(first.provider_id)
    removed = catalog.unload(first.provider_id)
    assert removed.status == ProviderStatus.REMOVED


def test_provider_catalog_rejects_stale_bound_generation_and_expires_drain() -> None:
    catalog = ProviderCatalog()
    first = catalog.register(
        provider_id="provider-v1",
        interface_key="model:hosted",
        version="1.0.0",
        health="healthy",
    )
    binding = catalog.resolve(DependencySpec(key="model:hosted"))
    assert binding is not None
    catalog.begin_drain(first.provider_id, deadline_at=10.0)
    with pytest.raises(RuntimeError, match="not accepting"):
        catalog.begin_bound_call(binding)
    expired = catalog.enforce_drain_deadline(first.provider_id, now=11.0)
    assert expired.isolated is True
    assert expired.metadata["drain_action"] == "isolated_manual_review"
    assert catalog.expired_drains(now=11.0)[0].provider_id == first.provider_id
    removed = catalog.enforce_drain_deadline(
        first.provider_id,
        now=11.0,
        force_unload=True,
    )
    assert removed.status == ProviderStatus.REMOVED


def test_provider_catalog_does_not_replace_a_generation_while_draining() -> None:
    catalog = ProviderCatalog()
    provider = catalog.register(
        provider_id="connector:draining",
        interface_key="connector:hosted",
        version="1",
        health="healthy",
    )
    catalog.begin_drain(provider.provider_id)
    with pytest.raises(ValueError, match="already registered"):
        catalog.register(
            provider_id=provider.provider_id,
            interface_key="connector:hosted",
            version="2",
        )


def test_provider_catalog_prefers_healthy_and_newer_generation() -> None:
    catalog = ProviderCatalog()
    catalog.register(
        provider_id="provider-old",
        interface_key="model:default",
        version="1",
        health="healthy",
    )
    catalog.register(
        provider_id="provider-new",
        interface_key="model:default",
        version="2",
        health="healthy",
    )
    binding = catalog.resolve(DependencySpec(key="model:default"))
    assert binding is not None
    assert binding.provider_id == "provider-new"


def test_provider_catalog_enforces_declared_version_range() -> None:
    catalog = ProviderCatalog()
    catalog.register(
        provider_id="provider-v1",
        interface_key="model:default",
        version="1.4.0",
        health="healthy",
    )
    catalog.register(
        provider_id="provider-v2",
        interface_key="model:default",
        version="2.1.0",
        health="healthy",
    )
    binding = catalog.resolve(DependencySpec(key="model:default", version_range="^2.0.0"))
    assert binding is not None
    assert binding.provider_id == "provider-v2"
    assert catalog.resolve(DependencySpec(key="model:default", version_range="^3.0.0")) is None


def test_turn_plan_validates_ready_nodes_conflicts_and_critical_path() -> None:
    plan = TurnPlan(
        plan_id="plan-1",
        revision=1,
        nodes=(
            TurnPlanNode(node_id="inspect", role="worker", conflict_keys=("repo:read",)),
            TurnPlanNode(node_id="docs", role="worker", conflict_keys=("docs:read",)),
            TurnPlanNode(
                node_id="implement",
                role="worker",
                depends_on=("inspect",),
                conflict_keys=("repo:write",),
            ),
            TurnPlanNode(
                node_id="review",
                role="reviewer",
                depends_on=("implement",),
                conflict_keys=("artifact:patch",),
            ),
        ),
    )
    assert {node.node_id for node in plan.ready_nodes([])} == {"docs", "inspect"}
    assert plan.can_run_together(plan.ready_nodes([])) is True
    assert plan.can_run_together(
        (
            TurnPlanNode(node_id="a", role="worker", conflict_keys=("repo",)),
            TurnPlanNode(node_id="b", role="worker", conflict_keys=("repo",)),
        )
    ) is False
    assert plan.critical_path() == ("inspect", "implement", "review")
    assert {node.node_id for node in next_ready_plan_nodes(plan)} == {"docs", "inspect"}


def test_provider_policy_interceptor_can_deny_resolution() -> None:
    catalog = ProviderCatalog()
    catalog.register(
        provider_id="connector:primary",
        interface_key="connector:hosted",
        version="1",
        health="healthy",
    )
    from hermes_runtime.composability.providers import PolicyInterceptor

    policy = PolicyInterceptor()
    policy.add(lambda dependency, operation: operation == "read")
    assert catalog.resolve_with_policy(
        DependencySpec(key="connector:hosted", policy="read_only"),
        policy,
        operation="write",
    ) is None


def test_turn_plan_rejects_cycles() -> None:
    with pytest.raises(TurnPlanError, match="cycle"):
        TurnPlan(
            plan_id="cycle",
            revision=1,
            nodes=(
                TurnPlanNode(node_id="a", role="worker", depends_on=("b",)),
                TurnPlanNode(node_id="b", role="worker", depends_on=("a",)),
            ),
        )


def test_scope_tracks_temporary_workspace_and_removes_only_marked_path(tmp_path) -> None:
    async def scenario() -> None:
        scope = EffectScope(owner_id="test")
        workspace = create_temporary_workspace(scope, workspace_id="test", parent=tmp_path)
        assert workspace.exists()
        await scope.close()
        assert not workspace.exists()

    asyncio.run(scenario())


def test_temporary_workspace_rejects_path_like_ids(tmp_path) -> None:
    async def scenario() -> None:
        scope = EffectScope(owner_id="test")
        with pytest.raises(ValueError, match="path-free"):
            create_temporary_workspace(scope, workspace_id="..\\outside", parent=tmp_path)
        await scope.close()

    asyncio.run(scenario())


def test_provider_update_uses_candidate_generation_and_rolls_back() -> None:
    catalog = ProviderCatalog()
    previous = catalog.register(
        provider_id="connector:primary",
        interface_key="connector:hosted",
        version="1",
        health="healthy",
    )
    drain_observations = []
    result = ProviderUpdateTransaction(
        catalog,
        interface_key="connector:hosted",
        provider_id="connector:primary",
        version="2",
    ).execute(load=lambda: object(), health_check=lambda _: False)
    assert result.committed is False
    assert result.rolled_back is True
    assert catalog.get(previous.provider_id).status == ProviderStatus.ACTIVE


def test_provider_update_drains_old_generation_before_commit() -> None:
    catalog = ProviderCatalog()
    previous = catalog.register(
        provider_id="model:primary",
        interface_key="model:hosted",
        version="1",
        health="healthy",
    )
    result = ProviderUpdateTransaction(
        catalog,
        interface_key="model:hosted",
        provider_id="model:primary",
        version="2",
    ).execute(load=lambda: object(), health_check=lambda _: True)
    assert result.committed is True
    assert result.candidate_provider_id != previous.provider_id
    assert catalog.get(previous.provider_id).status == ProviderStatus.REMOVED


def test_provider_update_records_and_enforces_a_drain_deadline() -> None:
    catalog = ProviderCatalog()
    previous = catalog.register(
        provider_id="connector:deadline",
        interface_key="connector:deadline",
        version="1",
        health="healthy",
    )
    catalog.begin_call(previous.provider_id)
    drain_observations = []
    result = ProviderUpdateTransaction(
        catalog,
        interface_key="connector:deadline",
        provider_id="connector:deadline",
        version="2",
        drain_deadline_seconds=0.01,
    ).execute(
        load=lambda: object(),
        health_check=lambda _: True,
        drain_timeout=lambda record: drain_observations.append(record.drain_deadline) or False,
    )
    assert result.committed is False
    drained = catalog.get(previous.provider_id)
    assert drained is not None
    assert drain_observations and drain_observations[0] is not None


def test_provider_update_can_replace_a_previously_committed_candidate() -> None:
    catalog = ProviderCatalog()
    catalog.register(
        provider_id="model:primary",
        interface_key="model:hosted",
        version="1",
        health="healthy",
    )
    first = ProviderUpdateTransaction(
        catalog,
        interface_key="model:hosted",
        provider_id="model:primary",
        version="2",
    ).execute(load=lambda: object(), health_check=lambda _: True)
    second = ProviderUpdateTransaction(
        catalog,
        interface_key="model:hosted",
        provider_id="model:primary",
        version="3",
    ).execute(load=lambda: object(), health_check=lambda _: True)
    assert first.committed is True
    assert second.committed is True
    assert catalog.get(first.candidate_provider_id).status == ProviderStatus.REMOVED


def test_prompt_metrics_exposes_quality_rates() -> None:
    metrics = PromptMetrics()
    metrics.observe(
        schema_valid=True,
        verdict="pass",
        prompt_cache_hit=True,
        token_cost=123,
        artifact_checked=True,
        artifact_accepted=True,
    )
    metrics.observe(
        schema_valid=False,
        verdict="unknown",
        false_pass=False,
        rework_requested=True,
        rework_accepted=True,
        prompt_cache_hit=False,
        token_cost=7,
        artifact_checked=True,
    )
    snapshot = metrics.snapshot()
    assert snapshot["schema_valid_rate"] == 0.5
    assert snapshot["prompt_cache_hit_rate"] == 0.5
    assert snapshot["artifact_acceptance_rate"] == 0.5
    assert snapshot["token_cost"] == 130
    assert snapshot["false_pass_rate"] == 0.0
    metrics.observe(
        schema_valid=False,
        verdict="unknown",
        false_reject=True,
        strict_reject=True,
    )
    assert metrics.snapshot()["false_reject"] == 1
    assert metrics.snapshot()["strict_reject"] == 1


def test_update_strategy_rejects_core_in_process_reload() -> None:
    classification = classify_update("run_agent.py")
    assert classification.mode == UpdateMode.DRAIN_RESTART
    result = TransactionalUpdate(classification).apply(
        snapshot=lambda: "old",
        isolated_load=lambda: "new",
        health_check=lambda _: True,
        traffic_shift=lambda _: None,
        drain=lambda _: True,
        commit=lambda _: None,
        rollback=lambda _: None,
    )
    assert result.committed is False
    assert result.rolled_back is False
    assert "rejected" in result.phases


def test_edge_update_rolls_back_after_failed_drain() -> None:
    classification = classify_update("plugins/connector_adapter.py")
    calls: list[str] = []
    result = TransactionalUpdate(classification).apply(
        snapshot=lambda: "old",
        isolated_load=lambda: "new",
        health_check=lambda _: True,
        traffic_shift=lambda _: calls.append("shift"),
        drain=lambda _: False,
        commit=lambda _: calls.append("commit"),
        rollback=lambda _: calls.append("rollback"),
    )
    assert result.rolled_back is True
    assert calls == ["shift", "rollback"]


def test_long_task_cancel_to_terminal_and_checkpoint_backpressure() -> None:
    controller = LongTaskController(
        LongTaskBudget(deadline_at=100.0, token_budget=10, checkpoint_interval_seconds=5.0)
    )
    assert controller.checkpoint({"step": 1}, now=0.0)["accepted"] is True
    assert controller.checkpoint({"step": 2}, now=1.0)["accepted"] is False
    controller.request_cancel("user")
    assert controller.should_stop(now=2.0) is True
    assert controller.settle("cancelled") == "cancelled"
    assert controller.checkpoint({}, now=10.0)["accepted"] is False
