from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.ios_mcp_supervisor import IOSMCPSupervisor, _configure_supervisor_journal_mode


class _WalUnavailableConnection:
    def __init__(self, *, disk_mode: str = "delete") -> None:
        self.disk_mode = disk_mode
        self.statements: list[str] = []

    def execute(self, sql: str):
        self.statements.append(sql)
        normalized = sql.lower().replace(" ", "")
        if normalized == "pragmajournal_mode=wal":
            raise sqlite3.OperationalError("disk I/O error")
        if normalized == "pragmajournal_mode" and self.disk_mode == "wal":
            return SimpleNamespace(fetchone=lambda: ("wal",))
        if normalized == "pragmajournal_mode=delete":
            self.disk_mode = "delete"
            return SimpleNamespace(fetchone=lambda: ("delete",))
        return SimpleNamespace(fetchone=lambda: (self.disk_mode,))


class _JournalProbeUnavailableConnection(_WalUnavailableConnection):
    def execute(self, sql: str):
        self.statements.append(sql)
        normalized = sql.lower().replace(" ", "")
        if "pragmajournal_mode" in normalized:
            raise sqlite3.OperationalError("disk I/O error")
        return SimpleNamespace(fetchone=lambda: ("delete",))


def test_supervisor_uses_delete_journal_on_wal_incompatible_mount(monkeypatch, tmp_path: Path):
    conn = _WalUnavailableConnection()
    monkeypatch.setattr(
        "hermes_cli.ios_mcp_supervisor.shutil.disk_usage",
        lambda path: SimpleNamespace(free=128 * 1024 * 1024),
    )

    mode = _configure_supervisor_journal_mode(conn, tmp_path / "supervisor.db")

    assert mode == "delete"
    assert "PRAGMA journal_mode=DELETE" in conn.statements


def test_supervisor_keeps_disk_io_error_on_full_mount(monkeypatch, tmp_path: Path):
    conn = _WalUnavailableConnection()
    monkeypatch.setattr(
        "hermes_cli.ios_mcp_supervisor.shutil.disk_usage",
        lambda path: SimpleNamespace(free=0),
    )

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        _configure_supervisor_journal_mode(conn, tmp_path / "supervisor.db")


def test_supervisor_allows_low_but_nonzero_free_space(monkeypatch, tmp_path: Path):
    conn = _WalUnavailableConnection()
    monkeypatch.setattr(
        "hermes_cli.ios_mcp_supervisor.shutil.disk_usage",
        lambda path: SimpleNamespace(free=1),
    )

    assert _configure_supervisor_journal_mode(conn, tmp_path / "supervisor.db") == "delete"


def test_supervisor_leaves_default_mode_when_mount_rejects_all_probes(
    monkeypatch, tmp_path: Path
):
    conn = _JournalProbeUnavailableConnection()
    monkeypatch.setattr(
        "hermes_cli.ios_mcp_supervisor.shutil.disk_usage",
        lambda path: SimpleNamespace(free=128 * 1024 * 1024),
    )

    assert (
        _configure_supervisor_journal_mode(conn, tmp_path / "supervisor.db")
        == "default"
    )
    assert "PRAGMA journal_mode=DELETE" in conn.statements


def test_supervisor_does_not_downgrade_an_existing_wal_database(
    monkeypatch, tmp_path: Path
):
    conn = _WalUnavailableConnection(disk_mode="wal")
    monkeypatch.setattr(
        "hermes_cli.ios_mcp_supervisor.shutil.disk_usage",
        lambda path: SimpleNamespace(free=128 * 1024 * 1024),
    )

    assert _configure_supervisor_journal_mode(conn, tmp_path / "supervisor.db") == "wal"

    assert "PRAGMA journal_mode=DELETE" not in conn.statements


def test_supervisor_uses_configured_fallback_when_home_mount_is_full(
    monkeypatch, tmp_path: Path
):
    home = tmp_path / "full-home"
    home.mkdir()
    fallback = tmp_path / "runtime"
    monkeypatch.delenv("HERMES_IOS_SUPERVISOR_DB", raising=False)
    monkeypatch.setenv("HERMES_IOS_SUPERVISOR_FALLBACK_DIR", str(fallback))
    monkeypatch.setattr("hermes_cli.ios_mcp_supervisor.get_hermes_home", lambda: home)

    import shutil

    real_disk_usage = shutil.disk_usage

    def disk_usage(path):
        if Path(path) == home:
            return SimpleNamespace(free=0)
        return real_disk_usage(path)

    monkeypatch.setattr("hermes_cli.ios_mcp_supervisor.shutil.disk_usage", disk_usage)

    supervisor = IOSMCPSupervisor()

    assert supervisor.path == fallback / "ios-mcp-supervisor.db"


def test_supervisor_honors_explicit_database_path_even_when_home_is_full(
    monkeypatch, tmp_path: Path
):
    configured = tmp_path / "configured" / "supervisor.db"
    monkeypatch.setenv("HERMES_IOS_SUPERVISOR_DB", str(configured))
    monkeypatch.setattr(
        "hermes_cli.ios_mcp_supervisor.shutil.disk_usage",
        lambda path: SimpleNamespace(free=0),
    )

    supervisor = IOSMCPSupervisor()

    assert supervisor.path == configured


def test_fork_process_adapter_forwards_process_lifecycle_methods():
    from hermes_cli.ios_mcp_supervisor import _ForkProcessAdapter

    class _RawProcess:
        pid = 4242
        exitcode = None

        def __init__(self):
            self.calls = []

        def is_alive(self):
            return False

        def join(self, _timeout=None):
            self.calls.append("join")

        def terminate(self):
            self.calls.append("terminate")

        def kill(self):
            self.calls.append("kill")

        def close(self):
            self.calls.append("close")

    raw = _RawProcess()
    adapter = _ForkProcessAdapter(raw, ["python", "-m", "child"])

    adapter.terminate()
    adapter.kill()
    adapter.close()

    assert raw.calls == ["terminate", "kill", "close"]


class _FakeProcess:
    pid = 4242

    def __init__(self):
        self.terminated = False
        self.closed = False

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def close(self):
        self.closed = True


def test_lazy_runtime_reaps_only_idle_child_without_health_probing(tmp_path, monkeypatch):
    from hermes_cli.ios_mcp_supervisor import IOSMCPRuntimeSupervisor

    runtime = IOSMCPRuntimeSupervisor(
        tmp_path / "lazy-runtime.db",
        capabilities=("ios-power",),
        lazy_start=True,
        idle_timeout_seconds=5,
        log_directory=tmp_path / "logs",
    )
    process = _FakeProcess()
    runtime._processes["ios-power"] = process
    runtime._lazy_last_activity["ios-power"] = time.monotonic() - 10
    monkeypatch.setattr(
        runtime,
        "health_service",
        lambda *_args, **_kwargs: pytest.fail("lazy health cycle must not probe MCP"),
    )

    try:
        results = runtime._reap_idle_lazy_services()
        assert results == [{
            "name": "ios-power",
            "lazy": True,
            "recycled": True,
            "reason": "idle_timeout",
        }]
        assert process.terminated is True
        assert process.closed is True
        assert "ios-power" not in runtime._processes
    finally:
        runtime.stop()


def test_lazy_runtime_keeps_recent_child_hot(tmp_path):
    from hermes_cli.ios_mcp_supervisor import IOSMCPRuntimeSupervisor

    runtime = IOSMCPRuntimeSupervisor(
        tmp_path / "lazy-hot-runtime.db",
        capabilities=("ios-power",),
        lazy_start=True,
        idle_timeout_seconds=30,
        log_directory=tmp_path / "logs",
    )
    process = _FakeProcess()
    runtime._processes["ios-power"] = process
    runtime._lazy_last_activity["ios-power"] = time.monotonic()
    try:
        assert runtime._reap_idle_lazy_services() == []
        assert process.terminated is False
    finally:
        runtime.stop()
