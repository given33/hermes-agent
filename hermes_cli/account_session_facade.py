"""Account-scoped mobile facade over Hermes' authoritative SessionDB.

The facade adds only ownership bindings and idempotency metadata to state.db.
Messages and sessions remain in the existing authoritative tables.  A branch
is copied at an exact message id in the same SQLite write transaction that
checks the current tip and records the idempotency result; the parent session
is intentionally left active.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from hermes_state import SessionDB


_FACADE_SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_session_account_deletions (
    owner_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    deleted_at REAL NOT NULL,
    PRIMARY KEY(owner_id, profile)
);

CREATE TABLE IF NOT EXISTS mobile_session_bindings (
    owner_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    bound_at REAL NOT NULL,
    PRIMARY KEY(owner_id, profile, session_id)
);
CREATE INDEX IF NOT EXISTS idx_mobile_session_bindings_session
    ON mobile_session_bindings(session_id, owner_id, profile);

CREATE TABLE IF NOT EXISTS mobile_session_forks (
    owner_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    at_message_id INTEGER NOT NULL,
    expected_tip_id INTEGER NOT NULL,
    child_session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(owner_id, profile, idempotency_key),
    UNIQUE(owner_id, profile, child_session_id)
);
"""


class SessionFacadeError(RuntimeError):
    code = "session_facade_error"


class SessionNotFound(SessionFacadeError):
    code = "session_not_found"


class SessionScopeDenied(SessionFacadeError):
    code = "session_scope_denied"


class SessionAccountDeleted(SessionScopeDenied):
    code = "session_account_deleted"


class SessionForkConflict(SessionFacadeError):
    code = "session_fork_conflict"


class AccountSessionFacade:
    def __init__(self, profile_home: Path, profile: str):
        self.profile_home = Path(profile_home)
        self.profile = str(profile or "").strip().lower()
        if not self.profile:
            raise ValueError("profile is required")
        self.db = SessionDB(db_path=self.profile_home / "state.db")
        self.db._execute_write(lambda conn: conn.executescript(_FACADE_SCHEMA))

    @staticmethod
    def _owner(owner_id: str) -> str:
        owner = str(owner_id or "").strip()[:512]
        if not owner:
            raise ValueError("owner_id is required")
        return owner

    @staticmethod
    def _session_id(session_id: str) -> str:
        value = str(session_id or "").strip()
        if not value or len(value) > 512 or any(ch in value for ch in "\r\n\x00"):
            raise ValueError("invalid session_id")
        return value

    def bind_existing(self, *, owner_id: str, session_id: str) -> bool:
        """Bind a session only after the API caller proved external ownership."""

        owner = self._owner(owner_id)
        sid = self._session_id(session_id)

        def _bind(conn):
            if conn.execute(
                "SELECT 1 FROM mobile_session_account_deletions "
                "WHERE owner_id=? AND profile=?",
                (owner, self.profile),
            ).fetchone() is not None:
                raise SessionAccountDeleted("account session scope was deleted")
            if conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone() is None:
                return False
            existing = conn.execute(
                "SELECT DISTINCT owner_id FROM mobile_session_bindings "
                "WHERE profile=? AND session_id=?",
                (self.profile, sid),
            ).fetchall()
            if existing and {str(row["owner_id"]) for row in existing} != {owner}:
                return False
            conn.execute(
                "INSERT OR IGNORE INTO mobile_session_bindings("
                "owner_id,profile,session_id,bound_at) VALUES(?,?,?,?)",
                (owner, self.profile, sid, time.time()),
            )
            return True

        return bool(self.db._execute_write(_bind))

    def is_bound(self, *, owner_id: str, session_id: str) -> bool:
        owner = self._owner(owner_id)
        sid = self._session_id(session_id)
        with self.db._lock:
            deleted = self.db._conn.execute(
                "SELECT 1 FROM mobile_session_account_deletions "
                "WHERE owner_id=? AND profile=?",
                (owner, self.profile),
            ).fetchone()
            if deleted is not None:
                return False
            rows = self.db._conn.execute(
                "SELECT DISTINCT owner_id FROM mobile_session_bindings "
                "WHERE profile=? AND session_id=?",
                (self.profile, sid),
            ).fetchall()
        return {str(row["owner_id"]) for row in rows} == {owner}

    def _require_bound(self, owner_id: str, session_id: str) -> tuple[str, str]:
        owner = self._owner(owner_id)
        sid = self._session_id(session_id)
        if not self.is_bound(owner_id=owner, session_id=sid):
            raise SessionScopeDenied("session is not bound to this account profile")
        return owner, sid

    @staticmethod
    def _fork_hash(
        *, source_session_id: str, at_message_id: int,
        expected_tip_id: int, title: str,
    ) -> str:
        payload = json.dumps(
            {
                "source_session_id": source_session_id,
                "at_message_id": int(at_message_id),
                "expected_tip_id": int(expected_tip_id),
                "title": str(title or "").strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _copy_message_columns(conn) -> list[str]:
        return [
            str(row[1])
            for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            if str(row[1]) not in {"id", "session_id"}
        ]

    @staticmethod
    def _allocate_branch_title(conn, requested_title: str, *, automatic: bool) -> str:
        """Resolve a globally unique session title inside the fork transaction."""

        if not automatic:
            exists = conn.execute(
                "SELECT 1 FROM sessions WHERE title=?", (requested_title,)
            ).fetchone()
            if exists is not None:
                raise SessionForkConflict("session title is already in use")
            return requested_title

        escaped = (
            requested_title.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        rows = conn.execute(
            "SELECT title FROM sessions WHERE title=? OR title LIKE ? ESCAPE '\\'",
            (requested_title, f"{escaped} #%"),
        ).fetchall()
        existing = [str(row["title"]) for row in rows]
        if not existing:
            return requested_title

        max_suffix = 1
        prefix = f"{requested_title} #"
        for existing_title in existing:
            if not existing_title.startswith(prefix):
                continue
            suffix = existing_title[len(prefix):]
            if suffix.isdigit():
                max_suffix = max(max_suffix, int(suffix))
        return f"{requested_title} #{max_suffix + 1}"

    def fork(
        self,
        *,
        owner_id: str,
        source_session_id: str,
        at_message_id: int,
        expected_tip_id: int,
        idempotency_key: str,
        title: str = "",
    ) -> dict[str, Any]:
        owner, source_id = self._require_bound(owner_id, source_session_id)
        boundary = int(at_message_id)
        expected_tip = int(expected_tip_id)
        key = str(idempotency_key or "").strip()[:256]
        if boundary <= 0 or expected_tip <= 0:
            raise ValueError("message ids must be positive integers")
        if not key:
            raise ValueError("idempotency_key is required")
        supplied_title = self.db.sanitize_title(str(title or ""))
        automatic_title = not supplied_title
        clean_title = supplied_title or "Branch"
        request_hash = self._fork_hash(
            source_session_id=source_id,
            at_message_id=boundary,
            expected_tip_id=expected_tip,
            title=clean_title,
        )

        def _fork(conn):
            if conn.execute(
                "SELECT 1 FROM mobile_session_account_deletions "
                "WHERE owner_id=? AND profile=?",
                (owner, self.profile),
            ).fetchone() is not None:
                raise SessionAccountDeleted("account session scope was deleted")
            binding = conn.execute(
                "SELECT 1 FROM mobile_session_bindings "
                "WHERE owner_id=? AND profile=? AND session_id=? "
                "AND NOT EXISTS (SELECT 1 FROM mobile_session_bindings other "
                "WHERE other.profile=? AND other.session_id=? AND other.owner_id<>?)",
                (
                    owner,
                    self.profile,
                    source_id,
                    self.profile,
                    source_id,
                    owner,
                ),
            ).fetchone()
            if binding is None:
                raise SessionScopeDenied("session binding changed")

            replay = conn.execute(
                "SELECT request_hash,child_session_id FROM mobile_session_forks "
                "WHERE owner_id=? AND profile=? AND idempotency_key=?",
                (owner, self.profile, key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise SessionForkConflict("idempotency key was reused with a different request")
                child = conn.execute(
                    "SELECT * FROM sessions WHERE id=?", (replay["child_session_id"],)
                ).fetchone()
                if child is None:
                    raise SessionForkConflict("idempotent fork result is missing")
                return dict(child), True

            source = conn.execute(
                "SELECT * FROM sessions WHERE id=?", (source_id,)
            ).fetchone()
            if source is None:
                raise SessionNotFound("source session does not exist")
            message = conn.execute(
                "SELECT id FROM messages WHERE session_id=? AND id=? AND active=1",
                (source_id, boundary),
            ).fetchone()
            if message is None:
                raise SessionNotFound("branch message is not an active message in this session")
            tip = conn.execute(
                "SELECT id FROM messages WHERE session_id=? AND active=1 "
                "ORDER BY id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            if tip is None or int(tip["id"]) != expected_tip:
                raise SessionForkConflict("session tip changed; reload before branching")

            lock = conn.execute(
                "SELECT holder FROM compression_locks "
                "WHERE session_id=? AND expires_at>?",
                (source_id, time.time()),
            ).fetchone()
            if lock is not None:
                raise SessionForkConflict("session compression is in progress")

            child_id = f"mobile_branch_{uuid.uuid4().hex}"
            now = time.time()
            allocated_title = self._allocate_branch_title(
                conn, clean_title, automatic=automatic_title
            )
            conn.execute(
                "INSERT INTO sessions("
                "id,source,user_id,model,model_config,system_prompt,parent_session_id,"
                "started_at,cwd,git_branch,git_repo_root,title,profile_name) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    child_id,
                    "mobile_branch",
                    source["user_id"],
                    source["model"],
                    source["model_config"],
                    source["system_prompt"],
                    source_id,
                    now,
                    source["cwd"],
                    source["git_branch"],
                    source["git_repo_root"],
                    allocated_title,
                    self.profile,
                ),
            )
            columns = self._copy_message_columns(conn)
            quoted = ",".join(f'"{column}"' for column in columns)
            conn.execute(
                f'INSERT INTO messages(session_id,{quoted}) '
                f'SELECT ?,{quoted} FROM messages '
                "WHERE session_id=? AND active=1 AND id<=? ORDER BY id",
                (child_id, source_id, boundary),
            )
            counts = conn.execute(
                "SELECT COUNT(*) AS messages,"
                "COALESCE(SUM(CASE WHEN role='tool' OR tool_calls IS NOT NULL "
                "THEN 1 ELSE 0 END),0) AS tools "
                "FROM messages WHERE session_id=? AND active=1",
                (child_id,),
            ).fetchone()
            conn.execute(
                "UPDATE sessions SET message_count=?,tool_call_count=? WHERE id=?",
                (int(counts["messages"]), int(counts["tools"]), child_id),
            )
            conn.execute(
                "INSERT INTO mobile_session_bindings("
                "owner_id,profile,session_id,bound_at) VALUES(?,?,?,?)",
                (owner, self.profile, child_id, now),
            )
            conn.execute(
                "INSERT INTO mobile_session_forks("
                "owner_id,profile,idempotency_key,request_hash,source_session_id,"
                "at_message_id,expected_tip_id,child_session_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    owner, self.profile, key, request_hash, source_id,
                    boundary, expected_tip, child_id, now,
                ),
            )
            child = conn.execute(
                "SELECT * FROM sessions WHERE id=?", (child_id,)
            ).fetchone()
            return dict(child), False

        session, replayed = self.db._execute_write(_fork)
        return {"session": session, "replayed": replayed}

    def lineage(self, *, owner_id: str, session_id: str) -> dict[str, Any]:
        owner, sid = self._require_bound(owner_id, session_id)
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT s.* FROM sessions s JOIN mobile_session_bindings b "
                "ON b.session_id=s.id WHERE b.owner_id=? AND b.profile=? "
                "ORDER BY s.started_at,s.id",
                (owner, self.profile),
            ).fetchall()
        sessions = [dict(row) for row in rows]
        by_id = {str(item["id"]): item for item in sessions}
        if sid not in by_id:
            raise SessionScopeDenied("session binding changed")
        component: set[str] = {sid}
        changed = True
        while changed:
            changed = False
            for item in sessions:
                item_id = str(item["id"])
                parent = str(item.get("parent_session_id") or "")
                if item_id in component or parent in component:
                    before = len(component)
                    component.add(item_id)
                    if parent in by_id:
                        component.add(parent)
                    changed = changed or len(component) != before
        selected = [item for item in sessions if str(item["id"]) in component]
        edges = [
            {"parent_id": item["parent_session_id"], "child_id": item["id"]}
            for item in selected
            if item.get("parent_session_id") in component
        ]
        roots = [item["id"] for item in selected if item.get("parent_session_id") not in component]
        return {"current_session_id": sid, "roots": roots, "sessions": selected, "edges": edges}

    def context(self, *, owner_id: str, session_id: str) -> dict[str, Any]:
        _owner, sid = self._require_bound(owner_id, session_id)
        session = self.db.get_session(sid)
        if session is None:
            raise SessionNotFound("session does not exist")
        with self.db._lock:
            stats = self.db._conn.execute(
                "SELECT COUNT(*) AS active_messages,"
                "COALESCE(SUM(COALESCE(token_count,0)),0) AS message_tokens,"
                "MAX(id) AS tip_message_id "
                "FROM messages WHERE session_id=? AND active=1",
                (sid,),
            ).fetchone()
            archived = self.db._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=? AND active=0",
                (sid,),
            ).fetchone()[0]
        compression_lineage = self.db.get_compression_lineage(sid)
        return {
            "session_id": sid,
            "profile": self.profile,
            "model": session.get("model"),
            "active_messages": int(stats["active_messages"]),
            "archived_messages": int(archived),
            "message_tokens": int(stats["message_tokens"]),
            "input_tokens": int(session.get("input_tokens") or 0),
            "output_tokens": int(session.get("output_tokens") or 0),
            "cache_read_tokens": int(session.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(session.get("cache_write_tokens") or 0),
            "reasoning_tokens": int(session.get("reasoning_tokens") or 0),
            "tip_message_id": stats["tip_message_id"],
            "compression_lineage": compression_lineage,
            "compression_count": max(0, len(compression_lineage) - 1),
            "compression_in_progress": self.db.get_compression_lock_holder(sid) is not None,
        }

    def delete_owner(self, owner_id: str) -> dict[str, int]:
        """Delete every branch created inside this account/profile boundary."""

        owner = self._owner(owner_id)

        # The permanent fence is committed before any sidecar cleanup. Every
        # bind/fork transaction checks it after acquiring the same SQLite write
        # lock, so a concurrent fork either commits before this fence (and is
        # included below) or observes deletion and aborts.
        def _fence_and_list(conn):
            conn.execute(
                "INSERT OR IGNORE INTO mobile_session_account_deletions("
                "owner_id,profile,deleted_at) VALUES(?,?,?)",
                (owner, self.profile, time.time()),
            )
            return [
                str(row["child_session_id"])
                for row in conn.execute(
                    "SELECT DISTINCT f.child_session_id FROM mobile_session_forks f "
                    "WHERE f.owner_id=? AND f.profile=? "
                    "AND NOT EXISTS (SELECT 1 FROM mobile_session_bindings b "
                    "WHERE b.profile=f.profile AND b.session_id=f.child_session_id "
                    "AND b.owner_id<>f.owner_id) "
                    "AND NOT EXISTS (SELECT 1 FROM mobile_session_forks other "
                    "WHERE other.profile=f.profile "
                    "AND other.child_session_id=f.child_session_id "
                    "AND other.owner_id<>f.owner_id)",
                    (owner, self.profile),
                ).fetchall()
            ]

        child_ids = self.db._execute_write(_fence_and_list)
        deleted_sessions = self.db.delete_sessions(
            child_ids,
            sessions_dir=self.profile_home / "sessions",
        )

        def _delete_metadata(conn):
            fork_count = conn.execute(
                "DELETE FROM mobile_session_forks WHERE owner_id=? AND profile=?",
                (owner, self.profile),
            ).rowcount
            binding_count = conn.execute(
                "DELETE FROM mobile_session_bindings WHERE owner_id=? AND profile=?",
                (owner, self.profile),
            ).rowcount
            return int(fork_count), int(binding_count)

        fork_count, binding_count = self.db._execute_write(_delete_metadata)
        return {
            "branch_sessions": int(deleted_sessions),
            "fork_records": fork_count,
            "bindings": binding_count,
        }
