"""Bounded session trace storage with explicit workspace/account boundaries."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


TRACE_PAYLOAD_KEYS = frozenset({
    "call_id", "parent_call_id", "tool_name", "status", "error_type",
    "args_digest", "result_digest", "artifact_digest", "provider_id",
    "provider_generation", "component", "dependency", "lifecycle",
    "source_revision", "prompt_version", "renderer_version", "view",
    "artifact_refs", "evidence_refs",
})


def _safe_trace_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep trace records structural; never persist raw prompt/result data."""

    if not isinstance(payload, Mapping):
        raise ValueError("trace payload must be an object")
    result: dict[str, Any] = {}
    for raw_key, value in payload.items():
        key = str(raw_key).strip().lower()
        if key not in TRACE_PAYLOAD_KEYS:
            continue
        if key in {"artifact_refs", "evidence_refs"}:
            if not isinstance(value, (list, tuple)):
                continue
            refs = [str(item).strip()[:512] for item in value if str(item).strip()]
            result[key] = list(dict.fromkeys(refs[:32]))
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = str(value)[:512] if isinstance(value, str) else value
    return result


@dataclass(frozen=True)
class TraceEvent:
    workspace_id: str
    account_generation: str
    kind: str
    payload: Mapping[str, Any]
    sequence: int = 0
    event_id: str = ""
    created_at_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id or f"trace_{uuid.uuid4().hex}",
            "workspace_id": self.workspace_id,
            "account_generation": self.account_generation,
            "sequence": self.sequence,
            "kind": self.kind,
            "payload": _safe_trace_payload(self.payload),
            "created_at_ms": self.created_at_ms or int(time.time() * 1000),
        }


class SessionTrace:
    def __init__(self, *, max_events: int = 2048) -> None:
        self.max_events = max(32, int(max_events))
        self._events: list[dict[str, Any]] = []
        self._sequence = 0

    def append(self, event: TraceEvent) -> dict[str, Any]:
        if not event.workspace_id or not event.account_generation:
            raise ValueError("workspace_id and account_generation are required")
        self._sequence += 1
        record = TraceEvent(
            workspace_id=event.workspace_id,
            account_generation=event.account_generation,
            kind=event.kind,
            payload=_safe_trace_payload(event.payload),
            sequence=self._sequence,
            event_id=event.event_id,
            created_at_ms=event.created_at_ms,
        ).as_dict()
        self._events.append(record)
        del self._events[:-self.max_events]
        return dict(record)

    def read(self, *, workspace_id: str, account_generation: str, after_sequence: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if not workspace_id or not account_generation:
            raise ValueError("workspace_id and account_generation are required")
        return [
            dict(item)
            for item in self._events
            if item["workspace_id"] == workspace_id
            and item["account_generation"] == account_generation
            and int(item["sequence"]) > max(0, int(after_sequence))
        ][: max(1, min(int(limit), 500))]

    def search(self, query: str, *, workspace_id: str, account_generation: str, after_sequence: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        needle = str(query or "").strip().lower()
        if not needle:
            return self.read(workspace_id=workspace_id, account_generation=account_generation, after_sequence=after_sequence, limit=limit)
        return [
            item for item in self.read(workspace_id=workspace_id, account_generation=account_generation, after_sequence=after_sequence, limit=self.max_events)
            if needle in str(item.get("kind") or "").lower()
            or needle in str(item.get("payload") or "").lower()
        ][: max(1, min(int(limit), 500))]
