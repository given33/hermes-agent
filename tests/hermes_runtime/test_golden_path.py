from __future__ import annotations

from pathlib import Path

import pytest

from hermes_runtime.composability import (
    DependencySpec,
    GoldenPathMetrics,
    GoldenPathQualityGateError,
    GoldenPathThresholds,
    ProviderCatalog,
)
from hermes_runtime.golden_path import (
    GoldenPathDisabled,
    GoldenPathPlan,
    GoldenPathRunner,
    GoldenPathSettings,
    GoldenPathTool,
    GoldenPathToolCall,
    JsonArtifactStore,
    StaticGoldenPathProvider,
    TransientGoldenPathError,
)
from hermes_services.hosted_event_protocol import append_hosted_event


def _settings(tmp_path: Path, *, turn_id: str = "turn-1", retries: int = 1) -> GoldenPathSettings:
    return GoldenPathSettings(
        enabled=True,
        runtime_layer_enabled=True,
        task_id="task-1",
        conversation_id="conversation-1",
        turn_id=turn_id,
        source_revision="git:test-revision",
        prompt_version="prompt:golden-v1",
        max_retries=retries,
    )


def _catalog_and_binding() -> tuple[ProviderCatalog, object]:
    catalog = ProviderCatalog()
    catalog.register(
        provider_id="model:golden-generation-1",
        interface_key="model:golden",
        version="1",
        health="healthy",
        metadata={"logical_provider_id": "model:golden"},
    )
    binding = catalog.resolve(DependencySpec(key="model:golden", version_range="^1.0"))
    assert binding is not None
    return catalog, binding


def _tools():
    return (
        GoldenPathTool(
            name="local.read_task",
            kind="local",
            handler=lambda arguments: {"task": arguments.get("task"), "source": "local"},
            effect_metadata={"external_boundary": "internal"},
        ),
        GoldenPathTool(
            name="mcp.visual.inspect_image",
            kind="mcp",
            handler=lambda arguments: {
                "ok": True,
                "provider_id": "hermes-visual-evidence",
                "provider_generation": 1,
                "artifact_ref": arguments.get("path", "visual://dashboard.png"),
            },
            registry_generation=1,
            effect_metadata={"external_boundary": "read_only_mcp"},
        ),
    )


def _provider(*, fail_first: bool = False, always_fail: bool = False):
    calls = {"count": 0}

    def planner(_task, _binding, _attempt):
        return GoldenPathPlan(
            calls=(
                GoldenPathToolCall("local.read_task", {"task": "inspect dashboard"}),
                GoldenPathToolCall("mcp.visual.inspect_image", {"path": "dashboard.png"}),
            )
        )

    provider = StaticGoldenPathProvider(planner=planner, model="test-pinned-model")
    return provider, calls


def test_disabled_golden_path_does_not_invoke_tools(tmp_path: Path) -> None:
    invoked = {"count": 0}

    def handler(_arguments):
        invoked["count"] += 1
        return {"unexpected": True}

    catalog, binding = _catalog_and_binding()
    settings = GoldenPathSettings()
    runner = GoldenPathRunner(
        catalog=catalog,
        binding=binding,
        provider=StaticGoldenPathProvider(
            planner=lambda *_: GoldenPathPlan((GoldenPathToolCall("local.read_task"),))
        ),
        tools=(GoldenPathTool("local.read_task", "local", handler), GoldenPathTool("mcp.visual.inspect_image", "mcp", lambda _: {})),
        artifact_store=JsonArtifactStore(tmp_path / "artifacts"),
        settings=settings,
    )
    with pytest.raises(GoldenPathDisabled):
        runner.run("native mode")
    assert invoked["count"] == 0


def test_environment_gates_are_off_by_default_and_require_both_flags(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_GOLDEN_PATH_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_RUNTIME_LAYER_ENABLED", raising=False)
    assert GoldenPathSettings.from_environment().enabled is False
    assert GoldenPathSettings.from_environment().runtime_layer_enabled is False
    monkeypatch.setenv("HERMES_GOLDEN_PATH_ENABLED", "true")
    monkeypatch.setenv("HERMES_RUNTIME_LAYER_ENABLED", "true")
    settings = GoldenPathSettings.from_environment(
        task_id="task",
        conversation_id="conversation",
        turn_id="turn-env",
        source_revision="git:test",
        prompt_version="prompt:test",
    )
    assert settings.enabled is True
    assert settings.runtime_layer_enabled is True


def test_golden_path_completes_with_pinned_provider_artifact_and_verdict(tmp_path: Path) -> None:
    catalog, binding = _catalog_and_binding()
    settings = _settings(tmp_path)
    runner = GoldenPathRunner(
        catalog=catalog,
        binding=binding,
        provider=StaticGoldenPathProvider(
            planner=lambda *_: GoldenPathPlan(
                (
                    GoldenPathToolCall("local.read_task", {"task": "inspect dashboard"}),
                    GoldenPathToolCall("mcp.visual.inspect_image", {"path": "dashboard.png"}),
                )
            ),
            model="test-pinned-model",
        ),
        tools=_tools(),
        artifact_store=JsonArtifactStore(tmp_path / "artifacts"),
        settings=settings,
    )

    result = runner.run("inspect dashboard")
    payload = result.as_dict()
    event_types = [event["event_type"] for event in result.events]

    assert result.status == "completed"
    assert result.provider_ref == "model:golden-generation-1@1"
    assert result.attempts == 1
    assert payload["schema_version"] == "hermes.golden-path.v1"
    assert result.artifact and len(result.artifact["digest"]) == 64
    assert result.supervisor_verdict and result.supervisor_verdict["verdict"] == "pass"
    assert result.supervisor_verdict["valid"] is True
    assert event_types[0] == "turn.plan_created"
    assert "tool.completed" in event_types
    assert "supervisor.verdict" in event_types
    assert event_types[-1] == "turn.completed"
    assert catalog.get(binding.provider_id).inflight == 0
    assert (tmp_path / "artifacts" / "golden-result.json").exists()


def test_transient_tool_failure_recovers_with_new_attempt_and_same_binding(tmp_path: Path) -> None:
    catalog, binding = _catalog_and_binding()
    calls = {"count": 0}

    def flaky(arguments):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TransientGoldenPathError("temporary provider-side timeout")
        return {"ok": True, "task": arguments.get("task")}

    tools = (
        GoldenPathTool("local.read_task", "local", flaky),
        GoldenPathTool("mcp.visual.inspect_image", "mcp", lambda _: {"ok": True}),
    )
    runner = GoldenPathRunner(
        catalog=catalog,
        binding=binding,
        provider=StaticGoldenPathProvider(
            planner=lambda *_: GoldenPathPlan((GoldenPathToolCall("local.read_task", {"task": "retry"}),))
        ),
        tools=tools,
        artifact_store=JsonArtifactStore(tmp_path / "artifacts"),
        settings=_settings(tmp_path, turn_id="turn-retry", retries=1),
    )

    result = runner.run("retry")
    event_types = [event["event_type"] for event in result.events]
    lifecycle_states = [event.get("runtime", {}).get("lifecycle_state") for event in result.events]

    assert result.status == "completed"
    assert result.attempts == 2
    assert calls["count"] == 2
    assert "component.recovering" in event_types
    assert "recovering" in lifecycle_states
    assert result.provider_ref == binding.witness_ref
    assert catalog.get(binding.provider_id).inflight == 0


def test_non_transient_failure_fails_closed_and_releases_provider(tmp_path: Path) -> None:
    catalog, binding = _catalog_and_binding()

    def broken(_arguments):
        raise RuntimeError("invalid local input")

    runner = GoldenPathRunner(
        catalog=catalog,
        binding=binding,
        provider=StaticGoldenPathProvider(
            planner=lambda *_: GoldenPathPlan((GoldenPathToolCall("local.read_task"),))
        ),
        tools=(GoldenPathTool("local.read_task", "local", broken), GoldenPathTool("mcp.visual.inspect_image", "mcp", lambda _: {})),
        artifact_store=JsonArtifactStore(tmp_path / "artifacts"),
        settings=_settings(tmp_path, turn_id="turn-fail", retries=1),
    )

    result = runner.run("fail")
    assert result.status == "failed"
    assert result.supervisor_verdict is None
    assert result.failure and result.failure["type"] == "RuntimeError"
    assert any(event["event_type"] == "turn.failed" for event in result.events)
    assert not any(event["event_type"] == "turn.completed" for event in result.events)
    assert catalog.get(binding.provider_id).inflight == 0


def test_golden_path_metrics_keep_safety_and_operational_metrics_separate(tmp_path: Path) -> None:
    catalog, binding = _catalog_and_binding()
    runner = GoldenPathRunner(
        catalog=catalog,
        binding=binding,
        provider=StaticGoldenPathProvider(
            planner=lambda *_: GoldenPathPlan((GoldenPathToolCall("local.read_task", {"task": "metrics"}),))
        ),
        tools=_tools(),
        artifact_store=JsonArtifactStore(tmp_path / "artifacts"),
        settings=_settings(tmp_path, turn_id="turn-metrics", retries=0),
    )
    result = runner.run("metrics").as_dict()
    metrics = GoldenPathMetrics()
    metrics.observe_run(
        result,
        token_cost=1,
        prompt_cache_hit=True,
        provider_drain_completed=True,
        stale_event_total=2,
        stale_event_rejected=2,
        process_killed=True,
        process_recovered=True,
        replay_consistent=True,
    )
    report = metrics.assert_quality_gate()
    assert report["pass"] is True
    assert report["metrics"]["task_success_rate"] == 1.0
    assert report["metrics"]["prompt_cache_hit_rate"] == 1.0
    assert report["metrics"]["token_cost"] == 1
    assert report["metrics"]["hard_safety"]["false_pass_zero"] is True

    unsafe = GoldenPathMetrics()
    unsafe.observe_run(result, false_pass=True)
    with pytest.raises(GoldenPathQualityGateError, match="false_pass"):
        unsafe.assert_quality_gate()


def test_event_replay_is_idempotent_and_does_not_block_turn_terminal(tmp_path: Path) -> None:
    catalog, binding = _catalog_and_binding()
    runner = GoldenPathRunner(
        catalog=catalog,
        binding=binding,
        provider=StaticGoldenPathProvider(
            planner=lambda *_: GoldenPathPlan((GoldenPathToolCall("local.read_task", {"task": "replay"}),))
        ),
        tools=_tools(),
        artifact_store=JsonArtifactStore(tmp_path / "artifacts"),
        settings=_settings(tmp_path, turn_id="turn-replay", retries=0),
    )
    result = runner.run("replay")
    replayed: dict[str, object] = {}
    for raw in result.events:
        runtime = raw.get("runtime") if isinstance(raw, dict) else {}
        runtime = runtime if isinstance(runtime, dict) else {}
        append_hosted_event(
            replayed,
            conversation_id=raw["conversation_id"],
            turn_id=raw["turn_id"],
            role_stage=raw["role_stage"],
            event_type=raw["event_type"],
            payload=raw.get("payload", {}),
            account_generation=raw.get("account_generation"),
            idempotency_key=raw["idempotency_key"],
            entity_id=raw.get("entity_id", ""),
            component_id=runtime.get("component_id", ""),
            parent_component_id=runtime.get("parent_component_id", ""),
            provider_refs=runtime.get("provider_refs", ()),
            dependency_state=runtime.get("dependency_state", {}),
            lifecycle_state=runtime.get("lifecycle_state", ""),
            effect_scope_id=runtime.get("effect_scope_id", ""),
            plan_node_id=runtime.get("plan_node_id", ""),
            artifact_refs=runtime.get("artifact_refs", ()),
            contract_revision=runtime.get("contract_revision", ""),
            policy_snapshot_hash=runtime.get("policy_snapshot_hash", ""),
        )
    before = replayed["hosted_event_cursor"]
    duplicate = append_hosted_event(
        replayed,
        conversation_id=result.conversation_id,
        turn_id=result.turn_id,
        role_stage="golden-path",
        event_type=result.events[0]["event_type"],
        payload=result.events[0]["payload"],
        account_generation=result.events[0]["account_generation"],
        idempotency_key=result.events[0]["idempotency_key"],
        component_id=result.events[0]["runtime"]["component_id"],
        lifecycle_state=result.events[0]["runtime"]["lifecycle_state"],
    )
    assert duplicate.appended is False
    assert duplicate.reason == "duplicate"
    assert replayed["hosted_event_cursor"] == before
    assert replayed["hosted_event_terminals"].get("turn:turn-replay") == "turn.completed"
