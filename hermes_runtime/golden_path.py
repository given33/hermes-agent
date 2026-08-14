"""Opt-in, bounded end-to-end path for product convergence.

The native agent loop remains the default.  This module is a deliberately
small runtime-layer adapter used to prove one complete workflow:

task -> pinned provider generation -> local/MCP tools -> JSON artifact ->
strict supervisor verdict -> canonical result.

It owns only the cross-boundary records and guards needed by that workflow.
Pure business functions and the native loop do not need to import it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Iterable, Mapping, Protocol

from hermes_runtime.composability.providers import (
    ProviderBinding,
    ProviderCatalog,
)
from hermes_runtime.composability.supervisor_verdict import (
    SupervisorVerdict,
    build_supervisor_verdict,
)
from hermes_runtime.evidence import EvidenceArtifact
from hermes_runtime.tool_execution import (
    ToolExecutionLedger,
    ToolPresentationMeta,
    build_envelope,
    stable_digest,
)
from hermes_services.hosted_event_protocol import append_hosted_event


SCHEMA_VERSION = "hermes.golden-path.v1"
ARTIFACT_SCHEMA_VERSION = "hermes.golden-artifact.v1"
CONTRACT_REVISION = "golden-path/v1"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class GoldenPathError(RuntimeError):
    """Base error for the opt-in golden path."""


class GoldenPathDisabled(GoldenPathError):
    """Raised when the optional runtime layer has not been explicitly enabled."""


class GoldenPathConfigurationError(GoldenPathError, ValueError):
    """Raised when a golden path contract is incomplete or ambiguous."""


class TransientGoldenPathError(GoldenPathError):
    """A tool/provider failure that is safe to retry within the turn contract."""


@dataclass(frozen=True)
class GoldenPathSettings:
    """Configuration for one bounded, inspectable turn."""

    enabled: bool = False
    runtime_layer_enabled: bool = False
    task_id: str = ""
    conversation_id: str = ""
    turn_id: str = ""
    account_generation: str = "golden-path"
    role_stage: str = "golden-path"
    local_tool_name: str = "local.read_task"
    mcp_tool_name: str = "mcp.visual.inspect_image"
    artifact_name: str = "golden-result.json"
    source_revision: str = ""
    prompt_version: str = ""
    provider_model: str = "unknown"
    max_retries: int = 1
    max_result_bytes: int = 8192

    @classmethod
    def from_environment(cls, **overrides: Any) -> "GoldenPathSettings":
        """Build settings with both runtime gates defaulting to false."""

        values = dict(overrides)
        values.setdefault("enabled", _env_truthy("HERMES_GOLDEN_PATH_ENABLED"))
        values.setdefault("runtime_layer_enabled", _env_truthy("HERMES_RUNTIME_LAYER_ENABLED"))
        return cls(**values)

    def __post_init__(self) -> None:
        if self.enabled and not self.runtime_layer_enabled:
            raise GoldenPathConfigurationError(
                "golden path requires runtime_layer_enabled=True"
            )
        if self.enabled:
            required = {
                "task_id": self.task_id,
                "conversation_id": self.conversation_id,
                "turn_id": self.turn_id,
                "source_revision": self.source_revision,
                "prompt_version": self.prompt_version,
            }
            missing = sorted(key for key, value in required.items() if not str(value).strip())
            if missing:
                raise GoldenPathConfigurationError(
                    "enabled golden path is missing: " + ", ".join(missing)
                )
        if int(self.max_retries) < 0 or int(self.max_retries) > 3:
            raise GoldenPathConfigurationError("max_retries must be between 0 and 3")
        if int(self.max_result_bytes) < 512:
            raise GoldenPathConfigurationError("max_result_bytes must be at least 512")
        if self.local_tool_name == self.mcp_tool_name:
            raise GoldenPathConfigurationError("local and MCP tools must be distinct")
        if Path(self.artifact_name).name != self.artifact_name:
            raise GoldenPathConfigurationError("artifact_name must be a file name")
        if not self.artifact_name.lower().endswith(".json"):
            raise GoldenPathConfigurationError("golden artifact must be JSON")


@dataclass(frozen=True)
class GoldenPathTool:
    """One explicitly admitted boundary tool for the golden path."""

    name: str
    kind: str
    handler: Callable[[Mapping[str, Any]], Any]
    registry_generation: int = 0
    effect_metadata: Mapping[str, Any] = field(default_factory=dict)
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise GoldenPathConfigurationError("tool name is required")
        if self.kind not in {"local", "mcp"}:
            raise GoldenPathConfigurationError("tool kind must be local or mcp")
        if not callable(self.handler):
            raise GoldenPathConfigurationError("tool handler must be callable")
        if int(self.registry_generation) < 0:
            raise GoldenPathConfigurationError("registry_generation must be non-negative")


@dataclass(frozen=True)
class GoldenPathToolCall:
    """Provider-produced tool intent.  The runner validates it before execution."""

    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.tool_name).strip():
            raise GoldenPathConfigurationError("tool call name is required")
        if not isinstance(self.arguments, Mapping):
            raise GoldenPathConfigurationError("tool call arguments must be an object")


@dataclass(frozen=True)
class GoldenPathPlan:
    calls: tuple[GoldenPathToolCall, ...]
    contract_revision: str = CONTRACT_REVISION

    def __post_init__(self) -> None:
        calls = tuple(self.calls)
        if not calls:
            raise GoldenPathConfigurationError("golden path plan must contain a tool call")
        if len(calls) > 2:
            raise GoldenPathConfigurationError("golden path admits at most two tool calls")
        if any(not isinstance(call, GoldenPathToolCall) for call in calls):
            raise GoldenPathConfigurationError("plan calls must be GoldenPathToolCall values")
        if not str(self.contract_revision).strip():
            raise GoldenPathConfigurationError("plan contract_revision is required")


class GoldenPathProvider(Protocol):
    """The pinned model/provider adapter used to turn a task into tool intent."""

    model: str

    def plan(
        self,
        task: str,
        binding: ProviderBinding,
        attempt: int,
    ) -> GoldenPathPlan:
        ...


@dataclass(frozen=True)
class StaticGoldenPathProvider:
    """Deterministic provider adapter for staging and contract tests."""

    planner: Callable[[str, ProviderBinding, int], GoldenPathPlan]
    model: str = "golden-static-provider"

    def plan(self, task: str, binding: ProviderBinding, attempt: int) -> GoldenPathPlan:
        plan = self.planner(task, binding, attempt)
        if not isinstance(plan, GoldenPathPlan):
            raise GoldenPathConfigurationError("provider must return GoldenPathPlan")
        return plan


class JsonArtifactStore:
    """Atomic, root-confined JSON artifact store with stable evidence digest."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        *,
        task_id: str,
        artifact_name: str,
        tool_results: Iterable[Mapping[str, Any]],
        refs: Iterable[str],
    ) -> dict[str, Any]:
        if Path(artifact_name).name != artifact_name or not artifact_name.lower().endswith(".json"):
            raise GoldenPathConfigurationError("artifact_name must be a root-confined JSON file name")
        safe_task = _safe_name(task_id, fallback="task")
        ref = f"artifact://golden/{safe_task}/{artifact_name}"
        payload = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "task_id": str(task_id),
            "tool_results": [dict(item) for item in tool_results],
        }
        artifact = EvidenceArtifact.from_payload(
            "json",
            ref,
            payload,
            refs=tuple(str(item) for item in refs if str(item).strip()),
            metadata={"format": "json", "task_id": str(task_id)[:256]},
        )
        target = (self.root / artifact_name).resolve()
        if target.parent != self.root:
            raise GoldenPathConfigurationError("artifact target escaped artifact root")
        rendered = json.dumps(
            {"artifact": artifact.as_dict(), "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        fd, temp_name = tempfile.mkstemp(prefix=".golden-", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(rendered)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return {"ref": ref, **artifact.as_dict()}


@dataclass(frozen=True)
class GoldenPathResult:
    status: str
    task_id: str
    conversation_id: str
    turn_id: str
    provider_ref: str
    provider_model: str
    attempts: int
    tool_calls: tuple[Mapping[str, Any], ...]
    artifact: Mapping[str, Any] | None
    supervisor_verdict: Mapping[str, Any] | None
    events: tuple[Mapping[str, Any], ...]
    failure: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "provider_ref": self.provider_ref,
            "provider_model": self.provider_model,
            "attempts": self.attempts,
            "tool_calls": [dict(item) for item in self.tool_calls],
            "artifact": dict(self.artifact) if self.artifact else None,
            "supervisor_verdict": dict(self.supervisor_verdict) if self.supervisor_verdict else None,
            "events": [dict(item) for item in self.events],
            "failure": dict(self.failure) if self.failure else None,
        }


class GoldenPathRunner:
    """Execute the one bounded path while preserving native-mode isolation."""

    def __init__(
        self,
        *,
        catalog: ProviderCatalog,
        binding: ProviderBinding,
        provider: GoldenPathProvider,
        tools: Iterable[GoldenPathTool],
        artifact_store: JsonArtifactStore,
        settings: GoldenPathSettings,
        event_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self.binding = binding
        self.provider = provider
        self.artifact_store = artifact_store
        self.settings = settings
        self.event_sink = event_sink
        tool_map: dict[str, GoldenPathTool] = {}
        for tool in tools:
            if tool.name in tool_map:
                raise GoldenPathConfigurationError(f"duplicate golden path tool: {tool.name}")
            tool_map[tool.name] = tool
        if settings.local_tool_name not in tool_map:
            raise GoldenPathConfigurationError("configured local tool is not registered")
        if settings.mcp_tool_name not in tool_map:
            raise GoldenPathConfigurationError("configured MCP tool is not registered")
        if tool_map[settings.local_tool_name].kind != "local":
            raise GoldenPathConfigurationError("configured local tool has the wrong kind")
        if tool_map[settings.mcp_tool_name].kind != "mcp":
            raise GoldenPathConfigurationError("configured MCP tool has the wrong kind")
        self._tools = tool_map
        self._conversation: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._ledger = ToolExecutionLedger(max_records=32)

    def run(self, task: str) -> GoldenPathResult:
        if not self.settings.enabled or not self.settings.runtime_layer_enabled:
            raise GoldenPathDisabled("golden path/runtime layer is disabled")
        normalized_task = str(task or "").strip()
        if not normalized_task:
            raise GoldenPathConfigurationError("task is required")
        provider_model = str(
            getattr(self.provider, "model", "") or self.settings.provider_model or "unknown"
        )[:256]
        provider_ref = self.binding.witness_ref
        self._append(
            "turn.plan_created",
            component_id=self._turn_component,
            lifecycle_state="declared",
            provider_refs=(provider_ref,),
            payload={
                "task_digest": stable_digest(normalized_task),
                "provider_ref": provider_ref,
                "contract_revision": CONTRACT_REVISION,
            },
            plan_node_id="golden:turn",
        )
        self._append(
            "component.activating",
            component_id=self._turn_component,
            lifecycle_state="activating",
            provider_refs=(provider_ref,),
            payload={"provider_ref": provider_ref},
            plan_node_id="golden:turn",
        )
        self._append(
            "turn.started",
            component_id=self._turn_component,
            lifecycle_state="active",
            provider_refs=(provider_ref,),
            payload={"task_digest": stable_digest(normalized_task)},
            plan_node_id="golden:turn",
        )

        attempts = 0
        tool_results: list[dict[str, Any]] = []
        last_failure: dict[str, Any] | None = None
        for attempts in range(1, int(self.settings.max_retries) + 2):
            if attempts > 1:
                self._append(
                    "component.recovering",
                    component_id=self._turn_component,
                    lifecycle_state="recovering",
                    provider_refs=(provider_ref,),
                    payload={"attempt": attempts, "previous_failure": last_failure or {}},
                    plan_node_id="golden:recovery",
                )
                self._append(
                    "component.active",
                    component_id=self._turn_component,
                    lifecycle_state="active",
                    provider_refs=(provider_ref,),
                    payload={"attempt": attempts},
                    plan_node_id="golden:recovery",
                )
            try:
                plan = self._make_plan(normalized_task, attempts)
                tool_results = self._execute_plan(plan, attempts)
                break
            except Exception as exc:
                last_failure = _failure(exc, recoverable=isinstance(exc, TransientGoldenPathError))
                if attempts > int(self.settings.max_retries) or not last_failure["recoverable"]:
                    return self._failed_result(attempts, provider_model, last_failure)
        else:
            return self._failed_result(attempts, provider_model, last_failure or _failure(RuntimeError("no attempt completed"), recoverable=False))

        tool_refs = tuple(str(item["evidence_ref"]) for item in tool_results)
        try:
            artifact = self.artifact_store.write(
                task_id=self.settings.task_id,
                artifact_name=self.settings.artifact_name,
                tool_results=tool_results,
                refs=tool_refs,
            )
            evidence = {
                "evidence_refs": (artifact["ref"], *tool_refs),
                "artifact_refs": (artifact["ref"],),
                "artifact_digest": artifact["digest"],
                "artifact_acceptance_required": True,
                "source_revision": self.settings.source_revision,
                "prompt_version": self.settings.prompt_version,
                "model": provider_model,
            }
            checks = {
                "provider_binding": bool(provider_ref),
                "tool_success": bool(tool_results) and all(item["status"] == "completed" for item in tool_results),
                "artifact_created": True,
                "artifact_digest": len(str(artifact.get("digest") or "")) == 64,
                "result_schema": all(item.get("schema_version") == "golden-tool-result/v1" for item in tool_results),
            }
            supervisor = build_supervisor_verdict(
                {
                    "verdict": "pass" if all(checks.values()) else "corrective_action",
                    "checks": checks,
                    "blockers": [] if all(checks.values()) else [key for key, value in checks.items() if not value],
                    "findings": [],
                    "required_actions": [] if all(checks.values()) else ["repair-golden-path-contract"],
                },
                evidence=evidence,
                artifact_digest=str(artifact["digest"]),
                source_revision=self.settings.source_revision,
                prompt_version=self.settings.prompt_version,
                model=provider_model,
            )
            self._append(
                "supervisor.verdict",
                component_id=self._supervisor_component,
                parent_component_id=self._turn_component,
                lifecycle_state="active",
                provider_refs=(provider_ref,),
                artifact_refs=(artifact["ref"],),
                payload=supervisor.public_dict(),
                plan_node_id="golden:supervisor",
            )
            self._append(
                "component.completed",
                component_id=self._supervisor_component,
                parent_component_id=self._turn_component,
                lifecycle_state="completed",
                provider_refs=(provider_ref,),
                artifact_refs=(artifact["ref"],),
                payload={"verdict": supervisor.verdict, "valid": supervisor.valid},
                plan_node_id="golden:supervisor",
                entity_id=self._supervisor_component,
            )
            if supervisor.valid and supervisor.verdict == "pass":
                self._append(
                    "turn.completed",
                    component_id=self._turn_component,
                    lifecycle_state="completed",
                    provider_refs=(provider_ref,),
                    artifact_refs=(artifact["ref"],),
                    payload={"artifact_digest": artifact["digest"], "verdict": supervisor.verdict},
                    plan_node_id="golden:turn",
                )
                return self._result(
                    "completed", attempts, provider_model, tool_results, artifact, supervisor, None
                )
            failure = {
                "type": "SupervisorRejected",
                "message": "strict supervisor verdict did not pass",
                "recoverable": True,
                "blockers": list(supervisor.blockers),
            }
            return self._failed_result(attempts, provider_model, failure, artifact, supervisor)
        except Exception as exc:
            return self._failed_result(
                attempts,
                provider_model,
                _failure(exc, recoverable=False),
            )

    @property
    def _turn_component(self) -> str:
        return f"golden-turn:{self.settings.turn_id}"

    @property
    def _supervisor_component(self) -> str:
        return f"golden-supervisor:{self.settings.turn_id}"

    def _make_plan(self, task: str, attempt: int) -> GoldenPathPlan:
        try:
            self.catalog.begin_bound_call(self.binding)
        except Exception:
            raise
        try:
            plan = self.provider.plan(task, self.binding, attempt)
        finally:
            self.catalog.end_call(self.binding.provider_id)
        if not isinstance(plan, GoldenPathPlan):
            raise GoldenPathConfigurationError("provider must return GoldenPathPlan")
        for call in plan.calls:
            if call.tool_name not in self._tools:
                raise GoldenPathConfigurationError(f"tool is outside golden path allowlist: {call.tool_name}")
            if call.tool_name not in {self.settings.local_tool_name, self.settings.mcp_tool_name}:
                raise GoldenPathConfigurationError(f"tool is not admitted by golden path: {call.tool_name}")
        return plan

    def _execute_plan(self, plan: GoldenPathPlan, attempt: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index, call in enumerate(plan.calls, start=1):
            tool = self._tools[call.tool_name]
            call_id = f"{self.settings.turn_id}:a{attempt}:tool{index}"
            envelope = build_envelope(
                tool_name=tool.name,
                args=call.arguments,
                call_id=call_id,
                turn_id=self.settings.turn_id,
                profile="golden-path",
                registry_generation=tool.registry_generation,
                effect_metadata={"kind": tool.kind, **dict(tool.effect_metadata)},
                presentation_meta=ToolPresentationMeta(
                    view="artifact" if tool.kind == "mcp" else "text",
                    title=tool.name,
                    summary=f"golden path {tool.kind} tool",
                    artifact_refs=(),
                ),
            )
            self._ledger.start(envelope)
            self._append(
                "tool.started",
                component_id=call_id,
                parent_component_id=self._turn_component,
                lifecycle_state="active",
                provider_refs=(self.binding.witness_ref,),
                payload={"tool_name": tool.name, "attempt": attempt, "args_digest": envelope.args_digest},
                plan_node_id=f"golden:tool:{index}",
                entity_id=call_id,
            )
            try:
                raw_result = tool.handler(dict(call.arguments))
                bounded_result = _bounded_json(raw_result, self.settings.max_result_bytes)
                self._ledger.finish(call_id, "completed", result=bounded_result)
                self._append(
                    "tool.completed",
                    component_id=call_id,
                    parent_component_id=self._turn_component,
                    lifecycle_state="completed",
                    provider_refs=(self.binding.witness_ref,),
                    payload={"tool_name": tool.name, "result_digest": envelope.result_digest},
                    plan_node_id=f"golden:tool:{index}",
                    entity_id=call_id,
                )
                results.append(
                    {
                        "schema_version": "golden-tool-result/v1",
                        "call_id": call_id,
                        "tool_name": tool.name,
                        "kind": tool.kind,
                        "status": "completed",
                        "result": {"sensitive": True, "digest": stable_digest(raw_result)}
                        if tool.sensitive
                        else bounded_result,
                        "result_digest": stable_digest(raw_result),
                        "evidence_ref": f"tool:{call_id}",
                    }
                )
            except Exception as exc:
                error_type = type(exc).__name__
                self._ledger.finish(call_id, "failed", error_type=error_type)
                self._append(
                    "tool.failed",
                    component_id=call_id,
                    parent_component_id=self._turn_component,
                    lifecycle_state="failed",
                    provider_refs=(self.binding.witness_ref,),
                    payload={"tool_name": tool.name, "error_type": error_type, "attempt": attempt},
                    plan_node_id=f"golden:tool:{index}",
                    entity_id=call_id,
                )
                raise
        return results

    def _append(
        self,
        event_type: str,
        *,
        component_id: str,
        lifecycle_state: str,
        payload: Mapping[str, Any],
        provider_refs: Iterable[str] = (),
        parent_component_id: str = "",
        artifact_refs: Iterable[str] = (),
        plan_node_id: str = "",
        entity_id: str = "",
    ) -> dict[str, Any]:
        result = append_hosted_event(
            self._conversation,
            conversation_id=self.settings.conversation_id,
            turn_id=self.settings.turn_id,
            role_stage=self.settings.role_stage,
            event_type=event_type,
            payload=dict(payload),
            account_generation=self.settings.account_generation,
            idempotency_key=f"golden:{self.settings.turn_id}:{event_type}:{component_id}:{entity_id}:{len(self._events)}",
            entity_id=entity_id,
            component_id=component_id,
            parent_component_id=parent_component_id,
            provider_refs=provider_refs,
            lifecycle_state=lifecycle_state,
            effect_scope_id=f"golden-scope:{self.settings.turn_id}",
            plan_node_id=plan_node_id,
            artifact_refs=artifact_refs,
            contract_revision=CONTRACT_REVISION,
            policy_snapshot_hash=stable_digest({"contract": CONTRACT_REVISION, "role": self.settings.role_stage}),
        )
        event = dict(result.event)
        if result.appended:
            self._events.append(event)
            if self.event_sink is not None:
                self.event_sink(event)
        return event

    def _result(
        self,
        status: str,
        attempts: int,
        provider_model: str,
        tool_results: list[dict[str, Any]],
        artifact: Mapping[str, Any] | None,
        supervisor: SupervisorVerdict | None,
        failure: Mapping[str, Any] | None,
    ) -> GoldenPathResult:
        return GoldenPathResult(
            status=status,
            task_id=self.settings.task_id,
            conversation_id=self.settings.conversation_id,
            turn_id=self.settings.turn_id,
            provider_ref=self.binding.witness_ref,
            provider_model=provider_model,
            attempts=attempts,
            tool_calls=tuple(self._ledger.snapshot()),
            artifact=artifact,
            supervisor_verdict=supervisor.public_dict() if supervisor else None,
            events=tuple(self._events),
            failure=failure,
        )

    def _failed_result(
        self,
        attempts: int,
        provider_model: str,
        failure: Mapping[str, Any],
        artifact: Mapping[str, Any] | None = None,
        supervisor: SupervisorVerdict | None = None,
    ) -> GoldenPathResult:
        current = self._conversation.get("hosted_events")
        terminal = any(
            isinstance(item, Mapping)
            and item.get("event_type") in {"turn.completed", "turn.failed", "turn.cancelled"}
            for item in (current if isinstance(current, list) else [])
        )
        if not terminal:
            self._append(
                "turn.failed",
                component_id=self._turn_component,
                lifecycle_state="failed",
                provider_refs=(self.binding.witness_ref,),
                artifact_refs=(artifact["ref"],) if artifact and artifact.get("ref") else (),
                payload={"failure": dict(failure), "artifact_digest": artifact.get("digest") if artifact else ""},
                plan_node_id="golden:turn",
            )
        return self._result("failed", attempts, provider_model, [], artifact, supervisor, failure)


def _safe_name(value: str, *, fallback: str) -> str:
    normalized = _SAFE_NAME.sub("-", str(value or "").strip()).strip(".-")
    return (normalized[:96] or fallback)


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _bounded_json(value: Any, limit: int) -> Any:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(rendered.encode("utf-8")) <= int(limit):
        try:
            return json.loads(rendered)
        except json.JSONDecodeError:
            return rendered[: int(limit)]
    return {
        "truncated": True,
        "digest": stable_digest(value),
        "preview": rendered[: max(128, int(limit) // 2)],
    }


def _failure(exc: Exception, *, recoverable: bool) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "message": str(exc).replace("\r", " ").replace("\n", " ")[:240],
        "recoverable": bool(recoverable),
    }


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "CONTRACT_REVISION",
    "GoldenPathDisabled",
    "GoldenPathError",
    "GoldenPathConfigurationError",
    "GoldenPathPlan",
    "GoldenPathProvider",
    "GoldenPathResult",
    "GoldenPathRunner",
    "GoldenPathSettings",
    "GoldenPathTool",
    "GoldenPathToolCall",
    "JsonArtifactStore",
    "SCHEMA_VERSION",
    "StaticGoldenPathProvider",
    "TransientGoldenPathError",
]
