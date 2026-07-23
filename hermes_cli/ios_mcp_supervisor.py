"""Durable supervisor for independent iOS MCP processes.

The supervisor stores service state and a command outbox in SQLite.  Runtime
callbacks are deliberately injected by the deployment layer, so the module is
also useful in tests and on hosts where MCPs are launched by systemd.
"""

from __future__ import annotations

import asyncio
import argparse
from dataclasses import dataclass
from enum import Enum
import json
import logging
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit
import uuid

from hermes_cli.config import get_hermes_home
from hermes_cli.sqlite_util import write_txn


logger = logging.getLogger(__name__)

_RUNTIME_STOPPING_ERROR = "runtime is stopping"
_IOS_MCP_FORKSERVER_PRELOAD = (
    "mcp.server.fastmcp",
    "hermes_cli.ios_intelligence",
)
_IOS_MCP_FORKSERVER_CONTEXT: Any | None = None
_IOS_MCP_FORKSERVER_LOCK = threading.Lock()
_IOS_MCP_PROCESS_ENV = {
    "PYTHONUNBUFFERED": "1",
    "MALLOC_ARENA_MAX": "2",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}


def _get_ios_mcp_forkserver_context() -> Any:
    """Return the one release-scoped preload broker for this dashboard."""

    global _IOS_MCP_FORKSERVER_CONTEXT
    with _IOS_MCP_FORKSERVER_LOCK:
        if _IOS_MCP_FORKSERVER_CONTEXT is None:
            multiprocessing.set_forkserver_preload(list(_IOS_MCP_FORKSERVER_PRELOAD))
            _IOS_MCP_FORKSERVER_CONTEXT = multiprocessing.get_context("forkserver")
        return _IOS_MCP_FORKSERVER_CONTEXT


class _ForkProcessAdapter:
    """Expose the Popen subset used by the runtime around an mp.Process."""

    def __init__(self, process: Any, command: Iterable[str]) -> None:
        self._process = process
        self.args = list(command)

    @property
    def pid(self) -> int:
        return int(self._process.pid or 0)

    def poll(self) -> int | None:
        exit_code = self._process.exitcode
        if exit_code is not None:
            return int(exit_code)
        if self._process.is_alive():
            return None
        exit_code = self._process.exitcode
        return None if exit_code is None else int(exit_code)

    def wait(self, timeout: float | None = None) -> int:
        self._process.join(timeout)
        if self._process.is_alive():
            raise subprocess.TimeoutExpired(self.args, timeout)
        exit_code = self._process.exitcode
        if exit_code is None:
            raise RuntimeError("forkserver child exited without a return code")
        return int(exit_code)

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def close(self) -> None:
        self._process.close()


class MCPState(str, Enum):
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    UPGRADING = "UPGRADING"
    QUARANTINED = "QUARANTINED"
    DISABLED = "DISABLED"
    RECOVERING = "RECOVERING"


class _ClosingSQLiteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


@dataclass
class MCPService:
    name: str
    state: MCPState = MCPState.RUNNING
    version: str = "1.0.0"
    active_version: str = "1.0.0"
    failures: int = 0
    last_error: str = ""
    updated_at: int = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_services (
    name TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    version TEXT NOT NULL,
    active_version TEXT NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_supervisor_queue (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    service TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at INTEGER NOT NULL,
    delivered_at INTEGER,
    created_at INTEGER NOT NULL,
    completed_at INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    UNIQUE(service, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_mcp_queue_ready
  ON mcp_supervisor_queue(state, available_at, created_at);
CREATE TABLE IF NOT EXISTS mcp_supervisor_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class IOSMCPSupervisor:
    """Manage lifecycle and recovery of the independent MCP processes."""

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        failure_threshold: int = 3,
        restart_backoff_seconds: float = 0.0,
    ) -> None:
        path = Path(db_path) if db_path else Path(get_hermes_home()) / "ios-mcp-supervisor.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.failure_threshold = max(1, int(failure_threshold))
        self.restart_backoff_seconds = max(0.0, float(restart_backoff_seconds))
        self._callbacks: dict[str, dict[str, Callable[..., Any] | None]] = {}
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(mcp_supervisor_queue)").fetchall()
            }
            if "delivered_at" not in columns:
                conn.execute("ALTER TABLE mcp_supervisor_queue ADD COLUMN delivered_at INTEGER")
                # Legacy delivered rows had no lease timestamp, so make them
                # retryable rather than leaving them stranded after upgrade.
                conn.execute(
                    "UPDATE mcp_supervisor_queue SET state='pending' WHERE state='delivered'"
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=_ClosingSQLiteConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def register(
        self,
        name: str,
        *,
        version: str = "1.0.0",
        health_check: Callable[[], Any] | None = None,
        start: Callable[..., Any] | None = None,
        stop: Callable[..., Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        service = self._name(name)
        now = int(time.time())
        state = MCPState.RUNNING.value if enabled else MCPState.DISABLED.value
        with self._lock, self._connect() as conn, write_txn(conn):
            existing = conn.execute("SELECT state,active_version FROM mcp_services WHERE name=?", (service,)).fetchone()
            if existing:
                state = existing["state"]
                active = str(existing["active_version"])
            else:
                active = str(version)
            conn.execute(
                "INSERT INTO mcp_services(name,state,version,active_version,metadata_json,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET version=excluded.version,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                (service, state, str(version), active, _json(dict(metadata or {})), now),
            )
            if not existing:
                conn.execute("INSERT INTO mcp_supervisor_events(service,from_state,to_state,reason,created_at) VALUES(?,?,?,?,?)", (service, "", state, "registered", now))
        self._callbacks[service] = {"health": health_check, "start": start, "stop": stop}
        return self.status(service)

    register_mcp = register

    @staticmethod
    def _name(name: Any) -> str:
        value = str(name or "").strip().lower()[:128]
        if not value:
            raise ValueError("service name is required")
        return value

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "name": row["name"], "state": row["state"], "version": row["version"],
            "active_version": row["active_version"], "failures": int(row["failures"]),
            "last_error": row["last_error"], "metadata": json.loads(row["metadata_json"] or "{}"),
            "updated_at": int(row["updated_at"]),
        }

    def status(self, name: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM mcp_services WHERE name=?", (self._name(name),)).fetchone()
        if row is None:
            raise KeyError(f"Unknown MCP: {name}")
        return self._row(row)

    get_status = status
    get_state = status

    def statuses(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM mcp_services ORDER BY name").fetchall()
        return [self._row(row) for row in rows]

    list_status = statuses

    def _transition(self, name: str, target: MCPState | str, reason: str = "", *, error: str = "") -> dict[str, Any]:
        service = self._name(name)
        target_state = target.value if isinstance(target, MCPState) else str(target).upper()
        if target_state not in {item.value for item in MCPState}:
            raise ValueError(f"Unknown MCP state: {target}")
        now = int(time.time())
        with self._lock, self._connect() as conn, write_txn(conn):
            row = conn.execute("SELECT state FROM mcp_services WHERE name=?", (service,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown MCP: {name}")
            prior = str(row["state"])
            conn.execute("UPDATE mcp_services SET state=?,last_error=?,updated_at=? WHERE name=?", (target_state, str(error)[:2000], now, service))
            if prior != target_state:
                conn.execute("INSERT INTO mcp_supervisor_events(service,from_state,to_state,reason,created_at) VALUES(?,?,?,?,?)", (service, prior, target_state, str(reason)[:512], now))
        return self.status(service)

    def set_state(self, name: str, state: MCPState | str, reason: str = "") -> dict[str, Any]:
        return self._transition(name, state, reason)

    transition = set_state

    def health_check(self, name: str) -> dict[str, Any]:
        service = self._name(name)
        current = self.status(service)
        callback = self._callbacks.get(service, {}).get("health")
        healthy = callback is not None
        detail: Any = None
        error = "" if callback is not None else "health callback is not registered"
        if callback is not None:
            try:
                detail = callback()
                healthy = bool(detail if isinstance(detail, bool) else (detail.get("ok", True) if isinstance(detail, Mapping) else True))
            except Exception as exc:
                healthy = False
                error = str(exc)[:2000]
        if healthy:
            return {**self.record_success(service), "healthy": True, "detail": detail}
        self.record_failure(service, error or "health check failed")
        return {**self.status(service), "healthy": False, "detail": detail, "error": error or "health check failed"}

    check_health = health_check
    health = health_check

    def check_all(self) -> list[dict[str, Any]]:
        return [self.health_check(item["name"]) for item in self.statuses()]

    run_health_checks = check_all

    def record_success(self, name: str) -> dict[str, Any]:
        """Persist a successful probe without invoking the health callback again."""

        service = self._name(name)
        current = self.status(service)
        if current["state"] in {
            MCPState.DEGRADED.value,
            MCPState.RECOVERING.value,
        }:
            self._transition(service, MCPState.RUNNING, "health restored")
        if current["state"] not in {
            MCPState.QUARANTINED.value,
            MCPState.DISABLED.value,
            MCPState.UPGRADING.value,
        }:
            with self._lock, self._connect() as conn, write_txn(conn):
                conn.execute(
                    "UPDATE mcp_services SET failures=0,last_error='',updated_at=? WHERE name=?",
                    (int(time.time()), service),
                )
        return self.status(service)

    def record_failure(
        self,
        name: str,
        error: str = "",
        *,
        schedule_restart: bool = True,
        quarantine_at_threshold: bool = True,
    ) -> dict[str, Any]:
        service = self._name(name)
        with self._lock, self._connect() as conn, write_txn(conn):
            row = conn.execute("SELECT failures,state FROM mcp_services WHERE name=?", (service,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown MCP: {name}")
            failures = int(row["failures"]) + 1
            state = (
                MCPState.QUARANTINED.value
                if quarantine_at_threshold and failures >= self.failure_threshold
                else MCPState.DEGRADED.value
            )
            conn.execute("UPDATE mcp_services SET failures=?,state=?,last_error=?,updated_at=? WHERE name=?", (failures, state, str(error)[:2000], int(time.time()), service))
            conn.execute("INSERT INTO mcp_supervisor_events(service,from_state,to_state,reason,created_at) VALUES(?,?,?,?,?)", (service, row["state"], state, str(error)[:512], int(time.time())))
        if schedule_restart and state != MCPState.QUARANTINED.value:
            # Each recovered crash starts a new failure generation. Reusing a
            # key based only on the failure count would collide with a
            # completed restart from an earlier crash and strand the process.
            self.enqueue(
                service,
                "restart",
                {"reason": error or "health failure"},
                idempotency_key=f"restart:{service}:{uuid.uuid4().hex}",
            )
        return self.status(service)

    failure = record_failure

    def restart(self, name: str, *, reason: str = "manual restart", version: str | None = None) -> dict[str, Any]:
        service = self._name(name)
        current = self.status(service)
        if current["state"] == MCPState.DISABLED.value:
            return current
        self._transition(service, MCPState.RECOVERING, reason)
        callbacks = self._callbacks.get(service, {})
        try:
            if self.restart_backoff_seconds:
                time.sleep(self.restart_backoff_seconds)
            stop = callbacks.get("stop")
            start = callbacks.get("start")
            if stop:
                stop()
            if start:
                try:
                    started = start(version or current["active_version"])
                except TypeError:
                    started = start()
                if started is False:
                    raise RuntimeError("MCP process failed to start")
            health = callbacks.get("health")
            if health:
                detail = health()
                healthy = bool(
                    detail
                    if isinstance(detail, bool)
                    else detail.get("ok", True) if isinstance(detail, Mapping)
                    else True
                )
                if not healthy:
                    raise RuntimeError("MCP process failed its restart health check")
            with self._connect() as conn, write_txn(conn):
                conn.execute("UPDATE mcp_services SET state=?,failures=0,last_error='',updated_at=? WHERE name=?", (MCPState.RUNNING.value, int(time.time()), service))
            return self.status(service)
        except Exception as exc:
            self._transition(service, MCPState.QUARANTINED, "restart failed", error=str(exc))
            return self.status(service)

    restart_mcp = restart
    restart_service = restart

    def quarantine(self, name: str, reason: str = "manual quarantine") -> dict[str, Any]:
        return self._transition(name, MCPState.QUARANTINED, reason)

    isolate = quarantine
    quarantine_service = quarantine

    def disable(self, name: str, reason: str = "manual disable") -> dict[str, Any]:
        return self._transition(name, MCPState.DISABLED, reason)

    def enable(self, name: str, *, reason: str = "manual enable") -> dict[str, Any]:
        result = self._transition(name, MCPState.RECOVERING, reason)
        return self.restart(name, reason=reason) if result["state"] == MCPState.RECOVERING.value else result

    def blue_green_upgrade(
        self,
        name: str,
        new_version: str,
        *,
        start_new: Callable[..., Any] | None = None,
        stop_old: Callable[..., Any] | None = None,
        health_check: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        service = self._name(name)
        current = self.status(service)
        old_version = current["active_version"]
        self._transition(service, MCPState.UPGRADING, f"upgrade to {new_version}")
        try:
            if start_new:
                try:
                    started = start_new(new_version)
                except TypeError:
                    started = start_new()
                if started is False:
                    raise RuntimeError("new MCP failed to start")
            if health_check:
                try:
                    healthy = health_check(new_version)
                except TypeError:
                    healthy = health_check()
                if healthy is False:
                    raise RuntimeError("new MCP health check failed")
            if stop_old:
                try:
                    stop_old(old_version)
                except TypeError:
                    stop_old()
            with self._connect() as conn, write_txn(conn):
                conn.execute("UPDATE mcp_services SET version=?,active_version=?,state=?,failures=0,last_error='',updated_at=? WHERE name=?", (str(new_version), str(new_version), MCPState.RUNNING.value, int(time.time()), service))
            return {**self.status(service), "upgraded": True, "previous_version": old_version}
        except Exception as exc:
            # Keep traffic on the old instance.  A deployment can clean up the
            # failed green process separately, while callers get a durable
            # recovery state and the precise rollback reason.
            with self._connect() as conn, write_txn(conn):
                conn.execute("UPDATE mcp_services SET state=?,version=?,active_version=?,last_error=?,updated_at=? WHERE name=?", (MCPState.RECOVERING.value, old_version, old_version, str(exc)[:2000], int(time.time()), service))
            return {**self.status(service), "upgraded": False, "rolled_back": True, "previous_version": old_version, "error": str(exc)}

    upgrade = blue_green_upgrade
    upgrade_service = blue_green_upgrade

    def enqueue(
        self,
        service: str,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        available_at: Any = None,
    ) -> dict[str, Any]:
        service = self._name(service)
        key = str(idempotency_key or uuid.uuid4().hex)[:512]
        now = int(time.time())
        available = int(available_at) if available_at is not None else now
        identifier = uuid.uuid4().hex
        with self._connect() as conn, write_txn(conn):
            existing = conn.execute("SELECT id,state,attempts FROM mcp_supervisor_queue WHERE service=? AND idempotency_key=?", (service, key)).fetchone()
            if existing:
                return {"id": existing["id"], "service": service, "action": str(action), "state": existing["state"], "attempts": existing["attempts"], "duplicate": True}
            conn.execute("INSERT INTO mcp_supervisor_queue(id,idempotency_key,service,action,payload_json,state,available_at,created_at) VALUES(?,?,?,?,?,'pending',?,?)", (identifier, key, service, str(action)[:128], _json(dict(payload or {})), available, now))
        return {"id": identifier, "service": service, "action": str(action), "state": "pending", "attempts": 0, "duplicate": False}

    enqueue_command = enqueue
    enqueue_request = enqueue

    def recover_delivered(self, *, lease_seconds: int = 120) -> int:
        """Return commands whose worker lease expired without an ACK."""

        cutoff = int(time.time()) - max(15, int(lease_seconds))
        with self._connect() as conn, write_txn(conn):
            changed = conn.execute(
                "UPDATE mcp_supervisor_queue SET state='pending',delivered_at=NULL "
                "WHERE state='delivered' AND (delivered_at IS NULL OR delivered_at<=?)",
                (cutoff,),
            ).rowcount
        return int(changed)

    def pull(self, limit: int = 100, *, lease_seconds: int = 120) -> list[dict[str, Any]]:
        now = int(time.time())
        with self._connect() as conn, write_txn(conn):
            cutoff = now - max(15, int(lease_seconds))
            conn.execute(
                "UPDATE mcp_supervisor_queue SET state='pending',delivered_at=NULL "
                "WHERE state='delivered' AND (delivered_at IS NULL OR delivered_at<=?)",
                (cutoff,),
            )
            rows = conn.execute(
                "SELECT * FROM mcp_supervisor_queue "
                "WHERE state IN ('pending','retry') AND available_at<=? "
                "ORDER BY created_at LIMIT ?",
                (now, max(1, min(int(limit), 1000))),
            ).fetchall()
            conn.executemany(
                "UPDATE mcp_supervisor_queue SET state='delivered',delivered_at=?,attempts=attempts+1 "
                "WHERE id=? AND state IN ('pending','retry')",
                [(now, row["id"]) for row in rows],
            )
        return [{"id": row["id"], "service": row["service"], "action": row["action"], "payload": json.loads(row["payload_json"] or "{}"), "attempts": int(row["attempts"]) + 1} for row in rows]

    pull_commands = pull
    pull_requests = pull

    def ack(self, command_id: str, *, success: bool = True, error: str = "", retry_at: Any = None) -> bool:
        state = "completed" if success else "retry"
        with self._connect() as conn, write_txn(conn):
            changed = conn.execute("UPDATE mcp_supervisor_queue SET state=?,last_error=?,available_at=?,delivered_at=NULL,completed_at=? WHERE id=? AND state='delivered'", (state, str(error)[:2000], int(retry_at or time.time()), int(time.time()) if success else None, str(command_id))).rowcount
        return bool(changed)

    ack_command = ack
    ack_request = ack


class IOSMCPRuntimeSupervisor:
    """Run and health-check the isolated HTTP MCP processes."""

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        db_dir: str | os.PathLike[str] | None = None,
        host: str = "127.0.0.1",
        base_port: int = 8760,
        health_interval_seconds: float = 15.0,
        initial_health_delay_seconds: float = 60.0,
        failure_threshold: int = 3,
        restart_backoff_seconds: float = 1.0,
        log_directory: str | os.PathLike[str] | None = None,
        python_executable: str | None = None,
        capabilities: tuple[str, ...] | None = None,
        blue_green_port_offset: int = 100,
        drain_timeout_seconds: float = 30.0,
        startup_timeout_seconds: float = 90.0,
        health_probe_timeout_seconds: float = 5.0,
        recovery_probe_timeout_seconds: float = 30.0,
        owner_id: str = "",
        granted_scopes: Mapping[str, Iterable[str] | str] | None = None,
    ) -> None:
        from hermes_cli.ios_mcp_server import CAPABILITIES, normalize_mcp_scope_grants

        selected = tuple(capabilities or CAPABILITIES)
        unknown = set(selected) - set(CAPABILITIES)
        if unknown:
            raise ValueError(f"Unknown iOS MCP capabilities: {', '.join(sorted(unknown))}")
        self.capabilities = selected
        self._all_capabilities = tuple(CAPABILITIES)
        self.db_dir = Path(db_dir) if db_dir else None
        self.host = str(host)
        self.base_port = int(base_port)
        self.blue_green_port_offset = max(len(self._all_capabilities) + 1, int(blue_green_port_offset))
        self.drain_timeout_seconds = max(0.0, float(drain_timeout_seconds))
        self.startup_timeout_seconds = min(
            600.0,
            max(1.0, float(startup_timeout_seconds)),
        )
        self.health_probe_timeout_seconds = min(
            60.0,
            max(1.0, float(health_probe_timeout_seconds)),
        )
        self.recovery_probe_timeout_seconds = min(
            180.0,
            max(
                self.health_probe_timeout_seconds,
                float(recovery_probe_timeout_seconds),
            ),
        )
        self.health_interval_seconds = max(2.0, float(health_interval_seconds))
        self.initial_health_delay_seconds = min(
            600.0,
            max(self.health_interval_seconds, float(initial_health_delay_seconds)),
        )
        self.python_executable = str(python_executable or sys.executable)
        self.owner_id = str(owner_id or "").strip()[:512]
        try:
            from hermes_cli.config import load_config

            configured_servers = load_config().get("mcp_servers") or {}
        except Exception:
            configured_servers = {}
        if not isinstance(configured_servers, Mapping):
            configured_servers = {}
        explicit_grants = granted_scopes or {}
        self.granted_scopes: dict[str, tuple[str, ...]] = {}
        for service in self.capabilities:
            configured = configured_servers.get(service)
            raw_grants: Iterable[str] | str | None = None
            if service in explicit_grants:
                explicit_value = explicit_grants[service]
                raw_grants = () if explicit_value is None else explicit_value
            elif isinstance(configured, Mapping) and "granted_scopes" in configured:
                configured_value = configured.get("granted_scopes")
                raw_grants = () if configured_value is None else configured_value
            self.granted_scopes[service] = normalize_mcp_scope_grants(service, raw_grants)
        self.log_directory = Path(log_directory) if log_directory else Path(get_hermes_home()) / "logs" / "ios-mcp"
        self.log_directory.mkdir(parents=True, exist_ok=True)
        self.supervisor = IOSMCPSupervisor(
            db_path,
            failure_threshold=failure_threshold,
            restart_backoff_seconds=restart_backoff_seconds,
        )
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._log_handles: dict[str, Any] = {}
        self._active_ports: dict[str, int] = {}
        self._lock = threading.RLock()
        self._health_cycle_lock = threading.Lock()
        self._health_generations = {name: 0 for name in self.capabilities}
        self._stop_event = threading.Event()
        self._startup_thread: threading.Thread | None = None
        self._thread: threading.Thread | None = None
        self.starting = False
        self.running = False
        self._active_ports.update(self._configured_active_ports())

    def _configured_active_ports(self) -> dict[str, int]:
        try:
            from hermes_cli.config import load_config

            servers = (load_config().get("mcp_servers") or {})
        except Exception:
            return {}
        result: dict[str, int] = {}
        if not isinstance(servers, Mapping):
            return result
        for service in self.capabilities:
            entry = servers.get(service)
            if not isinstance(entry, Mapping):
                continue
            try:
                parsed = urlsplit(str(entry.get("url") or ""))
                port = int(parsed.port or 0)
            except (TypeError, ValueError):
                continue
            stable = self.base_port + self._all_capabilities.index(service)
            if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and port in {
                stable,
                stable + self.blue_green_port_offset,
            }:
                result[service] = port
        return result

    def stable_port_for(self, name: str) -> int:
        service = self.supervisor._name(name)
        return self.base_port + self._all_capabilities.index(service)

    def port_for(self, name: str) -> int:
        service = self.supervisor._name(name)
        return self._active_ports.get(service, self.stable_port_for(service))

    def alternate_port_for(self, name: str) -> int:
        service = self.supervisor._name(name)
        stable = self.stable_port_for(service)
        alternate = stable + self.blue_green_port_offset
        return stable if self.port_for(service) == alternate else alternate

    def endpoint_for(self, name: str) -> str:
        return f"http://{self.host}:{self.port_for(name)}/mcp"

    def command_for(
        self,
        name: str,
        *,
        port: int | None = None,
        python_executable: str | None = None,
    ) -> list[str]:
        service = self.supervisor._name(name)
        command = [
            str(python_executable or self.python_executable),
            "-m",
            "hermes_cli.ios_mcp_server",
            "--transport",
            "streamable-http",
            "--host",
            self.host,
            "--port",
            str(self.port_for(service) if port is None else int(port)),
        ]
        if self.db_dir is not None:
            command.extend(["--db-dir", str(self.db_dir)])
        for scope in self.granted_scopes[service]:
            command.extend(["--grant-scope", scope])
        command.append(service)
        return command

    def _should_use_forkserver(
        self,
        *,
        python_executable: str | None = None,
        force_subprocess: bool = False,
    ) -> bool:
        if force_subprocess or not sys.platform.startswith("linux"):
            return False
        if "forkserver" not in multiprocessing.get_all_start_methods():
            return False
        requested = os.path.normcase(os.path.realpath(
            str(python_executable or self.python_executable)
        ))
        current = os.path.normcase(os.path.realpath(sys.executable))
        return requested == current

    def _register_callbacks(self) -> None:
        from hermes_cli.ios_mcp_server import MCP_VERSION, ios_mcp_manifests

        try:
            from hermes_cli.config import load_config

            configured_servers = load_config().get("mcp_servers") or {}
        except Exception:
            configured_servers = {}
        if not isinstance(configured_servers, Mapping):
            configured_servers = {}
        manifests = ios_mcp_manifests(
            transport="streamable-http",
            host=self.host,
            base_port=self.base_port,
        )
        for name in self.capabilities:
            manifest = dict(manifests[name])
            manifest["endpoint"] = self.endpoint_for(name)
            manifest["granted_scopes"] = list(self.granted_scopes[name])
            status = self.supervisor.register(
                name,
                version=MCP_VERSION,
                metadata=manifest,
                health_check=lambda service=name: self.health_service(service),
                start=lambda _version=None, service=name: self.start_service(service, verify=True),
                stop=lambda service=name: self.stop_service(service),
            )
            if (
                status["state"] == MCPState.QUARANTINED.value
                and status["last_error"] == _RUNTIME_STOPPING_ERROR
            ):
                status = self.supervisor._transition(
                    name,
                    MCPState.RECOVERING,
                    "recover shutdown-interrupted restart",
                    error=_RUNTIME_STOPPING_ERROR,
                )
            configured = configured_servers.get(name)
            if (
                isinstance(configured, Mapping)
                and configured.get("enabled") is False
                and status["state"] != MCPState.DISABLED.value
            ):
                self.supervisor.disable(name, "disabled in mcp_servers config")

    def _start_registered_services(self) -> None:
        """Start the required fleet without holding the runtime-wide lock.

        A full fleet can take tens of seconds to become healthy. Holding
        ``_lock`` across that work blocks truthful health checks and, when this
        method runs from a FastAPI lifespan, prevents the public API from ever
        binding before all child interpreters have reached steady state.
        """

        required_ok = True
        try:
            self._register_callbacks()
            for name in self.capabilities:
                if self._stop_event.is_set():
                    required_ok = False
                    break
                state = self.supervisor.status(name)["state"]
                if state in {
                    MCPState.DISABLED.value,
                    MCPState.QUARANTINED.value,
                    MCPState.UPGRADING.value,
                }:
                    continue
                if not self.start_service(name, verify=True):
                    required_ok = False
                else:
                    self._record_runtime_success(name)
        except Exception:
            required_ok = False
            raise
        finally:
            with self._lock:
                self.starting = False
                self.running = required_ok and not self._stop_event.is_set()
                if threading.current_thread() is self._startup_thread:
                    self._startup_thread = None
                if (
                    not self._stop_event.is_set()
                    and (self._thread is None or not self._thread.is_alive())
                ):
                    self._thread = threading.Thread(
                        target=self._run,
                        name="ios-mcp-runtime-supervisor",
                        daemon=True,
                    )
                    self._thread.start()

    def start(self) -> "IOSMCPRuntimeSupervisor":
        with self._lock:
            if self.running:
                return self
            if self.starting:
                startup_thread = self._startup_thread
            else:
                startup_thread = None
                self.starting = True
                self._stop_event.clear()
                self._startup_thread = threading.current_thread()
        if startup_thread is not None:
            if startup_thread is not threading.current_thread():
                startup_thread.join()
            return self
        self._start_registered_services()
        return self

    def start_async(self) -> threading.Thread:
        """Start the fleet in the background and return its one startup worker."""

        with self._lock:
            existing = self._startup_thread
            if existing is not None and existing.is_alive():
                return existing
            if self.running:
                completed = threading.Thread(
                    target=lambda: None,
                    name="ios-mcp-runtime-already-running",
                    daemon=True,
                )
                completed.start()
                return completed
            self.starting = True
            self._stop_event.clear()
            startup = threading.Thread(
                target=self._start_registered_services,
                name="ios-mcp-runtime-startup",
                daemon=True,
            )
            self._startup_thread = startup
            startup.start()
            return startup

    def health(self) -> dict[str, Any]:
        """Return truthful fleet health, including per-service probes."""

        from hermes_cli.ios_mcp_server import ios_mcp_manifests

        manifests = ios_mcp_manifests(
            transport="streamable-http",
            host=self.host,
            base_port=self.base_port,
        )
        services: list[dict[str, Any]] = []
        for name in self.capabilities:
            state = self.supervisor.status(name)["state"]
            if state == MCPState.DISABLED.value:
                continue
            probe = self.health_service(name)
            manifest = manifests[name]
            expected_tools = sorted((manifest.get("tool_scopes") or {}).keys())
            actual_tools = sorted(str(tool) for tool in (probe.get("tools") or ()))
            declared_scopes = list(manifest.get("scope") or ())
            granted_scopes = list(self.granted_scopes.get(name, ()))
            contract_ok = (
                bool(probe.get("ok"))
                and actual_tools == expected_tools
                and set(granted_scopes).issubset(declared_scopes)
            )
            services.append({
                "name": name,
                "state": state,
                **probe,
                "expected_tools": expected_tools,
                "declared_scopes": declared_scopes,
                "granted_scopes": granted_scopes,
                "contract_ok": contract_ok,
            })
        healthy = sum(
            1
            for item in services
            if item.get("ok") is True and item.get("contract_ok") is True
        )
        required = len(services)
        return {
            "ok": required > 0 and healthy == required,
            "running": bool(self.running),
            "starting": bool(self.starting),
            "healthy_count": healthy,
            "required_count": required,
            "services": services,
        }

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        startup_thread = self._startup_thread
        if (
            startup_thread
            and startup_thread.is_alive()
            and startup_thread is not threading.current_thread()
        ):
            startup_thread.join(max(0.1, float(timeout)))
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(max(0.1, float(timeout)))
        for name in self.capabilities:
            self.stop_service(name)
        self._startup_thread = None
        self._thread = None
        self.starting = False
        self.running = False

    close = stop

    def _spawn_process(
        self,
        service: str,
        *,
        port: int,
        python_executable: str | None = None,
        log_suffix: str = "",
        force_subprocess: bool = False,
    ) -> tuple[Any, Any]:
        safe_suffix = "".join(
            character for character in str(log_suffix) if character.isalnum() or character in {".", "_", "-"}
        )[:80]
        suffix = f"-{safe_suffix}" if safe_suffix else ""
        log_path = self.log_directory / f"{service}{suffix}.log"
        log_handle = log_path.open("ab", buffering=0)
        env = os.environ.copy()
        # Keep 21 isolated Python services within a predictable memory and
        # thread footprint. These are internal runtime controls, not user-facing
        # behavioral configuration.
        env["PYTHONUNBUFFERED"] = _IOS_MCP_PROCESS_ENV["PYTHONUNBUFFERED"]
        for key, value in _IOS_MCP_PROCESS_ENV.items():
            env.setdefault(key, value)
        if self.db_dir is not None:
            env["HERMES_IOS_INTELLIGENCE_DIR"] = str(self.db_dir)
        if self.owner_id:
            env["HERMES_IOS_OWNER_ID"] = self.owner_id
        command = self.command_for(
            service,
            port=port,
            python_executable=python_executable,
        )

        if self._should_use_forkserver(
            python_executable=python_executable,
            force_subprocess=force_subprocess,
        ):
            raw_process = None
            try:
                # These controls must be present when the broker interpreter
                # starts; setting them only inside a forked child is too late.
                for key, value in _IOS_MCP_PROCESS_ENV.items():
                    if key == "PYTHONUNBUFFERED":
                        os.environ[key] = value
                    else:
                        os.environ.setdefault(key, value)
                from hermes_cli.ios_mcp_server import _run_supervised_mcp_child

                raw_process = _get_ios_mcp_forkserver_context().Process(
                    target=_run_supervised_mcp_child,
                    args=(command[3:], env, str(log_path)),
                    name=f"ios-mcp-{service}",
                )
                raw_process.start()
                process = _ForkProcessAdapter(raw_process, command)
                self._prefer_child_for_oom_recovery(process.pid)
            except Exception:
                logger.warning(
                    "iOS MCP forkserver start failed for %s; using subprocess",
                    service,
                    exc_info=True,
                )
                if raw_process is not None and getattr(raw_process, "pid", None):
                    try:
                        candidate = _ForkProcessAdapter(raw_process, command)
                        self._terminate_process(candidate)
                    except Exception:
                        logger.warning(
                            "failed to clean partial forkserver child for %s",
                            service,
                            exc_info=True,
                        )
            else:
                log_handle.close()
                return process, None
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
            )
            self._prefer_child_for_oom_recovery(process.pid)
        except Exception:
            log_handle.close()
            raise
        return process, log_handle

    @staticmethod
    def _prefer_child_for_oom_recovery(pid: int) -> None:
        """Prefer degrading one MCP over losing the public API under pressure."""

        if os.name != "posix" or int(pid) <= 0:
            return
        try:
            Path(f"/proc/{int(pid)}/oom_score_adj").write_text("500", encoding="ascii")
        except OSError:
            # Kernels and containers may prohibit this procfs adjustment. The
            # supervisor remains functional; production resource probes catch
            # an undersized host independently.
            pass

    @staticmethod
    def _terminate_process(process: Any | None, log_handle: Any = None) -> None:
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            if process is not None:
                close_process = getattr(process, "close", None)
                if callable(close_process):
                    close_process()
            if log_handle is not None:
                log_handle.close()

    def start_service(self, name: str, *, verify: bool = True) -> bool:
        service = self.supervisor._name(name)
        if self._stop_event.is_set():
            return False
        with self._lock:
            current = self._processes.get(service)
            if current is not None and current.poll() is None:
                return True
            self._close_process_unlocked(service)
            try:
                process, log_handle = self._spawn_process(
                    service,
                    port=self.port_for(service),
                )
            except Exception:
                return False
            self._processes[service] = process
            self._log_handles[service] = log_handle
        if not verify:
            return True
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline and not self._stop_event.is_set():
            if self.health_service(service).get("ok"):
                return True
            time.sleep(0.2)
        self.stop_service(service)
        return False

    def stop_service(self, name: str) -> bool:
        service = self.supervisor._name(name)
        with self._lock:
            process = self._processes.pop(service, None)
            log_handle = self._log_handles.pop(service, None)
        self._terminate_process(process, log_handle)
        return True

    def _close_process_unlocked(self, service: str) -> None:
        process = self._processes.pop(service, None)
        log_handle = self._log_handles.pop(service, None)
        self._terminate_process(process, log_handle)

    def health_service(
        self,
        name: str,
        *,
        probe_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        service = self.supervisor._name(name)
        with self._lock:
            process = self._processes.get(service)
        if process is None:
            return {"ok": False, "error": "process_missing"}
        exit_code = process.poll()
        if exit_code is not None:
            return {
                "ok": False,
                "error": "process_exited",
                "exit_code": exit_code,
                "pid": process.pid,
            }
        try:
            timeout = (
                self.health_probe_timeout_seconds
                if probe_timeout_seconds is None
                else max(1.0, float(probe_timeout_seconds))
            )
            tools = self._probe_tools_with_timeout(self.endpoint_for(service), timeout)
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "pid": process.pid}
        return {"ok": bool(tools), "pid": process.pid, "tools": tools}

    def _persist_discovery_endpoint(
        self,
        name: str,
        port: int,
        *,
        version: str | None = None,
    ) -> None:
        from hermes_cli import config as config_module
        from hermes_cli.profiles import (
            _get_default_hermes_home,
            _get_profiles_root,
            validate_profile_name,
        )
        from hermes_constants import (
            get_config_path,
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from utils import atomic_yaml_write

        service = self.supervisor._name(name)
        endpoint = f"http://{self.host}:{int(port)}/mcp"
        default_home = _get_default_hermes_home()
        homes = [default_home]
        profiles_root = _get_profiles_root()
        if profiles_root.is_dir():
            for candidate in sorted(profiles_root.iterdir(), key=lambda item: item.name):
                if not candidate.is_dir():
                    continue
                try:
                    validate_profile_name(candidate.name)
                except ValueError:
                    continue
                homes.append(candidate)

        updates: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
        for home in homes:
            token = set_hermes_home_override(str(home))
            try:
                config = dict(config_module.read_raw_config() or {})
            finally:
                reset_hermes_home_override(token)
            raw_servers = config.get("mcp_servers")
            if not isinstance(raw_servers, Mapping) or service not in raw_servers:
                continue
            servers = dict(raw_servers)
            entry = dict(servers.get(service) or {})
            entry["enabled"] = entry.get("enabled", True)
            entry["url"] = endpoint
            manifest = dict(entry.get("manifest") or {})
            manifest.update({
                "endpoint": endpoint,
                "name": service,
                "transport": "streamable-http",
            })
            if version:
                manifest["version"] = str(version)
            entry["manifest"] = manifest
            servers[service] = entry
            updated = dict(config)
            updated["mcp_servers"] = servers
            if updated == config:
                continue
            updates.append((home, config, updated))

        written: list[tuple[Path, dict[str, Any]]] = []
        try:
            for home, original, updated in updates:
                token = set_hermes_home_override(str(home))
                try:
                    config_module.save_config(updated)
                finally:
                    reset_hermes_home_override(token)
                written.append((home, original))
        except BaseException:
            # Each individual save is atomic. Restore previously-written
            # profiles directly so a failed later save cannot leave discovery
            # split between blue and green endpoints.
            for home, original in reversed(written):
                token = set_hermes_home_override(str(home))
                try:
                    path = get_config_path()
                    atomic_yaml_write(path, original)
                    config_module._RAW_CONFIG_CACHE.pop(str(path), None)
                    config_module._LAST_EXPANDED_CONFIG_BY_PATH.pop(str(path), None)
                finally:
                    reset_hermes_home_override(token)
            raise

    @staticmethod
    def _chat_client_registered(name: str) -> bool:
        """Return whether this process currently has a live client slot."""

        try:
            from tools import mcp_tool

            with mcp_tool._lock:
                return name in mcp_tool._servers
        except Exception:
            return False

    @staticmethod
    def _reload_chat_client(name: str, *, required: bool = False) -> bool:
        """Reload the in-process Hermes MCP client after discovery cutover.

        The runtime supervisor and Hermes tool registry can share a process,
        while existing ``MCPServerTask`` instances retain their original URL.
        Persisting config alone therefore leaves active chats on the blue
        endpoint. Ask the canonical client registry to replace this server;
        its per-server RPC gate drains in-flight calls before reconnecting.
        A supervisor-only process has no registered clients, which is a valid
        no-op.
        """

        try:
            from hermes_cli.config import read_raw_config
            from tools import mcp_tool

            config = read_raw_config() or {}
            servers = config.get("mcp_servers") if isinstance(config, Mapping) else None
            entry = servers.get(name) if isinstance(servers, Mapping) else None
            if not isinstance(entry, Mapping):
                return not required
            with mcp_tool._lock:
                had_client = name in mcp_tool._servers
            if not had_client and not required:
                return True
            mcp_tool.register_mcp_servers({name: dict(entry)})
            # register_mcp_servers deliberately reports per-server connection
            # failures without raising. Verify the replacement directly so a
            # failed green connection cannot be mistaken for a successful
            # cutover and followed by terminating the healthy blue process.
            expected_fingerprint = json.dumps(
                dict(entry), sort_keys=True, separators=(",", ":"), default=str,
            )
            with mcp_tool._lock:
                server = mcp_tool._servers.get(name)
                actual_fingerprint = mcp_tool._server_config_fingerprints.get(name)
                live_config = getattr(server, "_config", None) if server else None
                endpoint_matches = True
                if isinstance(live_config, Mapping) and "url" in entry:
                    endpoint_matches = live_config.get("url") == entry.get("url")
                return bool(
                    server is not None
                    and getattr(server, "accepting_calls", True)
                    and getattr(server, "session", None) is not None
                    and getattr(server, "_registered_tool_names", ())
                    and actual_fingerprint == expected_fingerprint
                    and endpoint_matches
                )
        except Exception:
            logger.exception("MCP chat client reload failed for %s", name)
            return False

    def blue_green_upgrade(
        self,
        name: str,
        new_version: str,
        *,
        green_python_executable: str | None = None,
        drain_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Start green on the alternate port, cut discovery over, then drain old."""

        service = self.supervisor._name(name)
        with self._lock:
            old_process = self._processes.get(service)
            old_handle = self._log_handles.get(service)
        if old_process is None or old_process.poll() is not None:
            if not self.start_service(service, verify=True):
                return {
                    **self.supervisor.status(service),
                    "upgraded": False,
                    "rolled_back": True,
                    "error": "active MCP process is unavailable",
                }
            with self._lock:
                old_process = self._processes.get(service)
                old_handle = self._log_handles.get(service)
        old_port = self.port_for(service)
        green_port = self.alternate_port_for(service)
        green: dict[str, Any] = {}

        def start_new(_version: str) -> bool:
            process, handle = self._spawn_process(
                service,
                port=green_port,
                python_executable=green_python_executable,
                log_suffix=f"green-{new_version}",
                # The forkserver is an immutable snapshot of the running
                # release. Green must import from its requested executable so
                # stale preloaded code can never be labelled as the upgrade.
                force_subprocess=True,
            )
            green.update({"process": process, "handle": handle})
            deadline = time.monotonic() + self.startup_timeout_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    return False
                try:
                    tools = self._probe_tools(f"http://{self.host}:{green_port}/mcp")
                except Exception:
                    time.sleep(0.2)
                    continue
                if tools:
                    green["tools"] = tools
                    return True
            return False

        def health_new(_version: str) -> bool:
            process = green.get("process")
            if process is None or process.poll() is not None:
                return False
            tools = self._probe_tools(f"http://{self.host}:{green_port}/mcp")
            green["tools"] = tools
            return bool(tools)

        def cut_over_and_drain(_old_version: str) -> None:
            with self._lock:
                if self._processes.get(service) is not old_process:
                    raise RuntimeError("active MCP changed during blue-green upgrade")
                chat_client_registered = self._chat_client_registered(service)
                self._persist_discovery_endpoint(
                    service,
                    green_port,
                    version=str(new_version),
                )
                if not self._reload_chat_client(
                    service,
                    required=chat_client_registered,
                ):
                    # Restore blue discovery before reporting the upgrade as a
                    # rollback. The green process remains owned by the caller
                    # and is cleaned up by the failure path below.
                    self._persist_discovery_endpoint(
                        service,
                        old_port,
                        version=str(_old_version),
                    )
                    if chat_client_registered:
                        self._reload_chat_client(service, required=True)
                    raise RuntimeError("Hermes MCP client failed to reload green endpoint")
                self._processes[service] = green["process"]
                self._log_handles[service] = green["handle"]
                self._active_ports[service] = green_port
            timeout = (
                self.drain_timeout_seconds
                if drain_timeout_seconds is None
                else max(0.0, float(drain_timeout_seconds))
            )
            if timeout:
                self._stop_event.wait(timeout)
            self._terminate_process(old_process, old_handle)

        result = self.supervisor.blue_green_upgrade(
            service,
            str(new_version),
            start_new=start_new,
            health_check=health_new,
            stop_old=cut_over_and_drain,
        )
        if not result.get("upgraded"):
            self._terminate_process(green.get("process"), green.get("handle"))
        return {
            **result,
            "active_endpoint": self.endpoint_for(service),
            "previous_endpoint": f"http://{self.host}:{old_port}/mcp",
        }

    @staticmethod
    def _probe_tools_with_timeout(url: str, timeout_seconds: float) -> list[str]:
        timeout_seconds = min(180.0, max(1.0, float(timeout_seconds)))

        async def probe() -> list[str]:
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            request_timeout = max(1.0, timeout_seconds - 1.0)
            timeout = httpx.Timeout(request_timeout, connect=min(5.0, request_timeout))
            async with httpx.AsyncClient(timeout=timeout) as http_client:
                async with streamable_http_client(
                    url,
                    http_client=http_client,
                    terminate_on_close=True,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        return [tool.name for tool in result.tools]

        async def bounded_probe() -> list[str]:
            return await asyncio.wait_for(probe(), timeout=timeout_seconds)

        # Dashboard health checks run on the web server's asyncio loop. Running
        # asyncio.run() directly there raises and leaks the probe coroutine;
        # execute the short-lived MCP client loop in a helper thread instead.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(bounded_probe())
        result: list[list[str]] = []
        failure: list[BaseException] = []

        def run_probe() -> None:
            try:
                result.append(asyncio.run(bounded_probe()))
            except BaseException as exc:
                failure.append(exc)

        worker = threading.Thread(target=run_probe, name="ios-mcp-health-probe", daemon=True)
        worker.start()
        worker.join(timeout_seconds + 1.0)
        if worker.is_alive():
            raise TimeoutError("MCP tools/list probe timed out")
        if failure:
            raise failure[0]
        return result[0] if result else []

    @staticmethod
    def _probe_tools(url: str) -> list[str]:
        return IOSMCPRuntimeSupervisor._probe_tools_with_timeout(url, 5.0)

    def run_once(self) -> list[dict[str, Any]]:
        if not self._health_cycle_lock.acquire(blocking=False):
            return []
        try:
            if self._stop_event.is_set():
                self.running = False
                return []
            results: list[dict[str, Any]] = []
            for item in self.supervisor.statuses():
                if self._stop_event.is_set():
                    break
                if item["name"] not in self.capabilities or item["state"] in {
                    MCPState.DISABLED.value,
                    MCPState.QUARANTINED.value,
                    MCPState.UPGRADING.value,
                }:
                    continue
                service = item["name"]
                observed = self.health_service(service)
                if observed.get("ok"):
                    state = self._record_runtime_success(service)
                    results.append({**state, "healthy": True, "detail": observed})
                    continue

                error = str(observed.get("error") or "health check failed")
                definitive = error in {"process_missing", "process_exited"}
                state = self.supervisor.record_failure(
                    service,
                    error,
                    schedule_restart=False,
                    quarantine_at_threshold=False,
                )
                checked = {
                    **state,
                    "healthy": False,
                    "detail": observed,
                    "error": error,
                }
                results.append(checked)
                if definitive:
                    self._enqueue_health_restart(service, observed, error)
                    continue
                if state["failures"] < self.supervisor.failure_threshold:
                    continue

                # A live child can miss a short tools/list deadline while ARM
                # is saturated. Confirm with one longer probe before allowing
                # a destructive restart. This keeps transient latency in
                # DEGRADED and reserves restart/quarantine for a confirmed
                # hang or a process that actually exited.
                confirmed = self.health_service(
                    service,
                    probe_timeout_seconds=self.recovery_probe_timeout_seconds,
                )
                if confirmed.get("ok"):
                    restored = self._record_runtime_success(service)
                    results[-1] = {
                        **restored,
                        "healthy": True,
                        "detail": confirmed,
                        "recovered_by_extended_probe": True,
                    }
                    continue
                self._enqueue_health_restart(
                    service,
                    confirmed,
                    str(confirmed.get("error") or error),
                )
            if not self._stop_event.is_set():
                self._process_queue()
            if not self._stop_event.is_set():
                self._refresh_runtime_running()
            return results
        finally:
            self._health_cycle_lock.release()

    def _refresh_runtime_running(self) -> bool:
        """Recompute fleet readiness after queued recovery changes a process."""

        if self._stop_event.is_set() or self.starting:
            return False
        required: list[str] = []
        ready = True
        states = {
            service: self.supervisor.status(service)["state"]
            for service in self.capabilities
        }
        with self._lock:
            for service in self.capabilities:
                state = states[service]
                if state == MCPState.DISABLED.value:
                    continue
                required.append(service)
                process = self._processes.get(service)
                if (
                    state in {
                        MCPState.QUARANTINED.value,
                        MCPState.UPGRADING.value,
                    }
                    or process is None
                    or process.poll() is not None
                ):
                    ready = False
            self.running = bool(required) and ready
            return self.running

    def _record_runtime_success(self, service: str) -> dict[str, Any]:
        self._health_generations[service] = self._health_generations.get(service, 0) + 1
        return self.supervisor.record_success(service)

    def _enqueue_health_restart(
        self,
        service: str,
        observation: Mapping[str, Any],
        reason: str,
    ) -> None:
        expected_pid = int(observation.get("pid") or 0)
        generation = self._health_generations.get(service, 0)
        self.supervisor.enqueue(
            service,
            "restart",
            {
                "reason": str(reason),
                "trigger": "runtime_health",
                "expected_pid": expected_pid,
                "health_generation": generation,
            },
            idempotency_key=(
                f"restart:{service}:health:{generation}:pid:{expected_pid}"
            ),
        )

    def _process_queue(self) -> None:
        if self._stop_event.is_set():
            return
        commands = self.supervisor.pull(limit=100)
        for command in commands:
            if self._stop_event.is_set():
                self.supervisor.ack(
                    command["id"],
                    success=False,
                    error=_RUNTIME_STOPPING_ERROR,
                    retry_at=int(time.time()) + 5,
                )
                continue
            success = False
            error = ""
            try:
                if command["action"] == "restart":
                    if self._health_restart_is_stale(command):
                        success = True
                    else:
                        result = self._restart_runtime_service(
                            command["service"],
                            reason="queued restart",
                        )
                        success = result["state"] == MCPState.RUNNING.value
                        if result.get("last_error") == _RUNTIME_STOPPING_ERROR:
                            error = _RUNTIME_STOPPING_ERROR
                elif command["action"] == "start":
                    success = self.start_service(command["service"], verify=True)
                elif command["action"] == "stop":
                    success = self.stop_service(command["service"])
                else:
                    error = "unsupported supervisor action"
            except Exception as exc:
                error = type(exc).__name__
            self.supervisor.ack(
                command["id"],
                success=success,
                error=error or ("" if success else "runtime action failed"),
                retry_at=int(time.time()) + 5,
            )

    def _health_restart_is_stale(self, command: Mapping[str, Any]) -> bool:
        payload = command.get("payload")
        if not isinstance(payload, Mapping) or payload.get("trigger") != "runtime_health":
            return False
        service = self.supervisor._name(command.get("service"))
        status = self.supervisor.status(service)
        if status["state"] == MCPState.RUNNING.value and status["failures"] == 0:
            return True
        if int(payload.get("health_generation") or 0) != self._health_generations.get(
            service, 0
        ):
            return True
        expected_pid = int(payload.get("expected_pid") or 0)
        if expected_pid <= 0:
            return False
        with self._lock:
            process = self._processes.get(service)
        return bool(
            process is not None
            and process.poll() is None
            and int(process.pid) != expected_pid
        )

    def _restart_runtime_service(
        self,
        name: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Restart one child with exactly one verified startup probe sequence."""

        service = self.supervisor._name(name)
        current = self.supervisor.status(service)
        if current["state"] == MCPState.DISABLED.value:
            return current
        if self._stop_event.is_set():
            return self.supervisor._transition(
                service,
                MCPState.RECOVERING,
                "restart deferred during shutdown",
                error=_RUNTIME_STOPPING_ERROR,
            )
        self.supervisor._transition(service, MCPState.RECOVERING, reason)
        try:
            self.stop_service(service)
            if self.supervisor.restart_backoff_seconds and self._stop_event.wait(
                self.supervisor.restart_backoff_seconds
            ):
                raise RuntimeError(_RUNTIME_STOPPING_ERROR)
            if self._stop_event.is_set():
                raise RuntimeError(_RUNTIME_STOPPING_ERROR)
            if not self.start_service(service, verify=True):
                if self._stop_event.is_set():
                    raise RuntimeError(_RUNTIME_STOPPING_ERROR)
                raise RuntimeError("MCP process failed its verified restart")
            return self._record_runtime_success(service)
        except Exception as exc:
            if self._stop_event.is_set() or str(exc) == _RUNTIME_STOPPING_ERROR:
                return self.supervisor._transition(
                    service,
                    MCPState.RECOVERING,
                    "restart deferred during shutdown",
                    error=_RUNTIME_STOPPING_ERROR,
                )
            return self.supervisor._transition(
                service,
                MCPState.QUARANTINED,
                "restart failed",
                error=str(exc),
            )

    def _run(self) -> None:
        if self._stop_event.wait(self.initial_health_delay_seconds):
            return
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("iOS MCP runtime health cycle failed")
            self._stop_event.wait(self.health_interval_seconds)

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop_event.wait(3600):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


def register_default_mcp_services(
    db_path: str | os.PathLike[str] | None = None,
    *,
    version: str = "1.0.0",
    transport: str = "stdio",
    host: str = "127.0.0.1",
    base_port: int = 8760,
) -> dict[str, Any]:
    """Register the complete isolated iOS MCP fleet in the durable supervisor."""

    from hermes_cli.ios_mcp_server import ios_mcp_manifests

    supervisor = IOSMCPSupervisor(db_path)
    manifests = ios_mcp_manifests(
        transport=transport,
        host=host,
        base_port=base_port,
    )
    statuses = [
        supervisor.register(
            name,
            version=version,
            metadata=manifest,
            enabled=True,
        )
        for name, manifest in manifests.items()
    ]
    return {"count": len(statuses), "services": statuses, "db_path": str(supervisor.path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register and inspect Hermes iOS MCP supervisor services")
    parser.add_argument("--register", action="store_true", help="register every independent iOS MCP")
    parser.add_argument("--run", action="store_true", help="run and supervise every independent HTTP MCP")
    parser.add_argument("--db", type=Path, default=None, help="supervisor SQLite database path")
    parser.add_argument("--db-dir", type=Path, default=None, help="iOS intelligence database directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--base-port", type=int, default=8760)
    parser.add_argument("--health-interval", type=float, default=15.0)
    parser.add_argument("--log-directory", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.run:
        runtime = IOSMCPRuntimeSupervisor(
            args.db,
            db_dir=args.db_dir,
            host=args.host,
            base_port=args.base_port,
            health_interval_seconds=args.health_interval,
            log_directory=args.log_directory,
        )
        runtime.run_forever()
        return 0
    if args.register:
        result = register_default_mcp_services(
            args.db,
            transport="streamable-http",
            host=args.host,
            base_port=args.base_port,
        )
        print(_json(result))
        return 0
    supervisor = IOSMCPSupervisor(args.db)
    print(_json({"services": supervisor.statuses()}))
    return 0


MCPSupervisor = IOSMCPSupervisor
Supervisor = IOSMCPSupervisor

__all__ = [
    "IOSMCPRuntimeSupervisor", "IOSMCPSupervisor", "MCPService", "MCPState", "MCPSupervisor", "Supervisor",
    "main", "register_default_mcp_services",
]


if __name__ == "__main__":
    raise SystemExit(main())
