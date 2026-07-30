"""Append-only conversation entries with cursor and optional branch lineage."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import time
from typing import Any, MutableMapping
import uuid


SCHEMA_VERSION = "hermes.session-entry.v1"
ENTRY_TYPES = frozenset(
    {
        "message",
        "model_change",
        "tool_visibility_change",
        "collaboration_lift",
        "role_handoff",
        "intervention",
        "compaction",
        "label",
        "attachment",
        "terminal_state",
        "hook_trace",
    }
)
MAX_QUARANTINED_ENTRIES = 200
MAX_QUARANTINED_RAW_BYTES = 32 * 1024
MAX_MESSAGE_DELTA_CHARS = 16 * 1024


class SessionEntryError(ValueError):
    pass


def append_session_entry(
    conversation: MutableMapping[str, Any],
    *,
    entry_type: str,
    payload: MutableMapping[str, Any] | None = None,
    parent_entry_id: str = "",
    idempotency_key: str = "",
    occurred_at: int | None = None,
) -> tuple[dict[str, Any], bool]:
    normalize_session_entries(conversation)
    normalized_type = str(entry_type or "").strip().lower()
    if normalized_type not in ENTRY_TYPES:
        raise SessionEntryError(f"unsupported session entry type: {normalized_type!r}")
    entries = conversation.setdefault("session_entries", [])
    if not isinstance(entries, list):
        entries = []
        conversation["session_entries"] = entries
    cursor = _int(conversation.get("session_entry_cursor"))
    normalized_parent = str(
        parent_entry_id or conversation.get("session_entry_leaf_id") or ""
    ).strip()
    if normalized_parent and not any(
        isinstance(item, dict) and item.get("entry_id") == normalized_parent
        for item in entries
    ):
        raise SessionEntryError("parent_entry_id is not present in this conversation")
    key = str(idempotency_key or "").strip()[:512]
    if not key:
        canonical = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = "entry:" + hashlib.sha256(
            f"{normalized_type}\0{normalized_parent}\0{canonical}\0{cursor + 1}".encode("utf-8")
        ).hexdigest()
    for existing in reversed(entries):
        if isinstance(existing, dict) and existing.get("idempotency_key") == key:
            return deepcopy(existing), False
    timestamp = max(0, int(occurred_at if occurred_at is not None else time.time() * 1000))
    entry = {
        "entry_id": f"entry_{uuid.uuid4().hex}",
        "cursor": cursor + 1,
        "parent_entry_id": normalized_parent or None,
        "entry_type": normalized_type,
        "occurred_at": timestamp,
        "idempotency_key": key,
        "payload": _json_copy(payload or {}),
        "schema_version": SCHEMA_VERSION,
    }
    entries.append(entry)
    # Permanent session history is not silently truncated. Compaction changes
    # model context, not the account's append-only audit/history tree.
    conversation["session_entry_cursor"] = cursor + 1
    conversation["session_entry_leaf_id"] = entry["entry_id"]
    return deepcopy(entry), True


def append_message_stream_entries(
    conversation: MutableMapping[str, Any],
    *,
    message_id: str,
    previous_content: str,
    current_content: str,
    status: str,
    role: str,
    name: str,
    kind: str,
    turn_id: str,
    role_stage: str,
    occurred_at: int | None = None,
) -> list[dict[str, Any]]:
    """Append a linear-size audit trail for one mutable streamed message.

    Non-terminal prefix growth is stored as bounded suffix chunks. A provider
    rewrite is represented by a constant-size content reference while the
    authoritative message snapshot remains mutable. The terminal entry stores
    the complete text once, so replay always converges without persisting every
    cumulative streaming snapshot (which would grow quadratically).
    """

    normalized_id = str(message_id or "").strip()
    if not normalized_id:
        raise SessionEntryError("message_id is required")
    previous = str(previous_content or "")
    current = str(current_content or "")
    normalized_status = str(status or "streaming").strip().lower() or "streaming"
    timestamp = occurred_at
    digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
    common = {
        "message_id": normalized_id,
        "update_of": normalized_id,
        "status": normalized_status,
        "kind": str(kind or "message"),
        "turn_id": str(turn_id or normalized_id),
        "role_stage": str(role_stage or "chat"),
        "content_sha256": digest,
        "content_length": len(current),
    }
    appended: list[dict[str, Any]] = []

    if normalized_status in {"completed", "failed", "cancelled"}:
        entry, added = append_session_entry(
            conversation,
            entry_type="message",
            idempotency_key=(
                f"message-final:{normalized_id}:{normalized_status}:{digest}"
            ),
            payload={
                **common,
                "operation": "replace",
                "role": str(role or "assistant"),
                "name": str(name or ""),
                "content": current,
            },
            occurred_at=timestamp,
        )
        if added:
            appended.append(entry)
        return appended

    if current == previous:
        return appended

    if current.startswith(previous):
        suffix = current[len(previous) :]
        for relative_start in range(0, len(suffix), MAX_MESSAGE_DELTA_CHARS):
            chunk = suffix[
                relative_start : relative_start + MAX_MESSAGE_DELTA_CHARS
            ]
            absolute_start = len(previous) + relative_start
            chunk_digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
            entry, added = append_session_entry(
                conversation,
                entry_type="message",
                idempotency_key=(
                    f"message-delta:{normalized_id}:{absolute_start}:"
                    f"{absolute_start + len(chunk)}:{chunk_digest}"
                ),
                payload={
                    **common,
                    "operation": "append",
                    "offset": absolute_start,
                    "content_delta": chunk,
                },
                occurred_at=timestamp,
            )
            if added:
                appended.append(entry)
        return appended

    # Rewrites can happen when a provider replaces a provisional answer. Keep
    # those intermediate audit records bounded; the terminal replacement above
    # persists the final complete text exactly once.
    entry, added = append_session_entry(
        conversation,
        entry_type="message",
        idempotency_key=f"message-reference:{normalized_id}:{digest}",
        payload={
            **common,
            "operation": "reference",
            "content_ref": f"conversation-message:{normalized_id}",
        },
        occurred_at=timestamp,
    )
    if added:
        appended.append(entry)
    return appended


def entries_after(
    conversation: MutableMapping[str, Any],
    cursor: int,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    normalize_session_entries(conversation)
    requested = _int(cursor)
    entries = conversation.get("session_entries")
    if not isinstance(entries, list):
        return []
    return [
        deepcopy(item)
        for item in entries
        if isinstance(item, dict) and _int(item.get("cursor")) > requested
    ][: min(2_000, max(1, int(limit)))]


def branch_entries(
    conversation: MutableMapping[str, Any],
    *,
    from_entry_id: str,
) -> list[dict[str, Any]]:
    """Materialize one root-to-entry branch without mutating permanent history."""

    normalize_session_entries(conversation)
    entries = conversation.get("session_entries")
    if not isinstance(entries, list):
        raise SessionEntryError("conversation has no session entries")
    by_id = {
        str(item.get("entry_id")): item
        for item in entries
        if isinstance(item, dict) and item.get("entry_id")
    }
    current = by_id.get(str(from_entry_id or ""))
    if current is None:
        raise SessionEntryError("branch entry was not found")
    branch: list[dict[str, Any]] = []
    visited: set[str] = set()
    while current is not None:
        current_id = str(current.get("entry_id") or "")
        if current_id in visited:
            raise SessionEntryError("session entry parent cycle detected")
        visited.add(current_id)
        branch.append(deepcopy(current))
        parent_id = str(current.get("parent_entry_id") or "")
        current = by_id.get(parent_id) if parent_id else None
    branch.reverse()
    return branch


def validate_session_entry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionEntryError("session entry must be an object")
    if str(value.get("schema_version") or "") != SCHEMA_VERSION:
        raise SessionEntryError("unsupported session entry schema")
    entry_id = str(value.get("entry_id") or "").strip()
    if not entry_id:
        raise SessionEntryError("session entry id is required")
    entry_type = str(value.get("entry_type") or "").strip().lower()
    if entry_type not in ENTRY_TYPES:
        raise SessionEntryError("unsupported session entry type")
    cursor = _strict_positive_int(value.get("cursor"), "cursor")
    occurred_at = _strict_non_negative_int(value.get("occurred_at"), "occurred_at")
    idempotency_key = str(value.get("idempotency_key") or "").strip()
    if not idempotency_key:
        raise SessionEntryError("session entry idempotency key is required")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise SessionEntryError("session entry payload must be an object")
    parent = str(value.get("parent_entry_id") or "").strip()
    return {
        "entry_id": entry_id,
        "cursor": cursor,
        "parent_entry_id": parent or None,
        "entry_type": entry_type,
        "occurred_at": occurred_at,
        "idempotency_key": idempotency_key[:512],
        "payload": _json_copy(payload),
        "schema_version": SCHEMA_VERSION,
    }


def normalize_session_entries(
    conversation: MutableMapping[str, Any],
) -> list[dict[str, Any]]:
    """Isolate malformed history records while preserving reachable entries."""

    raw = conversation.get("session_entries")
    raw_entries = raw if isinstance(raw, list) else []
    valid: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    known_ids: set[str] = set()
    previous_cursor = 0
    for index, candidate in enumerate(raw_entries):
        try:
            entry = validate_session_entry(candidate)
            entry_id = str(entry["entry_id"])
            parent_id = str(entry.get("parent_entry_id") or "")
            if entry_id in known_ids:
                raise SessionEntryError("duplicate session entry id")
            if int(entry["cursor"]) <= previous_cursor:
                raise SessionEntryError("session entry cursor is not strictly increasing")
            if parent_id and parent_id not in known_ids:
                raise SessionEntryError("session entry parent is missing or unreachable")
        except SessionEntryError as exc:
            diagnostics.append({"index": index, "reason": str(exc)[:240]})
            _quarantine_entry(conversation, candidate, index=index, reason=str(exc))
            continue
        valid.append(entry)
        known_ids.add(entry_id)
        previous_cursor = int(entry["cursor"])
    conversation["session_entries"] = valid
    conversation["session_entry_cursor"] = previous_cursor
    conversation["session_entry_leaf_id"] = (
        str(valid[-1]["entry_id"]) if valid else ""
    )
    if diagnostics:
        conversation["session_entry_diagnostics"] = diagnostics[-200:]
    else:
        conversation.pop("session_entry_diagnostics", None)
    return diagnostics


def _quarantine_entry(
    conversation: MutableMapping[str, Any],
    candidate: Any,
    *,
    index: int,
    reason: str,
) -> None:
    try:
        encoded = json.dumps(
            candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(candidate).encode("utf-8", errors="replace")
    digest = hashlib.sha256(encoded).hexdigest()
    existing = conversation.get("session_entry_quarantine")
    quarantine = existing if isinstance(existing, list) else []
    if any(isinstance(item, dict) and item.get("sha256") == digest for item in quarantine):
        conversation["session_entry_quarantine"] = quarantine[-MAX_QUARANTINED_ENTRIES:]
        return
    record: dict[str, Any] = {
        "sha256": digest,
        "original_index": max(0, int(index)),
        "reason": str(reason or "invalid session entry")[:240],
        "quarantined_at": int(time.time() * 1000),
        "raw_size": len(encoded),
    }
    if len(encoded) <= MAX_QUARANTINED_RAW_BYTES:
        try:
            record["raw"] = _json_copy(candidate)
        except SessionEntryError:
            record["raw_text"] = encoded.decode("utf-8", errors="replace")
    else:
        record["raw_text_preview"] = encoded[:MAX_QUARANTINED_RAW_BYTES].decode(
            "utf-8", errors="replace"
        )
    quarantine.append(record)
    conversation["session_entry_quarantine"] = quarantine[-MAX_QUARANTINED_ENTRIES:]


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _strict_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise SessionEntryError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SessionEntryError(f"{field} must be a positive integer") from exc
    if parsed <= 0 or str(parsed) != str(value):
        raise SessionEntryError(f"{field} must be a positive integer")
    return parsed


def _strict_non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise SessionEntryError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SessionEntryError(f"{field} must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != str(value):
        raise SessionEntryError(f"{field} must be a non-negative integer")
    return parsed


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise SessionEntryError("session entry payload must be JSON serializable") from exc
