"""Provider identity, dependency resolution, and drain state.

This catalog is intentionally smaller than the existing model/provider UI
catalog.  It describes live runtime bindings: a provider identity is stable
for a generation, and a turn may retain that binding while a newer provider
is registering or an old provider is draining.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import re
import time
from threading import RLock
from typing import Any, Callable, Iterable


class ProviderStatus(str, Enum):
    REGISTERING = "registering"
    ACTIVE = "active"
    DRAINING = "draining"
    UNHEALTHY = "unhealthy"
    REMOVED = "removed"


class DependencyTransition(str, Enum):
    ACTIVATING = "activating"
    DEACTIVATING = "deactivating"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class DependencySpec:
    key: str
    version_range: str = "*"
    realm: str = "global"
    required: bool = True
    policy: str = "read_write"
    replaceable_during_turn: bool = False

    def __post_init__(self) -> None:
        if not str(self.key).strip():
            raise ValueError("dependency key is required")
        if not str(self.realm).strip():
            raise ValueError("dependency realm is required")
        if not str(self.policy).strip():
            raise ValueError("dependency policy is required")


@dataclass(frozen=True)
class ProviderRecord:
    provider_id: str
    interface_key: str
    version: str
    generation: int
    realm: str = "global"
    status: ProviderStatus = ProviderStatus.REGISTERING
    health: str = "unknown"
    capacity: int = 1
    inflight: int = 0
    dependencies: tuple["DependencySpec", ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    drain_deadline: float | None = None
    isolated: bool = False

    def __post_init__(self) -> None:
        if not str(self.provider_id).strip():
            raise ValueError("provider_id is required")
        if not str(self.interface_key).strip():
            raise ValueError("interface_key is required")
        if self.generation < 0:
            raise ValueError("generation must be non-negative")
        if self.capacity < 0 or self.inflight < 0:
            raise ValueError("capacity and inflight must be non-negative")


@dataclass(frozen=True)
class ProviderBinding:
    dependency: DependencySpec
    provider_id: str
    version: str
    generation: int
    realm: str

    @property
    def witness_ref(self) -> str:
        """Stable audit reference for this exact provider generation."""

        return f"{self.provider_id}@{self.generation}"


class DependencyGraph:
    """Small declarative dependency graph used by live component adapters."""

    def __init__(self) -> None:
        self._dependencies: dict[str, tuple[DependencySpec, ...]] = {}
        self._available: set[str] = set()

    def declare(
        self,
        component_id: str,
        dependencies: Iterable[DependencySpec] = (),
    ) -> None:
        component = str(component_id or "").strip()
        if not component:
            raise ValueError("component_id is required")
        specs = tuple(dependencies)
        previous = self._dependencies.get(component)
        self._dependencies[component] = specs
        try:
            self._assert_acyclic()
        except Exception:
            if previous is None:
                self._dependencies.pop(component, None)
            else:
                self._dependencies[component] = previous
            raise

    def remove(self, component_id: str) -> None:
        self._dependencies.pop(str(component_id or "").strip(), None)

    def set_available(self, key: str, available: bool = True) -> None:
        normalized = str(key or "").strip()
        if not normalized:
            raise ValueError("dependency key is required")
        if available:
            self._available.add(normalized)
        else:
            self._available.discard(normalized)

    def missing(self, component_id: str) -> tuple[DependencySpec, ...]:
        return tuple(
            dependency
            for dependency in self._dependencies.get(str(component_id or "").strip(), ())
            if dependency.required and dependency.key not in self._available
        )

    def ready(self, component_id: str) -> bool:
        return not self.missing(component_id)

    def transition(
        self,
        before: Iterable[str],
        after: Iterable[str],
    ) -> DependencyTransition:
        before_set = set(before)
        after_set = set(after)
        if before_set == after_set:
            return DependencyTransition.NEUTRAL
        if before_set < after_set:
            return DependencyTransition.ACTIVATING
        if after_set < before_set:
            return DependencyTransition.DEACTIVATING
        # A replacement has both sides; a component must not be treated as
        # active until all of its required bindings are present.
        return DependencyTransition.ACTIVATING if after_set else DependencyTransition.DEACTIVATING

    def components(self) -> tuple[str, ...]:
        return tuple(sorted(self._dependencies))

    def _assert_acyclic(self) -> None:
        edges = {
            component: {
                dependency.key
                for dependency in specs
                if dependency.key in self._dependencies
            }
            for component, specs in self._dependencies.items()
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(component: str) -> None:
            if component in visiting:
                raise ValueError(f"dependency cycle detected: {component}")
            if component in visited:
                return
            visiting.add(component)
            for dependency in edges.get(component, set()):
                visit(dependency)
            visiting.remove(component)
            visited.add(component)

        for component in edges:
            visit(component)


class PolicyInterceptor:
    """Composable capability policy hook for dependency/provider access."""

    def __init__(self) -> None:
        self._hooks: list[Callable[[DependencySpec, str], bool]] = []

    def add(self, hook: Callable[[DependencySpec, str], bool]) -> None:
        if not callable(hook):
            raise TypeError("policy hook must be callable")
        self._hooks.append(hook)

    def allow(self, dependency: DependencySpec, operation: str) -> bool:
        return all(bool(hook(dependency, operation)) for hook in self._hooks)


class ProviderCatalog:
    """Thread-safe live provider catalog with explicit drain semantics."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._providers: dict[str, ProviderRecord] = {}
        self._next_generation: dict[tuple[str, str], int] = {}
        self._dependencies: dict[str, tuple[str, ...]] = {}

    def register(
        self,
        *,
        provider_id: str,
        interface_key: str,
        version: str,
        realm: str = "global",
        health: str = "unknown",
        capacity: int = 1,
        metadata: dict[str, Any] | None = None,
        dependencies: Iterable[DependencySpec] = (),
        activate: bool = True,
    ) -> ProviderRecord:
        key = str(interface_key).strip()
        provider = str(provider_id).strip()
        if not provider or not key:
            raise ValueError("provider_id and interface_key are required")
        with self._lock:
            previous = self._providers.get(provider)
            if previous is not None and previous.status != ProviderStatus.REMOVED:
                raise ValueError(f"provider is already registered: {provider}")
            generation_key = (key, str(realm).strip() or "global")
            generation = self._next_generation.get(generation_key, 0) + 1
            self._next_generation[generation_key] = generation
            record = ProviderRecord(
                provider_id=provider,
                interface_key=key,
                version=str(version or "").strip() or "0",
                generation=generation,
                realm=generation_key[1],
                status=ProviderStatus.ACTIVE if activate else ProviderStatus.REGISTERING,
                health=str(health or "unknown"),
                capacity=max(0, int(capacity)),
                dependencies=tuple(dependencies),
                metadata=dict(metadata or {}),
            )
            self._providers[provider] = record
            self._dependencies[provider] = tuple(
                dependency.key for dependency in record.dependencies
            )
            return record

    def get(self, provider_id: str) -> ProviderRecord | None:
        with self._lock:
            return self._providers.get(str(provider_id).strip())

    def active_for(self, logical_provider_id: str) -> ProviderRecord | None:
        """Return the active generation for a logical provider identity."""

        logical = str(logical_provider_id or "").strip()
        with self._lock:
            candidates = [
                record
                for record in self._providers.values()
                if record.status == ProviderStatus.ACTIVE
                and (
                    record.provider_id == logical
                    or str(record.metadata.get("logical_provider_id") or "") == logical
                )
            ]
        return max(candidates, key=lambda record: record.generation, default=None)

    def update_health(self, provider_id: str, health: str) -> ProviderRecord:
        with self._lock:
            current = self._require(provider_id)
            status = current.status
            if str(health).strip().lower() in {"failed", "unhealthy"}:
                status = ProviderStatus.UNHEALTHY
            elif status == ProviderStatus.UNHEALTHY:
                status = ProviderStatus.ACTIVE
            updated = replace(current, health=str(health or "unknown"), status=status)
            self._providers[updated.provider_id] = updated
            return updated

    def begin_call(self, provider_id: str) -> ProviderRecord:
        with self._lock:
            current = self._require(provider_id)
            if current.status != ProviderStatus.ACTIVE:
                raise RuntimeError(
                    f"provider {provider_id!r} is not accepting new calls: {current.status.value}"
                )
            if current.capacity and current.inflight >= current.capacity:
                raise RuntimeError(f"provider {provider_id!r} is at capacity")
            updated = replace(current, inflight=current.inflight + 1)
            self._providers[updated.provider_id] = updated
            return updated

    def end_call(self, provider_id: str) -> ProviderRecord:
        with self._lock:
            current = self._require(provider_id)
            updated = replace(current, inflight=max(0, current.inflight - 1))
            self._providers[updated.provider_id] = updated
            return updated

    def begin_bound_call(self, binding: ProviderBinding) -> ProviderRecord:
        """Start a call only against the exact provider generation in a turn."""

        with self._lock:
            current = self._require(binding.provider_id)
            if (
                current.generation != binding.generation
                or current.version != binding.version
                or current.realm != binding.realm
                or current.interface_key != binding.dependency.key
            ):
                raise RuntimeError("provider binding generation is stale")
        return self.begin_call(binding.provider_id)

    def begin_drain(
        self,
        provider_id: str,
        *,
        deadline_seconds: float | None = None,
        deadline_at: float | None = None,
    ) -> ProviderRecord:
        if deadline_seconds is not None and deadline_seconds <= 0:
            raise ValueError("drain deadline must be positive")
        if deadline_at is not None and deadline_at <= 0:
            raise ValueError("drain deadline timestamp must be positive")
        with self._lock:
            current = self._require(provider_id)
            if current.status == ProviderStatus.REMOVED:
                return current
            resolved_deadline = deadline_at
            if resolved_deadline is None and deadline_seconds is not None:
                resolved_deadline = time.monotonic() + float(deadline_seconds)
            if current.status == ProviderStatus.DRAINING and current.drain_deadline is not None:
                resolved_deadline = (
                    min(current.drain_deadline, resolved_deadline)
                    if resolved_deadline is not None
                    else current.drain_deadline
                )
            updated = replace(
                current,
                status=ProviderStatus.DRAINING,
                drain_deadline=resolved_deadline,
            )
            self._providers[updated.provider_id] = updated
            return updated

    def expired_drains(self, *, now: float | None = None) -> tuple[ProviderRecord, ...]:
        """Return draining providers that require alert/manual action."""

        current_time = time.monotonic() if now is None else float(now)
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self._providers.values()
                        if item.status == ProviderStatus.DRAINING
                        and item.drain_deadline is not None
                        and current_time >= item.drain_deadline
                    ),
                    key=lambda item: item.provider_id,
                )
            )

    def enforce_drain_deadline(
        self,
        provider_id: str,
        *,
        now: float | None = None,
        force_unload: bool = False,
    ) -> ProviderRecord:
        """Isolate an expired drain, or explicitly force-unload it.

        The default path never discards in-flight ownership. It marks the
        provider isolated and keeps it draining for alert/manual review.
        ``force_unload`` is explicit because it may abandon an external call.
        """

        current_time = time.monotonic() if now is None else float(now)
        with self._lock:
            current = self._require(provider_id)
            if (
                current.status != ProviderStatus.DRAINING
                or current.drain_deadline is None
                or current_time < current.drain_deadline
            ):
                return current
            if force_unload:
                return self.unload(provider_id, force=True)
            metadata = dict(current.metadata)
            metadata.update(
                {
                    "drain_expired": True,
                    "drain_expired_at": current_time,
                    "drain_action": "isolated_manual_review",
                }
            )
            updated = replace(
                current,
                metadata=metadata,
                capacity=0,
                isolated=True,
            )
            self._providers[updated.provider_id] = updated
            return updated

    def resume(self, provider_id: str) -> ProviderRecord:
        """Resume an existing drained provider after a failed replacement."""

        with self._lock:
            current = self._require(provider_id)
            if current.status == ProviderStatus.REMOVED:
                raise RuntimeError(f"provider {provider_id!r} has been removed")
            updated = replace(
                current,
                status=ProviderStatus.ACTIVE,
                drain_deadline=None,
                isolated=False,
            )
            self._providers[updated.provider_id] = updated
            return updated

    def unload(self, provider_id: str, *, force: bool = False) -> ProviderRecord:
        with self._lock:
            current = self._require(provider_id)
            dependents = [
                provider.provider_id
                for provider in self._providers.values()
                if current.interface_key in self._dependencies.get(provider.provider_id, ())
                and provider.status != ProviderStatus.REMOVED
            ]
            if dependents and not force:
                raise RuntimeError(
                    f"provider {provider_id!r} still has dependent provider(s): "
                    + ", ".join(sorted(dependents))
                )
            if current.inflight and not force:
                raise RuntimeError(
                    f"provider {provider_id!r} still has {current.inflight} in-flight call(s)"
                )
            updated = replace(
                current,
                status=ProviderStatus.REMOVED,
                inflight=0,
                drain_deadline=None,
                isolated=True,
            )
            self._providers[updated.provider_id] = updated
            return updated

    def dependent_first_drain_order(self, provider_id: str) -> tuple[str, ...]:
        """Return dependents before their providers for safe teardown."""

        root = str(provider_id or "").strip()
        with self._lock:
            records = dict(self._providers)
            dependencies = dict(self._dependencies)
        order: list[str] = []
        visiting: set[str] = set()

        def visit(target: str) -> None:
            if target in visiting:
                raise RuntimeError(f"provider dependency cycle detected: {target}")
            visiting.add(target)
            for provider, required in dependencies.items():
                target_interface = records[target].interface_key
                if target_interface in required and records.get(provider, current_removed()).status != ProviderStatus.REMOVED:
                    visit(provider)
            visiting.remove(target)
            if target not in order:
                order.append(target)

        def current_removed() -> ProviderRecord:
            return ProviderRecord(
                provider_id="missing",
                interface_key="missing",
                version="0",
                generation=0,
                status=ProviderStatus.REMOVED,
            )

        if root not in records:
            raise KeyError(f"unknown provider: {provider_id}")
        visit(root)
        return tuple(order)

    def list(self, *, interface_key: str = "", realm: str = "") -> list[ProviderRecord]:
        with self._lock:
            items = list(self._providers.values())
        if interface_key:
            items = [item for item in items if item.interface_key == interface_key]
        if realm:
            items = [item for item in items if item.realm == realm]
        return sorted(items, key=lambda item: (item.interface_key, item.realm, item.generation))

    def resolve(self, dependency: DependencySpec) -> ProviderBinding | None:
        with self._lock:
            candidates = [
                item
                for item in self._providers.values()
                if item.interface_key == dependency.key
                and item.realm == dependency.realm
                and item.status == ProviderStatus.ACTIVE
                and item.health not in {"failed", "unhealthy"}
                and _version_satisfies(item.version, dependency.version_range)
            ]
        if not candidates:
            return None
        # Prefer healthy, lower inflight, newer generation. Provider identity
        # remains part of the returned binding so a turn never resolves a
        # later call by interface name alone.
        candidates.sort(
            key=lambda item: (
                0 if item.health == "healthy" else 1,
                item.inflight,
                -item.generation,
                item.provider_id,
            )
        )
        selected = candidates[0]
        return ProviderBinding(
            dependency=dependency,
            provider_id=selected.provider_id,
            version=selected.version,
            generation=selected.generation,
            realm=selected.realm,
        )

    def resolve_with_policy(
        self,
        dependency: DependencySpec,
        policy: PolicyInterceptor | None = None,
        *,
        operation: str = "resolve",
    ) -> ProviderBinding | None:
        """Resolve only when every registered policy hook permits access."""

        if policy is not None and not policy.allow(dependency, operation):
            return None
        return self.resolve(dependency)

    def resolve_all(self, dependencies: Iterable[DependencySpec]) -> dict[str, ProviderBinding]:
        resolved: dict[str, ProviderBinding] = {}
        missing: list[str] = []
        for dependency in dependencies:
            binding = self.resolve(dependency)
            if binding is None:
                if dependency.required:
                    missing.append(dependency.key)
                continue
            resolved[dependency.key] = binding
        if missing:
            raise LookupError(f"required providers unavailable: {', '.join(sorted(missing))}")
        return resolved

    def _require(self, provider_id: str) -> ProviderRecord:
        provider = self._providers.get(str(provider_id).strip())
        if provider is None:
            raise KeyError(f"unknown provider: {provider_id}")
        return provider


_SEMVER_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$")


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.match(str(value or "").strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _version_satisfies(version: str, version_range: str) -> bool:
    """Evaluate the bounded semver range vocabulary used by manifests."""

    requested = str(version_range or "*").strip()
    if requested in {"", "*", "latest"}:
        return True
    parsed = _parse_version(version)
    if parsed is None:
        return str(version).lstrip("v") == requested.lstrip("v")
    if requested.startswith("^"):
        base = _parse_version(requested[1:])
        if base is None:
            return False
        upper = (base[0] + 1, 0, 0) if base[0] else (0, base[1] + 1, 0)
        return parsed >= base and parsed < upper
    if requested.startswith("~"):
        base = _parse_version(requested[1:])
        return base is not None and parsed >= base and parsed < (base[0], base[1] + 1, 0)
    if "x" in requested.lower() or "*" in requested:
        parts = requested.lstrip("v").split(".")
        if len(parts) > 3:
            return False
        for index, part in enumerate(parts):
            if part.lower() in {"x", "*"}:
                break
            if not part.isdigit() or parsed[index] != int(part):
                return False
        return True
    for token in requested.split(","):
        match = re.match(r"^(<=|>=|<|>|=)?\s*(v?\d+(?:\.\d+){0,2})$", token.strip())
        if not match:
            return False
        target = _parse_version(match.group(2))
        if target is None:
            return False
        operator = match.group(1) or "="
        if operator == "=" and parsed != target:
            return False
        if operator == ">=" and parsed < target:
            return False
        if operator == "<=" and parsed > target:
            return False
        if operator == ">" and parsed <= target:
            return False
        if operator == "<" and parsed >= target:
            return False
    return True
