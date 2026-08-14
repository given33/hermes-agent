"""Canonical tool execution records shared by the agent loop and clients.

The execution record is deliberately metadata-only.  Tool arguments and raw
results stay in the normal tool pipeline (and its existing redaction/storage
rules); this module stores bounded digests and presentation hints so desktop,
iOS, replay, and audit consumers have one stable contract.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "tool-execution/v1"
TERMINAL_STATES = frozenset({"completed", "failed", "blocked", "cancelled", "timed_out"})
ACTIVE_STATES = frozenset({"started", "running"})
_ALLOWED_STATES = ACTIVE_STATES | TERMINAL_STATES


def stable_digest(value: Any) -> str:
    """Return a deterministic non-reversible digest for audit correlation."""

    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        payload = repr(value)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _bounded_text(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


@dataclass(frozen=True)
class ToolPresentationMeta:
    """Client-facing rendering hints, never the source of execution truth."""

    view: str = "text"
    title: str = ""
    summary: str = ""
    replayable: bool = True
    artifact_refs: tuple[str, ...] = ()
    renderer_version: str = "tool-view/v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "view": self.view,
            "title": _bounded_text(self.title, 120),
            "summary": _bounded_text(self.summary),
            "replayable": bool(self.replayable),
            "artifact_refs": list(self.artifact_refs),
            "renderer_version": self.renderer_version,
        }


@dataclass
class ToolExecutionEnvelope:
    """Strict, lossless metadata envelope for one top-level or child call."""

    tool_name: str
    call_id: str = ""
    parent_call_id: str = ""
    owner_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    profile: str = ""
    registry_generation: int = 0
    status: str = "started"
    started_at_ms: int = 0
    finished_at_ms: int | None = None
    duration_ms: int = 0
    args_digest: str = ""
    result_digest: str = ""
    effect_metadata: dict[str, Any] = field(default_factory=dict)
    presentation_meta: ToolPresentationMeta = field(default_factory=ToolPresentationMeta)
    children: list[str] = field(default_factory=list)
    error_type: str = ""

    def __post_init__(self) -> None:
        if not str(self.tool_name or "").strip():
            raise ValueError("tool_name is required")
        if not self.call_id:
            self.call_id = f"call_{uuid.uuid4().hex}"
        if self.status not in _ALLOWED_STATES:
            raise ValueError(f"invalid tool execution status: {self.status}")
        if not self.started_at_ms:
            self.started_at_ms = int(time.time() * 1000)
        self.children = list(dict.fromkeys(str(item) for item in self.children if item))

    def transition(self, status: str, *, result: Any = None, error_type: str = "") -> None:
        if status not in _ALLOWED_STATES:
            raise ValueError(f"invalid tool execution status: {status}")
        if self.status in TERMINAL_STATES:
            if status != self.status:
                raise ValueError(f"terminal tool execution cannot transition: {self.status} -> {status}")
            # Duplicate terminal observations are replay/idempotency events;
            # they must not rewrite the original completion time or digest.
            return
        self.status = status
        if result is not None:
            self.result_digest = stable_digest(result)
        if error_type:
            self.error_type = _bounded_text(error_type, 80)
        if status in TERMINAL_STATES:
            self.finished_at_ms = int(time.time() * 1000)
            self.duration_ms = max(0, self.finished_at_ms - self.started_at_ms)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "call_id": self.call_id,
            "parent_call_id": self.parent_call_id,
            "tool_name": self.tool_name,
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "profile": self.profile,
            "registry_generation": int(self.registry_generation),
            "status": self.status,
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": self.finished_at_ms,
            "duration_ms": self.duration_ms,
            "args_digest": self.args_digest,
            "result_digest": self.result_digest,
            "effect_metadata": dict(self.effect_metadata),
            "presentation_meta": self.presentation_meta.as_dict(),
            "children": list(self.children),
            "error_type": self.error_type,
        }


class ToolExecutionLedger:
    """Thread-safe bounded ledger used by an agent instance and replay tests."""

    def __init__(self, *, max_records: int = 512) -> None:
        self._max_records = max(16, int(max_records))
        self._records: dict[str, ToolExecutionEnvelope] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()

    def start(self, envelope: ToolExecutionEnvelope) -> ToolExecutionEnvelope:
        with self._lock:
            if envelope.call_id in self._records:
                return self._records[envelope.call_id]
            self._records[envelope.call_id] = envelope
            self._order.append(envelope.call_id)
            if envelope.parent_call_id and envelope.parent_call_id in self._records:
                self._records[envelope.parent_call_id].children.append(envelope.call_id)
            while len(self._order) > self._max_records:
                old_id = self._order.pop(0)
                self._records.pop(old_id, None)
            return envelope

    def finish(self, call_id: str, status: str, *, result: Any = None, error_type: str = "") -> ToolExecutionEnvelope | None:
        with self._lock:
            envelope = self._records.get(call_id)
            if envelope is None:
                return None
            envelope.transition(status, result=result, error_type=error_type)
            return envelope

    def get(self, call_id: str) -> ToolExecutionEnvelope | None:
        with self._lock:
            return self._records.get(call_id)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._records[item].as_dict() for item in self._order if item in self._records]


def replay_projection(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Project stored metadata without touching files, network, or providers."""

    if str(envelope.get("schema_version") or "") != SCHEMA_VERSION:
        raise ValueError("unsupported tool execution envelope schema")
    presentation = envelope.get("presentation_meta")
    if not isinstance(presentation, Mapping):
        raise ValueError("presentation_meta is required for replay")
    return {
        "call_id": str(envelope.get("call_id") or ""),
        "parent_call_id": str(envelope.get("parent_call_id") or ""),
        "tool_name": str(envelope.get("tool_name") or ""),
        "status": str(envelope.get("status") or ""),
        "duration_ms": int(envelope.get("duration_ms") or 0),
        "presentation_meta": dict(presentation),
        "result_digest": str(envelope.get("result_digest") or ""),
    }


def build_envelope(*, tool_name: str, args: Any, call_id: str = "", parent_call_id: str = "", owner_id: str = "", session_id: str = "", turn_id: str = "", profile: str = "", registry_generation: int = 0, effect_metadata: Mapping[str, Any] | None = None, presentation_meta: ToolPresentationMeta | None = None) -> ToolExecutionEnvelope:
    return ToolExecutionEnvelope(
        tool_name=tool_name,
        call_id=call_id,
        parent_call_id=parent_call_id,
        owner_id=owner_id,
        session_id=session_id,
        turn_id=turn_id,
        profile=profile,
        registry_generation=registry_generation,
        args_digest=stable_digest(args),
        effect_metadata=dict(effect_metadata or {}),
        presentation_meta=presentation_meta or ToolPresentationMeta(title=tool_name),
    )
