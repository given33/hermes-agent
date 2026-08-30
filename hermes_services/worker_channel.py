"""Bounded, fenced worker WebSocket channel primitives.

The channel is deliberately transport-only: task ownership remains in the
durable collaboration queue.  A reconnecting worker can resume by sequence,
while a stale connection is fenced before it can publish another event.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import re
import threading
import time
from typing import Any, Callable, Mapping

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
    "hk-worker": "hk",
}
WORKER_NODE_IDS = frozenset({"dbb3-worker", "pc-worker", "hk-worker"})
WORKER_MANAGED_NODE_IDS = {
    "dbb3-worker": "dbb3",
    "pc-worker": "wsl",
    "hk-worker": "hk",
}
MANAGED_WORKER_NODE_IDS = tuple(WORKER_MANAGED_NODE_IDS.values())
MANAGED_WORKER_LABELS = {
    "dbb3": "DBB3",
    "wsl": "Windows PC + WSL",
    "hk": "Hong Kong Worker",
}
MAX_BACKLOG = 4096
MAX_EVENT_BYTES = 512 * 1024
HEARTBEAT_TIMEOUT_SECONDS = 90.0
MAX_STATUS_BYTES = 16 * 1024
_MAX_COUNTER = 2**63 - 1
_RELEASE_COMMIT = re.compile(r"[0-9a-fA-F]{7,64}")
_CONNECTION_GENERATION = re.compile(r"[A-Za-z0-9._:-]{1,128}")


@dataclass
class WorkerConnection:
    node_id: str
    connection_generation: str
    lease_id: str
    capabilities: tuple[str, ...] = ()
    version: str = ""
    last_heartbeat: float = field(default_factory=time.monotonic)
    last_seen_at: float = field(default_factory=time.time)
    runtime: dict[str, Any] = field(default_factory=dict)
    release: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    sequence_window: SequenceWindow = field(default_factory=SequenceWindow)
    canonical_events: dict[str, dict[str, Any]] = field(default_factory=dict)
    connected: bool = True


class WorkerChannelRegistry:
    """Thread-safe worker connections plus bounded replay windows."""

    def __init__(
        self,
        *,
        max_backlog: int = MAX_BACKLOG,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._changed = threading.Condition(threading.RLock())
        self._max_backlog = max(64, min(int(max_backlog), MAX_BACKLOG))
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
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
        runtime: Mapping[str, Any] | None = None,
        release: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> WorkerConnection:
        node = str(node_id or "").strip()
        if node not in WORKER_NODE_IDS:
            allowed = ", ".join(sorted(WORKER_NODE_IDS))
            raise ProtocolError(f"only {allowed} may connect remotely")
        generation = str(connection_generation or "").strip()
        if _CONNECTION_GENERATION.fullmatch(generation) is None:
            raise ProtocolError("connection_generation is invalid")
        expected_connector = str(expected_connector_id or "").strip()
        supplied_connector = str(connector_id or "").strip()
        if expected_connector and supplied_connector != expected_connector:
            raise ProtocolError("connector is not mapped to this worker node")
        normalized_runtime, normalized_release, normalized_metrics = _sanitize_worker_status(
            node,
            runtime=runtime,
            release=release,
            metrics=metrics,
        )
        normalized_version = _status_text(version, 128)
        with self._changed:
            previous = self._connections.get(node)
            if previous is not None:
                previous.connected = False
            connection = WorkerConnection(
                node_id=node,
                connection_generation=generation[:128],
                lease_id=f"{node}:{generation}:{time.time_ns()}",
                capabilities=tuple(str(item)[:128] for item in capabilities if str(item).strip())[:128],
                version=normalized_version,
                last_heartbeat=self._monotonic_clock(),
                last_seen_at=self._wall_clock(),
                runtime=normalized_runtime,
                release=normalized_release,
                metrics=normalized_metrics,
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
            self._changed.notify_all()
            return True

    def heartbeat(
        self,
        node_id: str,
        lease_id: str,
        *,
        runtime: Mapping[str, Any] | None = None,
        release: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> bool:
        normalized_node = str(node_id or "").strip()
        normalized_runtime, normalized_release, normalized_metrics = _sanitize_worker_status(
            normalized_node,
            runtime=runtime,
            release=release,
            metrics=metrics,
        )
        with self._changed:
            current = self._connections.get(normalized_node)
            if current is None or current.lease_id != lease_id or not current.connected:
                return False
            current.last_heartbeat = self._monotonic_clock()
            current.last_seen_at = self._wall_clock()
            if runtime is not None:
                current.runtime = normalized_runtime
            if release is not None:
                current.release = normalized_release
            if metrics is not None:
                current.metrics = normalized_metrics
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
            return (self._monotonic_clock() - current.last_heartbeat) <= timeout

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
            if self._monotonic_clock() - current.last_heartbeat > HEARTBEAT_TIMEOUT_SECONDS:
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
        deadline = self._monotonic_clock() + max(0.0, float(timeout))
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
                            self._monotonic_clock() - current.last_heartbeat
                            > HEARTBEAT_TIMEOUT_SECONDS
                        )
                    ):
                        return []
                events = replay_after(list(self._events.get(node, ())), cursor)
                if events:
                    return events
                remaining = deadline - self._monotonic_clock()
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
                "last_seen_at": current.last_seen_at,
                "sequence": self._sequence.get(current.node_id, 0),
            }

    def managed_snapshots(
        self,
        *,
        timeout_seconds: float = HEARTBEAT_TIMEOUT_SECONDS,
    ) -> list[dict[str, Any]]:
        """Return the fixed three-worker public health view.

        Connection generations and lease identifiers are deliberately omitted.
        Monotonic time determines freshness while the paired wall-clock value is
        exposed for clients that need an absolute observation timestamp.
        """

        timeout = max(0.0, float(timeout_seconds))
        monotonic_now = self._monotonic_clock()
        with self._changed:
            snapshots: list[dict[str, Any]] = []
            for worker_node_id, managed_node_id in WORKER_MANAGED_NODE_IDS.items():
                current = self._connections.get(worker_node_id)
                if current is None:
                    snapshots.append(_offline_managed_snapshot(worker_node_id, managed_node_id))
                    continue
                age_seconds = max(0.0, monotonic_now - current.last_heartbeat)
                fresh = bool(current.connected) and age_seconds <= timeout
                release_version = str(current.release.get("version") or "")
                snapshots.append({
                    "id": managed_node_id,
                    "label": MANAGED_WORKER_LABELS[managed_node_id],
                    "worker_node_id": worker_node_id,
                    "online": fresh,
                    "fresh": fresh,
                    "gateway_state": "ready" if fresh else "offline",
                    "version": release_version or current.version,
                    "observed_at": datetime.fromtimestamp(
                        current.last_seen_at,
                        tz=timezone.utc,
                    ).isoformat(timespec="seconds"),
                    "age_seconds": round(age_seconds, 3),
                    "runtime": dict(current.runtime),
                    "release": dict(current.release),
                    "metrics": dict(current.metrics),
                    "active_tasks": int(current.runtime.get("active_tasks") or 0),
                })
            return snapshots

    def deployment_snapshot(
        self,
        worker_node_id: str,
        *,
        timeout_seconds: float = HEARTBEAT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Return connector-scoped rollout evidence without exposing a lease."""

        node = str(worker_node_id or "").strip()
        managed_node_id = WORKER_MANAGED_NODE_IDS.get(node)
        if managed_node_id is None:
            raise ProtocolError("deployment snapshot requires a worker node")
        timeout = max(0.0, float(timeout_seconds))
        with self._changed:
            current = self._connections.get(node)
            if current is None:
                return {
                    "node_id": node,
                    "managed_node_id": managed_node_id,
                    "online": False,
                    "fresh": False,
                    "connection_generation": "",
                    "observed_at": "",
                    "version": "",
                    "release": {},
                }
            age_seconds = max(
                0.0,
                self._monotonic_clock() - current.last_heartbeat,
            )
            fresh = bool(current.connected) and age_seconds <= timeout
            return {
                "node_id": node,
                "managed_node_id": managed_node_id,
                "online": fresh,
                "fresh": fresh,
                "connection_generation": current.connection_generation,
                "observed_at": datetime.fromtimestamp(
                    current.last_seen_at,
                    tz=timezone.utc,
                ).isoformat(timespec="seconds"),
                "version": str(current.release.get("version") or current.version),
                "release": dict(current.release),
            }


def _offline_managed_snapshot(worker_node_id: str, managed_node_id: str) -> dict[str, Any]:
    return {
        "id": managed_node_id,
        "label": MANAGED_WORKER_LABELS[managed_node_id],
        "worker_node_id": worker_node_id,
        "online": False,
        "fresh": False,
        "gateway_state": "offline",
        "version": "",
        "observed_at": "",
        "age_seconds": None,
        "runtime": {},
        "release": {},
        "metrics": {},
        "active_tasks": 0,
    }


def _sanitize_worker_status(
    worker_node_id: str,
    *,
    runtime: Mapping[str, Any] | None,
    release: Mapping[str, Any] | None,
    metrics: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    for label, value in (("runtime", runtime), ("release", release), ("metrics", metrics)):
        if value is not None and not isinstance(value, Mapping):
            raise ProtocolError(f"worker {label} must be an object")
    try:
        encoded_size = len(json.dumps(
            {"runtime": runtime or {}, "release": release or {}, "metrics": metrics or {}},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProtocolError("worker status must be JSON serializable") from exc
    if encoded_size > MAX_STATUS_BYTES:
        raise ProtocolError("worker status exceeds 16 KiB limit")

    runtime_source = runtime or {}
    normalized_runtime: dict[str, Any] = {}
    if "worker_ready" in runtime_source:
        normalized_runtime["worker_ready"] = _status_bool(
            runtime_source.get("worker_ready"), "runtime.worker_ready"
        )
    if "active_tasks" in runtime_source:
        normalized_runtime["active_tasks"] = _status_integer(
            runtime_source.get("active_tasks"), "runtime.active_tasks", maximum=1_000_000
        )
    sampled_at = _status_text(runtime_source.get("sampled_at"), 64)
    if sampled_at:
        normalized_runtime["sampled_at"] = sampled_at

    release_source = release or {}
    normalized_release: dict[str, str] = {}
    schema = _status_text(release_source.get("schema"), 64)
    if schema:
        normalized_release["schema"] = schema
    release_node = _status_text(release_source.get("node_id"), 32)
    expected_node = WORKER_MANAGED_NODE_IDS.get(worker_node_id)
    if release_node:
        if expected_node is None or release_node != expected_node:
            raise ProtocolError("worker release node_id does not match connection")
        normalized_release["node_id"] = release_node
    commit = _status_text(release_source.get("commit"), 64)
    if commit:
        if _RELEASE_COMMIT.fullmatch(commit) is None:
            raise ProtocolError("worker release commit is invalid")
        normalized_release["commit"] = commit.lower()
    release_version = _status_text(release_source.get("version"), 128)
    if release_version:
        normalized_release["version"] = release_version

    metrics_source = metrics or {}
    normalized_metrics: dict[str, Any] = {}
    percent_fields = ("cpu_percent", "memory_percent", "disk_percent")
    for field_name in percent_fields:
        if field_name in metrics_source:
            normalized_metrics[field_name] = _status_number(
                metrics_source.get(field_name), f"metrics.{field_name}", maximum=100.0
            )
    counter_fields = (
        "memory_total_bytes",
        "memory_available_bytes",
        "disk_total_bytes",
        "disk_free_bytes",
        "uptime_seconds",
    )
    for field_name in counter_fields:
        if field_name in metrics_source:
            normalized_metrics[field_name] = _status_integer(
                metrics_source.get(field_name), f"metrics.{field_name}", maximum=_MAX_COUNTER
            )
    metrics_sampled_at = _status_text(metrics_source.get("sampled_at"), 64)
    if metrics_sampled_at:
        normalized_metrics["sampled_at"] = metrics_sampled_at
    if "available" in metrics_source:
        normalized_metrics["available"] = _status_bool(
            metrics_source.get("available"), "metrics.available"
        )
    return normalized_runtime, normalized_release, normalized_metrics


def _status_text(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProtocolError("worker status text fields must be strings")
    normalized = value.strip()
    if len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise ProtocolError("worker status text field is invalid")
    return normalized


def _status_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{label} must be a boolean")
    return value


def _status_number(value: Any, label: str, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{label} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0 or normalized > maximum:
        raise ProtocolError(f"{label} is outside the allowed range")
    return normalized


def _status_integer(value: Any, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{label} must be an integer")
    if value < 0 or value > maximum:
        raise ProtocolError(f"{label} is outside the allowed range")
    return value


_SHARED_WORKER_CHANNEL = WorkerChannelRegistry()


def get_worker_channel_registry() -> WorkerChannelRegistry:
    """Return the process-wide registry shared by WS and management APIs."""

    return _SHARED_WORKER_CHANNEL


__all__ = [
    "HEARTBEAT_TIMEOUT_SECONDS",
    "HOSTED_MEMBER_NODES",
    "MANAGED_WORKER_LABELS",
    "MANAGED_WORKER_NODE_IDS",
    "MAX_STATUS_BYTES",
    "WORKER_NODE_IDS",
    "WORKER_MANAGED_NODE_IDS",
    "WorkerChannelRegistry",
    "get_worker_channel_registry",
]
