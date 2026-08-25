"""Bounded, fenced worker WebSocket channel primitives.

The channel is deliberately transport-only: task ownership remains in the
durable collaboration queue.  A reconnecting worker can resume by sequence,
while a stale connection is fenced before it can publish another event.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import threading
import time
from typing import Any, Mapping

from hermes_services.low_latency_protocol import (
    ProtocolError,
    SequenceWindow,
    make_event,
    protocol_int,
    replay_after,
    validate_event,
)


HOSTED_MEMBER_NODES = {
    "hermes-manager": "server",
    "dbb3-worker": "dbb3",
    "pc-worker": "wsl",
}
WORKER_NODE_IDS = frozenset({"dbb3-worker", "pc-worker"})
MAX_BACKLOG = 4096
MAX_EVENT_BYTES = 512 * 1024
HEARTBEAT_TIMEOUT_SECONDS = 90.0


@dataclass
class WorkerConnection:
    node_id: str
    connection_generation: str
    lease_id: str
    capabilities: tuple[str, ...] = ()
    version: str = ""
    last_heartbeat: float = field(default_factory=time.monotonic)
    sequence_window: SequenceWindow = field(default_factory=SequenceWindow)
    canonical_events: dict[str, dict[str, Any]] = field(default_factory=dict)
    connected: bool = True


class WorkerChannelRegistry:
    """Thread-safe worker connections plus bounded replay windows."""

    def __init__(self, *, max_backlog: int = MAX_BACKLOG) -> None:
        self._changed = threading.Condition(threading.RLock())
        self._max_backlog = max(64, min(int(max_backlog), MAX_BACKLOG))
        self._connections: dict[str, WorkerConnection] = {}
        self._events: dict[str, deque[dict[str, Any]]] = {}
        self._sequence: dict[str, int] = {}

    def connect(
        self,
        node_id: str,
        *,
        connection_generation: str,
        capabilities: list[str] | tuple[str, ...] = (),
        version: str = "",
        connector_id: str = "",
        expected_connector_id: str = "",
    ) -> WorkerConnection:
        node = str(node_id or "").strip()
        if node not in WORKER_NODE_IDS:
            raise ProtocolError("only dbb3-worker and pc-worker may connect remotely")
        generation = str(connection_generation or "").strip()
        if not generation:
            raise ProtocolError("connection_generation is required")
        expected_connector = str(expected_connector_id or "").strip()
        supplied_connector = str(connector_id or "").strip()
        if expected_connector and supplied_connector != expected_connector:
            raise ProtocolError("connector is not mapped to this worker node")
        with self._changed:
            previous = self._connections.get(node)
            if previous is not None:
                previous.connected = False
            connection = WorkerConnection(
                node_id=node,
                connection_generation=generation[:128],
                lease_id=f"{node}:{generation}:{time.time_ns()}",
                capabilities=tuple(str(item)[:128] for item in capabilities if str(item).strip())[:128],
                version=str(version or "")[:128],
            )
            self._connections[node] = connection
            self._events.setdefault(node, deque(maxlen=self._max_backlog))
            self._sequence.setdefault(node, 0)
            # At-least-once redelivery after reconnect must not look new to a
            # fresh in-memory window. Seed it from the retained replay stream.
            retained = self._events[node]
            connection.sequence_window.last_sequence = self._sequence[node]
            connection.sequence_window.seen_event_ids = {
                str(event.get("event_id") or "")
                for event in retained
                if event.get("event_id")
            }
            connection.canonical_events = {
                str(event["event_id"]): dict(event)
                for event in retained
                if event.get("event_id")
            }
            self._changed.notify_all()
            return connection

    def disconnect(self, node_id: str, lease_id: str) -> bool:
        with self._changed:
            current = self._connections.get(str(node_id or "").strip())
            if current is None or current.lease_id != lease_id:
                return False
            current.connected = False
            self._connections.pop(current.node_id, None)
            self._changed.notify_all()
            return True

    def heartbeat(self, node_id: str, lease_id: str) -> bool:
        with self._changed:
            current = self._connections.get(str(node_id or "").strip())
            if current is None or current.lease_id != lease_id or not current.connected:
                return False
            current.last_heartbeat = time.monotonic()
            return True

    def wake_replay_waiters(self) -> None:
        """Wake condition waiters so cancelled websocket tasks can exit."""

        with self._changed:
            self._changed.notify_all()

    def lease_alive(
        self,
        node_id: str,
        lease_id: str,
        *,
        timeout_seconds: float = HEARTBEAT_TIMEOUT_SECONDS,
    ) -> bool:
        """Return whether a lease has received a recent worker heartbeat.

        This is intentionally read-only. Sending a server heartbeat must not
        refresh a lease when the peer has stopped responding.
        """

        timeout = max(0.0, float(timeout_seconds))
        with self._changed:
            current = self._connections.get(str(node_id or "").strip())
            if current is None or current.lease_id != lease_id or not current.connected:
                return False
            return (time.monotonic() - current.last_heartbeat) <= timeout

    def append(
        self,
        node_id: str,
        lease_id: str,
        event: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Validate and append an event, returning ``(event, duplicate)``."""
        normalized_node = str(node_id or "").strip()
        with self._changed:
            current = self._connections.get(normalized_node)
            if current is None or current.lease_id != lease_id or not current.connected:
                raise ProtocolError("stale worker lease")
            if time.monotonic() - current.last_heartbeat > HEARTBEAT_TIMEOUT_SECONDS:
                raise ProtocolError("worker lease expired")
            normalized = validate_event(event)
            if normalized["node_id"] != normalized_node:
                raise ProtocolError("event node_id does not match connection")
            encoded_size = len(json.dumps(
                normalized,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"))
            if encoded_size > MAX_EVENT_BYTES:
                raise ProtocolError("worker event exceeds 512 KiB limit")
            event_id = str(normalized.get("event_id") or "")
            canonical = current.canonical_events.get(event_id)
            if canonical is not None:
                # ACK/retry must expose the server-assigned replay sequence,
                # not the producer-local sequence from the retry frame.
                return dict(canonical), True
            if not current.sequence_window.accept(normalized):
                # The bounded id window can outlive the retained replay entry.
                # It is still a duplicate, but there is no canonical frame to
                # return after eviction.
                return normalized, True
            next_sequence = max(
                self._sequence.get(normalized_node, 0) + 1,
                int(normalized.get("sequence") or 0),
            )
            normalized["sequence"] = next_sequence
            self._sequence[normalized_node] = next_sequence
            self._events[normalized_node].append(normalized)
            if event_id:
                current.canonical_events[event_id] = dict(normalized)
                if len(current.canonical_events) > current.sequence_window.max_seen:
                    oldest = next(iter(current.canonical_events))
                    current.canonical_events.pop(oldest, None)
            self._changed.notify_all()
            return normalized, False

    def publish(self, node_id: str, event_type: str, *, payload: Mapping[str, Any] | None = None,
                request_id: str = "", turn_id: str = "") -> dict[str, Any]:
        node = str(node_id or "").strip()
        with self._changed:
            current = self._connections.get(node)
            if current is None or not current.connected:
                raise ProtocolError("worker is not connected")
            sequence = self._sequence.get(node, 0) + 1
            event = make_event(
                event_type,
                node_id=node,
                request_id=request_id,
                turn_id=turn_id,
            sequence=sequence,
            payload=payload,
        )
            encoded_size = len(json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"))
            if encoded_size > MAX_EVENT_BYTES:
                raise ProtocolError("published worker event exceeds 512 KiB limit")
            self._sequence[node] = sequence
            self._events[node].append(event)
            self._changed.notify_all()
            return event

    def replay(self, node_id: str, after_sequence: int) -> list[dict[str, Any]]:
        with self._changed:
            events = list(self._events.get(str(node_id or "").strip(), ()))
        return replay_after(events, protocol_int(after_sequence, "cursor", default=0))

    def wait_for_replay(
        self,
        node_id: str,
        after_sequence: int,
        *,
        timeout: float,
        lease_id: str = "",
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        """Block until new frames arrive or the heartbeat deadline expires."""

        node = str(node_id or "").strip()
        expected_lease = str(lease_id or "").strip()
        cursor = protocol_int(after_sequence, "cursor", default=0)
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._changed:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return []
                if expected_lease:
                    current = self._connections.get(node)
                    if (
                        current is None
                        or current.lease_id != expected_lease
                        or not current.connected
                        or (
                            time.monotonic() - current.last_heartbeat
                            > HEARTBEAT_TIMEOUT_SECONDS
                        )
                    ):
                        return []
                events = replay_after(list(self._events.get(node, ())), cursor)
                if events:
                    return events
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._changed.wait(remaining)

    def snapshot(self, node_id: str) -> dict[str, Any]:
        with self._changed:
            current = self._connections.get(str(node_id or "").strip())
            if current is None:
                return {"connected": False, "node_id": str(node_id or "").strip()}
            return {
                "connected": bool(current.connected),
                "node_id": current.node_id,
                "connection_generation": current.connection_generation,
                "lease_id": current.lease_id,
                "capabilities": list(current.capabilities),
                "version": current.version,
                "last_heartbeat": current.last_heartbeat,
                "sequence": self._sequence.get(current.node_id, 0),
            }


__all__ = [
    "HEARTBEAT_TIMEOUT_SECONDS",
    "HOSTED_MEMBER_NODES",
    "WORKER_NODE_IDS",
    "WorkerChannelRegistry",
]
