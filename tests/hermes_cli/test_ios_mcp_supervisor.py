from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.ios_mcp_supervisor import _configure_supervisor_journal_mode


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
