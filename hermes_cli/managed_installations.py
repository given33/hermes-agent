"""Durable, allowlisted installation orchestration for managed Hermes nodes.

The main server owns the operation log. DBB3 and WSL expose the same narrow
receiver protocol and execute only structured skill, MCP, or project installs.
No endpoint accepts a command line, environment value, or credential payload.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from hermes_constants import get_hermes_home


KINDS = frozenset({"skill", "mcp", "project"})
NODES = frozenset({"server", "dbb3", "wsl"})
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_STATES = frozenset({"pending", "accepted", "running", "retry"})
MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 15 * 60
DEFAULT_LEASE_SECONDS = 30.0
REMOTE_POLL_INTERVAL_SECONDS = 1.0
MAX_INSTALL_ATTEMPTS = 8
MAX_ERROR_LENGTH = 2048
PROBE_KIND = "probe"
PROBE_IDENTIFIER = "managed-installation-route-probe"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+#=-]{0,511}$")
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;]+"
)
_THREADS_LOCK = threading.Lock()
_RECEIVER_THREADS: dict[str, threading.Thread] = {}


class _ExecutionFence:
    """An OS-backed target lock held for the full side-effect window."""

    def __init__(self, file_handle: Any) -> None:
        self.file_handle = file_handle
        self.released = False

    @property
    def fd(self) -> int:
        return self.file_handle.fileno()

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            if os.name == "nt":
                import msvcrt

                self.file_handle.seek(0)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            self.file_handle.close()


def _try_execution_fence(path: Path, operation_id: str, node_id: str) -> _ExecutionFence | None:
    lock_root = path.parent / ".managed-installation-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{operation_id}:{node_id}".encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{digest}.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        if os.name == "nt":
            import msvcrt

            if lock_path.stat().st_size == 0:
                handle.write(b"0")
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        return None
    return _ExecutionFence(handle)


def _release_execution_fence(claimed: dict[str, Any]) -> None:
    fence = claimed.pop("_execution_fence", None)
    if isinstance(fence, _ExecutionFence):
        fence.release()


def managed_installations_db_path() -> Path:
    return Path(get_hermes_home()) / "managed-installations.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS managed_installations (
          id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL UNIQUE,
          kind TEXT NOT NULL,
          identifier TEXT NOT NULL,
          profile TEXT NOT NULL,
          scope TEXT NOT NULL,
          locality TEXT NOT NULL,
          project_name TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS managed_installation_targets (
          operation_id TEXT NOT NULL,
          node_id TEXT NOT NULL,
          state TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          failure_count INTEGER NOT NULL DEFAULT 0,
          next_attempt_at REAL NOT NULL DEFAULT 0,
          lease_token TEXT NOT NULL DEFAULT '',
          lease_until REAL NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL,
          error TEXT NOT NULL DEFAULT '',
          detail_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY (operation_id, node_id),
          FOREIGN KEY (operation_id) REFERENCES managed_installations(id)
            ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS managed_installation_targets_ready
          ON managed_installation_targets(state, next_attempt_at, lease_until);
        """
    )
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(managed_installation_targets)").fetchall()
    }
    if "failure_count" not in columns:
        conn.execute(
            "ALTER TABLE managed_installation_targets "
            "ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
        )
    return conn


def _normalize_kind(value: str) -> str:
    kind = str(value or "").strip().lower()
    if kind not in KINDS:
        raise ValueError("kind must be skill, mcp, or project")
    return kind


def _normalize_identifier(kind: str, value: str) -> str:
    identifier = str(value or "").strip()
    if not identifier or len(identifier) > 1024 or "\x00" in identifier:
        raise ValueError("identifier is required")
    if kind == "project":
        if "://" not in identifier:
            if not re.fullmatch(r"github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?", identifier):
                raise ValueError("project source must be an HTTPS git repository")
            identifier = f"https://{identifier.rstrip('/')}"
        parsed = urlsplit(identifier)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "https" and parsed.username is not None)
        ):
            raise ValueError("project source must be an HTTPS git repository")
        return identifier
    if kind == "mcp" and "://" in identifier:
        raise ValueError("MCP identifier must be a catalog name")
    if identifier.startswith("https://"):
        parsed = urlsplit(identifier)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("HTTPS identifiers must not contain credentials, query, or fragment")
        return identifier
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError("identifier contains unsupported characters")
    return identifier


def resolve_installation_targets(
    kind: str,
    *,
    scope: str = "auto",
    locality: str = "portable",
    targets: Iterable[str] | None = None,
) -> list[str]:
    """Resolve the product policy into an explicit, stable node list."""

    normalized_kind = _normalize_kind(kind)
    explicit = [str(item).strip().lower() for item in (targets or []) if str(item).strip()]
    if explicit:
        if len(set(explicit)) != len(explicit) or any(item not in NODES for item in explicit):
            raise ValueError("targets must contain unique server, dbb3, or wsl values")
        return [node for node in ("server", "dbb3", "wsl") if node in explicit]

    normalized_scope = str(scope or "auto").strip().lower()
    if normalized_scope in {"fleet", "all"}:
        return ["server", "dbb3", "wsl"]
    if normalized_scope == "server":
        return ["server"]
    if normalized_scope in {"workers", "worker"}:
        return ["dbb3", "wsl"]
    if normalized_scope != "auto":
        raise ValueError("scope must be auto, fleet, server, or workers")

    if normalized_kind == "skill":
        return ["server", "dbb3", "wsl"]
    if normalized_kind == "project":
        return ["dbb3", "wsl"]

    normalized_locality = str(locality or "portable").strip().lower()
    if normalized_locality in {"portable", "network", "ios-relay", "auto"}:
        return ["server", "dbb3", "wsl"]
    if normalized_locality == "server":
        return ["server"]
    if normalized_locality in {"worker", "workers"}:
        return ["dbb3", "wsl"]
    if normalized_locality == "node":
        raise ValueError("node-local MCP installation requires explicit targets")
    raise ValueError("unsupported MCP locality")


def require_managed_installation_topology(
    targets: Iterable[str],
    *,
    config_path: Path | None = None,
) -> None:
    """Fail before persistence when the main-server fleet is not deployable."""

    from hermes_cli.managed_nodes import load_managed_nodes_config

    resolved_targets = [str(item).strip().lower() for item in targets]
    configs = load_managed_nodes_config(config_path)
    if not configs:
        raise RuntimeError("managed-nodes configuration is required for fleet installations")
    for node_id in resolved_targets:
        if node_id != "server":
            _installation_route(node_id, config_path)


def create_managed_installation(
    *,
    kind: str,
    identifier: str,
    profile: str = "default",
    request_id: str = "",
    scope: str = "auto",
    locality: str = "portable",
    targets: Iterable[str] | None = None,
    project_name: str = "",
    db_path: Path | None = None,
    require_topology: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    normalized_kind = _normalize_kind(kind)
    normalized_identifier = _normalize_identifier(normalized_kind, identifier)
    normalized_profile = str(profile or "default").strip() or "default"
    if len(normalized_profile) > 80 or not re.fullmatch(r"[A-Za-z0-9._-]+", normalized_profile):
        raise ValueError("profile contains unsupported characters")
    normalized_project_name = str(project_name or "").strip()
    if normalized_project_name and not _PROJECT_NAME_RE.fullmatch(normalized_project_name):
        raise ValueError("project_name contains unsupported characters")
    normalized_request_id = str(request_id or "").strip() or f"install-{uuid4()}"
    if len(normalized_request_id) > 160 or "\x00" in normalized_request_id:
        raise ValueError("request_id is invalid")
    resolved_targets = resolve_installation_targets(
        normalized_kind,
        scope=scope,
        locality=locality,
        targets=targets,
    )
    if require_topology:
        require_managed_installation_topology(resolved_targets, config_path=config_path)
    path = db_path or managed_installations_db_path()
    now = _utc_now()
    operation_id = f"mi-{uuid4()}"
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM managed_installations WHERE request_id = ?",
            (normalized_request_id,),
        ).fetchone()
        if existing:
            existing_targets = [
                str(row["node_id"])
                for row in conn.execute(
                    "SELECT node_id FROM managed_installation_targets "
                    "WHERE operation_id = ? ORDER BY CASE node_id "
                    "WHEN 'server' THEN 0 WHEN 'dbb3' THEN 1 ELSE 2 END",
                    (existing["id"],),
                ).fetchall()
            ]
            expected = (
                normalized_kind,
                normalized_identifier,
                normalized_profile,
                str(scope or "auto").strip().lower(),
                str(locality or "portable").strip().lower(),
                normalized_project_name,
                resolved_targets,
            )
            actual = (
                str(existing["kind"]),
                str(existing["identifier"]),
                str(existing["profile"]),
                str(existing["scope"]),
                str(existing["locality"]),
                str(existing["project_name"]),
                existing_targets,
            )
            if actual != expected:
                conn.execute("ROLLBACK")
                raise ValueError("request_id is already bound to a different installation")
            conn.execute("COMMIT")
            return get_managed_installation(str(existing["id"]), db_path=path)
        conn.execute(
            """
            INSERT INTO managed_installations(
              id, request_id, kind, identifier, profile, scope, locality,
              project_name, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
            """,
            (
                operation_id,
                normalized_request_id,
                normalized_kind,
                normalized_identifier,
                normalized_profile,
                str(scope or "auto").strip().lower(),
                str(locality or "portable").strip().lower(),
                normalized_project_name,
                now,
                now,
            ),
        )
        conn.executemany(
            """
            INSERT INTO managed_installation_targets(
              operation_id, node_id, state, updated_at
            ) VALUES (?, ?, 'pending', ?)
            """,
            [(operation_id, node_id, now) for node_id in resolved_targets],
        )
        conn.execute("COMMIT")
    return get_managed_installation(operation_id, db_path=path)


def _operation_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    targets = conn.execute(
        """
        SELECT node_id, state, attempts, failure_count, updated_at, error, detail_json
        FROM managed_installation_targets
        WHERE operation_id = ? ORDER BY CASE node_id
          WHEN 'server' THEN 0 WHEN 'dbb3' THEN 1 ELSE 2 END
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "request_id": row["request_id"],
        "kind": row["kind"],
        "identifier": row["identifier"],
        "profile": row["profile"],
        "scope": row["scope"],
        "locality": row["locality"],
        "project_name": row["project_name"],
        "state": row["state"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "error": row["error"],
        "targets": [
            {
                "node_id": target["node_id"],
                "state": target["state"],
                "attempts": int(target["attempts"]),
                "failure_count": int(target["failure_count"]),
                "updated_at": target["updated_at"],
                "error": target["error"],
                "detail": _safe_json_object(target["detail_json"]),
            }
            for target in targets
        ],
    }


def get_managed_installation(operation_id: str, *, db_path: Path | None = None) -> dict[str, Any]:
    with closing(_connect(db_path or managed_installations_db_path())) as conn:
        row = conn.execute(
            "SELECT * FROM managed_installations WHERE id = ?",
            (str(operation_id or "").strip(),),
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return _operation_payload(conn, row)


def list_managed_installations(
    *,
    kind: str = "",
    profile: str = "",
    limit: int = 50,
    db_path: Path | None = None,
) -> dict[str, Any]:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind and normalized_kind not in KINDS:
        raise ValueError("kind must be skill, mcp, project, or empty")
    normalized_profile = str(profile or "").strip()
    sql = "SELECT * FROM managed_installations"
    params: list[Any] = []
    clauses: list[str] = []
    if normalized_kind:
        clauses.append("kind = ?")
        params.append(normalized_kind)
    if normalized_profile:
        clauses.append("profile = ?")
        params.append(normalized_profile)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    with closing(_connect(db_path or managed_installations_db_path())) as conn:
        rows = conn.execute(sql, params).fetchall()
        return {"operations": [_operation_payload(conn, row) for row in rows]}


def _safe_json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
        return decoded if isinstance(decoded, dict) else {}
    except json.JSONDecodeError:
        return {}


def _redact_error(value: Any) -> str:
    text = str(value or "").replace("\x00", " ")
    return _SECRET_RE.sub(
        lambda match: re.split(r"[=:]", match.group(0), maxsplit=1)[0] + "=<redacted>",
        text,
    )[:MAX_ERROR_LENGTH]


def _recompute_operation(conn: sqlite3.Connection, operation_id: str) -> None:
    rows = conn.execute(
        "SELECT state, error FROM managed_installation_targets WHERE operation_id = ?",
        (operation_id,),
    ).fetchall()
    states = [str(row["state"]) for row in rows]
    if states and all(state == "completed" for state in states):
        state, error = "completed", ""
    elif states and all(state in TERMINAL_STATES for state in states):
        state = "failed"
        error = next((str(row["error"]) for row in rows if row["error"]), "installation failed")
    elif any(state == "running" for state in states):
        state, error = "running", ""
    elif any(state in {"accepted", "retry"} for state in states):
        state, error = "dispatching", ""
    else:
        state, error = "accepted", ""
    conn.execute(
        "UPDATE managed_installations SET state = ?, error = ?, updated_at = ? WHERE id = ?",
        (state, error, _utc_now(), operation_id),
    )


def _claim_target(
    path: Path,
    *,
    now: float,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any] | None:
    token = uuid4().hex
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT t.operation_id, t.node_id
            FROM managed_installation_targets t
            WHERE t.state IN ('pending', 'accepted', 'running', 'retry')
              AND t.next_attempt_at <= ?
              AND (t.lease_until <= ? OR t.lease_token = '')
            ORDER BY t.updated_at ASC LIMIT 1
            """,
            (now, now),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        fence = _try_execution_fence(path, str(row["operation_id"]), str(row["node_id"]))
        if fence is None:
            conn.execute("COMMIT")
            return None
        try:
            conn.execute(
                """
                UPDATE managed_installation_targets
                SET lease_token = ?, lease_until = ?, attempts = attempts + 1,
                    updated_at = ?
                WHERE operation_id = ? AND node_id = ?
                """,
                (token, now + lease_seconds, _utc_now(), row["operation_id"], row["node_id"]),
            )
            operation = conn.execute(
                "SELECT * FROM managed_installations WHERE id = ?",
                (row["operation_id"],),
            ).fetchone()
            target = conn.execute(
                "SELECT * FROM managed_installation_targets "
                "WHERE operation_id = ? AND node_id = ?",
                (row["operation_id"], row["node_id"]),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            fence.release()
            raise
        result = dict(operation)
        result["target_state"] = target["state"]
        result["node_id"] = target["node_id"]
        result["attempts"] = int(target["attempts"])
        result["failure_count"] = int(target["failure_count"])
        result["lease_token"] = token
        result["lease_until"] = float(target["lease_until"])
        result["_execution_fence"] = fence
        return result


def _renew_target_lease(
    path: Path,
    claimed: dict[str, Any],
    lease_seconds: float,
) -> float | None:
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        renewed_at = time.time()
        lease_deadline = renewed_at + lease_seconds
        updated = conn.execute(
            """
            UPDATE managed_installation_targets SET lease_until = ?, updated_at = ?
            WHERE operation_id = ? AND node_id = ? AND lease_token = ?
              AND state NOT IN ('completed', 'failed', 'cancelled')
              AND lease_until > ?
            """,
            (
                lease_deadline,
                _utc_now(),
                claimed["id"],
                claimed["node_id"],
                claimed["lease_token"],
                renewed_at,
            ),
        ).rowcount
        conn.execute("COMMIT")
        return lease_deadline if updated else None


class _LeaseHeartbeat:
    """Keep a claim exclusive while an allowlisted install or HTTP call is in flight."""

    def __init__(
        self,
        path: Path,
        claimed: dict[str, Any],
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.path = path
        self.claimed = claimed
        self.lease_seconds = max(1.0, lease_seconds)
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._state_lock = threading.Lock()
        self._lease_deadline = float(claimed.get("lease_until") or time.time())
        self.last_successful_renewal = time.time()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"managed-install-lease-{str(claimed['id'])[-8:]}",
        )

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=min(2.0, self.lease_seconds))
        self._mark_lost_if_expired()

    def _mark_lost_if_expired(self) -> bool:
        with self._state_lock:
            if time.time() >= self._lease_deadline:
                self.lost.set()
        return self.lost.is_set()

    def ensure_owned(self) -> None:
        if self._mark_lost_if_expired():
            raise RuntimeError("managed installation lease was lost")

    def _run(self) -> None:
        interval = max(0.25, self.lease_seconds / 3)
        while True:
            if self._mark_lost_if_expired():
                return
            with self._state_lock:
                remaining = max(0.0, self._lease_deadline - time.time())
            if self._stop.wait(min(interval, remaining)):
                return
            if self._mark_lost_if_expired():
                return
            try:
                lease_deadline = _renew_target_lease(
                    self.path,
                    self.claimed,
                    self.lease_seconds,
                )
                if lease_deadline is None:
                    self.lost.set()
                    return
                with self._state_lock:
                    self.last_successful_renewal = lease_deadline - self.lease_seconds
                    self._lease_deadline = lease_deadline
            except sqlite3.Error:
                # Retrying is safe only while the last confirmed lease is still live.
                if self._mark_lost_if_expired():
                    return
                continue


def _finish_target(
    path: Path,
    claimed: dict[str, Any],
    *,
    state: str,
    error: str = "",
    detail: dict[str, Any] | None = None,
    retry_after: float = 0,
    failure_count: int | None = None,
) -> bool:
    now = time.time()
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """
            UPDATE managed_installation_targets
            SET state = ?, error = ?, detail_json = ?, next_attempt_at = ?,
                failure_count = COALESCE(?, failure_count),
                lease_token = '', lease_until = 0, updated_at = ?
            WHERE operation_id = ? AND node_id = ? AND lease_token = ?
              AND state NOT IN ('completed', 'failed', 'cancelled')
              AND lease_until > ?
            """,
            (
                state,
                _redact_error(error),
                json.dumps(detail or {}, ensure_ascii=True, separators=(",", ":")),
                now + max(0, retry_after),
                failure_count,
                _utc_now(),
                claimed["id"],
                claimed["node_id"],
                claimed["lease_token"],
                now,
            ),
        ).rowcount
        if updated:
            _recompute_operation(conn, claimed["id"])
        conn.execute("COMMIT")
        return bool(updated)


def dispatch_managed_installations_once(
    *,
    db_path: Path | None = None,
    config_path: Path | None = None,
    executor: Any = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> bool:
    path = db_path or managed_installations_db_path()
    claimed = _claim_target(path, now=time.time(), lease_seconds=lease_seconds)
    if claimed is None:
        return False
    try:
        with _LeaseHeartbeat(path, claimed, lease_seconds=lease_seconds) as heartbeat:
            if claimed["node_id"] == "server":
                heartbeat.ensure_owned()
                detail = _execute_allowlisted_installation(
                    claimed,
                    executor=executor,
                    ownership_guard=heartbeat.ensure_owned,
                )
                heartbeat.ensure_owned()
                if not heartbeat.lost.is_set():
                    _finish_target(
                        path,
                        claimed,
                        state="completed",
                        detail=detail,
                        failure_count=0,
                    )
            else:
                heartbeat.ensure_owned()
                remote = _dispatch_or_poll_remote(claimed, config_path=config_path)
                heartbeat.ensure_owned()
                if heartbeat.lost.is_set():
                    return True
                remote_state = str(remote.get("state") or "running").lower()
                if remote_state == "completed":
                    _finish_target(
                        path, claimed, state="completed", detail=remote, failure_count=0,
                    )
                elif remote_state in {"failed", "cancelled"}:
                    _finish_target(
                        path,
                        claimed,
                        state="failed",
                        error=str(remote.get("error") or "remote installation failed"),
                        detail=remote,
                    )
                else:
                    _finish_target(
                        path,
                        claimed,
                        state="running",
                        detail=remote,
                        retry_after=REMOTE_POLL_INTERVAL_SECONDS,
                        failure_count=0,
                    )
    except Exception as exc:
        failures = int(claimed.get("failure_count") or 0) + 1
        terminal = failures >= MAX_INSTALL_ATTEMPTS
        _finish_target(
            path,
            claimed,
            state="failed" if terminal else "retry",
            error=exc,
            retry_after=0 if terminal else min(60, 2 ** min(failures, 5)),
            failure_count=failures,
        )
    finally:
        _release_execution_fence(claimed)
    return True


def _dispatch_or_poll_remote(
    claimed: dict[str, Any],
    *,
    config_path: Path | None,
) -> dict[str, Any]:
    route = _installation_route(str(claimed["node_id"]), config_path)
    operation_id = quote(str(claimed["id"]), safe="")
    if str(claimed.get("target_state") or "") in {"pending", "accepted", "retry"}:
        body = json.dumps({
            "id": claimed["id"],
            "request_id": claimed["request_id"],
            "node_id": claimed["node_id"],
            "kind": claimed["kind"],
            "identifier": claimed["identifier"],
            "profile": claimed["profile"],
            "project_name": claimed["project_name"],
        }, separators=(",", ":")).encode("utf-8")
        request = Request(
            route["url"],
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-DBB3-Token": route["token"],
            },
            method="POST",
        )
    else:
        request = Request(
            f"{route['url'].rstrip('/')}/{operation_id}",
            headers={"Accept": "application/json", "X-DBB3-Token": route["token"]},
            method="GET",
        )
    return _read_json_response(request, route["timeout"])


def _installation_route(node_id: str, config_path: Path | None) -> dict[str, Any]:
    from hermes_cli.managed_nodes import load_managed_nodes_config, read_private_token

    for config in load_managed_nodes_config(config_path):
        url = str((config.get("installation_urls") or {}).get(node_id) or "").strip()
        if not url:
            continue
        token_path = str(config.get("installation_token_file") or "").strip()
        if not token_path:
            raise RuntimeError(
                f"managed installation route for {node_id} has no dedicated credential"
            )
        if (
            Path(token_path).expanduser().resolve(strict=False)
            == Path(str(config.get("token_file") or "")).expanduser().resolve(strict=False)
        ):
            raise RuntimeError(
                f"managed installation route for {node_id} reuses the status credential"
            )
        token = read_private_token(token_path, label="managed installation credential")
        return {"url": url, "token": token, "timeout": config["timeout_seconds"]}
    raise RuntimeError(f"managed installation route for {node_id} is not configured")


def _read_json_response(request: Request, timeout: float) -> dict[str, Any]:
    with urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("managed installation response exceeded the size limit")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("managed installation response must be an object")
    return payload


def run_managed_installation_dispatcher(
    stop_event: threading.Event,
    *,
    db_path: Path | None = None,
    config_path: Path | None = None,
    interval: float = 1.0,
) -> None:
    while not stop_event.is_set():
        worked = dispatch_managed_installations_once(db_path=db_path, config_path=config_path)
        stop_event.wait(0 if worked else max(0.1, interval))


def load_managed_installation_receiver_config(
    path: Path | None = None,
) -> dict[str, Any] | None:
    from hermes_cli.managed_nodes import managed_nodes_config_path

    config_path = path or managed_nodes_config_path()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid managed-nodes configuration: {exc}") from exc
    raw = payload.get("installation_receiver") if isinstance(payload, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("installation_receiver must be an object")
    node_id = str(raw.get("node_id") or "").strip().lower()
    if node_id not in {"dbb3", "wsl"}:
        raise ValueError("installation_receiver node_id must be dbb3 or wsl")
    token_file = str(raw.get("token_file") or "").strip()
    if not token_file:
        raise ValueError("installation_receiver requires token_file")
    state_file = str(raw.get("state_file") or "managed-installations.db").strip()
    project_root = str(raw.get("project_root") or "managed-projects").strip()
    state_path = Path(state_file)
    root_path = Path(project_root)
    if not state_path.is_absolute():
        state_path = config_path.parent / state_path
    if not root_path.is_absolute():
        root_path = config_path.parent / root_path
    return {
        "node_id": node_id,
        "token_file": token_file,
        "state_file": state_path.resolve(),
        "project_root": root_path.resolve(),
    }


def accept_managed_installation(
    payload: dict[str, Any],
    presented_token: str,
    config_path: Path | None = None,
) -> dict[str, Any]:
    config = load_managed_installation_receiver_config(config_path)
    if config is None:
        raise RuntimeError("managed installation receiver is not configured")
    _authenticate_receiver(config, presented_token)
    node_id = str(payload.get("node_id") or "").strip().lower()
    if node_id != config["node_id"]:
        raise ValueError("installation target does not match this node")
    is_probe = payload.get("probe") is True
    if is_probe and (
        str(payload.get("kind") or "").strip().lower() != PROBE_KIND
        or str(payload.get("identifier") or "").strip() != PROBE_IDENTIFIER
    ):
        raise ValueError("managed installation probe contract is invalid")
    operation = create_managed_installation(
        kind="skill" if is_probe else str(payload.get("kind") or ""),
        identifier=(
            "builtin/managed-installation-route-probe"
            if is_probe
            else str(payload.get("identifier") or "")
        ),
        profile=str(payload.get("profile") or "default"),
        request_id=str(payload.get("id") or payload.get("request_id") or ""),
        scope="auto",
        targets=[node_id],
        project_name=str(payload.get("project_name") or ""),
        db_path=config["state_file"],
    )
    if is_probe:
        claimed, _wait_seconds = _claim_received_target(
            Path(config["state_file"]),
            operation["id"],
            node_id,
        )
        if claimed is not None:
            try:
                _finish_target(
                    Path(config["state_file"]),
                    claimed,
                    state="completed",
                    detail={"probe": True, "persisted": True, "node_id": node_id},
                    failure_count=0,
                )
            finally:
                _release_execution_fence(claimed)
    else:
        _start_receiver_thread(operation["id"], config)
    current = get_managed_installation(operation["id"], db_path=config["state_file"])
    target = current["targets"][0]
    return {
        "accepted": True,
        "id": str(payload.get("id") or operation["id"]),
        "state": target["state"],
        "node_id": node_id,
    }


def get_received_managed_installation(
    operation_id: str,
    presented_token: str,
    config_path: Path | None = None,
) -> dict[str, Any]:
    config = load_managed_installation_receiver_config(config_path)
    if config is None:
        raise RuntimeError("managed installation receiver is not configured")
    _authenticate_receiver(config, presented_token)
    # The receiver uses the main operation id as its idempotency key.
    with closing(_connect(config["state_file"])) as conn:
        row = conn.execute(
            "SELECT id FROM managed_installations WHERE request_id = ? OR id = ?",
            (operation_id, operation_id),
        ).fetchone()
    if row is None:
        raise KeyError(operation_id)
    current = get_managed_installation(str(row["id"]), db_path=config["state_file"])
    target = current["targets"][0]
    return {
        "id": operation_id,
        "node_id": target["node_id"],
        "state": target["state"],
        "error": target["error"],
        "detail": target["detail"],
        "updated_at": target["updated_at"],
    }


def resume_received_managed_installations(config_path: Path | None = None) -> int:
    config = load_managed_installation_receiver_config(config_path)
    if config is None:
        return 0
    with closing(_connect(config["state_file"])) as conn:
        conn.execute("BEGIN IMMEDIATE")
        ids = [
            str(row["id"])
            for row in conn.execute(
                """
                SELECT DISTINCT i.id FROM managed_installations i
                JOIN managed_installation_targets t ON t.operation_id = i.id
                WHERE t.state IN ('pending', 'accepted', 'running', 'retry')
                """
            ).fetchall()
        ]
        conn.execute("COMMIT")
    for operation_id in ids:
        _start_receiver_thread(operation_id, config)
    return len(ids)


def _authenticate_receiver(config: dict[str, Any], presented_token: str) -> None:
    from hermes_cli.managed_nodes import read_private_token

    expected = read_private_token(
        config["token_file"], label="managed installation receiver credential"
    )
    supplied = str(presented_token or "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise PermissionError("invalid managed installation credential")


def _start_receiver_thread(operation_id: str, config: dict[str, Any]) -> None:
    key = f"{config['state_file']}:{operation_id}"
    with _THREADS_LOCK:
        existing = _RECEIVER_THREADS.get(key)
        if existing and existing.is_alive():
            return
        thread = threading.Thread(
            target=_run_received_installation,
            args=(operation_id, config, key),
            daemon=True,
            name=f"managed-install-{operation_id[-8:]}",
        )
        _RECEIVER_THREADS[key] = thread
        thread.start()


def _run_received_installation(operation_id: str, config: dict[str, Any], key: str) -> None:
    try:
        path = Path(config["state_file"])
        while True:
            claimed, wait_seconds = _claim_received_target(
                path,
                operation_id,
                str(config["node_id"]),
            )
            if claimed is None:
                if wait_seconds <= 0:
                    return
                time.sleep(min(wait_seconds, DEFAULT_LEASE_SECONDS))
                continue
            try:
                with _LeaseHeartbeat(path, claimed) as heartbeat:
                    heartbeat.ensure_owned()
                    detail = _execute_allowlisted_installation(
                        claimed,
                        project_root=config["project_root"],
                        ownership_guard=heartbeat.ensure_owned,
                    )
                    heartbeat.ensure_owned()
                    if heartbeat.lost.is_set():
                        return
                    _finish_target(
                        path,
                        claimed,
                        state="completed",
                        detail=detail,
                        failure_count=0,
                    )
                return
            except Exception as exc:
                failures = int(claimed.get("failure_count") or 0) + 1
                terminal = failures >= MAX_INSTALL_ATTEMPTS
                updated = _finish_target(
                    path,
                    claimed,
                    state="failed" if terminal else "retry",
                    error=exc,
                    retry_after=0 if terminal else min(60, 2 ** min(failures, 5)),
                    failure_count=failures,
                )
                if terminal or not updated:
                    return
            finally:
                _release_execution_fence(claimed)
    finally:
        with _THREADS_LOCK:
            _RECEIVER_THREADS.pop(key, None)


def _claim_received_target(
    path: Path,
    operation_id: str,
    node_id: str,
    *,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> tuple[dict[str, Any] | None, float]:
    """Claim one receiver operation, returning how long an active peer owns it."""

    now = time.time()
    token = uuid4().hex
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        operation = conn.execute(
            "SELECT * FROM managed_installations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        target = conn.execute(
            "SELECT * FROM managed_installation_targets "
            "WHERE operation_id = ? AND node_id = ?",
            (operation_id, node_id),
        ).fetchone()
        if operation is None or target is None or str(target["state"]) in TERMINAL_STATES:
            conn.execute("COMMIT")
            return None, 0
        ready_at = max(float(target["next_attempt_at"]), float(target["lease_until"]))
        if (
            float(target["next_attempt_at"]) > now
            or (str(target["lease_token"]) and float(target["lease_until"]) > now)
        ):
            conn.execute("COMMIT")
            return None, max(0.1, ready_at - now)
        updated = conn.execute(
            """
            UPDATE managed_installation_targets
            SET state = 'running', lease_token = ?, lease_until = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE operation_id = ? AND node_id = ?
              AND state IN ('pending', 'accepted', 'running', 'retry')
              AND next_attempt_at <= ?
              AND (lease_token = '' OR lease_until <= ?)
            """,
            (
                token,
                now + lease_seconds,
                _utc_now(),
                operation_id,
                node_id,
                now,
                now,
            ),
        ).rowcount
        if not updated:
            conn.execute("COMMIT")
            return None, 0.1
        fence = _try_execution_fence(path, operation_id, node_id)
        if fence is None:
            conn.execute("ROLLBACK")
            return None, 0.1
        try:
            _recompute_operation(conn, operation_id)
            target = conn.execute(
                "SELECT * FROM managed_installation_targets "
                "WHERE operation_id = ? AND node_id = ?",
                (operation_id, node_id),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            fence.release()
            raise
    claimed = dict(operation)
    claimed.update({
        "node_id": node_id,
        "target_state": str(target["state"]),
        "attempts": int(target["attempts"]),
        "failure_count": int(target["failure_count"]),
        "lease_token": token,
        "lease_until": float(target["lease_until"]),
        "_execution_fence": fence,
    })
    return claimed, 0


def _execute_allowlisted_installation(
    operation: dict[str, Any],
    *,
    executor: Any = None,
    project_root: Path | None = None,
    ownership_guard: Any = None,
) -> dict[str, Any]:
    kind = _normalize_kind(str(operation["kind"]))
    identifier = _normalize_identifier(kind, str(operation["identifier"]))
    profile = str(operation.get("profile") or "default")
    guard = ownership_guard or (lambda: None)
    if executor is None:
        fence = operation.get("_execution_fence")
        runner = lambda command, *, timeout: _run_command_fenced(
            command,
            timeout=timeout,
            ownership_guard=guard,
            fence=fence if isinstance(fence, _ExecutionFence) else None,
        )
    else:
        runner = executor
    if kind == "skill":
        guard()
        command = [sys.executable, "-m", "hermes_cli.main", "-p", profile, "skills", "install", identifier, "--yes"]
        result = runner(command, timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS)
        guard()
        return {"installed": True, "kind": kind, "summary": _command_summary(result)}
    if kind == "mcp":
        guard()
        command = [sys.executable, "-m", "hermes_cli.main", "-p", profile, "mcp", "install", identifier]
        result = runner(command, timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS)
        guard()
        return {"installed": True, "kind": kind, "summary": _command_summary(result)}

    guard()
    root = Path(project_root or (Path(get_hermes_home()) / "managed-projects")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = str(operation.get("project_name") or "").strip() or _project_name(identifier)
    destination = (root / name).resolve()
    destination.relative_to(root)
    if destination.exists():
        guard()
        head = _validate_managed_project(
            destination,
            identifier,
            runner=runner,
            require_marker=True,
        )
        guard()
        return {
            "installed": True,
            "kind": kind,
            "path": str(destination),
            "existing": True,
            "head": head,
        }
    staging_id = hashlib.sha256(
        f"{operation.get('id') or identifier}:{name}".encode("utf-8")
    ).hexdigest()[:16]
    staging = (root / f".{name}.managed-install-{staging_id}").resolve()
    staging.relative_to(root)
    if staging.exists() or staging.is_symlink():
        if staging.is_symlink() or not staging.is_dir():
            raise RuntimeError("managed project staging path is unsafe")
        shutil.rmtree(staging)
    guard()
    result = runner(
        ["git", "clone", "--filter=blob:none", "--", identifier, str(staging)],
        timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )
    head = _validate_managed_project(
        staging,
        identifier,
        runner=runner,
        require_marker=False,
    )
    marker = staging / ".git" / "hermes-managed-install.json"
    marker_temporary = marker.with_name(f"{marker.name}.new-{uuid4().hex}")
    marker_temporary.write_text(
        json.dumps(
            {"version": 1, "origin": _normalize_project_origin(identifier), "head": head},
            ensure_ascii=True,
            separators=(",", ":"),
        ) + "\n",
        encoding="utf-8",
    )
    os.replace(marker_temporary, marker)
    guard()
    os.replace(staging, destination)
    return {
        "installed": True,
        "kind": kind,
        "path": str(destination),
        "existing": False,
        "head": head,
        "summary": _command_summary(result),
    }


def _command_output(result: Any) -> str:
    if isinstance(result, subprocess.CompletedProcess):
        return str(result.stdout or "").strip()
    return str(result or "").strip()


def _validate_managed_project(
    destination: Path,
    identifier: str,
    *,
    runner: Any,
    require_marker: bool,
) -> str:
    git_directory = destination / ".git"
    if not destination.is_dir() or not git_directory.is_dir() or git_directory.is_symlink():
        raise RuntimeError("project destination is not a complete git worktree")
    origin_result = runner(
        ["git", "-C", str(destination), "remote", "get-url", "origin"],
        timeout=30,
    )
    expected_origin = _normalize_project_origin(identifier)
    if _normalize_project_origin(_command_output(origin_result)) != expected_origin:
        raise RuntimeError("project destination origin does not match requested repository")
    head_result = runner(
        ["git", "-C", str(destination), "rev-parse", "--verify", "HEAD"],
        timeout=30,
    )
    head = _command_output(head_result).lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", head):
        raise RuntimeError("project destination has no valid checked-out HEAD")
    if not any(item.name != ".git" for item in destination.iterdir()):
        raise RuntimeError("project destination worktree is empty")
    marker = git_directory / "hermes-managed-install.json"
    if require_marker:
        try:
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("project destination has no valid completion marker") from exc
        if (
            not isinstance(marker_payload, dict)
            or marker_payload.get("version") != 1
            or marker_payload.get("origin") != expected_origin
            or str(marker_payload.get("head") or "").lower() != head
        ):
            raise RuntimeError("project destination completion marker does not match")
    return head


def _normalize_project_origin(identifier: str) -> str:
    """Return a credential-free canonical HTTPS repository origin."""

    value = _normalize_identifier("project", identifier)
    parsed = urlsplit(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    port = parsed.port
    if port not in (None, 443):
        host = f"{host}:{port}"
    path = re.sub(r"/+", "/", unquote(parsed.path)).rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    if not path or path == "/" or any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("project source path is invalid")
    return urlunsplit(("https", host, path, "", ""))


def _project_name(identifier: str) -> str:
    path = urlsplit(identifier if "://" in identifier else f"https://{identifier}").path
    candidate = Path(path.rstrip("/")).name.removesuffix(".git") or "project"
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip("-.")[:80]
    if not candidate:
        candidate = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12]
    return candidate


def _run_command(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=os.environ.copy(),
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or f"command exited {completed.returncode}")
    return completed


def _run_command_fenced(
    command: list[str],
    *,
    timeout: int,
    ownership_guard: Any,
    fence: _ExecutionFence | None,
) -> subprocess.CompletedProcess[str]:
    """Run a command in a cancellable process group while holding its OS fence."""

    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": stdout_file,
            "stderr": stderr_file,
            "text": True,
            "env": os.environ.copy(),
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
            if fence is not None:
                kwargs["pass_fds"] = (fence.fd,)
        process = subprocess.Popen(command, **kwargs)
        try:
            while process.poll() is None:
                ownership_guard()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.1)
            ownership_guard()
        except BaseException:
            _terminate_process_group(process)
            raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        completed = subprocess.CompletedProcess(
            command,
            int(process.returncode or 0),
            stdout_file.read(),
            stderr_file.read(),
        )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr or completed.stdout or f"command exited {completed.returncode}"
        )
    return completed


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            taskkill = shutil.which("taskkill")
            if not taskkill:
                raise OSError("taskkill is unavailable")
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            kill_process_group = getattr(os, "killpg", None)
            if not callable(kill_process_group):
                raise OSError("process-group termination is unavailable")
            kill_process_group(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "nt":
                process.kill()
            else:
                kill_process_group = getattr(os, "killpg", None)
                if not callable(kill_process_group):
                    raise OSError("process-group termination is unavailable")
                kill_process_group(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _command_summary(result: Any) -> str:
    if isinstance(result, subprocess.CompletedProcess):
        value = result.stdout or result.stderr or "completed"
    else:
        value = str(result or "completed")
    return _redact_error(value)[-1024:]
