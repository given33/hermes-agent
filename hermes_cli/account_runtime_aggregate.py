"""Account-scoped projection of authoritative Hermes runtime records."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


_TERMINAL = {"completed", "failed", "cancelled", "unknown"}


def _timestamp(record: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = record.get(key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _artifact_refs(record: dict[str, Any]) -> list[dict[str, Any]]:
    values = record.get("attachments") or record.get("artifacts") or []
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    for item in values[:100]:
        if isinstance(item, dict):
            result.append(
                {
                    key: item.get(key)
                    for key in ("id", "name", "mime_type", "status", "size")
                    if item.get(key) is not None
                }
            )
    return result


def _hosted_records(
    conversations: list[dict[str, Any]],
    owner_id: str,
    profile: str,
) -> tuple[list[dict[str, Any]], set[str]]:
    records: list[dict[str, Any]] = []
    session_ids: set[str] = set()
    for conversation in conversations:
        if not isinstance(conversation, dict):
            continue
        if str(conversation.get("owner_id") or "").strip() != owner_id:
            continue
        conversation_id = str(conversation.get("id") or "")
        conversation_profile = str(conversation.get("profile") or "default").lower()
        runtime_sessions = conversation.get("runtime_sessions") or {}
        if isinstance(runtime_sessions, dict):
            for mapped_profile, session_id in runtime_sessions.items():
                if str(mapped_profile).lower() == profile and str(session_id).strip():
                    session_ids.add(str(session_id).strip())

        runtime_runs = conversation.get("runtime_runs") or {}
        if isinstance(runtime_runs, dict):
            for run_profile, raw in runtime_runs.items():
                if str(run_profile).lower() != profile or not isinstance(raw, dict):
                    continue
                session_id = str(raw.get("session_id") or "").strip()
                if session_id:
                    session_ids.add(session_id)
                status = str(raw.get("status") or "unknown").lower()
                records.append(
                    {
                        "id": f"chat:{conversation_id}:{run_profile}",
                        "source": "chat",
                        "source_run_id": str(raw.get("turn_id") or session_id),
                        "conversation_id": conversation_id,
                        "profile": profile,
                        "title": str(conversation.get("title") or ""),
                        "status": status,
                        "started_at": _timestamp(raw, "started_at", "created_at"),
                        "updated_at": _timestamp(raw, "updated_at", "completed_at", "started_at"),
                        "completed_at": _timestamp(raw, "completed_at") or None,
                        "current_node": str(raw.get("current_node") or ""),
                        "error": str(raw.get("error") or ""),
                        "session_id": session_id,
                        "terminal": status in _TERMINAL,
                        "cancel_supported": False,
                        "artifacts": _artifact_refs(raw),
                    }
                )

        if conversation_profile != profile:
            continue
        hosted_turns = conversation.get("hosted_turns") or {}
        if not isinstance(hosted_turns, dict):
            continue
        for turn_id, raw in hosted_turns.items():
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or "unknown").lower()
            active_profile = str(raw.get("profile") or profile).lower()
            if active_profile != profile and profile not in {
                str(value).lower() for value in (raw.get("profiles") or [])
            }:
                continue
            session_id = str(raw.get("runtime_session_id") or "").strip()
            if session_id:
                session_ids.add(session_id)
            records.append(
                {
                    "id": f"hosted:{conversation_id}:{turn_id}",
                    "source": "hosted",
                    "source_run_id": str(turn_id),
                    "conversation_id": conversation_id,
                    "profile": profile,
                    "title": str(raw.get("title") or conversation.get("title") or ""),
                    "status": status,
                    "started_at": _timestamp(raw, "started_at", "created_at"),
                    "updated_at": _timestamp(raw, "updated_at", "completed_at", "created_at"),
                    "completed_at": _timestamp(raw, "completed_at") or None,
                    "current_node": str(raw.get("current_node") or raw.get("stage") or ""),
                    "error": str(raw.get("error") or ""),
                    "session_id": session_id,
                    "terminal": status in _TERMINAL,
                    "cancel_supported": status not in _TERMINAL,
                    "cancel_url": (
                        f"/api/plugins/collaboration/single/conversations/"
                        f"{conversation_id}/hosted-turns/{turn_id}/cancel"
                    ),
                    "retry_supported": status in {"failed", "cancelled"},
                    "retry_url": (
                        f"/api/plugins/collaboration/single/conversations/"
                        f"{conversation_id}/hosted-turns/{turn_id}/retry"
                    ),
                    "kanban_task_id": str(raw.get("kanban_task_id") or ""),
                    "artifacts": _artifact_refs(raw),
                }
            )
    return records, session_ids


def _delegation_records(profile_home: Path, session_ids: set[str], profile: str) -> list[dict[str, Any]]:
    """Expose only delegations whose persisted origin is already owner-bound."""

    if not session_ids:
        return []
    path = Path(profile_home) / "state.db"
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='async_delegations'"
        ).fetchone()
        if exists is None:
            return []
        placeholders = ",".join("?" for _ in session_ids)
        rows = conn.execute(
            "SELECT * FROM async_delegations WHERE origin_session IN ("
            + placeholders
            + ") OR origin_ui_session_id IN ("
            + placeholders
            + ") OR parent_session_id IN ("
            + placeholders
            + ") ORDER BY updated_at DESC LIMIT 500",
            tuple(session_ids) * 3,
        ).fetchall()
        for row in rows:
            item = dict(row)
            status = str(item.get("state") or "unknown").lower()
            details: dict[str, Any] = {}
            try:
                decoded = json.loads(item.get("result_json") or "{}")
                if isinstance(decoded, dict):
                    details = decoded
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            result.append(
                {
                    "id": f"delegation:{item['delegation_id']}",
                    "source": "delegation",
                    "source_run_id": item["delegation_id"],
                    "profile": profile,
                    "status": status,
                    "started_at": float(item.get("dispatched_at") or 0),
                    "updated_at": float(item.get("updated_at") or 0),
                    "completed_at": item.get("completed_at"),
                    "error": str(details.get("error") or ""),
                    "terminal": status in _TERMINAL,
                    "cancel_supported": False,
                    "session_id": str(
                        item.get("origin_ui_session_id")
                        or item.get("origin_session")
                        or ""
                    ),
                    "artifacts": _artifact_refs(details),
                }
            )
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except (NameError, sqlite3.Error):
            pass
    return result


def aggregate_account_runtime(
    *,
    owner_id: str,
    profile: str,
    profile_home: Path,
    conversations: list[dict[str, Any]],
    limit: int = 200,
) -> dict[str, Any]:
    """Return persisted source records without inferring client-side status."""

    owner = str(owner_id or "").strip()
    profile_name = str(profile or "").strip().lower()
    if not owner or not profile_name:
        raise ValueError("owner_id and profile are required")
    records, session_ids = _hosted_records(conversations, owner, profile_name)
    records.extend(_delegation_records(Path(profile_home), session_ids, profile_name))
    records.sort(
        key=lambda item: (
            float(item.get("updated_at") or item.get("started_at") or 0),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )
    bounded = max(1, min(int(limit), 500))
    return {
        "owner_id": owner,
        "profile": profile_name,
        "runs": records[:bounded],
        "total": len(records),
        "sources": {
            "chat": "authoritative",
            "hosted": "authoritative",
            "delegation": "authoritative_owner_bound",
            "kanban": "linked_from_hosted_only",
            "cron": "withheld_until_account_binding",
            "profile_worker": "linked_from_hosted_only",
        },
    }
