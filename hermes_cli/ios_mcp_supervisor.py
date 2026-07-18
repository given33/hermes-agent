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
            return {**self.status(service), "healthy": True, "detail": detail}
        self.record_failure(service, error or "health check failed")
        return {**self.status(service), "healthy": False, "detail": detail, "error": error or "health check failed"}

    check_health = health_check
    health = health_check

    def check_all(self) -> list[dict[str, Any]]:
        return [self.health_check(item["name"]) for item in self.statuses()]

    run_health_checks = check_all

    def record_failure(self, name: str, error: str = "") -> dict[str, Any]:
        service = self._name(name)
        with self._lock, self._connect() as conn, write_txn(conn):
            row = conn.execute("SELECT failures,state FROM mcp_services WHERE name=?", (service,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown MCP: {name}")
            failures = int(row["failures"]) + 1
            state = MCPState.DEGRADED.value if failures < self.failure_threshold else MCPState.QUARANTINED.value
            conn.execute("UPDATE mcp_services SET failures=?,state=?,last_error=?,updated_at=? WHERE name=?", (failures, state, str(error)[:2000], int(time.time()), service))
            conn.execute("INSERT INTO mcp_supervisor_events(service,from_state,to_state,reason,created_at) VALUES(?,?,?,?,?)", (service, row["state"], state, str(error)[:512], int(time.time())))
        if failures < self.failure_threshold:
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
            rows = conn.execute("SELECT * FROM mcp_supervisor_queue WHERE state='pending' AND available_at<=? ORDER BY created_at LIMIT ?", (now, max(1, min(int(limit), 1000)))).fetchall()
            conn.executemany(
                "UPDATE mcp_supervisor_queue SET state='delivered',delivered_at=?,attempts=attempts+1 WHERE id=?",
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
        failure_threshold: int = 3,
        restart_backoff_seconds: float = 1.0,
        log_directory: str | os.PathLike[str] | None = None,
        python_executable: str | None = None,
        capabilities: tuple[str, ...] | None = None,
        blue_green_port_offset: int = 100,
        drain_timeout_seconds: float = 30.0,
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
        self.health_interval_seconds = max(2.0, float(health_interval_seconds))
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
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
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
            configured = configured_servers.get(name)
            if (
                isinstance(configured, Mapping)
                and configured.get("enabled") is False
                and status["state"] != MCPState.DISABLED.value
            ):
                self.supervisor.disable(name, "disabled in mcp_servers config")

    def start(self) -> "IOSMCPRuntimeSupervisor":
        with self._lock:
            if self.running:
                return self
            self._register_callbacks()
            self._stop_event.clear()
            required_ok = True
            for name in self.capabilities:
                state = self.supervisor.status(name)["state"]
                if state in {
                    MCPState.DISABLED.value,
                    MCPState.QUARANTINED.value,
                    MCPState.UPGRADING.value,
                }:
                    continue
                if not self.start_service(name, verify=True):
                    required_ok = False
            self.running = required_ok
            self._thread = threading.Thread(
                target=self._run,
                name="ios-mcp-runtime-supervisor",
                daemon=True,
            )
            self._thread.start()
        return self

    def health(self) -> dict[str, Any]:
        """Return truthful fleet health, including per-service probes."""

        services: list[dict[str, Any]] = []
        for name in self.capabilities:
            state = self.supervisor.status(name)["state"]
            if state == MCPState.DISABLED.value:
                continue
            probe = self.health_service(name)
            services.append({"name": name, "state": state, **probe})
        healthy = sum(1 for item in services if item.get("ok") is True)
        required = len(services)
        return {
            "ok": required > 0 and healthy == required,
            "running": bool(self.running),
            "healthy_count": healthy,
            "required_count": required,
            "services": services,
        }

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(max(0.1, float(timeout)))
        for name in self.capabilities:
            self.stop_service(name)
        self._thread = None
        self.running = False

    close = stop

    def _spawn_process(
        self,
        service: str,
        *,
        port: int,
        python_executable: str | None = None,
        log_suffix: str = "",
    ) -> tuple[subprocess.Popen[Any], Any]:
        safe_suffix = "".join(
            character for character in str(log_suffix) if character.isalnum() or character in {".", "_", "-"}
        )[:80]
        suffix = f"-{safe_suffix}" if safe_suffix else ""
        log_path = self.log_directory / f"{service}{suffix}.log"
        log_handle = log_path.open("ab", buffering=0)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if self.db_dir is not None:
            env["HERMES_IOS_INTELLIGENCE_DIR"] = str(self.db_dir)
        if self.owner_id:
            env["HERMES_IOS_OWNER_ID"] = self.owner_id
        try:
            process = subprocess.Popen(
                self.command_for(
                    service,
                    port=port,
                    python_executable=python_executable,
                ),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
            )
        except Exception:
            log_handle.close()
            raise
        return process, log_handle

    @staticmethod
    def _terminate_process(process: subprocess.Popen[Any] | None, log_handle: Any = None) -> None:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if log_handle is not None:
            log_handle.close()

    def start_service(self, name: str, *, verify: bool = True) -> bool:
        service = self.supervisor._name(name)
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
        deadline = time.monotonic() + 10.0
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

    def health_service(self, name: str) -> dict[str, Any]:
        service = self.supervisor._name(name)
        with self._lock:
            process = self._processes.get(service)
        if process is None:
            return {"ok": False, "error": "process_missing"}
        exit_code = process.poll()
        if exit_code is not None:
            return {"ok": False, "error": "process_exited", "exit_code": exit_code}
        try:
            tools = self._probe_tools(self.endpoint_for(service))
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}
        return {"ok": bool(tools), "pid": process.pid, "tools": tools}

    def _persist_discovery_endpoint(
        self,
        name: str,
        port: int,
        *,
        version: str | None = None,
    ) -> None:
        from hermes_cli.config import read_raw_config, save_config

        service = self.supervisor._name(name)
        config = dict(read_raw_config() or {})
        raw_servers = config.get("mcp_servers")
        servers = dict(raw_servers) if isinstance(raw_servers, Mapping) else {}
        entry = dict(servers.get(service) or {})
        endpoint = f"http://{self.host}:{int(port)}/mcp"
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
        config["mcp_servers"] = servers
        save_config(config)

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
            with mcp_tool._lock:
                server = mcp_tool._servers.get(name)
                return bool(
                    server is not None
                    and getattr(server, "session", None) is not None
                    and getattr(server, "_registered_tool_names", ())
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
            )
            green.update({"process": process, "handle": handle})
            deadline = time.monotonic() + 10.0
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
    def _probe_tools(url: str) -> list[str]:
        async def probe() -> list[str]:
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            timeout = httpx.Timeout(3.0, connect=3.0)
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
            return await asyncio.wait_for(probe(), timeout=5.0)

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
        worker.join(6.0)
        if worker.is_alive():
            raise TimeoutError("MCP tools/list probe timed out")
        if failure:
            raise failure[0]
        return result[0] if result else []

    def run_once(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in self.supervisor.statuses():
            if item["name"] not in self.capabilities or item["state"] in {
                MCPState.DISABLED.value,
                MCPState.QUARANTINED.value,
                MCPState.UPGRADING.value,
            }:
                continue
            checked = self.supervisor.health_check(item["name"])
            results.append(checked)
            if checked["state"] == MCPState.QUARANTINED.value:
                self.stop_service(item["name"])
        self._process_queue()
        return results

    def _process_queue(self) -> None:
        for command in self.supervisor.pull(limit=100):
            success = False
            error = ""
            try:
                if command["action"] == "restart":
                    result = self.supervisor.restart(command["service"], reason="queued restart")
                    success = result["state"] == MCPState.RUNNING.value
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

    def _run(self) -> None:
        if self._stop_event.wait(5.0):
            return
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                pass
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
