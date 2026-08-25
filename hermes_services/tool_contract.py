"""Canonical execution metadata shared by tool planners and hosted clients."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from threading import RLock
from typing import Any, Callable

from hermes_services.bounded_dict import BoundedDict

EXECUTION_MODES = frozenset({"parallel", "sequential"})
SIDE_EFFECT_CLASSES = frozenset({"none", "read", "write", "destructive", "external"})
OUTPUT_POLICIES = frozenset({"inline", "bounded_tail", "artifact"})
TIMEOUT_POLICIES = frozenset({"hard"})


@dataclass(frozen=True)
class ToolExecutionContract:
    execution_mode: str = "sequential"
    side_effect_class: str = "external"
    requires_approval: bool = False
    timeout_seconds: float | None = None
    timeout_policy: str = "hard"
    supports_progress: bool = False
    output_policy: str = "bounded_tail"

    def __post_init__(self) -> None:
        if self.execution_mode not in EXECUTION_MODES:
            raise ValueError("execution_mode must be parallel or sequential")
        if self.side_effect_class not in SIDE_EFFECT_CLASSES:
            raise ValueError("invalid side_effect_class")
        if self.output_policy not in OUTPUT_POLICIES:
            raise ValueError("invalid output_policy")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.timeout_policy not in TIMEOUT_POLICIES:
            raise ValueError("timeout_policy must be hard")
        if (
            self.timeout_seconds is not None
            and self.side_effect_class not in {"none", "read"}
        ):
            raise ValueError(
                "hard tool deadlines are limited to none/read side-effect classes"
            )

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


_LOCK = RLock()
_CONTRACTS: dict[str, ToolExecutionContract] = {}
_PUBLISHED_BINDINGS: BoundedDict[tuple[str, int], dict[str, Any]] = BoundedDict(1024)
_REGISTRY_GENERATION: Callable[[], int] | None = None
_BUMP_REGISTRY_GENERATION: Callable[[], None] | None = None
_REGISTRATION_FINGERPRINT: Callable[[str], str | None] | None = None
_INVALIDATE_DEFINITION_CACHE: Callable[[], None] | None = None


def configure_tool_contract_runtime(
    *,
    registry_generation: Callable[[], int],
    bump_registry_generation: Callable[[], None],
    registration_fingerprint: Callable[[str], str | None],
    invalidate_definition_cache: Callable[[], None],
) -> None:
    """Install upper-layer registry callbacks at the application boundary."""

    global _REGISTRY_GENERATION
    global _BUMP_REGISTRY_GENERATION
    global _REGISTRATION_FINGERPRINT
    global _INVALIDATE_DEFINITION_CACHE
    with _LOCK:
        _REGISTRY_GENERATION = registry_generation
        _BUMP_REGISTRY_GENERATION = bump_registry_generation
        _REGISTRATION_FINGERPRINT = registration_fingerprint
        _INVALIDATE_DEFINITION_CACHE = invalidate_definition_cache


def register_tool_contract(
    tool_name: str,
    contract: ToolExecutionContract,
    *,
    replace_existing: bool = False,
) -> None:
    name = str(tool_name or "").strip()
    if not name:
        raise ValueError("tool_name is required")
    with _LOCK:
        if name in _CONTRACTS and not replace_existing:
            raise ValueError(f"tool contract already registered: {name}")
        _CONTRACTS[name] = contract
        _PUBLISHED_BINDINGS.clear()
    _bump_runtime_registry_generation()
    _invalidate_tool_definition_cache()


def resolve_tool_contract(tool_name: str) -> ToolExecutionContract:
    name = str(tool_name or "").strip()
    with _LOCK:
        explicit = _CONTRACTS.get(name)
    if explicit is not None:
        return explicit
    if name in {"read_file", "search_files", "web_search", "tool_search"}:
        return ToolExecutionContract(
            execution_mode="parallel",
            side_effect_class="read",
            output_policy="bounded_tail",
        )
    if name in {"terminal", "write_file", "patch", "browser_navigate"}:
        return ToolExecutionContract(
            execution_mode="sequential",
            side_effect_class="write" if name != "browser_navigate" else "external",
            # Terminal approval is command-sensitive and remains enforced by
            # tools.approval. Marking every terminal call here would prompt for
            # harmless reads twice. Explicit custom contracts use the uniform
            # contract approval gate in agent.tool_executor.
            requires_approval=False,
            supports_progress=name == "terminal",
            output_policy="artifact" if name == "terminal" else "bounded_tail",
        )
    return ToolExecutionContract()


def has_registered_tool_contract(tool_name: str) -> bool:
    name = str(tool_name or "").strip()
    with _LOCK:
        return name in _CONTRACTS


def batch_execution_mode(tool_names: list[str]) -> str:
    """Conservative batch policy: one barrier serializes the entire batch."""

    return (
        "sequential"
        if any(
            (contract := resolve_tool_contract(name)).execution_mode == "sequential"
            or contract.side_effect_class in {"write", "destructive", "external"}
            for name in tool_names
        )
        else "parallel"
    )


def contract_timeout_seconds(tool_names: list[str]) -> float | None:
    """Return the strictest hard execution deadline in a batch."""

    values: list[float] = []
    for name in tool_names:
        timeout = resolve_tool_contract(name).timeout_seconds
        if timeout is not None:
            values.append(float(timeout))
    return min(values) if values else None


def tool_contract_event_metadata(tool_name: str) -> dict[str, Any]:
    name = str(tool_name or "").strip()
    metadata: dict[str, Any] = {
        "execution_contract": resolve_tool_contract(name).public_dict(),
    }
    generation = _runtime_registry_generation()
    with _LOCK:
        binding = _PUBLISHED_BINDINGS.get((name, generation))
    if binding is not None:
        metadata["execution_contract_binding"] = dict(binding)
    return metadata


def validate_tool_contract_binding(
    tool_name: str,
    *,
    advertised_registry_generation: int,
) -> str | None:
    """Reject execution when advertised and live registration metadata drift.

    Model tool definitions are a capability snapshot. A plugin/MCP refresh or
    a contract replacement after that snapshot must not silently execute a
    different handler or policy under the old description.
    """

    name = str(tool_name or "").strip()
    expected_generation = int(advertised_registry_generation)
    current_generation = _runtime_registry_generation()
    if current_generation != expected_generation:
        return (
            f"Tool '{name}' registration changed after this session advertised it "
            f"(snapshot={expected_generation}, current={current_generation}); "
            "refresh tools before retrying."
        )

    entry_fingerprint = _runtime_registration_fingerprint(name)
    # Agent-runtime tools are appended outside the central registry. Their
    # inline dispatcher remains authoritative and has no mutable registry entry
    # to bind. Every registry-dispatched tool must have an advertised binding.
    if entry_fingerprint is None:
        return None

    with _LOCK:
        binding = _PUBLISHED_BINDINGS.get((name, expected_generation))
    if binding is None:
        # Backward-compatible test/embedding agents can construct a registry
        # generation without ever requesting model definitions. There is no
        # advertised contract to drift in that case. Production definitions go
        # through annotate_tool_definitions() and always create this binding.
        return None
    if not _constant_text_equal(
        str(binding.get("registration_sha256") or ""),
        entry_fingerprint,
    ):
        return f"Tool '{name}' registration metadata drifted; execution was blocked."
    contract_fingerprint = _contract_fingerprint(resolve_tool_contract(name))
    if not _constant_text_equal(
        str(binding.get("contract_sha256") or ""),
        contract_fingerprint,
    ):
        return f"Tool '{name}' execution contract drifted; execution was blocked."
    return None


def annotate_tool_definitions(
    definitions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose the effective contract in model-visible descriptions.

    The OpenAI tool envelope is intentionally kept schema-compatible: the
    metadata is rendered into the description rather than adding unknown
    top-level keys that strict providers may reject.
    """

    annotated: list[dict[str, Any]] = []
    for definition in definitions:
        function = definition.get("function") if isinstance(definition, dict) else None
        if not isinstance(function, dict):
            annotated.append(definition)
            continue
        name = str(function.get("name") or "")
        contract = resolve_tool_contract(name)
        generation = _runtime_registry_generation()
        registration_fingerprint = _runtime_registration_fingerprint(name)
        if registration_fingerprint is not None:
            binding = {
                "registry_generation": generation,
                "registration_sha256": registration_fingerprint,
                "contract_sha256": _contract_fingerprint(contract),
            }
            with _LOCK:
                _PUBLISHED_BINDINGS[(name, generation)] = binding
        timeout = (
            f"hard-deadline-after-{contract.timeout_seconds:g}s"
            if contract.timeout_seconds is not None
            else "runtime-default"
        )
        marker = (
            "Hermes execution contract: "
            f"mode={contract.execution_mode}; "
            f"side_effect={contract.side_effect_class}; "
            f"approval={'required' if contract.requires_approval else 'policy-driven'}; "
            f"timeout={timeout}; "
            f"timeout_policy={contract.timeout_policy}; "
            f"progress={'supported' if contract.supports_progress else 'lifecycle-only'}; "
            f"output={contract.output_policy}."
        )
        description = str(function.get("description") or "").rstrip()
        if marker not in description:
            description = f"{description}\n\n{marker}" if description else marker
        annotated.append({
            **definition,
            "function": {**function, "description": description},
        })
    return annotated


def _invalidate_tool_definition_cache() -> None:
    callback = _INVALIDATE_DEFINITION_CACHE
    if callback is not None:
        callback()


def _runtime_registry_generation() -> int:
    callback = _REGISTRY_GENERATION
    return int(callback()) if callback is not None else 0


def _bump_runtime_registry_generation() -> None:
    """Make contract mutations invalidate every production tool snapshot."""

    callback = _BUMP_REGISTRY_GENERATION
    if callback is not None:
        callback()


def _runtime_registration_fingerprint(tool_name: str) -> str | None:
    callback = _REGISTRATION_FINGERPRINT
    return callback(tool_name) if callback is not None else None


def _contract_fingerprint(contract: ToolExecutionContract) -> str:
    encoded = json.dumps(
        contract.public_dict(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _constant_text_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("ascii", "ignore"), right.encode("ascii", "ignore"))


def reset_tool_contracts_for_tests() -> None:
    with _LOCK:
        changed = bool(_CONTRACTS or _PUBLISHED_BINDINGS)
        _CONTRACTS.clear()
        _PUBLISHED_BINDINGS.clear()
    if changed:
        _bump_runtime_registry_generation()
    _invalidate_tool_definition_cache()
