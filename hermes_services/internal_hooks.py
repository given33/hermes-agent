"""Typed, sealed hook runtime for trusted Hermes modules.

The registry is deliberately not a plugin extension surface.  Production hooks
must be listed in ``_BUILTIN_HOOKS`` and are installed once during bootstrap.
The human-readable ``source`` field is telemetry only and never grants trust.
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass, field
import json
import logging
from queue import Empty, Queue
from threading import BoundedSemaphore, Lock, RLock, Thread
import time
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, TypedDict
import uuid

from hermes_runtime.redaction import redact_sensitive_text


logger = logging.getLogger(__name__)

HookPoint = Literal[
    "after_hosted_event_persistence",
    "after_provider_response",
    "after_tool_result",
    "before_context_compaction",
    "before_hosted_event_commit",
    "before_model_request",
    "before_tool_call",
]
FailurePolicy = Literal["fail_open", "fail_closed"]

HOOK_POINTS = frozenset(
    {
        "after_hosted_event_persistence",
        "after_provider_response",
        "after_tool_result",
        "before_context_compaction",
        "before_hosted_event_commit",
        "before_model_request",
        "before_tool_call",
    }
)


class HookTrace(TypedDict):
    point: str
    name: str
    source: str
    version: str
    failure_policy: str
    status: str
    error: str
    duration_ms: int
    invocation_id: str


class ModelRequestContext(TypedDict):
    task_id: str
    turn_id: str
    api_request_id: str
    session_id: str
    model: str
    provider: str
    api_call_count: int


class ToolCallContext(TypedDict):
    tool_name: str
    task_id: str
    session_id: str
    tool_call_id: str
    turn_id: str


class ToolResultContext(TypedDict):
    tool_name: str
    tool_use_id: str


class CompactionContext(TypedDict):
    session_id: str
    model: str
    approx_tokens: int
    focus_topic: str
    forced: bool


class HostedEventContext(TypedDict):
    conversation_id: str
    turn_id: str
    role_stage: str


class PersistedHostedEventContext(HostedEventContext):
    cursor: int
    store_path: str
    event_id: str
    idempotency_key: str
    delivery_id: str
    attempt: int


@dataclass(frozen=True)
class InternalHook:
    name: str
    source: str
    version: str
    callback: Callable[..., Any]
    order: int = 100
    timeout_seconds: float = 2.0
    failure_policy: FailurePolicy = "fail_open"
    circuit_breaker_seconds: float = 30.0
    failure_threshold: int = 3

    def __post_init__(self) -> None:
        if self.failure_policy not in {"fail_open", "fail_closed"}:
            raise ValueError("failure_policy must be fail_open or fail_closed")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.circuit_breaker_seconds < 0:
            raise ValueError("circuit_breaker_seconds must be non-negative")
        if self.failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("hook name is required")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("hook source is required")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("hook version is required")
        if not callable(self.callback):
            raise TypeError("hook callback must be callable")


class InternalHookExecutionError(RuntimeError):
    """Fail-closed hook error carrying the trace produced before the raise."""

    def __init__(
        self,
        message: str,
        *,
        trace: list[HookTrace],
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.trace = tuple(dict(item) for item in trace)
        self.payload = payload
        self.blocked = True


@dataclass(frozen=True)
class InternalHookResult:
    """Uniform hook outcome while retaining two-value unpack compatibility."""

    payload: Any
    trace: list[HookTrace]
    blocked: bool = False

    def __iter__(self):
        yield self.payload
        yield self.trace


@dataclass(frozen=True)
class _HookContract:
    payload_name: str
    payload_validator: Callable[[Any], bool]
    required_context: Mapping[str, type | tuple[type, ...]]
    optional_context: Mapping[str, type | tuple[type, ...]] = field(
        default_factory=dict
    )


@dataclass
class _HookRuntimeState:
    active_invocation_id: str = ""
    timed_out_invocation_id: str = ""
    recover_after: float = 0.0
    consecutive_failures: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    last_status: str = "never_run"
    last_error: str = ""
    last_duration_ms: int = 0
    last_finished_at: float = 0.0


class _SlotLease:
    """Release a worker slot at most once, including timeout abandonment."""

    def __init__(self, semaphore: BoundedSemaphore) -> None:
        self._semaphore = semaphore
        self._lock = Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._semaphore.release()


_STRING_FIELDS = (str,)
_INTEGER_FIELDS = (int,)
_BOOL_FIELDS = (bool,)


def _is_mapping(value: Any) -> bool:
    return isinstance(value, dict)


def _is_tool_result(value: Any) -> bool:
    return isinstance(value, str)


def _is_message_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _is_provider_response(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("content"), str)
        and isinstance(value.get("finish_reason"), str)
        and _is_plain_int(value.get("tool_call_count"))
        and (
            value.get("response_model") is None
            or isinstance(value.get("response_model"), str)
        )
    )


def _is_hosted_event(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    required = {
        "event_id",
        "cursor",
        "account_generation",
        "conversation_id",
        "turn_id",
        "role_stage",
        "event_type",
        "sequence",
        "occurred_at",
        "idempotency_key",
        "payload",
        "schema_version",
    }
    return (
        required.issubset(value)
        and all(
            isinstance(value.get(field), str)
            for field in (
                "event_id",
                "account_generation",
                "conversation_id",
                "turn_id",
                "role_stage",
                "event_type",
                "idempotency_key",
                "schema_version",
            )
        )
        and all(
            _is_plain_int(value.get(field))
            for field in ("cursor", "sequence", "occurred_at")
        )
        and isinstance(value.get("payload"), dict)
    )


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_MODEL_CONTEXT = MappingProxyType(
    {
        "task_id": _STRING_FIELDS,
        "turn_id": _STRING_FIELDS,
        "api_request_id": _STRING_FIELDS,
        "session_id": _STRING_FIELDS,
        "model": _STRING_FIELDS,
        "provider": _STRING_FIELDS,
        "api_call_count": _INTEGER_FIELDS,
    }
)
_HOSTED_CONTEXT = MappingProxyType(
    {
        "conversation_id": _STRING_FIELDS,
        "turn_id": _STRING_FIELDS,
        "role_stage": _STRING_FIELDS,
    }
)

_CONTRACTS: Mapping[str, _HookContract] = MappingProxyType(
    {
        "before_model_request": _HookContract(
            "model request mapping", _is_mapping, _MODEL_CONTEXT
        ),
        "after_provider_response": _HookContract(
            "provider response mapping", _is_provider_response, _MODEL_CONTEXT
        ),
        "before_tool_call": _HookContract(
            "tool argument mapping",
            _is_mapping,
            MappingProxyType(
                {
                    "tool_name": _STRING_FIELDS,
                    "task_id": _STRING_FIELDS,
                    "session_id": _STRING_FIELDS,
                    "tool_call_id": _STRING_FIELDS,
                    "turn_id": _STRING_FIELDS,
                }
            ),
        ),
        "after_tool_result": _HookContract(
            "tool result string",
            _is_tool_result,
            MappingProxyType(
                {
                    "tool_name": _STRING_FIELDS,
                    "tool_use_id": _STRING_FIELDS,
                }
            ),
        ),
        "before_context_compaction": _HookContract(
            "message mapping list",
            _is_message_list,
            MappingProxyType(
                {
                    "session_id": _STRING_FIELDS,
                    "model": _STRING_FIELDS,
                    "approx_tokens": _INTEGER_FIELDS,
                    "focus_topic": _STRING_FIELDS,
                    "forced": _BOOL_FIELDS,
                }
            ),
        ),
        "before_hosted_event_commit": _HookContract(
            "hosted event envelope", _is_hosted_event, _HOSTED_CONTEXT
        ),
        "after_hosted_event_persistence": _HookContract(
            "persisted hosted event envelope",
            _is_hosted_event,
            MappingProxyType(
                {
                    **dict(_HOSTED_CONTEXT),
                    "cursor": _INTEGER_FIELDS,
                    "store_path": _STRING_FIELDS,
                    "event_id": _STRING_FIELDS,
                    "idempotency_key": _STRING_FIELDS,
                    "delivery_id": _STRING_FIELDS,
                    "attempt": _INTEGER_FIELDS,
                }
            ),
        ),
    }
)


# Production registration is code-reviewed data, not a runtime extension API.
# Additions must include an exact point and callback here.  An empty tuple is
# intentional until Hermes ships a built-in observer that needs this surface.
_BUILTIN_HOOKS: tuple[tuple[HookPoint, InternalHook], ...] = ()
_MAX_HOOKS = 64
_MAX_HOOK_WORKERS = 8

_LOCK = RLock()
_HOOKS: dict[str, list[InternalHook]] = {point: [] for point in HOOK_POINTS}
_RUNTIME: dict[tuple[str, str], _HookRuntimeState] = {}
_HOOK_SLOTS = BoundedSemaphore(_MAX_HOOK_WORKERS)
_BOOTSTRAPPED = False
_SEALED = False


def bootstrap_internal_hooks() -> None:
    """Install the central built-in allowlist once and seal the registry."""

    global _BOOTSTRAPPED, _SEALED
    with _LOCK:
        if _BOOTSTRAPPED:
            return
        for point, hook in _BUILTIN_HOOKS:
            _validate_hook_registration(point, hook)
            entries = _HOOKS[point]
            entries.append(hook)
            entries.sort(key=lambda item: (item.order, item.name))
            _RUNTIME[(point, hook.name)] = _HookRuntimeState()
        _BOOTSTRAPPED = True
        _SEALED = True


def register_internal_hook(point: HookPoint, hook: InternalHook) -> None:
    """Reject runtime registration; hook trust is established by bootstrap."""

    del point, hook
    raise PermissionError(
        "runtime internal-hook registration is disabled; use the built-in allowlist"
    )


def _validate_hook_registration(point: HookPoint, hook: InternalHook) -> None:
    if point not in HOOK_POINTS:
        raise ValueError(f"unsupported hook point: {point}")
    if point == "after_hosted_event_persistence" and hook.failure_policy != "fail_open":
        raise ValueError("post-persistence hooks must fail open")
    if sum(len(entries) for entries in _HOOKS.values()) >= _MAX_HOOKS:
        raise ValueError("internal hook registry limit reached")
    entries = _HOOKS[point]
    if any(item.name == hook.name for item in entries):
        raise ValueError(f"internal hook already registered: {point}:{hook.name}")


def internal_hook_registry_status() -> dict[str, Any]:
    """Return bounded operational metrics without callback or payload data."""

    bootstrap_internal_hooks()
    now = time.monotonic()
    with _LOCK:
        hooks: list[dict[str, Any]] = []
        for point in sorted(HOOK_POINTS):
            for hook in _HOOKS[point]:
                state = _RUNTIME[(point, hook.name)]
                circuit_open = bool(
                    state.timed_out_invocation_id
                    or state.active_invocation_id
                    or state.recover_after > now
                )
                hooks.append(
                    {
                        "point": point,
                        "name": hook.name,
                        "source": hook.source,
                        "version": hook.version,
                        "failure_policy": hook.failure_policy,
                        "circuit_open": circuit_open,
                        "recover_after_seconds": max(0.0, state.recover_after - now),
                        "active": bool(state.active_invocation_id),
                        "consecutive_failures": state.consecutive_failures,
                        "counts": dict(state.counts),
                        "last_status": state.last_status,
                        "last_error": state.last_error,
                        "last_duration_ms": state.last_duration_ms,
                        "last_finished_at": state.last_finished_at,
                    }
                )
        return {
            "bootstrapped": _BOOTSTRAPPED,
            "sealed": _SEALED,
            "worker_limit": _MAX_HOOK_WORKERS,
            "hook_count": len(hooks),
            "hooks": hooks,
        }


def has_internal_hooks(point: HookPoint) -> bool:
    _validate_point(point)
    bootstrap_internal_hooks()
    with _LOCK:
        return bool(_HOOKS[point])


def run_internal_hooks(
    point: HookPoint,
    payload: Any,
    **context: Any,
) -> InternalHookResult:
    _validate_point(point)
    bootstrap_internal_hooks()

    # Contract validation is part of the point boundary, not a property of
    # whether an observer happens to be installed.  Production normally has an
    # empty registry, and silently accepting malformed payloads in that state
    # would make the contract depend on deployment configuration.  Validate
    # first so callers always receive the same structured fail-closed trace.
    contract = _CONTRACTS[point]
    contract_error = _contract_error(contract, payload, context)
    if contract_error:
        trace = [_contract_trace(point, contract_error)]
        _audit_hook_trace(trace[0], context)
        raise InternalHookExecutionError(
            contract_error,
            trace=trace,
            payload=payload,
        )

    with _LOCK:
        hooks = tuple(_HOOKS[point])
    if not hooks:
        return InternalHookResult(payload=payload, trace=[], blocked=False)

    current = payload
    trace: list[HookTrace] = []
    for hook in hooks:
        started = time.monotonic()
        invocation_id = uuid.uuid4().hex
        try:
            payload_snapshot = copy.deepcopy(current)
            context_snapshot = copy.deepcopy(context)
        except Exception as exc:
            item = _make_trace(
                point,
                hook,
                invocation_id,
                "failed",
                f"hook input snapshot failed: {exc}",
                started,
            )
            _finish_trace(point, hook, item, successful=False)
            trace.append(item)
            _audit_hook_trace(item, context)
            if hook.failure_policy == "fail_closed":
                raise InternalHookExecutionError(
                    f"internal hook {hook.name} input snapshot failed: {exc}",
                    trace=trace,
                    payload=current,
                ) from exc
            continue

        blocked_status = _begin_invocation(point, hook, invocation_id)
        if blocked_status:
            error = (
                "hook circuit is open after a timed-out callback"
                if blocked_status == "circuit_open"
                else "hook callback is already running"
            )
            item = _make_trace(
                point, hook, invocation_id, blocked_status, error, started
            )
            _finish_trace(point, hook, item, successful=False)
            trace.append(item)
            _audit_hook_trace(item, context)
            if hook.failure_policy == "fail_closed":
                raise InternalHookExecutionError(
                    f"internal hook {hook.name} {blocked_status}: {error}",
                    trace=trace,
                    payload=current,
                )
            continue

        semaphore = _HOOK_SLOTS
        if not semaphore.acquire(blocking=False):
            _abandon_invocation(point, hook, invocation_id)
            item = _make_trace(
                point,
                hook,
                invocation_id,
                "saturated",
                "trusted hook worker limit reached",
                started,
            )
            _finish_trace(point, hook, item, successful=False)
            trace.append(item)
            _audit_hook_trace(item, context)
            if hook.failure_policy == "fail_closed":
                raise InternalHookExecutionError(
                    f"internal hook {hook.name} worker limit reached",
                    trace=trace,
                    payload=current,
                )
            continue

        result_queue: Queue[tuple[bool, Any]] = Queue(maxsize=1)
        lease = _SlotLease(semaphore)

        def invoke(
            *,
            _hook: InternalHook = hook,
            _payload: Any = payload_snapshot,
            _context: dict[str, Any] = context_snapshot,
            _point: str = point,
            _invocation_id: str = invocation_id,
            _queue: Queue[tuple[bool, Any]] = result_queue,
            _lease: _SlotLease = lease,
        ) -> None:
            try:
                result = _hook.callback(_payload, **_context)
                try:
                    _queue.put_nowait((True, result))
                except Exception:
                    pass
            except BaseException as exc:  # reported on the caller thread
                try:
                    _queue.put_nowait((False, exc))
                except Exception:
                    pass
            finally:
                _lease.release()
                _complete_worker(_point, _hook, _invocation_id)

        worker = Thread(
            target=invoke,
            daemon=True,
            name=f"hermes-hook-{hook.name[:32]}",
        )
        try:
            worker.start()
        except BaseException as exc:
            lease.release()
            _abandon_invocation(point, hook, invocation_id)
            item = _make_trace(
                point,
                hook,
                invocation_id,
                "failed",
                f"hook worker could not start: {exc}",
                started,
            )
            _finish_trace(point, hook, item, successful=False)
            trace.append(item)
            _audit_hook_trace(item, context)
            if hook.failure_policy == "fail_closed":
                raise InternalHookExecutionError(
                    f"internal hook {hook.name} worker could not start: {exc}",
                    trace=trace,
                    payload=current,
                ) from exc
            logger.warning(
                "Internal hook %s failed open: %s",
                hook.name,
                item["error"],
            )
            continue

        status = "completed"
        error = ""
        successful = False
        cause: BaseException | None = None
        try:
            succeeded, result = result_queue.get(timeout=hook.timeout_seconds)
            if not succeeded:
                cause = result
                raise result
            if result is not None:
                if not contract.payload_validator(result):
                    raise TypeError(
                        f"hook result must be a {contract.payload_name} or None"
                    )
                current = result
            successful = True
        except Empty as exc:
            cause = exc
            status = "timeout"
            error = f"hook timed out after {hook.timeout_seconds:g}s"
            lease.release()
            _mark_timeout(point, hook, invocation_id)
        except BaseException as exc:
            cause = exc
            status = "invalid_result" if isinstance(exc, TypeError) else "failed"
            error = str(exc)[:1000]

        item = _make_trace(
            point, hook, invocation_id, status, error, started
        )
        _finish_trace(point, hook, item, successful=successful)
        trace.append(item)
        _audit_hook_trace(item, context)
        if status != "completed" and hook.failure_policy == "fail_closed":
            raise InternalHookExecutionError(
                f"internal hook {hook.name} {status}: {error}",
                trace=trace,
                payload=current,
            ) from cause
        if status != "completed":
            logger.warning(
                "Internal hook %s failed open: %s",
                hook.name,
                item["error"],
            )
    return InternalHookResult(payload=current, trace=trace, blocked=False)


def _audit_hook_trace(item: HookTrace, context: Mapping[str, Any]) -> None:
    """Write bounded, redacted hook telemetry without persisting payload data."""

    try:
        trace = dict(item)
        trace["error"] = redact_sensitive_text(str(trace.get("error") or ""))[:1000]
        allowed_context = {
            key: str(context.get(key) or "")[:256]
            for key in (
                "account_generation",
                "api_request_id",
                "conversation_id",
                "delivery_id",
                "event_id",
                "session_id",
                "task_id",
                "tool_call_id",
                "turn_id",
            )
            if context.get(key) not in {None, ""}
        }
        logging.getLogger("hermes.audit.internal_hooks").info(
            "internal_hook_trace %s",
            json.dumps(
                {"trace": trace, "context": allowed_context},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    except Exception:
        logger.debug("internal hook audit logging failed", exc_info=True)


def _validate_point(point: str) -> None:
    if point not in HOOK_POINTS:
        raise ValueError(f"unsupported hook point: {point}")


def _contract_error(
    contract: _HookContract,
    payload: Any,
    context: Mapping[str, Any],
) -> str:
    if not contract.payload_validator(payload):
        return f"hook payload must be a {contract.payload_name}"
    for field_name, expected in contract.required_context.items():
        if field_name not in context:
            return f"hook context is missing {field_name}"
        value = context[field_name]
        if expected == _INTEGER_FIELDS and not _is_plain_int(value):
            return f"hook context {field_name} must be int"
        if not isinstance(value, expected):
            names = ", ".join(item.__name__ for item in expected)
            return f"hook context {field_name} must be {names}"
    for field_name, expected in contract.optional_context.items():
        if field_name in context and not isinstance(context[field_name], expected):
            names = ", ".join(item.__name__ for item in expected)
            return f"hook context {field_name} must be {names}"
    return ""


def _contract_trace(point: str, error: str) -> HookTrace:
    return {
        "point": point,
        "name": "<contract>",
        "source": "hermes.internal_hooks",
        "version": "1",
        "failure_policy": "fail_closed",
        "status": "invalid_input",
        "error": error[:1000],
        "duration_ms": 0,
        "invocation_id": uuid.uuid4().hex,
    }


def _make_trace(
    point: str,
    hook: InternalHook,
    invocation_id: str,
    status: str,
    error: str,
    started: float,
) -> HookTrace:
    try:
        safe_error = redact_sensitive_text(str(error or ""))[:1000]
    except Exception:
        safe_error = type(error).__name__ if error else ""
    return {
        "point": point,
        "name": hook.name,
        "source": hook.source,
        "version": hook.version,
        "failure_policy": hook.failure_policy,
        "status": status,
        "error": safe_error,
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
        "invocation_id": invocation_id,
    }


def _begin_invocation(
    point: str,
    hook: InternalHook,
    invocation_id: str,
) -> str:
    now = time.monotonic()
    with _LOCK:
        state = _RUNTIME[(point, hook.name)]
        if state.timed_out_invocation_id or state.recover_after > now:
            return "circuit_open"
        if state.active_invocation_id:
            return "busy"
        state.active_invocation_id = invocation_id
        return ""


def _abandon_invocation(point: str, hook: InternalHook, invocation_id: str) -> None:
    with _LOCK:
        state = _RUNTIME[(point, hook.name)]
        if state.active_invocation_id == invocation_id:
            state.active_invocation_id = ""


def _mark_timeout(point: str, hook: InternalHook, invocation_id: str) -> None:
    now = time.monotonic()
    with _LOCK:
        state = _RUNTIME[(point, hook.name)]
        if state.active_invocation_id == invocation_id:
            state.timed_out_invocation_id = invocation_id
        else:
            state.recover_after = max(
                state.recover_after,
                now + hook.circuit_breaker_seconds,
            )


def _complete_worker(point: str, hook: InternalHook, invocation_id: str) -> None:
    now = time.monotonic()
    with _LOCK:
        state = _RUNTIME.get((point, hook.name))
        if state is None:
            return
        if state.active_invocation_id == invocation_id:
            state.active_invocation_id = ""
        if state.timed_out_invocation_id == invocation_id:
            state.timed_out_invocation_id = ""
            state.recover_after = max(
                state.recover_after,
                now + hook.circuit_breaker_seconds,
            )


def _finish_trace(
    point: str,
    hook: InternalHook,
    trace: HookTrace,
    *,
    successful: bool,
) -> None:
    now = time.monotonic()
    with _LOCK:
        state = _RUNTIME[(point, hook.name)]
        status = trace["status"]
        state.counts[status] += 1
        state.last_status = status
        state.last_error = trace["error"]
        state.last_duration_ms = trace["duration_ms"]
        state.last_finished_at = time.time()
        if successful:
            state.consecutive_failures = 0
            state.recover_after = 0.0
        else:
            state.consecutive_failures += 1
            if (
                status not in {"timeout", "circuit_open", "busy"}
                and state.consecutive_failures >= hook.failure_threshold
            ):
                state.recover_after = max(
                    state.recover_after,
                    now + hook.circuit_breaker_seconds,
                )


# Seal before dashboard/plugin discovery can import arbitrary extension code.
bootstrap_internal_hooks()
