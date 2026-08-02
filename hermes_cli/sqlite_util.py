"""Shared SQLite primitives for the small per-profile / board stores.

The projects and kanban stores open WAL SQLite files with the same two
primitives — an idempotent column-add migration and an IMMEDIATE write
transaction. One definition here keeps the two stores from drifting.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
import shutil
import sqlite3


logger = logging.getLogger(__name__)


def _fallback_marker(path: Path) -> Path:
    return path.with_name(path.name + ".hermes-fallback")


def _write_fallback_marker(path: Path, source: Path | None) -> None:
    marker = _fallback_marker(path)
    if marker.exists():
        return
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(str(source or "") + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, marker)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def resolve_sqlite_fallback(
    path: Path,
    *,
    env_var: str,
    label: str,
    fallback_relative: str,
) -> tuple[Path, Path | None]:
    """Return a writable fallback path when *path*'s filesystem is full.

    The fallback is only selected when the requested mount reports zero free
    bytes.  The optional second return value is the source database that must
    be copied after the caller has selected the destination.  Keeping this
    decision in one helper makes every small SQLite store use the same
    operator-configurable, bounded fallback policy.
    """

    path = Path(path)
    configured_root = os.environ.get(env_var, "").strip()
    roots = (
        [Path(configured_root).expanduser()]
        if configured_root
        else [
            Path("/dev/shm/hermes-agent"),
            Path("/tmp/hermes-agent"),
            Path("/var/tmp/hermes-agent"),
        ]
    )
    relative = Path(fallback_relative)
    for root in roots:
        destination = root / relative
        if destination == path:
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not os.access(destination.parent, os.W_OK | os.X_OK):
                continue
            if shutil.disk_usage(destination.parent).free <= 0:
                continue
        except OSError:
            continue
        if destination.is_file() and _fallback_marker(destination).is_file():
            logger.warning(
                "%s is using its previously selected fallback database path %s",
                label,
                destination,
            )
            return destination, None

    path = Path(path)
    try:
        if shutil.disk_usage(path.parent).free > 0:
            return path, None
    except OSError:
        return path, None

    for root in roots:
        destination = root / relative
        if destination == path:
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not os.access(destination.parent, os.W_OK | os.X_OK):
                continue
            if shutil.disk_usage(destination.parent).free <= 0:
                continue
        except OSError:
            continue
        source = path if path.is_file() and not path.is_symlink() else None
        logger.warning(
            "%s is on a full filesystem; using fallback database path %s "
            "(source: %s)",
            label,
            destination,
            path,
        )
        return destination, source
    return path, None


def copy_sqlite_fallback(source: Path | None, destination: Path) -> None:
    """Copy a quiesced SQLite file and data-bearing sidecars once.

    Deployments stop the service before the first copy.  ``-wal`` and
    ``-journal`` are retained because they may contain committed rows that
    have not yet been checkpointed into the main database; ``-shm`` is a
    coordination file and is intentionally recreated by SQLite.
    """

    if source is None:
        return
    if destination.exists():
        _write_fallback_marker(destination, source)
        return
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.fallback-{os.getpid()}")
    temporary_sidecars: list[Path] = []
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        for suffix in ("-wal", "-journal"):
            source_sidecar = Path(f"{source}{suffix}")
            destination_sidecar = Path(f"{destination}{suffix}")
            temporary_sidecar = Path(f"{temporary}{suffix}")
            if source_sidecar.is_file() and not source_sidecar.is_symlink():
                temporary_sidecars.append(temporary_sidecar)
                shutil.copy2(source_sidecar, temporary_sidecar)
                os.replace(temporary_sidecar, destination_sidecar)
        os.chmod(destination, 0o600)
        _write_fallback_marker(destination, source)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        for temporary_sidecar in temporary_sidecars:
            try:
                temporary_sidecar.unlink()
            except FileNotFoundError:
                pass


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """``ALTER TABLE <table> ADD COLUMN <ddl>``, idempotent across races.

    Returns ``True`` when this call added the column. Swallows the
    ``duplicate column name`` error a concurrent migrator may have run first
    (issue #21708). ``column`` is the human-readable name for the call site;
    ``ddl`` carries the actual definition.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return False
        raise


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """An IMMEDIATE write transaction: at most one concurrent writer wins.

    The explicit ROLLBACK is guarded so a SQLite auto-rollback (no active
    transaction left under EIO / lock contention / corruption) cannot shadow
    the original exception with a spurious rollback error.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        conn.execute("COMMIT")
