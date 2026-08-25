"""Shared low-latency worker/event protocol primitives.

The durable collaboration queue remains authoritative.  This module only
normalizes the envelope used by WebSocket/SSE transports and provides the
small pieces needed to reject duplicates, stale leases, and out-of-order
frames before they reach a worker or UI reducer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
import uuid
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "hermes.low-latency.v1"
MAX_EVENT_TYPE_LENGTH = 96
MAX_NODE_ID_LENGTH = 128
MAX_REQUEST_ID_LENGTH = 256
MAX_SAFE_INTEGER = 2**53 - 1


class ProtocolError(ValueError):
    """Raised when a transport envelope violates the protocol contract."""


def _text(value: Any, limit: int) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def protocol_int(value: Any, field: str, *, default: int | None = None) -> int:
    """Parse transport integers without Python's lossy coercions."""

    if value is None or value == "":
        if default is not None:
            return default
        raise ProtocolError(f"{field} must be an integer")
    if isinstance(value, bool) or isinstance(value, float):
        raise ProtocolError(f"{field} must be an integer")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str):
        try:
            normalized = int(value.strip(), 10)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProtocolError(f"{field} must be an integer") from exc
    else:
        raise ProtocolError(f"{field} must be an integer")
    if normalized < 0:
        raise ProtocolError(f"{field} must be non-negative")
    if normalized > MAX_SAFE_INTEGER:
        raise ProtocolError(f"{field} exceeds the safe integer limit")
    return normalized


@dataclass(frozen=True)
class EventEnvelope:
    event_type: str
    node_id: str
    request_id: str = ""
    turn_id: str = ""
    sequence: int = 0
    cursor: int = 0
    event_id: str = ""
    occurred_at: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "cursor": self.cursor,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "node_id": self.node_id,
            "type": self.event_type,
            "timestamp": self.occurred_at or int(time.time() * 1000),
            "payload": dict(self.payload),
        }


def make_event(
    event_type: str,
    *,
    node_id: str,
    request_id: str = "",
    turn_id: str = "",
    sequence: int = 0,
    cursor: int = 0,
    payload: Mapping[str, Any] | None = None,
    event_id: str = "",
) -> dict[str, Any]:
    normalized_type = _text(event_type, MAX_EVENT_TYPE_LENGTH).lower()
    normalized_node = _text(node_id, MAX_NODE_ID_LENGTH)
    if not normalized_type or not normalized_node:
        raise ProtocolError("event type and node_id are required")
    sequence = protocol_int(sequence, "sequence")
    cursor = protocol_int(cursor, "cursor")
    return EventEnvelope(
        event_type=normalized_type,
        node_id=normalized_node,
        request_id=_text(request_id, MAX_REQUEST_ID_LENGTH),
        turn_id=_text(turn_id, MAX_REQUEST_ID_LENGTH),
        sequence=int(sequence),
        cursor=int(cursor),
        event_id=_text(event_id, 256) or str(uuid.uuid4()),
        occurred_at=int(time.time() * 1000),
        payload=dict(payload or {}),
    ).to_dict()


def validate_event(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError("event must be an object")
    schema = _text(value.get("schema_version"), 64)
    if schema != SCHEMA_VERSION:
        raise ProtocolError("unsupported schema_version")
    try:
        sequence = protocol_int(value.get("sequence"), "sequence", default=0)
        cursor = protocol_int(value.get("cursor"), "cursor", default=0)
        timestamp = protocol_int(value.get("timestamp"), "timestamp", default=0)
    except ProtocolError as exc:
        # Keep the transport-level error category stable for existing callers
        # while retaining the offending field for diagnostics.
        raise ProtocolError(
            f"{exc}; sequence, cursor, and timestamp must be integers"
        ) from exc
    try:
        event = make_event(
            _text(value.get("type"), MAX_EVENT_TYPE_LENGTH),
            node_id=_text(value.get("node_id"), MAX_NODE_ID_LENGTH),
            request_id=value.get("request_id"),
            turn_id=value.get("turn_id"),
            sequence=sequence,
            cursor=cursor,
            payload=value.get("payload") if isinstance(value.get("payload"), Mapping) else {},
            event_id=value.get("event_id"),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProtocolError(str(exc)) from exc
    if timestamp > 0:
        event["timestamp"] = timestamp
    return event


@dataclass
class SequenceWindow:
    """Bounded event-id dedupe window for producer-local sequence hints.

    Worker producers may restart or scope ``sequence`` by role/turn.  The
    server assigns the node-wide replay sequence, so a lower producer value
    is not by itself a duplicate.  Only a repeated event_id is a duplicate.
    """

    last_sequence: int = 0
    seen_event_ids: set[str] = field(default_factory=set)
    max_seen: int = 4096

    def accept(self, event: Mapping[str, Any]) -> bool:
        normalized = validate_event(event)
        event_id = str(normalized.get("event_id") or "")
        sequence = int(normalized.get("sequence") or 0)
        if event_id and event_id in self.seen_event_ids:
            return False
        if event_id:
            self.seen_event_ids.add(event_id)
            if len(self.seen_event_ids) > self.max_seen:
                self.seen_event_ids = set(list(self.seen_event_ids)[-self.max_seen:])
        self.last_sequence = max(self.last_sequence, sequence)
        return True


@dataclass(frozen=True)
class LeaseFence:
    request_id: str
    worker_id: str
    lease_id: str
    expires_at: float

    @property
    def valid(self) -> bool:
        return bool(self.lease_id and self.expires_at > time.time())


def new_lease(request_id: str, worker_id: str, ttl_seconds: float = 30.0) -> LeaseFence:
    return LeaseFence(
        request_id=_text(request_id, MAX_REQUEST_ID_LENGTH),
        worker_id=_text(worker_id, MAX_NODE_ID_LENGTH),
        lease_id=str(uuid.uuid4()),
        expires_at=time.time() + max(1.0, float(ttl_seconds)),
    )


def replay_after(events: Iterable[Mapping[str, Any]], sequence: int) -> list[dict[str, Any]]:
    """Return strictly newer, validated events in sequence order."""
    normalized = [validate_event(event) for event in events]
    return sorted(
        (event for event in normalized if int(event.get("sequence") or 0) > sequence),
        key=lambda event: (int(event.get("sequence") or 0), str(event.get("event_id") or "")),
    )
