"""Privacy-aware, read-only trajectory projection for Hosted events.

The projector follows the Codex Trajectory data model without importing its
runtime or reading arbitrary log paths. Hosted events are already account and
generation scoped, so this module only exposes a bounded, redacted ledger.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import time
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 500
MAX_RECORDS = 1_000
DETAIL_LEVELS = frozenset({"summary", "full"})
_SECRET_RE = re.compile(
    r"(?i)\b(?:bearer\s+[A-Za-z0-9._~+/=-]+|"
    r"(?:api[_-]?key|apikey|access[_-]?token|accesstoken|refresh[_-]?token|"
    r"authorization|cookie|password|secret)"
    r"\s*[:=]\s*[^\s,;]+)"
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|apikey|access[_-]?token|accesstoken|"
    r"refresh[_-]?token|authorization|cookie|password|secret)"
)
_DETAIL_KEYS = frozenset({
    "text", "preview", "summary", "partial_result", "partial_summary", "error",
    "tool_name", "name", "status", "duration_ms", "call_id", "tool_call_id",
})


def project_hosted_trajectory(
    events: Iterable[Mapping[str, Any]],
    *,
    session_id: str,
    title: str = "",
    model: str | None = None,
    detail_level: str = "summary",
    max_records: int = DEFAULT_MAX_RECORDS,
) -> dict[str, Any]:
    """Project canonical Hosted events into a stable, bounded trajectory."""

    detail = str(detail_level or "summary").strip().lower()
    if detail not in DETAIL_LEVELS:
        raise ValueError("detail_level must be 'summary' or 'full'")
    if isinstance(max_records, bool) or not isinstance(max_records, int):
        raise ValueError("max_records must be an integer")
    if not 50 <= max_records <= MAX_RECORDS:
        raise ValueError(f"max_records must be between 50 and {MAX_RECORDS}")

    ordered = sorted(
        (dict(event) for event in events if isinstance(event, Mapping)),
        key=lambda event: _non_negative_int(event.get("cursor")),
    )
    records: list[dict[str, Any]] = []
    turns: dict[str, dict[str, Any]] = {}
    first_time: int | None = None
    last_time: int | None = None
    tool_calls = 0
    failed_tools = 0
    subagents = 0
    compactions = 0
    latest_usage: dict[str, Any] | None = None
    seen_event_ids: set[str] = set()

    for event in ordered:
        event_id = _text(event.get("event_id")) or f"event-{len(records) + 1}"
        if event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        event_type = _text(event.get("event_type")).lower() or "unknown"
        occurred_at = _non_negative_int(event.get("occurred_at")) or None
        if occurred_at is not None:
            first_time = occurred_at if first_time is None else min(first_time, occurred_at)
            last_time = occurred_at if last_time is None else max(last_time, occurred_at)
        turn_id = _text(event.get("turn_id")) or "turn-unknown"
        turn = turns.setdefault(
            turn_id,
            {
                "index": len(turns) + 1,
                "id": turn_id,
                "startedAt": _iso(occurred_at),
                "completedAt": None,
                "durationMs": None,
                "timeToFirstTokenMs": None,
                "status": "running",
                "error": None,
                "records": 0,
                "steps": 0,
            },
        )
        if turn["startedAt"] is None and occurred_at is not None:
            turn["startedAt"] = _iso(occurred_at)
        if event_type in {"turn.completed", "turn.cancelled", "turn.failed"}:
            turn["completedAt"] = _iso(occurred_at)
            turn["status"] = "aborted" if event_type == "turn.cancelled" else (
                "error" if event_type == "turn.failed" else "complete"
            )
        payload = event.get("payload")
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        kind = _kind_for(event_type, payload)
        status = _status_for(event_type, payload)
        if kind == "tool":
            call_id = _text(payload.get("call_id") or payload.get("tool_call_id"))
            if event_type.endswith(".started") or (not call_id and event_type.endswith(".completed")):
                tool_calls += 1
            failed_tools += int(status == "error")
        if kind == "subagent":
            subagents += int(event_type in {"subagent.started", "subagent.queued"})
        if event_type in {"compaction.started", "compaction.completed", "context.compacted"}:
            compactions += 1
        if event_type == "token_count":
            usage = payload.get("usage") or payload.get("total_token_usage")
            if isinstance(usage, Mapping):
                latest_usage = _safe_usage(usage)
            continue
        if event_type in {"message.delta", "thinking.delta", "tool.progress", "subagent.progress"}:
            turn["steps"] = max(turn["steps"], len(records) + 1)
        record = {
            "index": len(records) + 1,
            "id": event_id,
            "turn": turn["index"],
            "step": turn["steps"] or None,
            "kind": kind,
            "event": event_type,
            "summary": _summary(event_type, payload),
            "startedAt": _iso(occurred_at),
            "completedAt": _iso(occurred_at) if status != "running" else None,
            "durationMs": _duration(payload),
            "status": status,
            "callId": _text(payload.get("call_id") or payload.get("tool_call_id")) or None,
            "input": _detail(payload) if detail == "full" else None,
            "output": _detail(payload) if detail == "full" else None,
            "error": _redact(payload.get("error")) if status == "error" else None,
            "usage": _safe_usage(payload.get("usage")),
            "metadata": _metadata(event, payload) if detail == "full" else {},
        }
        records.append(record)
        turn["records"] += 1

    visible_records = records[-max_records:]
    total_records = len(records)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "detailLevel": detail,
        "generatedAt": _iso(_now_ms()),
        "session": {
            "id": _text(session_id) or "hosted-turn",
            "title": _shorten(_redact(title or "Hermes hosted task"), 100),
            "cwd": None,
            "model": _redact(_text(model)) or None,
            "effort": None,
            "startedAt": _iso(first_time),
            "updatedAt": _iso(last_time),
            "archived": False,
        },
        "stats": {
            "turns": len(turns),
            "records": total_records,
            "visibleRecords": len(visible_records),
            "omittedRecords": max(0, total_records - len(visible_records)),
            "toolCalls": tool_calls,
            "failedTools": failed_tools,
            "subagents": subagents,
            "compactions": compactions,
            "tokens": latest_usage,
        },
        "turns": list(turns.values()),
        "records": visible_records,
        "warnings": [],
    }


def _kind_for(event_type: str, payload: Mapping[str, Any]) -> str:
    if event_type.startswith(("tool.", "command.")):
        return "tool"
    if event_type.startswith("subagent."):
        return "subagent"
    if event_type.startswith(("thinking.", "reasoning.")):
        return "reasoning"
    if event_type.startswith(("compaction.",)) or event_type == "context.compacted":
        return "compaction"
    if event_type.startswith("message.") and _text(payload.get("role")).lower() == "user":
        return "user"
    return "assistant"


def _status_for(event_type: str, payload: Mapping[str, Any]) -> str:
    raw = _text(payload.get("status")).lower()
    if event_type.endswith(".failed") or payload.get("error") or raw in {"error", "failed"}:
        return "error"
    if event_type.endswith(".cancelled") or raw in {"aborted", "cancelled", "stopped"}:
        return "aborted"
    if event_type.endswith((".started", ".progress", ".delta")):
        return "running"
    return "complete"


def _summary(event_type: str, payload: Mapping[str, Any]) -> str:
    for key in ("summary", "preview", "text", "partial_summary", "partial_result", "name", "tool_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _shorten(_redact(value), 220)
    return event_type.replace(".", " / ")


def _detail(payload: Mapping[str, Any]) -> str | None:
    values: dict[str, Any] = {}
    for key in ("text", "preview", "summary", "partial_summary", "partial_result", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            values[key] = _redact(value)
    if not values:
        return None
    return _bounded(json.dumps(values, ensure_ascii=False, sort_keys=True), 12_000)


def _metadata(event: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    runtime = event.get("runtime")
    if isinstance(runtime, Mapping):
        for key in (
            "component_id", "parent_component_id", "provider_refs", "dependency_state",
            "lifecycle_state", "effect_scope_id", "plan_node_id", "artifact_refs",
            "contract_revision", "policy_snapshot_hash",
        ):
            if key in runtime:
                result[key] = _sanitize_metadata(runtime[key])
    for key in ("tool_name", "name", "status", "duration_ms", "call_id", "tool_call_id"):
        if key in payload:
            value = payload[key]
            if isinstance(value, (str, int, float, bool)) or value is None:
                result[key] = _redact(value)
    return result


def _safe_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    result = {
        key: _non_negative_int(value.get(key))
        for key in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens")
        if value.get(key) is not None
    }
    return result or None


def _duration(payload: Mapping[str, Any]) -> int | None:
    value = payload.get("duration_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return round(value)
    return None


def _redact(value: Any) -> str:
    return _SECRET_RE.sub("[REDACTED]", str(value or ""))[:12_000]


def _sanitize_metadata(value: Any, *, depth: int = 0, key: str = "") -> Any:
    """Keep allowlisted runtime metadata bounded and safe for display."""

    if key and _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _redact(value)[:1_200]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 3:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        return {
            str(child_key)[:80]: _sanitize_metadata(child_value, depth=depth + 1, key=str(child_key))
            for child_key, child_value in list(value.items())[:24]
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_metadata(item, depth=depth + 1) for item in list(value)[:24]]
    return _redact(value)[:1_200]


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n\n[truncated {len(value) - limit} characters]"


def _shorten(value: str, limit: int) -> str:
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "..."


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed >= 0 else 0


def _iso(milliseconds: int | None) -> str | None:
    if milliseconds is None:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["DEFAULT_MAX_RECORDS", "MAX_RECORDS", "SCHEMA_VERSION", "project_hosted_trajectory"]
