"""Versioned, append-only protocol for hosted conversation lifecycle events.

The protocol is intentionally independent from FastAPI and the collaboration
plugin.  Producers append JSON-shaped events to an account-scoped conversation
record; transports may replay them by cursor over SSE or polling.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
import time
from typing import Any, Iterable, Mapping, MutableMapping
import uuid

from hermes_runtime.composability.lifecycle import (
    assert_lifecycle_transition,
    normalize_lifecycle_state,
)


SCHEMA_VERSION = "hermes.hosted-event.v1"
MAX_RETAINED_EVENTS = 20_000
RUNTIME_FIELDS = frozenset(
    {
        "component_id",
        "parent_component_id",
        "provider_refs",
        "dependency_state",
        "lifecycle_state",
        "effect_scope_id",
        "plan_node_id",
        "artifact_refs",
        "contract_revision",
        "policy_snapshot_hash",
    }
)
_PENDING_PERSISTENCE_HOOKS = "_hosted_event_persistence_pending"
_PERSISTENCE_HOOK_OUTBOX = "_hosted_event_persistence_outbox"
_PERSISTENCE_HOOK_ACKS = "_hosted_event_persistence_acks"
_ACCOUNT_DELETION_TOMBSTONES = "account_deletion_tombstones"

EVENT_TYPES = frozenset(
    {
        "agent.started",
        "agent.settled",
        "gateway.ready",
        "skin.changed",
        "reaction",
        "turn.started",
        "turn.accepted",
        "turn.completed",
        "manager.started",
        "manager.delta",
        "manager.plan",
        "worker.queued",
        "worker.started",
        "worker.completed",
        "worker.failed",
        "assistant.delta",
        "message.started",
        "message.delta",
        "message.interim",
        "message.completed",
        "thinking.started",
        "thinking.delta",
        "thinking.completed",
        "tool.started",
        "tool.delta",
        "tool.progress",
        "tool.completed",
        "tool.failed",
        "subagent.started",
        "subagent.queued",
        "subagent.progress",
        "subagent.completed",
        "subagent.failed",
        "command.started",
        "command.output",
        "command.completed",
        "command.failed",
        "connection.retry_scheduled",
        "connection.retry_started",
        "connection.retry_finished",
        "notification.show",
        "notification.clear",
        "billing.step_up.verification",
        "voice.status",
        "voice.transcript",
        "wake.detected",
        "dashboard.new_session_requested",
        "browser.progress",
        "gateway.stderr",
        "gateway.start_timeout",
        "gateway.protocol_error",
        "moa.reference",
        "moa.aggregating",
        "moa.progress",
        "moa.phase",
        "clarify.request",
        "approval.request",
        "sudo.request",
        "secret.request",
        "sudo.expire",
        "secret.expire",
        "background.complete",
        "intervention.queued",
        "intervention.claimed",
        "intervention.replied",
        "intervention.completed",
        "role.handoff",
        "role.rework_requested",
        "turn.cancel_requested",
        "turn.cancelled",
        "turn.failed",
        "awaiting.choice",
        "component.declared",
        "component.waiting",
        "component.activating",
        "component.active",
        "component.quiescing",
        "component.leaving",
        "component.unloading",
        "component.recovering",
        "component.failed",
        "component.completed",
        "provider.registered",
        "provider.health_changed",
        "provider.draining",
        "provider.removed",
        "dependency.waiting",
        "dependency.satisfied",
        "dependency.lost",
        "turn.plan_created",
        "turn.node_ready",
        "turn.node_blocked",
        "turn.node_completed",
    }
)

TERMINAL_EVENT_TYPES = frozenset(
    {
        "agent.settled",
        "turn.completed",
        "turn.cancelled",
        "turn.failed",
        "message.completed",
        "thinking.completed",
        "tool.completed",
        "tool.failed",
        "subagent.completed",
        "subagent.failed",
        "command.completed",
        "command.failed",
        "intervention.completed",
        "component.failed",
        "component.completed",
        "provider.removed",
        "turn.node_completed",
    }
)

PROGRESS_EVENT_TYPES = frozenset(
    {
        "message.delta",
        "message.interim",
        "thinking.delta",
        "tool.progress",
        "subagent.progress",
        "command.output",
        "browser.progress",
        "moa.reference",
        "moa.progress",
        "moa.phase",
    }
)


class HostedEventProtocolError(ValueError):
    """Raised when a producer violates the hosted-event contract."""


@dataclass(frozen=True)
class AppendResult:
    event: dict[str, Any]
    appended: bool
    reason: str = ""


@dataclass(frozen=True)
class HostedEventPage:
    events: list[dict[str, Any]]
    requested_cursor: int
    min_cursor: int
    next_cursor: int
    has_gap: bool
    reset_cursor: bool
    reset_reason: str = ""


@dataclass(frozen=True)
class PersistenceHookDispatchResult:
    """A post-commit delivery attempt against a durable outbox snapshot."""

    state: dict[str, Any]
    traces: list[dict[str, Any]]
    attempted_event_ids: tuple[str, ...]
    acknowledged_event_ids: tuple[str, ...]


def normalize_event_type(value: Any) -> str:
    event_type = str(value or "").strip().lower()
    if event_type not in EVENT_TYPES:
        raise HostedEventProtocolError(f"unsupported hosted event type: {event_type!r}")
    return event_type


def normalize_account_generation(value: Any) -> str:
    generation = str(value or "").strip().replace("\x00", "")
    return generation[:256] or "legacy"


def stable_idempotency_key(
    *,
    conversation_id: str,
    turn_id: str,
    role_stage: str,
    event_type: str,
    sequence_hint: Any = "",
    entity_id: str = "",
) -> str:
    material = "\0".join(
        (
            str(conversation_id or ""),
            str(turn_id or ""),
            str(role_stage or ""),
            str(event_type or ""),
            str(sequence_hint or ""),
            str(entity_id or ""),
        )
    )
    return "hosted:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def append_hosted_event(
    conversation: MutableMapping[str, Any],
    *,
    conversation_id: str,
    turn_id: str,
    role_stage: str,
    event_type: str,
    payload: MutableMapping[str, Any] | None = None,
    account_generation: Any = "legacy",
    idempotency_key: str = "",
    occurred_at: int | None = None,
    entity_id: str = "",
    component_id: str = "",
    parent_component_id: str = "",
    provider_refs: Iterable[str] | None = None,
    dependency_state: Mapping[str, Any] | None = None,
    lifecycle_state: str = "",
    effect_scope_id: str = "",
    plan_node_id: str = "",
    artifact_refs: Iterable[str] | None = None,
    contract_revision: str = "",
    policy_snapshot_hash: str = "",
    assume_current_indexes: bool = False,
) -> AppendResult:
    """Append one lifecycle event with idempotency and terminal CAS guards."""

    normalized_type = normalize_event_type(event_type)
    normalized_conversation_id = str(conversation_id or "").strip()
    normalized_turn_id = str(turn_id or "").strip()
    normalized_role_stage = str(role_stage or "").strip() or "chat"
    if not normalized_conversation_id:
        raise HostedEventProtocolError("conversation_id is required")
    if not normalized_turn_id:
        raise HostedEventProtocolError("turn_id is required")
    normalized_payload = _json_copy(_sanitize_event_value(payload or {}))
    normalized_entity_id = str(
        entity_id or _payload_entity_id(normalized_payload)
    ).strip()[:512]
    normalized_component_id = str(component_id or "").strip()[:512]
    normalized_lifecycle_state = str(lifecycle_state or "").strip().lower()[:128]
    if normalized_lifecycle_state:
        try:
            normalize_lifecycle_state(normalized_lifecycle_state)
        except ValueError as exc:
            raise HostedEventProtocolError(str(exc)) from exc

    stored_events = conversation.get("hosted_events")
    events = stored_events if isinstance(stored_events, list) else []
    cursor = _non_negative_int(conversation.get("hosted_event_cursor"))
    stored_sequences = conversation.get("hosted_event_sequences")
    sequences = stored_sequences if isinstance(stored_sequences, dict) else {}
    if not assume_current_indexes:
        _rebuild_retained_event_indexes(events, sequences=sequences)
    sequence_scope = f"{normalized_turn_id}:{normalized_role_stage}"
    sequence = _non_negative_int(sequences.get(sequence_scope)) + 1
    key = str(idempotency_key or "").strip()[:512]
    if not key:
        if normalized_type in PROGRESS_EVENT_TYPES:
            raise HostedEventProtocolError(
                f"{normalized_type} requires an explicit idempotency_key"
            )
        key = stable_idempotency_key(
            conversation_id=normalized_conversation_id,
            turn_id=normalized_turn_id,
            role_stage=normalized_role_stage,
            event_type=normalized_type,
            sequence_hint="terminal-or-boundary",
            entity_id=normalized_entity_id,
        )

    if not assume_current_indexes:
        for existing in reversed(events):
            if not isinstance(existing, dict):
                continue
            if str(existing.get("idempotency_key") or "") == key:
                return AppendResult(deepcopy(existing), False, "duplicate")

    stored_terminal_scopes = conversation.get("hosted_event_terminals")
    terminal_scopes = (
        stored_terminal_scopes if isinstance(stored_terminal_scopes, dict) else {}
    )
    if not assume_current_indexes:
        _rebuild_retained_event_indexes(events, terminal_scopes=terminal_scopes)
    turn_terminal_scope = f"turn:{normalized_turn_id}"
    prior_turn_terminal = str(terminal_scopes.get(turn_terminal_scope) or "")
    if prior_turn_terminal:
        return AppendResult(
            {
                "event_type": normalized_type,
                "turn_id": normalized_turn_id,
                "role_stage": normalized_role_stage,
            },
            False,
            f"turn_terminal:{prior_turn_terminal}",
        )
    entity_scope = _entity_scope(
        normalized_turn_id,
        normalized_role_stage,
        normalized_type,
        normalized_entity_id,
    )
    prior_terminal = str(terminal_scopes.get(entity_scope) or "")
    if prior_terminal:
        return AppendResult(
            {
                "event_type": normalized_type,
                "turn_id": normalized_turn_id,
                "role_stage": normalized_role_stage,
            },
            False,
            f"terminal:{prior_terminal}",
        )

    # Idempotent replay and terminal CAS checks run first. A delayed duplicate
    # from before completion is not a new state transition and must remain a
    # no-op instead of being rejected as completed -> active.
    if normalized_component_id and normalized_lifecycle_state:
        previous_lifecycle = _latest_component_lifecycle(
            events,
            turn_id=normalized_turn_id,
            component_id=normalized_component_id,
        )
        if previous_lifecycle is not None:
            try:
                assert_lifecycle_transition(previous_lifecycle, normalized_lifecycle_state)
            except ValueError as exc:
                raise HostedEventProtocolError(str(exc)) from exc

    next_cursor = cursor + 1
    timestamp = int(occurred_at if occurred_at is not None else time.time() * 1000)
    event = {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "cursor": next_cursor,
        "account_generation": normalize_account_generation(account_generation),
        "conversation_id": normalized_conversation_id,
        "turn_id": normalized_turn_id,
        "role_stage": normalized_role_stage,
        "event_type": normalized_type,
        "entity_id": normalized_entity_id,
        "sequence": sequence,
        "occurred_at": max(0, timestamp),
        "idempotency_key": key,
        "payload": normalized_payload,
        "schema_version": SCHEMA_VERSION,
    }
    # Runtime metadata is optional so v1 producers remain compatible. When
    # present, it is typed at the envelope boundary and becomes the canonical
    # source for Fiber/provider reducers; clients must not infer it from a
    # display-oriented role_stage string.
    normalized_provider_refs = _sanitize_runtime_refs(provider_refs or (), "provider")
    normalized_artifact_refs = _sanitize_runtime_refs(artifact_refs or (), "artifact")
    runtime_metadata = {
        "component_id": normalized_component_id,
        "parent_component_id": str(parent_component_id or "").strip()[:512],
        "provider_refs": list(normalized_provider_refs),
        "dependency_state": _json_copy(_sanitize_event_value(dependency_state or {})),
        "lifecycle_state": normalized_lifecycle_state,
        "effect_scope_id": str(effect_scope_id or "").strip()[:512],
        "plan_node_id": str(plan_node_id or "").strip()[:512],
        "artifact_refs": list(normalized_artifact_refs),
        "contract_revision": str(contract_revision or "").strip()[:256],
        "policy_snapshot_hash": str(policy_snapshot_hash or "").strip()[:256],
    }
    if any(runtime_metadata.values()):
        event["runtime"] = runtime_metadata
    owner_id = str(conversation.get("owner_id") or "").strip().lower()
    if owner_id:
        # This metadata is intentionally part of the event envelope so a
        # legacy outbox can still be mapped to its deletion boundary after the
        # owning conversation has been removed.
        event["owner_id"] = owner_id
    # Run trusted guards before mutating the caller's state. A fail-closed hook
    # must leave the conversation byte-for-byte unchanged so a later save cannot
    # accidentally persist a rejected event.
    from hermes_services.internal_hooks import has_internal_hooks, run_internal_hooks

    if has_internal_hooks("before_hosted_event_commit"):
        hook_result = run_internal_hooks(
            "before_hosted_event_commit",
            deepcopy(event),
            conversation_id=normalized_conversation_id,
            turn_id=normalized_turn_id,
            role_stage=normalized_role_stage,
        )
        hook_trace = hook_result.trace
        if hook_trace:
            event["hook_trace"] = hook_trace
            from hermes_services.session_entries import append_session_entry

            append_session_entry(
                conversation,
                entry_type="hook_trace",
                idempotency_key=f"hook-trace:{event['event_id']}:before-commit",
                payload={
                    "event_id": event["event_id"],
                    "point": "before_hosted_event_commit",
                    "trace": deepcopy(hook_trace),
                },
                occurred_at=event["occurred_at"],
            )
    if not isinstance(stored_events, list):
        conversation["hosted_events"] = events
    if not isinstance(stored_sequences, dict):
        conversation["hosted_event_sequences"] = sequences
    if not isinstance(stored_terminal_scopes, dict):
        conversation["hosted_event_terminals"] = terminal_scopes
    events.append(event)
    if len(events) > MAX_RETAINED_EVENTS:
        del events[: len(events) - MAX_RETAINED_EVENTS]
    conversation["hosted_event_min_cursor"] = (
        _non_negative_int(events[0].get("cursor")) if events else next_cursor + 1
    )
    conversation["hosted_event_cursor"] = next_cursor
    conversation["event_updated_at"] = event["occurred_at"]
    sequences[sequence_scope] = sequence
    if normalized_type in TERMINAL_EVENT_TYPES:
        terminal_scopes[entity_scope] = normalized_type
    if normalized_type in {"turn.completed", "turn.cancelled", "turn.failed"}:
        terminal_scopes[turn_terminal_scope] = normalized_type
    # Persistence observers are staged on the in-memory transaction. The state
    # store promotes these records into the root durable outbox in the same
    # atomic document write as the event itself.
    if has_internal_hooks("after_hosted_event_persistence"):
        pending = conversation.get(_PENDING_PERSISTENCE_HOOKS)
        if not isinstance(pending, list):
            pending = []
            conversation[_PENDING_PERSISTENCE_HOOKS] = pending
        pending.append(deepcopy(event))
    return AppendResult(deepcopy(event), True)


def state_with_persistence_hook_outbox(
    state: MutableMapping[str, Any],
) -> dict[str, Any]:
    """Promote staged callbacks into an event-ID-keyed durable outbox."""

    persisted = deepcopy(dict(state))
    raw_outbox = persisted.get(_PERSISTENCE_HOOK_OUTBOX)
    outbox = deepcopy(raw_outbox) if isinstance(raw_outbox, dict) else {}
    raw_acks = persisted.get(_PERSISTENCE_HOOK_ACKS)
    acknowledgements = deepcopy(raw_acks) if isinstance(raw_acks, dict) else {}
    for acknowledged_event_id in acknowledgements:
        outbox.pop(acknowledged_event_id, None)
    for owner in _mappings_with_pending_persistence_hooks(persisted):
        raw_pending = owner.get(_PENDING_PERSISTENCE_HOOKS)
        pending = list(raw_pending) if isinstance(raw_pending, list) else []
        for snapshot in pending:
            if not isinstance(snapshot, dict):
                continue
            event_id = str(snapshot.get("event_id") or "").strip()
            if not event_id:
                raise HostedEventProtocolError(
                    "post-persistence hook event is missing event_id"
                )
            if event_id in acknowledgements:
                continue
            outbox.setdefault(
                event_id,
                {
                    "delivery_id": f"after_hosted_event_persistence:{event_id}",
                    "event": deepcopy(snapshot),
                    # Keep the deletion boundary beside the delivery record,
                    # rather than relying on the event body alone.  This lets
                    # account cleanup revoke a queued/ACKed delivery even when
                    # the conversation record is removed first.
                    "owner_id": str(
                        owner.get("owner_id") or snapshot.get("owner_id") or ""
                    ).strip().lower(),
                    "account_generation": normalize_account_generation(
                        snapshot.get("account_generation")
                    ),
                    "attempts": 0,
                    "created_at": _non_negative_int(snapshot.get("occurred_at")),
                },
            )
    if outbox:
        persisted[_PERSISTENCE_HOOK_OUTBOX] = outbox
    else:
        persisted.pop(_PERSISTENCE_HOOK_OUTBOX, None)
    if acknowledgements:
        persisted[_PERSISTENCE_HOOK_ACKS] = acknowledgements
    else:
        persisted.pop(_PERSISTENCE_HOOK_ACKS, None)
    _remove_pending_persistence_hooks(persisted)
    _purge_deleted_persistence_hook_work(persisted)
    return persisted


def preserve_persistence_hook_outbox(
    source: MutableMapping[str, Any],
    target: MutableMapping[str, Any],
) -> None:
    """Keep undelivered work when a state loader normalizes root fields."""

    for key in (_PERSISTENCE_HOOK_OUTBOX, _PERSISTENCE_HOOK_ACKS):
        source_values = source.get(key)
        if not isinstance(source_values, dict) or not source_values:
            continue
        target_values = target.get(key)
        merged = deepcopy(target_values) if isinstance(target_values, dict) else {}
        for event_id, value in source_values.items():
            merged.setdefault(event_id, deepcopy(value))
        target[key] = merged
    _purge_deleted_persistence_hook_work(target)


def dispatch_persisted_hosted_event_hooks(
    state: MutableMapping[str, Any],
    *,
    store_path: str,
) -> PersistenceHookDispatchResult:
    """Attempt durable observer deliveries without mutating the input state.

    A successful delivery is removed from the returned outbox so the caller can
    persist an ACK. Failed, unavailable, or interrupted deliveries remain for a
    later process. Delivery IDs are stable across retries and are supplied to
    callbacks as idempotency keys.
    """

    from hermes_services.internal_hooks import has_internal_hooks, run_internal_hooks

    working = deepcopy(dict(state))
    # A deletion tombstone is authoritative even when this is a restart
    # recovery read.  Purge before checking hook availability so an empty
    # registry cannot accidentally preserve old-account deliveries.
    _purge_deleted_persistence_hook_work(working)
    all_traces: list[dict[str, Any]] = []
    attempted: list[str] = []
    acknowledged: list[str] = []
    raw_outbox = working.get(_PERSISTENCE_HOOK_OUTBOX)
    outbox = raw_outbox if isinstance(raw_outbox, dict) else {}
    raw_acks = working.get(_PERSISTENCE_HOOK_ACKS)
    acknowledgements = raw_acks if isinstance(raw_acks, dict) else {}
    if not has_internal_hooks("after_hosted_event_persistence"):
        return PersistenceHookDispatchResult(working, [], (), ())
    for event_id in sorted(tuple(outbox)):
        entry = outbox.get(event_id)
        if not isinstance(entry, dict):
            continue
        snapshot = entry.get("event")
        if not isinstance(snapshot, dict):
            continue
        delivery_id = str(entry.get("delivery_id") or "").strip()
        if not delivery_id:
            delivery_id = f"after_hosted_event_persistence:{event_id}"
            entry["delivery_id"] = delivery_id
        attempts = _non_negative_int(entry.get("attempts")) + 1
        entry["attempts"] = attempts
        entry["last_attempt_at"] = int(time.time() * 1000)
        attempted.append(event_id)
        try:
            hook_result = run_internal_hooks(
                "after_hosted_event_persistence",
                deepcopy(snapshot),
                conversation_id=str(snapshot.get("conversation_id") or ""),
                turn_id=str(snapshot.get("turn_id") or ""),
                role_stage=str(snapshot.get("role_stage") or ""),
                cursor=int(snapshot.get("cursor") or 0),
                store_path=str(store_path or ""),
                event_id=event_id,
                idempotency_key=delivery_id,
                delivery_id=delivery_id,
                attempt=attempts,
                owner_id=str(entry.get("owner_id") or ""),
                account_generation=str(
                    entry.get("account_generation")
                    or snapshot.get("account_generation")
                    or ""
                ),
            )
            trace = hook_result.trace
        except BaseException as exc:
            trace = [dict(item) for item in getattr(exc, "trace", ())]
            if not trace:
                trace = [
                    {
                        "point": "after_hosted_event_persistence",
                        "name": "<dispatch>",
                        "source": "hermes.hosted_event_protocol",
                        "version": "1",
                        "failure_policy": "fail_open",
                        "status": "failed",
                        "error": str(exc)[:1000],
                        "duration_ms": 0,
                        "invocation_id": delivery_id,
                    }
                ]
        entry["last_trace"] = deepcopy(trace)
        if trace:
            all_traces.extend(trace)
            _attach_persistence_trace(working, snapshot, trace)
        if trace and all(item.get("status") == "completed" for item in trace):
            outbox.pop(event_id, None)
            acknowledgements[event_id] = {
                "delivery_id": delivery_id,
                "owner_id": str(entry.get("owner_id") or "").strip().lower(),
                "account_generation": normalize_account_generation(
                    entry.get("account_generation")
                    or snapshot.get("account_generation")
                ),
                "acknowledged_at": int(time.time() * 1000),
                "trace": deepcopy(trace),
            }
            acknowledged.append(event_id)
    if not outbox:
        working.pop(_PERSISTENCE_HOOK_OUTBOX, None)
    if acknowledgements:
        working[_PERSISTENCE_HOOK_ACKS] = acknowledgements
    return PersistenceHookDispatchResult(
        working,
        all_traces,
        tuple(attempted),
        tuple(acknowledged),
    )


def _persistence_hook_tombstones(
    state: Mapping[str, Any],
) -> dict[str, set[str]]:
    """Normalize collaboration account tombstones for delivery filtering."""

    raw = state.get(_ACCOUNT_DELETION_TOMBSTONES)
    if not isinstance(raw, dict):
        return {}
    result: dict[str, set[str]] = {}
    for owner, generations in raw.items():
        normalized_owner = str(owner or "").strip().lower()
        if not normalized_owner or not isinstance(generations, dict):
            continue
        values = {
            str(generation or "").strip()
            for generation in generations
            if str(generation or "").strip()
        }
        if values:
            result[normalized_owner] = values
    return result


def _hosted_event_boundary(
    state: Mapping[str, Any],
    event_id: str,
) -> tuple[str, str]:
    """Find an owner boundary for legacy outbox records lacking metadata."""

    wanted = str(event_id or "").strip()
    if not wanted:
        return "", ""

    def walk(value: Any) -> tuple[str, str]:
        if isinstance(value, dict):
            owner = str(value.get("owner_id") or "").strip().lower()
            events = value.get("hosted_events")
            if owner and isinstance(events, list):
                for event in events:
                    if (
                        isinstance(event, dict)
                        and str(event.get("event_id") or "").strip() == wanted
                    ):
                        return owner, normalize_account_generation(
                            event.get("account_generation")
                        )
            for child in value.values():
                found = walk(child)
                if found != ("", ""):
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found != ("", ""):
                    return found
        return "", ""

    return walk(state)


def _persistence_hook_boundary(
    state: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    event_id: str = "",
) -> tuple[str, str]:
    event = record.get("event")
    event_mapping = event if isinstance(event, dict) else {}
    owner = str(
        record.get("owner_id")
        or event_mapping.get("owner_id")
        or ""
    ).strip().lower()
    generation = str(
        record.get("account_generation")
        or event_mapping.get("account_generation")
        or ""
    ).strip()
    if (not owner or not generation) and event_id:
        found_owner, found_generation = _hosted_event_boundary(state, event_id)
        owner = owner or found_owner
        generation = generation or found_generation
    return owner, generation or "legacy"


def _persistence_boundary_is_deleted(
    tombstones: Mapping[str, set[str]],
    owner: str,
    generation: str,
) -> bool:
    if not owner:
        return False
    generations = tombstones.get(owner, set())
    return "*" in generations or generation in generations


def _purge_deleted_persistence_hook_work(state: MutableMapping[str, Any]) -> None:
    """Remove queued and acknowledged observer work for deleted boundaries."""

    tombstones = _persistence_hook_tombstones(state)
    if not tombstones:
        return
    raw_outbox = state.get(_PERSISTENCE_HOOK_OUTBOX)
    outbox = raw_outbox if isinstance(raw_outbox, dict) else {}
    raw_acks = state.get(_PERSISTENCE_HOOK_ACKS)
    acks = raw_acks if isinstance(raw_acks, dict) else {}
    deleted_ids: set[str] = set()
    for event_id, entry in list(outbox.items()):
        if not isinstance(entry, dict):
            continue
        owner, generation = _persistence_hook_boundary(
            state, entry, event_id=str(event_id)
        )
        # Unknown legacy ownership is fail-closed once any tombstone exists:
        # it cannot be proven safe to deliver after an account purge.
        if not owner or _persistence_boundary_is_deleted(
            tombstones, owner, generation
        ):
            outbox.pop(event_id, None)
            deleted_ids.add(str(event_id))
    for event_id, ack in list(acks.items()):
        if not isinstance(ack, dict):
            continue
        owner, generation = _persistence_hook_boundary(
            state, ack, event_id=str(event_id)
        )
        if (
            str(event_id) in deleted_ids
            or _persistence_boundary_is_deleted(tombstones, owner, generation)
        ):
            acks.pop(event_id, None)
    if outbox:
        state[_PERSISTENCE_HOOK_OUTBOX] = outbox
    else:
        state.pop(_PERSISTENCE_HOOK_OUTBOX, None)
    if acks:
        state[_PERSISTENCE_HOOK_ACKS] = acks
    else:
        state.pop(_PERSISTENCE_HOOK_ACKS, None)


def drop_account_persistence_hook_work(
    state: MutableMapping[str, Any],
    *,
    owner_id: str,
    account_generation: str,
) -> None:
    """Synchronously revoke queued/ACKed observer work for one owner epoch."""

    owner = str(owner_id or "").strip().lower()
    generation = str(account_generation or "").strip()
    if not owner or not generation:
        raise ValueError("owner_id and account_generation are required")
    raw_outbox = state.get(_PERSISTENCE_HOOK_OUTBOX)
    outbox = raw_outbox if isinstance(raw_outbox, dict) else {}
    raw_acks = state.get(_PERSISTENCE_HOOK_ACKS)
    acks = raw_acks if isinstance(raw_acks, dict) else {}
    removed: set[str] = set()
    for event_id, entry in list(outbox.items()):
        if not isinstance(entry, dict):
            continue
        entry_owner, entry_generation = _persistence_hook_boundary(
            state, entry, event_id=str(event_id)
        )
        if (
            not entry_owner
            or entry_owner == owner
            and entry_generation in {generation, "legacy"}
        ):
            outbox.pop(event_id, None)
            removed.add(str(event_id))
    for event_id, ack in list(acks.items()):
        if not isinstance(ack, dict):
            continue
        ack_owner, ack_generation = _persistence_hook_boundary(
            state, ack, event_id=str(event_id)
        )
        if (
            str(event_id) in removed
            or not ack_owner
            or ack_owner == owner
            and ack_generation in {generation, "legacy"}
        ):
            acks.pop(event_id, None)
    if outbox:
        state[_PERSISTENCE_HOOK_OUTBOX] = outbox
    else:
        state.pop(_PERSISTENCE_HOOK_OUTBOX, None)
    if acks:
        state[_PERSISTENCE_HOOK_ACKS] = acks
    else:
        state.pop(_PERSISTENCE_HOOK_ACKS, None)


def _remove_pending_persistence_hooks(value: Any) -> None:
    if isinstance(value, dict):
        value.pop(_PENDING_PERSISTENCE_HOOKS, None)
        for child in value.values():
            _remove_pending_persistence_hooks(child)
    elif isinstance(value, list):
        for child in value:
            _remove_pending_persistence_hooks(child)


def _mappings_with_pending_persistence_hooks(value: Any) -> list[dict[str, Any]]:
    owners: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get(_PENDING_PERSISTENCE_HOOKS), list):
            owners.append(value)
        for child in value.values():
            owners.extend(_mappings_with_pending_persistence_hooks(child))
    elif isinstance(value, list):
        for child in value:
            owners.extend(_mappings_with_pending_persistence_hooks(child))
    return owners


def _attach_persistence_trace(
    owner: MutableMapping[str, Any],
    snapshot: MutableMapping[str, Any],
    trace: list[dict[str, Any]],
) -> None:
    event_id = str(snapshot.get("event_id") or "")
    if not event_id:
        return
    for candidate in _mappings_with_hosted_events(owner):
        events = candidate.get("hosted_events")
        if not isinstance(events, list):
            continue
        for event in reversed(events):
            if (
                isinstance(event, dict)
                and str(event.get("event_id") or "") == event_id
            ):
                event["persistence_hook_trace"] = deepcopy(trace)
                from hermes_services.session_entries import append_session_entry

                append_session_entry(
                    candidate,
                    entry_type="hook_trace",
                    idempotency_key=(
                        f"hook-trace:{event_id}:after-persistence"
                    ),
                    payload={
                        "event_id": event_id,
                        "point": "after_hosted_event_persistence",
                        "trace": deepcopy(trace),
                    },
                    occurred_at=int(time.time() * 1000),
                )
                return


def _mappings_with_hosted_events(value: Any) -> list[dict[str, Any]]:
    owners: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get("hosted_events"), list):
            owners.append(value)
        for child in value.values():
            owners.extend(_mappings_with_hosted_events(child))
    elif isinstance(value, list):
        for child in value:
            owners.extend(_mappings_with_hosted_events(child))
    return owners


def hosted_events_after(
    conversation: MutableMapping[str, Any],
    cursor: int,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return events strictly after cursor in authoritative order."""

    requested = _non_negative_int(cursor)
    bounded_limit = min(2_000, max(1, int(limit)))
    events = conversation.get("hosted_events")
    if not isinstance(events, list):
        return []
    selected = [
        deepcopy(event)
        for event in events
        if isinstance(event, dict) and _non_negative_int(event.get("cursor")) > requested
    ]
    selected.sort(key=lambda item: (_non_negative_int(item.get("cursor")), _non_negative_int(item.get("sequence"))))
    return selected[:bounded_limit]


def hosted_event_page(
    conversation: MutableMapping[str, Any],
    cursor: int,
    *,
    limit: int = 500,
) -> HostedEventPage:
    requested = _non_negative_int(cursor)
    events = hosted_events_after(conversation, requested, limit=limit)
    raw = conversation.get("hosted_events")
    retained = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    min_cursor = (
        _non_negative_int(retained[0].get("cursor"))
        if retained
        else _non_negative_int(conversation.get("hosted_event_cursor")) + 1
    )
    authoritative_cursor = _non_negative_int(
        conversation.get("hosted_event_cursor")
    )
    reset_cursor = requested > authoritative_cursor
    next_cursor = (
        authoritative_cursor
        if reset_cursor
        else (
            _non_negative_int(events[-1].get("cursor"))
            if events
            else authoritative_cursor
        )
    )
    return HostedEventPage(
        events=events,
        requested_cursor=requested,
        min_cursor=min_cursor,
        next_cursor=next_cursor,
        has_gap=bool(reset_cursor or (retained and requested + 1 < min_cursor)),
        reset_cursor=reset_cursor,
        reset_reason="future_cursor" if reset_cursor else "",
    )


def validate_event_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HostedEventProtocolError("hosted event must be an object")
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
    missing = sorted(required.difference(value))
    if missing:
        raise HostedEventProtocolError(f"hosted event is missing: {', '.join(missing)}")
    normalize_event_type(value.get("event_type"))
    if str(value.get("schema_version") or "") != SCHEMA_VERSION:
        raise HostedEventProtocolError("unsupported hosted event schema version")
    for field in ("cursor", "sequence", "occurred_at"):
        if _non_negative_int(value.get(field)) != value.get(field):
            raise HostedEventProtocolError(f"{field} must be a non-negative integer")
    if not isinstance(value.get("payload"), dict):
        raise HostedEventProtocolError("payload must be an object")
    if "entity_id" in value and not isinstance(value.get("entity_id"), str):
        raise HostedEventProtocolError("entity_id must be a string")
    runtime = value.get("runtime")
    if runtime is not None:
        if not isinstance(runtime, dict):
            raise HostedEventProtocolError("runtime must be an object")
        unknown_fields = sorted(set(runtime).difference(RUNTIME_FIELDS))
        if unknown_fields:
            raise HostedEventProtocolError(
                "runtime has unsupported field(s): " + ", ".join(unknown_fields)
            )
        string_fields = {
            "component_id",
            "parent_component_id",
            "lifecycle_state",
            "effect_scope_id",
            "plan_node_id",
            "contract_revision",
            "policy_snapshot_hash",
        }
        for field in string_fields:
            if field in runtime and not isinstance(runtime.get(field), str):
                raise HostedEventProtocolError(f"runtime.{field} must be a string")
        if "lifecycle_state" in runtime and runtime.get("lifecycle_state"):
            try:
                normalize_lifecycle_state(runtime.get("lifecycle_state"))
            except ValueError as exc:
                raise HostedEventProtocolError(str(exc)) from exc
        for field in ("provider_refs", "artifact_refs"):
            if field in runtime:
                refs = runtime.get(field)
                if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
                    raise HostedEventProtocolError(f"runtime.{field} must be a string list")
        if "dependency_state" in runtime and not isinstance(runtime.get("dependency_state"), dict):
            raise HostedEventProtocolError("runtime.dependency_state must be an object")
    return _json_copy(value)


def normalize_legacy_profile_event(event: Any) -> tuple[str, dict[str, Any], str]:
    """Map the existing profile-runner stream to the canonical vocabulary."""

    if not isinstance(event, dict):
        raise HostedEventProtocolError("profile event must be an object")
    event_type = str(event.get("type") or "").strip().lower()
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    entity_id = str(
        payload.get("entity_id")
        or payload.get("tool_id")
        or payload.get("child_session_id")
        or payload.get("subagent_id")
        or payload.get("task_id")
        or payload.get("id")
        or ""
    )
    mapping = {
        "request.accepted": "agent.started",
        "session.info": "agent.started",
        "message.start": "message.started",
        "message.delta": "message.delta",
        "message.interim": "message.interim",
        "message.complete": "message.completed",
        "reasoning.delta": "thinking.delta",
        "reasoning.available": "thinking.completed",
        "thinking.delta": "thinking.delta",
        "tool.generating": "tool.progress",
        "tool.progress": "tool.progress",
        "tool.start": "tool.started",
        "tool.complete": "tool.failed" if payload.get("error") else "tool.completed",
        "subagent.start": "subagent.started",
        "subagent.spawn_requested": "subagent.queued",
        "subagent.progress": "subagent.progress",
        "subagent.text": "subagent.progress",
        "subagent.thinking": "subagent.progress",
        "subagent.tool": "subagent.progress",
        "subagent.complete": (
            "subagent.failed"
            if payload.get("error")
            or str(payload.get("status") or "").lower() in {"error", "failed"}
            else "subagent.completed"
        ),
        "connection.retry": "connection.retry_started",
        "error": "turn.failed",
    }
    canonical = mapping.get(event_type)
    if canonical is None:
        canonical = event_type if event_type in EVENT_TYPES else "command.output"
    normalized_payload = _json_copy(payload)
    normalized_payload["source_event_type"] = event_type
    if canonical == "command.output" and event_type != "status.update":
        normalized_payload["unmapped_frontend_event"] = True
    return canonical, normalized_payload, entity_id


def _entity_scope(turn_id: str, role_stage: str, event_type: str, entity_id: str) -> str:
    if event_type.startswith("tool."):
        category = "tool"
    elif event_type.startswith("subagent."):
        category = "subagent"
    elif event_type.startswith("command."):
        category = "command"
    elif event_type.startswith("message."):
        category = "message"
    elif event_type.startswith("thinking."):
        category = "thinking"
    elif event_type.startswith("intervention."):
        category = "intervention"
    elif event_type.startswith("agent."):
        category = "agent"
    elif event_type.startswith("component."):
        category = "component"
    elif event_type.startswith("provider."):
        category = "provider"
    elif event_type.startswith("dependency."):
        category = "dependency"
    elif event_type.startswith("turn.node_"):
        category = "turn_node"
    else:
        category = "turn"
    return f"{turn_id}:{role_stage}:{category}:{entity_id}"


def _rebuild_retained_event_indexes(
    events: Iterable[Any],
    *,
    sequences: MutableMapping[str, Any] | None = None,
    terminal_scopes: MutableMapping[str, Any] | None = None,
) -> None:
    """Repair derived CAS indexes from authoritative retained events."""

    for raw in events:
        if not isinstance(raw, dict):
            continue
        turn_id = str(raw.get("turn_id") or "").strip()
        role_stage = str(raw.get("role_stage") or "").strip() or "chat"
        event_type = str(raw.get("event_type") or "").strip().lower()
        if not turn_id or event_type not in EVENT_TYPES:
            continue
        if sequences is not None:
            scope = f"{turn_id}:{role_stage}"
            sequences[scope] = max(
                _non_negative_int(sequences.get(scope)),
                _non_negative_int(raw.get("sequence")),
            )
        if terminal_scopes is None or event_type not in TERMINAL_EVENT_TYPES:
            continue
        entity_id = _event_entity_id(raw)
        terminal_scopes.setdefault(
            _entity_scope(turn_id, role_stage, event_type, entity_id),
            event_type,
        )
        if event_type in {"turn.completed", "turn.cancelled", "turn.failed"}:
            terminal_scopes.setdefault(f"turn:{turn_id}", event_type)


def _latest_component_lifecycle(
    events: Iterable[Any],
    *,
    turn_id: str,
    component_id: str,
) -> str | None:
    """Find the last structured lifecycle witness for one component."""

    for raw in reversed(list(events)):
        if not isinstance(raw, dict) or str(raw.get("turn_id") or "") != turn_id:
            continue
        runtime = raw.get("runtime")
        if not isinstance(runtime, dict):
            continue
        if str(runtime.get("component_id") or "") != component_id:
            continue
        state = str(runtime.get("lifecycle_state") or "").strip().lower()
        if state:
            return state
    return None


def _payload_entity_id(payload: MutableMapping[str, Any] | dict[str, Any]) -> str:
    for key in (
        "entity_id",
        "tool_id",
        "command_id",
        "message_id",
        "thinking_id",
        "id",
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:512]
    return ""


def _event_entity_id(event: MutableMapping[str, Any] | dict[str, Any]) -> str:
    direct = str(event.get("entity_id") or "").strip()
    if direct:
        return direct[:512]
    raw_payload = event.get("payload")
    payload: dict[str, Any] = (
        dict(raw_payload) if isinstance(raw_payload, dict) else {}
    )
    return _payload_entity_id(payload)


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


_SENSITIVE_KEY_PARTS = (
    "api_key",
    "access_token",
    "refresh_token",
    "auth_token",
    "session_token",
    "password",
    "passwd",
    "secret",
    "credential",
    "private_key",
)
_INLINE_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\|file:)", re.I)


def _sensitive_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _sanitize_event_value(value: Any) -> Any:
    """Redact credential-shaped values at the durable protocol boundary."""

    if isinstance(value, Mapping):
        return {
            str(key)[:256]: (
                "[REDACTED]"
                if _sensitive_key(key)
                else _sanitize_event_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_event_value(item) for item in value]
    if not isinstance(value, str):
        return value
    redacted = _INLINE_BEARER_RE.sub(r"\1 [REDACTED]", value)
    return _INLINE_SECRET_RE.sub(r"\1=[REDACTED]", redacted)


def _sanitize_runtime_ref(value: Any, kind: str) -> str:
    """Keep stable IDs while replacing host paths with opaque witnesses."""

    text = str(_sanitize_event_value(str(value or "")) or "").strip()
    if not text:
        return ""
    if _ABSOLUTE_PATH_RE.match(text):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{kind}:{digest}"
    return text[:512]


def _sanitize_runtime_refs(values: Iterable[Any], kind: str) -> tuple[str, ...]:
    refs: list[str] = []
    for value in values:
        ref = _sanitize_runtime_ref(value, kind)
        if ref:
            refs.append(ref)
        if len(refs) >= 100:
            break
    return tuple(refs)


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise HostedEventProtocolError("hosted event payload must be JSON serializable") from exc
