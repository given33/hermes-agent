"""Durable, revocable mobile device sessions and APNs registrations.

The native app keeps only opaque credentials in Keychain.  The server is the
source of truth for device sessions, token rotation, revocation, and APNs
registrations.  Business data remains in the existing ``HERMES_HOME`` stores;
this database contains authentication and delivery metadata only.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from hermes_cli.dashboard_auth.base import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
)
from hermes_cli.sqlite_util import write_txn
from hermes_constants import get_hermes_home


logger = logging.getLogger(__name__)


ACCESS_TTL_SECONDS = 15 * 60
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60
# A benign concurrent double-send (HTTP retry racing the keychain write,
# main app + extension refreshing together) replays a token seconds after
# its winning rotation. Revoking on that single race logged users out and
# silently disabled APNs; only replays older than this window are hostile.
_REFRESH_REPLAY_GRACE_SECONDS = 120
ACCOUNT_DELETION_LEASE_SECONDS = 300
SCHEMA_VERSION = 7

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mobile_devices (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    account_generation TEXT NOT NULL DEFAULT 'legacy',
    name            TEXT NOT NULL,
    model           TEXT NOT NULL DEFAULT '',
    os_version      TEXT NOT NULL DEFAULT '',
    app_version     TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    revoked_at      INTEGER,
    revoke_reason   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mobile_sessions (
    id                  TEXT PRIMARY KEY,
    device_id           TEXT NOT NULL REFERENCES mobile_devices(id) ON DELETE CASCADE,
    user_id             TEXT NOT NULL,
    account_generation  TEXT NOT NULL DEFAULT '',
    access_token_hash   TEXT NOT NULL UNIQUE,
    refresh_token_hash  TEXT NOT NULL UNIQUE,
    access_expires_at   INTEGER NOT NULL,
    refresh_expires_at  INTEGER NOT NULL,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    last_seen_at        INTEGER NOT NULL,
    revoked_at          INTEGER,
    revoke_reason       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_mobile_sessions_device
    ON mobile_sessions(device_id);
CREATE INDEX IF NOT EXISTS idx_mobile_sessions_access
    ON mobile_sessions(access_token_hash);
CREATE INDEX IF NOT EXISTS idx_mobile_sessions_refresh
    ON mobile_sessions(refresh_token_hash);

CREATE TABLE IF NOT EXISTS mobile_refresh_history (
    token_hash      TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES mobile_sessions(id) ON DELETE CASCADE,
    rotated_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mobile_refresh_history_session
    ON mobile_refresh_history(session_id);

CREATE TABLE IF NOT EXISTS mobile_refresh_idempotency (
    token_hash           TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL REFERENCES mobile_sessions(id) ON DELETE CASCADE,
    response_nonce       BLOB NOT NULL,
    response_ciphertext  BLOB NOT NULL,
    created_at           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mobile_refresh_idempotency_session
    ON mobile_refresh_idempotency(session_id);

CREATE TABLE IF NOT EXISTS mobile_apns_tokens (
    id              TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL REFERENCES mobile_devices(id) ON DELETE CASCADE,
    token           TEXT NOT NULL,
    token_hash      TEXT NOT NULL,
    environment     TEXT NOT NULL,
    bundle_id       TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    disabled_at     INTEGER,
    last_error      TEXT NOT NULL DEFAULT '',
    UNIQUE(device_id, environment, bundle_id)
);

CREATE INDEX IF NOT EXISTS idx_mobile_apns_active
    ON mobile_apns_tokens(disabled_at, environment);
CREATE INDEX IF NOT EXISTS idx_mobile_apns_token_hash
    ON mobile_apns_tokens(token_hash);

CREATE TABLE IF NOT EXISTS mobile_account_deletion_outbox (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    account_generation      TEXT NOT NULL,
    owner_scope             TEXT NOT NULL,
    state                   TEXT NOT NULL DEFAULT 'pending',
    device_deliveries_json  TEXT NOT NULL DEFAULT '{}',
    attempts                INTEGER NOT NULL DEFAULT 0,
    available_at            INTEGER NOT NULL,
    lease_token             TEXT NOT NULL DEFAULT '',
    leased_until            INTEGER NOT NULL DEFAULT 0,
    requested_at            INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL,
    completed_at            INTEGER,
    last_error              TEXT NOT NULL DEFAULT '',
    UNIQUE(user_id, account_generation)
);

CREATE INDEX IF NOT EXISTS idx_mobile_account_deletion_due
    ON mobile_account_deletion_outbox(state, available_at, leased_until);

CREATE TABLE IF NOT EXISTS mobile_account_generations (
    user_id         TEXT PRIMARY KEY COLLATE NOCASE,
    generation      TEXT NOT NULL UNIQUE,
    created_at      INTEGER NOT NULL
);
"""


def mobile_auth_db_path() -> Path:
    configured = os.environ.get("HERMES_MOBILE_AUTH_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    return get_hermes_home() / "dashboard" / "mobile-auth.db"


def _fallback_mobile_auth_db_path(path: Path) -> Optional[Path]:
    """Choose a writable database mount when the normal Hermes home is full."""

    try:
        if shutil.disk_usage(path.parent).free > 0:
            return None
    except OSError:
        return None

    configured_root = os.environ.get("HERMES_MOBILE_AUTH_FALLBACK_DIR", "").strip()
    roots = (
        [Path(configured_root).expanduser()]
        if configured_root
        else [
            Path("/var/lib/hermes-agent"),
            Path("/dev/shm/hermes-agent"),
            Path("/tmp/hermes-agent"),
            Path("/var/tmp/hermes-agent"),
        ]
    )
    for root in roots:
        fallback_parent = root / "dashboard"
        try:
            fallback_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not os.access(fallback_parent, os.W_OK | os.X_OK):
                continue
            if shutil.disk_usage(fallback_parent).free <= 0:
                continue
        except OSError:
            continue
        fallback = fallback_parent / path.name
        if fallback != path:
            return fallback
    return None


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    """Copy a quiesced SQLite database without losing sidecar-backed rows.

    The deployment installer stops the service before this migration. A
    SQLite ``backup()`` can still fail when the source mount has no free
    blocks, even for a read-only source connection, so copy the database file
    directly and carry the rollback/WAL sidecar that contains pending rows.
    SQLite recreates the ``-shm`` coordination file on the destination.
    """

    temporary = destination.with_name(f".{destination.name}.migrate-{os.getpid()}")
    temporary_sidecars: list[Path] = []
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        for suffix in ("-wal", "-journal"):
            source_sidecar = Path(f"{source}{suffix}")
            destination_sidecar = Path(f"{destination}{suffix}")
            temporary_sidecar = Path(f"{temporary}{suffix}")
            if source_sidecar.is_file():
                temporary_sidecars.append(temporary_sidecar)
                shutil.copy2(source_sidecar, temporary_sidecar)
                os.replace(temporary_sidecar, destination_sidecar)
            else:
                try:
                    destination_sidecar.unlink()
                except FileNotFoundError:
                    pass
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


def _now() -> int:
    return int(time.time())


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_access_token() -> str:
    return "hma_" + secrets.token_urlsafe(48)


def _new_refresh_token() -> str:
    return "hmr_" + secrets.token_urlsafe(64)


def _bounded(value: str, limit: int) -> str:
    return str(value or "").strip()[:limit]


@dataclass(frozen=True)
class MobileDeviceInfo:
    id: str = ""
    name: str = ""
    model: str = ""
    os_version: str = ""
    app_version: str = ""


@dataclass(frozen=True)
class MobileSessionRecord:
    session_id: str
    device_id: str
    user_id: str
    account_generation: str
    access_expires_at: int
    refresh_expires_at: int


@dataclass(frozen=True)
class MobileTokenPair:
    access_token: str
    refresh_token: str
    session: MobileSessionRecord


class MobileDeviceStore:
    """Small per-HERMES_HOME SQLite store with one connection per operation."""

    _ACCESS_CACHE: dict[str, tuple[int, Optional[MobileSessionRecord]]] = {}
    _ACCESS_CACHE_LOCK = threading.Lock()
    _ACCESS_CACHE_TTL_SECONDS = 1

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        clock: Callable[[], int] = _now,
    ) -> None:
        self._manage_parent_permissions = db_path is None
        requested_path = db_path if db_path is not None else mobile_auth_db_path()
        self._fallback_source_path: Optional[Path] = None
        if db_path is None:
            fallback_path = _fallback_mobile_auth_db_path(requested_path)
            if fallback_path is not None:
                self._fallback_source_path = (
                    requested_path if requested_path.exists() else None
                )
                requested_path = fallback_path
                logger.warning(
                    "mobile-auth.db is on a full filesystem; using fallback "
                    "database path %s",
                    requested_path,
                )
        self.db_path = requested_path
        self._clock = clock

    def connect(self) -> sqlite3.Connection:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._manage_parent_permissions:
            self._restrict_permissions(path.parent, 0o700)
        if self._fallback_source_path and not path.exists():
            _copy_sqlite_database(self._fallback_source_path, path)
            self._fallback_source_path = None
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            from hermes_state import apply_wal_with_fallback

            apply_wal_with_fallback(
                conn, db_label="mobile-auth.db", database_path=path
            )
            current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    "mobile-auth.db was created by a newer Hermes version "
                    f"(schema {current_version} > {SCHEMA_VERSION})"
                )
            session_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(mobile_sessions)").fetchall()
            }
            if session_columns and "account_generation" not in session_columns:
                # Pre-v6 credentials did not capture their issuance boundary.
                # Leave them unbound so they fail closed instead of guessing
                # that a same-name account is the original account.
                conn.execute(
                    "ALTER TABLE mobile_sessions ADD COLUMN account_generation "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if current_version < 7:
                # Some pre-v7 databases contain the deletion outbox but not
                # the generation lookup table. The v7 outbox rebuild queries
                # that table, so create this additive dependency before the
                # data migration; the complete schema and indexes are applied
                # immediately afterwards.
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS mobile_account_generations ("
                    "user_id TEXT PRIMARY KEY COLLATE NOCASE,"
                    "generation TEXT NOT NULL UNIQUE,"
                    "created_at INTEGER NOT NULL)"
                )
                self._migrate_generation_boundaries_v7(conn)
            conn.executescript(_SCHEMA_SQL)
            if current_version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
            self._restrict_permissions(path, 0o600)
            return conn
        except Exception:
            conn.close()
            raise

    @staticmethod
    def _migrate_generation_boundaries_v7(conn: sqlite3.Connection) -> None:
        device_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(mobile_devices)").fetchall()
        }
        if device_columns and "account_generation" not in device_columns:
            conn.execute(
                "ALTER TABLE mobile_devices ADD COLUMN account_generation "
                "TEXT NOT NULL DEFAULT 'legacy'"
            )
            conn.execute(
                "UPDATE mobile_devices SET account_generation=COALESCE(("
                "SELECT NULLIF(s.account_generation,'') FROM mobile_sessions AS s "
                "WHERE s.device_id=mobile_devices.id ORDER BY s.created_at DESC LIMIT 1"
                "),'legacy')"
            )

        deletion_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(mobile_account_deletion_outbox)"
            ).fetchall()
        }
        if deletion_columns and "account_generation" not in deletion_columns:
            conn.execute("DROP INDEX IF EXISTS idx_mobile_account_deletion_due")
            conn.execute(
                "ALTER TABLE mobile_account_deletion_outbox "
                "RENAME TO mobile_account_deletion_outbox_v6"
            )
            conn.execute(
                """
                CREATE TABLE mobile_account_deletion_outbox (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    account_generation TEXT NOT NULL,
                    owner_scope TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    device_deliveries_json TEXT NOT NULL DEFAULT '{}',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at INTEGER NOT NULL,
                    lease_token TEXT NOT NULL DEFAULT '',
                    leased_until INTEGER NOT NULL DEFAULT 0,
                    requested_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(user_id, account_generation)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO mobile_account_deletion_outbox (
                    id, user_id, account_generation, owner_scope, state,
                    device_deliveries_json, attempts, available_at, lease_token,
                    leased_until, requested_at, updated_at, completed_at, last_error
                )
                SELECT old.id, old.user_id,
                       COALESCE((
                           SELECT NULLIF(g.generation, '')
                           FROM mobile_account_generations AS g
                           WHERE g.user_id=old.user_id COLLATE NOCASE
                       ), 'legacy'),
                       old.owner_scope, old.state, old.device_deliveries_json,
                       old.attempts, old.available_at, old.lease_token,
                       old.leased_until, old.requested_at, old.updated_at,
                       old.completed_at, old.last_error
                FROM mobile_account_deletion_outbox_v6 AS old
                """
            )
            conn.execute("DROP TABLE mobile_account_deletion_outbox_v6")

    @contextlib.contextmanager
    def connection(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _restrict_permissions(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError:
            pass

    def create_session(
        self,
        *,
        user_id: str,
        device: Optional[MobileDeviceInfo] = None,
    ) -> MobileTokenPair:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        now = self._clock()
        normalized = self._normalize_device(device)
        access_token = _new_access_token()
        refresh_token = _new_refresh_token()
        session_id = "ms_" + uuid.uuid4().hex
        access_expires_at = now + ACCESS_TTL_SECONDS
        refresh_expires_at = now + REFRESH_TTL_SECONDS
        account_generation = ""
        with self.connection() as conn, write_txn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO mobile_account_generations(user_id,generation,created_at) "
                "VALUES (?,?,?)",
                (normalized_user_id, "acctgen_" + uuid.uuid4().hex, now),
            )
            generation_row = conn.execute(
                "SELECT generation FROM mobile_account_generations "
                "WHERE user_id=? COLLATE NOCASE",
                (normalized_user_id,),
            ).fetchone()
            if generation_row is None:
                raise RuntimeError("account generation could not be persisted")
            account_generation = str(generation_row["generation"])
            deletion = conn.execute(
                "SELECT state FROM mobile_account_deletion_outbox "
                "WHERE user_id=? COLLATE NOCASE AND account_generation=?",
                (normalized_user_id, account_generation),
            ).fetchone()
            if deletion is not None:
                raise PermissionError("account deletion tombstone is active")
            existing = conn.execute(
                "SELECT id, user_id, account_generation FROM mobile_devices WHERE id=?",
                (normalized.id,),
            ).fetchone()
            if existing is not None:
                existing_user = str(existing["user_id"] or "")
                if existing_user and existing_user != normalized_user_id:
                    # Never rebind a device row across accounts — that would
                    # revoke the legitimate owner's sessions for the same id.
                    raise PermissionError(
                        "device_id is already bound to another account"
                    )
                if str(existing["account_generation"] or "") != account_generation:
                    # A stable native device identifier may be reused after an
                    # explicitly activated replacement account. Removing the
                    # old binding also prevents an old deletion push from
                    # targeting the newly registered account on that device.
                    conn.execute(
                        "DELETE FROM mobile_devices WHERE id=? AND user_id=?",
                        (normalized.id, normalized_user_id),
                    )
                    existing = None
            if existing is not None:
                conn.execute(
                    """
                    UPDATE mobile_devices
                    SET name=?, model=?, os_version=?, app_version=?,
                        updated_at=?, last_seen_at=?, revoked_at=NULL, revoke_reason=''
                    WHERE id=? AND user_id=? AND account_generation=?
                    """,
                    (
                        normalized.name,
                        normalized.model,
                        normalized.os_version,
                        normalized.app_version,
                        now,
                        now,
                        normalized.id,
                        normalized_user_id,
                        account_generation,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO mobile_devices (
                        id, user_id, account_generation, name, model, os_version, app_version,
                        created_at, updated_at, last_seen_at, revoked_at, revoke_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '')
                    """,
                    (
                        normalized.id,
                        normalized_user_id,
                        account_generation,
                        normalized.name,
                        normalized.model,
                        normalized.os_version,
                        normalized.app_version,
                        now,
                        now,
                        now,
                    ),
                )
            conn.execute(
                """
                UPDATE mobile_sessions
                SET revoked_at=?, revoke_reason='replaced_by_login', updated_at=?
                WHERE device_id=? AND user_id=? AND account_generation=?
                  AND revoked_at IS NULL
                """,
                (now, now, normalized.id, normalized_user_id, account_generation),
            )
            conn.execute(
                """
                INSERT INTO mobile_sessions (
                    id, device_id, user_id, account_generation, access_token_hash,
                    refresh_token_hash, access_expires_at, refresh_expires_at,
                    created_at, updated_at, last_seen_at, revoked_at, revoke_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '')
                """,
                (
                    session_id,
                    normalized.id,
                    normalized_user_id,
                    account_generation,
                    _token_hash(access_token),
                    _token_hash(refresh_token),
                    access_expires_at,
                    refresh_expires_at,
                    now,
                    now,
                    now,
                ),
            )
        record = MobileSessionRecord(
            session_id=session_id,
            device_id=normalized.id,
            user_id=normalized_user_id,
            account_generation=account_generation,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
        )
        return MobileTokenPair(access_token, refresh_token, record)

    def account_generation(self, user_id: str, *, create: bool = False) -> str:
        """Return the immutable random boundary for one active account."""

        normalized = str(user_id or "").strip()
        if not normalized:
            raise ValueError("user_id is required")
        if create:
            return self.activate_account_generation(normalized)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT generation FROM mobile_account_generations "
                "WHERE user_id=? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
        return str(row["generation"]) if row is not None else ""

    def activate_account_generation(
        self,
        user_id: str,
        *,
        replace_deleting: bool = False,
    ) -> str:
        """Return the active generation or explicitly create its replacement."""

        normalized = str(user_id or "").strip()
        if not normalized:
            raise ValueError("user_id is required")
        now = self._clock()
        with self.connection() as conn, write_txn(conn):
            row = conn.execute(
                "SELECT generation FROM mobile_account_generations "
                "WHERE user_id=? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
            current = str(row["generation"]) if row is not None else ""
            deletion = None
            if current:
                deletion = conn.execute(
                    "SELECT state FROM mobile_account_deletion_outbox "
                    "WHERE user_id=? COLLATE NOCASE AND account_generation=?",
                    (normalized, current),
                ).fetchone()
            if current and deletion is None:
                return current
            if deletion is not None and not replace_deleting:
                raise PermissionError("account deletion tombstone is active")

            generation = "acctgen_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO mobile_account_generations(user_id,generation,created_at) "
                "VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "generation=excluded.generation,created_at=excluded.created_at",
                (normalized, generation, now),
            )
        return generation

    def verify_access(
        self,
        token: str,
        *,
        touch: bool = True,
    ) -> Optional[MobileSessionRecord]:
        if not token or not token.startswith("hma_"):
            return None
        now = self._clock()
        token_hash = _token_hash(token)
        cached = self._access_cache_get(token_hash, now)
        if cached is not None:
            return cached
        record: Optional[MobileSessionRecord] = None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT s.*
                FROM mobile_sessions AS s
                JOIN mobile_devices AS d
                  ON d.id=s.device_id
                 AND d.account_generation=s.account_generation
                JOIN mobile_account_generations AS g
                  ON g.user_id=s.user_id COLLATE NOCASE
                 AND g.generation=s.account_generation
                WHERE s.access_token_hash=?
                  AND s.revoked_at IS NULL
                  AND d.revoked_at IS NULL
                  AND s.access_expires_at>?
                  AND NOT EXISTS (
                      SELECT 1 FROM mobile_account_deletion_outbox AS deletion
                      WHERE deletion.user_id=s.user_id COLLATE NOCASE
                        AND deletion.account_generation=s.account_generation
                  )
                """,
                (token_hash, now),
            ).fetchone()
            if row is None:
                self._access_cache_put(token_hash, None, now)
                return None
            if touch and int(row["last_seen_at"] or 0) <= now - 300:
                with write_txn(conn):
                    conn.execute(
                        "UPDATE mobile_sessions SET last_seen_at=?, updated_at=? WHERE id=?",
                        (now, now, row["id"]),
                    )
                    conn.execute(
                        "UPDATE mobile_devices SET last_seen_at=?, updated_at=? WHERE id=?",
                        (now, now, row["device_id"]),
                    )
            record = self._session_from_row(row)
        self._access_cache_put(token_hash, record, now)
        return record

    @classmethod
    def _access_cache_get(
        cls, token_hash: str, now: int
    ) -> Optional[MobileSessionRecord]:
        with cls._ACCESS_CACHE_LOCK:
            entry = cls._ACCESS_CACHE.get(token_hash)
        if entry is None or entry[0] <= now:
            return None
        return entry[1]

    @classmethod
    def _access_cache_put(
        cls,
        token_hash: str,
        record: Optional[MobileSessionRecord],
        now: int,
    ) -> None:
        expires_at = now + cls._ACCESS_CACHE_TTL_SECONDS
        with cls._ACCESS_CACHE_LOCK:
            # Keep the hot-path map small; normal deployments have a handful of
            # native devices, while invalid-token probes must not grow it.
            if len(cls._ACCESS_CACHE) >= 1024:
                cls._ACCESS_CACHE.clear()
            cls._ACCESS_CACHE[token_hash] = (expires_at, record)

    @classmethod
    def _invalidate_access_cache(cls, token_hashes: Sequence[str]) -> None:
        with cls._ACCESS_CACHE_LOCK:
            for token_hash in token_hashes:
                if token_hash:
                    cls._ACCESS_CACHE.pop(token_hash, None)

    @classmethod
    def _clear_access_cache(cls) -> None:
        with cls._ACCESS_CACHE_LOCK:
            cls._ACCESS_CACHE.clear()

    def rotate_refresh(self, refresh_token: str) -> Optional[MobileTokenPair]:
        if not refresh_token or not refresh_token.startswith("hmr_"):
            return None
        now = self._clock()
        next_access = _new_access_token()
        next_refresh = _new_refresh_token()
        old_hash = _token_hash(refresh_token)
        with self.connection() as conn, write_txn(conn):
            row = conn.execute(
                """
                SELECT s.*
                FROM mobile_sessions AS s
                JOIN mobile_devices AS d
                  ON d.id=s.device_id
                 AND d.account_generation=s.account_generation
                JOIN mobile_account_generations AS g
                  ON g.user_id=s.user_id COLLATE NOCASE
                 AND g.generation=s.account_generation
                WHERE s.refresh_token_hash=?
                  AND s.revoked_at IS NULL
                  AND d.revoked_at IS NULL
                  AND s.refresh_expires_at>?
                  AND NOT EXISTS (
                      SELECT 1 FROM mobile_account_deletion_outbox AS deletion
                      WHERE deletion.user_id=s.user_id COLLATE NOCASE
                        AND deletion.account_generation=s.account_generation
                  )
                """,
                (old_hash, now),
            ).fetchone()
            if row is None:
                replayed = conn.execute(
                    "SELECT session_id, rotated_at FROM mobile_refresh_history WHERE token_hash=?",
                    (old_hash,),
                ).fetchone()
                if replayed is not None:
                    rotated_at = int(replayed["rotated_at"] or 0)
                    if now - rotated_at <= _REFRESH_REPLAY_GRACE_SECONDS:
                        # Concurrent double-send: the winning rotation already
                        # replaced this token moments ago. Return None WITHOUT
                        # revoking — the winner's fresh pair stays valid and
                        # the client's next attempt uses it. Revoking here used
                        # to destroy the whole session and disable APNs on a
                        # single lost race.
                        return None
                    self._revoke_replayed_session(
                        conn,
                        str(replayed["session_id"]),
                        now,
                    )
                return None
            access_expires_at = now + ACCESS_TTL_SECONDS
            refresh_expires_at = now + REFRESH_TTL_SECONDS
            updated = conn.execute(
                """
                UPDATE mobile_sessions
                SET access_token_hash=?, refresh_token_hash=?,
                    access_expires_at=?, refresh_expires_at=?,
                    updated_at=?, last_seen_at=?
                WHERE id=? AND refresh_token_hash=? AND revoked_at IS NULL
                """,
                (
                    _token_hash(next_access),
                    _token_hash(next_refresh),
                    access_expires_at,
                    refresh_expires_at,
                    now,
                    now,
                    row["id"],
                    old_hash,
                ),
            )
            if updated.rowcount != 1:
                return None
            conn.execute(
                """
                INSERT OR IGNORE INTO mobile_refresh_history (
                    token_hash, session_id, rotated_at
                ) VALUES (?, ?, ?)
                """,
                (old_hash, row["id"], now),
            )
            conn.execute(
                "UPDATE mobile_devices SET last_seen_at=?, updated_at=? WHERE id=?",
                (now, now, row["device_id"]),
            )
            conn.execute(
                "DELETE FROM mobile_refresh_idempotency WHERE session_id=?",
                (row["id"],),
            )
            old_access_hash = str(row["access_token_hash"] or "")
            new_access_hash = _token_hash(next_access)
            record = MobileSessionRecord(
                session_id=str(row["id"]),
                device_id=str(row["device_id"]),
                user_id=str(row["user_id"]),
                account_generation=str(row["account_generation"]),
                access_expires_at=access_expires_at,
                refresh_expires_at=refresh_expires_at,
            )
            pair = MobileTokenPair(next_access, next_refresh, record)
        self._invalidate_access_cache([old_hash, old_access_hash, new_access_hash])
        return pair

    @staticmethod
    def _revoke_replayed_session(
        conn: sqlite3.Connection,
        session_id: str,
        now: int,
    ) -> None:
        row = conn.execute(
            "SELECT device_id FROM mobile_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return
        conn.execute(
            """
            UPDATE mobile_sessions
            SET revoked_at=COALESCE(revoked_at, ?),
                revoke_reason=CASE
                    WHEN revoked_at IS NULL THEN 'refresh_token_replay'
                    ELSE revoke_reason
                END,
                updated_at=?
            WHERE id=?
            """,
            (now, now, session_id),
        )
        conn.execute(
            "DELETE FROM mobile_refresh_idempotency WHERE session_id=?",
            (session_id,),
        )
        conn.execute(
            """
            UPDATE mobile_apns_tokens
            SET disabled_at=COALESCE(disabled_at, ?), updated_at=?,
                last_error='refresh_token_replay'
            WHERE device_id=?
            """,
            (now, now, row["device_id"]),
        )

    def revoke_session(
        self,
        *,
        access_token: str = "",
        refresh_token: str = "",
        reason: str = "logout",
    ) -> bool:
        predicates: list[str] = []
        predicate_values: list[Any] = []
        if access_token:
            predicates.append("access_token_hash=?")
            predicate_values.append(_token_hash(access_token))
        if refresh_token:
            predicates.append("refresh_token_hash=?")
            predicate_values.append(_token_hash(refresh_token))
        if not predicates:
            return False
        now = self._clock()
        revoked_token_hashes = [value for value in predicate_values]
        with self.connection() as conn, write_txn(conn):
            device_rows = conn.execute(
                f"SELECT DISTINCT device_id FROM mobile_sessions WHERE {' OR '.join(predicates)}",
                tuple(predicate_values),
            ).fetchall()
            result = conn.execute(
                f"""
                UPDATE mobile_sessions
                SET revoked_at=?, revoke_reason=?, updated_at=?
                WHERE ({' OR '.join(predicates)}) AND revoked_at IS NULL
                """,
                (now, _bounded(reason, 120), now, *predicate_values),
            )
            for row in device_rows:
                device_id = str(row["device_id"])
                active = conn.execute(
                    """
                    SELECT 1 FROM mobile_sessions
                    WHERE device_id=? AND revoked_at IS NULL AND refresh_expires_at>?
                    LIMIT 1
                    """,
                    (device_id, now),
                ).fetchone()
                if active is None:
                    conn.execute(
                        """
                        UPDATE mobile_apns_tokens
                        SET disabled_at=?, updated_at=?
                        WHERE device_id=? AND disabled_at IS NULL
                        """,
                        (now, now, device_id),
                    )
        self._invalidate_access_cache(revoked_token_hashes)
        return result.rowcount > 0

    def list_devices(
        self,
        *,
        user_id: str = "",
        current_device_id: str = "",
    ) -> list[dict[str, Any]]:
        now = self._clock()
        normalized_user_id = str(user_id or "").strip()
        with self.connection() as conn:
            if normalized_user_id:
                device_rows = conn.execute(
                    """
                    SELECT d.*,
                           COUNT(CASE WHEN s.revoked_at IS NULL
                                          AND s.refresh_expires_at>? THEN 1 END) AS active_sessions
                    FROM mobile_devices AS d
                    LEFT JOIN mobile_sessions AS s
                      ON s.device_id=d.id
                     AND s.account_generation=d.account_generation
                    JOIN mobile_account_generations AS g
                      ON g.user_id=d.user_id COLLATE NOCASE
                     AND g.generation=d.account_generation
                    WHERE d.user_id=?
                    GROUP BY d.id
                    ORDER BY d.last_seen_at DESC, d.created_at DESC
                    """,
                    (now, normalized_user_id),
                ).fetchall()
                push_rows = conn.execute(
                    """
                    SELECT t.id, t.device_id, t.environment, t.bundle_id, t.token, t.updated_at
                    FROM mobile_apns_tokens AS t
                    INNER JOIN mobile_devices AS d ON d.id=t.device_id
                    JOIN mobile_account_generations AS g
                      ON g.user_id=d.user_id COLLATE NOCASE
                     AND g.generation=d.account_generation
                    WHERE t.disabled_at IS NULL AND d.user_id=?
                    ORDER BY t.updated_at DESC
                    """,
                    (normalized_user_id,),
                ).fetchall()
            else:
                # Unscoped listing is retained only for internal admin tooling;
                # owner-facing routes must pass user_id.
                device_rows = conn.execute(
                    """
                    SELECT d.*,
                           COUNT(CASE WHEN s.revoked_at IS NULL
                                          AND s.refresh_expires_at>? THEN 1 END) AS active_sessions
                    FROM mobile_devices AS d
                    LEFT JOIN mobile_sessions AS s
                      ON s.device_id=d.id
                     AND s.account_generation=d.account_generation
                    GROUP BY d.id
                    ORDER BY d.last_seen_at DESC, d.created_at DESC
                    """,
                    (now,),
                ).fetchall()
                push_rows = conn.execute(
                    """
                    SELECT id, device_id, environment, bundle_id, token, updated_at
                    FROM mobile_apns_tokens
                    WHERE disabled_at IS NULL
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        pushes: dict[str, list[dict[str, Any]]] = {}
        for row in push_rows:
            token = str(row["token"])
            pushes.setdefault(str(row["device_id"]), []).append(
                {
                    "id": row["id"],
                    "environment": row["environment"],
                    "bundle_id": row["bundle_id"],
                    "token_suffix": token[-8:],
                    "updated_at": row["updated_at"],
                }
            )
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "model": row["model"],
                "os_version": row["os_version"],
                "app_version": row["app_version"],
                "created_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "revoked_at": row["revoked_at"],
                "active": row["revoked_at"] is None and int(row["active_sessions"] or 0) > 0,
                "current": str(row["id"]) == current_device_id,
                "apns": pushes.get(str(row["id"]), []),
            }
            for row in device_rows
        ]

    def revoke_device(
        self,
        device_id: str,
        *,
        user_id: str = "",
        reason: str = "device_revoked",
    ) -> bool:
        now = self._clock()
        normalized_user_id = str(user_id or "").strip()
        with self.connection() as conn, write_txn(conn):
            if normalized_user_id:
                device = conn.execute(
                    "SELECT id FROM mobile_devices WHERE id=? AND user_id=?",
                    (device_id, normalized_user_id),
                ).fetchone()
            else:
                device = conn.execute(
                    "SELECT id FROM mobile_devices WHERE id=?",
                    (device_id,),
                ).fetchone()
            if device is None:
                return False
            if normalized_user_id:
                conn.execute(
                    """
                    UPDATE mobile_devices
                    SET revoked_at=?, revoke_reason=?, updated_at=?
                    WHERE id=? AND user_id=?
                    """,
                    (now, _bounded(reason, 120), now, device_id, normalized_user_id),
                )
                conn.execute(
                    """
                    UPDATE mobile_sessions
                    SET revoked_at=COALESCE(revoked_at, ?),
                        revoke_reason=CASE WHEN revoked_at IS NULL THEN ? ELSE revoke_reason END,
                        updated_at=?
                    WHERE device_id=? AND user_id=?
                    """,
                    (now, _bounded(reason, 120), now, device_id, normalized_user_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE mobile_devices
                    SET revoked_at=?, revoke_reason=?, updated_at=?
                    WHERE id=?
                    """,
                    (now, _bounded(reason, 120), now, device_id),
                )
                conn.execute(
                    """
                    UPDATE mobile_sessions
                    SET revoked_at=COALESCE(revoked_at, ?),
                        revoke_reason=CASE WHEN revoked_at IS NULL THEN ? ELSE revoke_reason END,
                        updated_at=?
                    WHERE device_id=?
                    """,
                    (now, _bounded(reason, 120), now, device_id),
                )
            conn.execute(
                "UPDATE mobile_apns_tokens SET disabled_at=?, updated_at=? WHERE device_id=? AND disabled_at IS NULL",
                (now, now, device_id),
            )
        # ``revoke_device`` revokes every live session on the device, so the
        # access-token cache (1s TTL) cannot safely hand out a stale "valid"
        # result. Clear it: clearing is a small fixed cost, and the test
        # contract — the next request after revoke MUST 401 — is a stronger
        # correctness invariant than the cache's hot-path micro-optimization.
        self._ACCESS_CACHE.clear()
        return True

    def register_apns(
        self,
        *,
        device_id: str,
        token: str,
        environment: str,
        bundle_id: str,
    ) -> dict[str, Any]:
        normalized_token = self.normalize_apns_token(token)
        normalized_environment = environment.strip().lower()
        if normalized_environment not in {"sandbox", "production"}:
            raise ValueError("APNs environment must be sandbox or production")
        normalized_bundle = _bounded(bundle_id, 255)
        if (
            len(normalized_bundle) < 3
            or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for ch in normalized_bundle)
        ):
            raise ValueError("Invalid APNs bundle id")
        now = self._clock()
        registration_id = "apns_" + uuid.uuid4().hex
        with self.connection() as conn, write_txn(conn):
            device = conn.execute(
                "SELECT id FROM mobile_devices WHERE id=? AND revoked_at IS NULL",
                (device_id,),
            ).fetchone()
            if device is None:
                raise KeyError(device_id)
            conn.execute(
                "DELETE FROM mobile_apns_tokens WHERE token_hash=? AND device_id<>?",
                (_token_hash(normalized_token), device_id),
            )
            conn.execute(
                """
                INSERT INTO mobile_apns_tokens (
                    id, device_id, token, token_hash, environment, bundle_id,
                    created_at, updated_at, disabled_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, '')
                ON CONFLICT(device_id, environment, bundle_id) DO UPDATE SET
                    token=excluded.token,
                    token_hash=excluded.token_hash,
                    updated_at=excluded.updated_at,
                    disabled_at=NULL,
                    last_error=''
                """,
                (
                    registration_id,
                    device_id,
                    normalized_token,
                    _token_hash(normalized_token),
                    normalized_environment,
                    normalized_bundle,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id, environment, bundle_id, token, updated_at
                FROM mobile_apns_tokens
                WHERE device_id=? AND environment=? AND bundle_id=?
                """,
                (device_id, normalized_environment, normalized_bundle),
            ).fetchone()
        return {
            "id": row["id"],
            "environment": row["environment"],
            "bundle_id": row["bundle_id"],
            "token_suffix": str(row["token"])[-8:],
            "updated_at": row["updated_at"],
        }

    def unregister_apns(
        self,
        *,
        device_id: str,
        environment: str = "",
        bundle_id: str = "",
    ) -> int:
        clauses = ["device_id=?", "disabled_at IS NULL"]
        predicate_values: list[Any] = [device_id]
        if environment:
            clauses.append("environment=?")
            predicate_values.append(environment.strip().lower())
        if bundle_id:
            clauses.append("bundle_id=?")
            predicate_values.append(bundle_id.strip())
        now = self._clock()
        with self.connection() as conn, write_txn(conn):
            result = conn.execute(
                f"UPDATE mobile_apns_tokens SET disabled_at=?, updated_at=? WHERE {' AND '.join(clauses)}",
                (now, now, *predicate_values),
            )
        return result.rowcount

    def disable_apns_registration(
        self,
        *,
        registration_id: str,
        error: str = "",
    ) -> bool:
        registration = str(registration_id or "").strip()
        if not registration:
            return False
        now = self._clock()
        with self.connection() as conn, write_txn(conn):
            result = conn.execute(
                """
                UPDATE mobile_apns_tokens
                SET disabled_at=?, updated_at=?, last_error=?
                WHERE id=? AND disabled_at IS NULL
                """,
                (now, now, _bounded(error, 240), registration),
            )
        return result.rowcount > 0

    def list_active_apns_registrations(
        self,
        *,
        user_id: str,
        environment: str = "",
    ) -> list[dict[str, Any]]:
        """Return internal delivery records; public APIs never expose tokens."""
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return []
        now = self._clock()
        clauses = [
            "p.disabled_at IS NULL",
            "d.revoked_at IS NULL",
            "d.user_id=?",
            "EXISTS (SELECT 1 FROM mobile_account_generations AS g "
            "WHERE g.user_id=d.user_id COLLATE NOCASE "
            "AND g.generation=d.account_generation)",
            "EXISTS (SELECT 1 FROM mobile_sessions AS s "
            "WHERE s.device_id=d.id "
            "AND s.account_generation=d.account_generation "
            "AND s.revoked_at IS NULL "
            "AND s.refresh_expires_at>?)",
        ]
        values: list[Any] = [normalized_user_id, now]
        if environment:
            clauses.append("p.environment=?")
            values.append(environment.strip().lower())
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT p.id, p.device_id, p.token, p.environment, p.bundle_id,
                       d.user_id, d.account_generation,
                       p.created_at, p.updated_at
                FROM mobile_apns_tokens AS p
                JOIN mobile_devices AS d ON d.id=p.device_id
                WHERE {' AND '.join(clauses)}
                ORDER BY p.updated_at DESC
                """,
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_account_deletion_apns_registrations(
        self,
        *,
        user_id: str,
        account_generation: str = "",
        environment: str = "",
    ) -> list[dict[str, Any]]:
        """Return retained APNs rows after account sessions are revoked."""

        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return []
        generation = str(account_generation or "").strip()
        clauses = ["p.disabled_at IS NULL", "d.user_id=?"]
        values: list[Any] = [normalized_user_id]
        if generation:
            clauses.append("d.account_generation=?")
            values.append(generation)
        if environment:
            clauses.append("p.environment=?")
            values.append(environment.strip().lower())
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT p.id, p.device_id, p.token, p.environment, p.bundle_id,
                       d.user_id, d.account_generation,
                       p.created_at, p.updated_at
                FROM mobile_apns_tokens AS p
                JOIN mobile_devices AS d ON d.id=p.device_id
                WHERE {' AND '.join(clauses)}
                ORDER BY p.updated_at DESC
                """,
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]

    def begin_account_deletion(
        self,
        user_id: str,
        owner_scope: str,
        account_generation: str = "",
    ) -> dict[str, Any]:
        """Revoke access immediately and persist APNs cleanup until terminal."""

        normalized_user_id = str(user_id or "").strip()
        normalized_scope = str(owner_scope or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        if not normalized_scope:
            raise ValueError("owner_scope is required")
        now = self._clock()
        identifier = "account_delete_" + uuid.uuid4().hex
        with self.connection() as conn, write_txn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO mobile_account_generations(user_id,generation,created_at) "
                "VALUES (?,?,?)",
                (normalized_user_id, "acctgen_" + uuid.uuid4().hex, now),
            )
            generation_row = conn.execute(
                "SELECT generation FROM mobile_account_generations "
                "WHERE user_id=? COLLATE NOCASE",
                (normalized_user_id,),
            ).fetchone()
            if generation_row is None:
                raise RuntimeError("account generation could not be persisted")
            active_generation = str(generation_row["generation"])
            requested_generation = str(account_generation or "").strip()
            if requested_generation and requested_generation != active_generation:
                raise PermissionError("account generation is no longer active")
            account_generation = active_generation
            device_rows = conn.execute(
                "SELECT id FROM mobile_devices "
                "WHERE user_id=? AND account_generation=?",
                (normalized_user_id, account_generation),
            ).fetchall()
            device_ids = [str(row["id"]) for row in device_rows]
            sessions = 0
            apns = 0
            if device_ids:
                placeholders = ",".join("?" for _ in device_ids)
                sessions = int(conn.execute(
                    f"SELECT COUNT(*) FROM mobile_sessions WHERE device_id IN ({placeholders})",
                    tuple(device_ids),
                ).fetchone()[0])
                apns = int(conn.execute(
                    f"SELECT COUNT(*) FROM mobile_apns_tokens WHERE device_id IN ({placeholders})",
                    tuple(device_ids),
                ).fetchone()[0])
                conn.execute(
                    f"UPDATE mobile_sessions SET revoked_at=COALESCE(revoked_at,?),"
                    f"revoke_reason=CASE WHEN revoked_at IS NULL THEN 'account_deleted' ELSE revoke_reason END,"
                    f"updated_at=? WHERE device_id IN ({placeholders})",
                    (now, now, *device_ids),
                )
                conn.execute(
                    f"UPDATE mobile_devices SET revoked_at=COALESCE(revoked_at,?),"
                    f"revoke_reason=CASE WHEN revoked_at IS NULL THEN 'account_deleted' ELSE revoke_reason END,"
                    f"updated_at=? WHERE id IN ({placeholders})",
                    (now, now, *device_ids),
                )
            conn.execute(
                "INSERT INTO mobile_account_deletion_outbox("
                "id,user_id,account_generation,owner_scope,state,device_deliveries_json,attempts,"
                "available_at,lease_token,leased_until,requested_at,updated_at,completed_at,last_error"
                ") VALUES(?,?,?,?,'pending','{}',0,?,'',0,?,?,NULL,'') "
                "ON CONFLICT(user_id,account_generation) DO UPDATE SET "
                "owner_scope=excluded.owner_scope,state='pending',available_at=excluded.available_at,"
                "lease_token='',leased_until=0,updated_at=excluded.updated_at,completed_at=NULL,last_error=''",
                (
                    identifier,
                    normalized_user_id,
                    account_generation,
                    normalized_scope,
                    now,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id,state FROM mobile_account_deletion_outbox "
                "WHERE user_id=? AND account_generation=?",
                (normalized_user_id, account_generation),
            ).fetchone()
        # Account deletion is a security boundary; do not let a one-second
        # positive cache admit a request after its generation is fenced.
        self._clear_access_cache()
        return {
            "id": str(row["id"]),
            "state": str(row["state"]),
            "devices": len(device_ids),
            "sessions": sessions,
            "apns": apns,
            "account_generation": account_generation,
        }

    def claim_account_deletions(
        self,
        *,
        limit: int = 100,
        lease_seconds: int = ACCOUNT_DELETION_LEASE_SECONDS,
        user_id: str = "",
        exclude_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        now = self._clock()
        lease_until = now + max(15, min(int(lease_seconds), 3600))
        clauses = [
            "available_at<=?",
            "(state IN ('pending','retry') OR (state='delivering' AND leased_until<=?))",
        ]
        values: list[Any] = [now, now]
        if str(user_id or "").strip():
            clauses.append("user_id=?")
            values.append(str(user_id).strip())
        excluded = [str(item) for item in (exclude_ids or ()) if str(item)]
        if excluded:
            clauses.append(f"id NOT IN ({','.join('?' for _ in excluded)})")
            values.extend(excluded)
        values.append(max(1, min(int(limit), 1000)))
        with self.connection() as conn, write_txn(conn):
            rows = conn.execute(
                "SELECT * FROM mobile_account_deletion_outbox "
                f"WHERE {' AND '.join(clauses)} ORDER BY requested_at LIMIT ?",
                tuple(values),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                lease_token = uuid.uuid4().hex
                changed = conn.execute(
                    "UPDATE mobile_account_deletion_outbox SET "
                    "state='delivering',attempts=attempts+1,lease_token=?,leased_until=?,updated_at=? "
                    "WHERE id=? AND (state IN ('pending','retry') OR (state='delivering' AND leased_until<=?))",
                    (lease_token, lease_until, now, row["id"], now),
                ).rowcount
                if not changed:
                    continue
                item = dict(row)
                item["state"] = "delivering"
                item["attempts"] = int(row["attempts"]) + 1
                item["lease_token"] = lease_token
                try:
                    deliveries = json.loads(str(row["device_deliveries_json"] or "{}"))
                except (TypeError, ValueError):
                    deliveries = {}
                item["device_deliveries"] = deliveries if isinstance(deliveries, dict) else {}
                claimed.append(item)
        return claimed

    def update_account_deletion_progress(
        self,
        deletion_id: str,
        deliveries: dict[str, dict[str, Any]],
        *,
        lease_token: str,
        lease_seconds: int = ACCOUNT_DELETION_LEASE_SECONDS,
    ) -> bool:
        with self.connection() as conn, write_txn(conn):
            now = self._clock()
            lease_until = now + max(15, min(int(lease_seconds), 3600))
            changed = conn.execute(
                "UPDATE mobile_account_deletion_outbox SET "
                "device_deliveries_json=?,leased_until=?,updated_at=? "
                "WHERE id=? AND state='delivering' AND lease_token=? AND leased_until>?",
                (
                    json.dumps(deliveries, ensure_ascii=False, separators=(",", ":")),
                    lease_until,
                    now,
                    str(deletion_id),
                    str(lease_token),
                    now,
                ),
            ).rowcount
        return bool(changed)

    def finish_account_deletion(
        self,
        deletion_id: str,
        state: str,
        *,
        deliveries: dict[str, dict[str, Any]],
        lease_token: str,
        error: str = "",
        retry_seconds: int = 60,
    ) -> dict[str, Any]:
        normalized_state = str(state or "retry").strip().lower()
        terminal = normalized_state in {"delivered", "no_recipients", "permanent_failure"}
        if not terminal:
            normalized_state = "retry"
        removed = {"devices": 0, "sessions": 0, "apns": 0}
        with self.connection() as conn, write_txn(conn):
            now = self._clock()
            row = conn.execute(
                "SELECT user_id,account_generation FROM mobile_account_deletion_outbox "
                "WHERE id=? AND state='delivering' AND lease_token=? AND leased_until>?",
                (str(deletion_id), str(lease_token), now),
            ).fetchone()
            if row is None:
                return {"updated": False, "state": normalized_state, **removed}
            user_id = str(row["user_id"])
            account_generation = str(row["account_generation"])
            if terminal:
                device_rows = conn.execute(
                    "SELECT id FROM mobile_devices "
                    "WHERE user_id=? AND account_generation=?",
                    (user_id, account_generation),
                ).fetchall()
                device_ids = [str(item["id"]) for item in device_rows]
                if device_ids:
                    placeholders = ",".join("?" for _ in device_ids)
                    removed["sessions"] = int(conn.execute(
                        f"SELECT COUNT(*) FROM mobile_sessions WHERE device_id IN ({placeholders})",
                        tuple(device_ids),
                    ).fetchone()[0])
                    removed["apns"] = int(conn.execute(
                        f"SELECT COUNT(*) FROM mobile_apns_tokens WHERE device_id IN ({placeholders})",
                        tuple(device_ids),
                    ).fetchone()[0])
                    conn.execute(
                        f"DELETE FROM mobile_devices WHERE id IN ({placeholders})",
                        tuple(device_ids),
                    )
                    removed["devices"] = len(device_ids)
            conn.execute(
                "UPDATE mobile_account_deletion_outbox SET state=?,device_deliveries_json=?,"
                "available_at=?,lease_token='',leased_until=0,updated_at=?,completed_at=?,last_error=? "
                "WHERE id=? AND state='delivering' AND lease_token=? AND leased_until>?",
                (
                    normalized_state,
                    json.dumps(deliveries, ensure_ascii=False, separators=(",", ":")),
                    now if terminal else now + max(5, min(int(retry_seconds), 86400)),
                    now,
                    now if terminal else None,
                    _bounded(error, 240),
                    str(deletion_id),
                    str(lease_token),
                    now,
                ),
            )
        return {"updated": True, "state": normalized_state, **removed}

    def account_deletion_status(
        self,
        user_id: str,
        account_generation: str = "",
        *,
        include_historical: bool = False,
    ) -> dict[str, Any] | None:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return None
        generation = str(account_generation or "").strip()
        with self.connection() as conn:
            if not generation and not include_historical:
                active = conn.execute(
                    "SELECT generation FROM mobile_account_generations "
                    "WHERE user_id=? COLLATE NOCASE",
                    (normalized_user_id,),
                ).fetchone()
                generation = str(active["generation"]) if active is not None else ""
            if generation:
                row = conn.execute(
                    "SELECT * FROM mobile_account_deletion_outbox "
                    "WHERE user_id=? COLLATE NOCASE AND account_generation=?",
                    (normalized_user_id, generation),
                ).fetchone()
            elif include_historical:
                row = conn.execute(
                    "SELECT * FROM mobile_account_deletion_outbox "
                    "WHERE user_id=? COLLATE NOCASE "
                    "ORDER BY requested_at DESC LIMIT 1",
                    (normalized_user_id,),
                ).fetchone()
            else:
                row = None
        if row is None:
            return None
        result = dict(row)
        result.pop("device_deliveries_json", None)
        return result

    def clear_completed_account_deletion(self, user_id: str) -> bool:
        """Activate a replacement generation while retaining the old tombstone."""

        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return False
        status = self.account_deletion_status(normalized_user_id)
        if status is None or str(status.get("state")) not in {
            "delivered",
            "no_recipients",
            "permanent_failure",
        }:
            return False
        self.activate_account_generation(
            normalized_user_id,
            replace_deleting=True,
        )
        return True

    def delete_user(self, user_id: str) -> dict[str, int]:
        """Remove a user's device, session, refresh-history and APNs rows."""

        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        with self.connection() as conn, write_txn(conn):
            device_rows = conn.execute(
                "SELECT id FROM mobile_devices WHERE user_id=?",
                (normalized_user_id,),
            ).fetchall()
            device_ids = [str(row["id"]) for row in device_rows]
            if not device_ids:
                return {"devices": 0, "sessions": 0, "apns": 0}
            placeholders = ",".join("?" for _ in device_ids)
            sessions = conn.execute(
                f"SELECT COUNT(*) FROM mobile_sessions WHERE device_id IN ({placeholders})",
                tuple(device_ids),
            ).fetchone()[0]
            apns = conn.execute(
                f"SELECT COUNT(*) FROM mobile_apns_tokens WHERE device_id IN ({placeholders})",
                tuple(device_ids),
            ).fetchone()[0]
            conn.execute(
                f"DELETE FROM mobile_devices WHERE id IN ({placeholders})",
                tuple(device_ids),
            )
        return {"devices": len(device_ids), "sessions": int(sessions), "apns": int(apns)}

    delete_account = delete_user

    @staticmethod
    def normalize_apns_token(token: str) -> str:
        value = str(token or "").strip()
        if value.startswith("<") and value.endswith(">"):
            value = value[1:-1]
        value = "".join(value.split()).lower()
        if not (32 <= len(value) <= 256 and len(value) % 2 == 0):
            raise ValueError("Invalid APNs device token")
        if any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("Invalid APNs device token")
        return value

    def _normalize_device(self, device: Optional[MobileDeviceInfo]) -> MobileDeviceInfo:
        source = device or MobileDeviceInfo()
        device_id = _bounded(source.id, 128)
        if not device_id:
            device_id = "device_" + uuid.uuid4().hex
        if (
            len(device_id) < 8
            or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for ch in device_id)
        ):
            raise ValueError("Invalid device id")
        return MobileDeviceInfo(
            id=device_id,
            name=_bounded(source.name, 120) or "Hermes device",
            model=_bounded(source.model, 120),
            os_version=_bounded(source.os_version, 120),
            app_version=_bounded(source.app_version, 64),
        )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> MobileSessionRecord:
        return MobileSessionRecord(
            session_id=str(row["id"]),
            device_id=str(row["device_id"]),
            user_id=str(row["user_id"]),
            account_generation=str(row["account_generation"]),
            access_expires_at=int(row["access_expires_at"]),
            refresh_expires_at=int(row["refresh_expires_at"]),
        )


class OwnerMobileTokenProvider(DashboardAuthProvider):
    """Token-only provider backed by :class:`MobileDeviceStore`.

    The provider participates in the bearer-verify path: the iOS native app
    issues access tokens via ``/auth/mobile/token``, and the dashboard
    middleware must honor ``revoked_at`` to honor cross-device revocations
    (test contract: ``test_http_device_revoke_is_isolated_and_apns_is_current_device_only``).
    ``supports_session = True`` is what the bearer-verify loop looks for via
    ``list_session_providers()``; the provider does not issue browser cookie
    sessions, but the field is read here for token-bearer recognition, not
    for cookie issuance.
    """

    name = "owner-mobile"
    display_name = "Hermes mobile device"
    supports_session = True
    supports_token = True

    def __init__(
        self,
        store_factory: Callable[[], MobileDeviceStore] = MobileDeviceStore,
    ) -> None:
        self._store_factory = store_factory

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        store = self._store_factory()
        record = store.verify_access(token)
        if record is None:
            return None
        if not record.account_generation:
            return None
        return TokenPrincipal(
            principal=record.user_id,
            provider=self.name,
            scopes=("dashboard:admin",),
            account_generation=record.account_generation,
        )

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError("OwnerMobileTokenProvider is token-only")

    def complete_login(
        self,
        *,
        code: str,
        state: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> Session:
        raise NotImplementedError("OwnerMobileTokenProvider is token-only")

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        # ``OwnerMobileTokenProvider`` is a token-only provider: the middleware
        # calls ``verify_session`` with the bearer access token and expects a
        # fully-populated :class:`Session` back. The previous stub returned
        # ``None`` unconditionally, which left revoked devices' tokens valid
        # for the life of the access token — a real iOS-side bug that broke
        # the cross-device-revoke test contract. Build the Session from the
        # device store record, which already enforces ``revoked_at IS NULL``
        # in the SQL filter.
        record = self._store_factory().verify_access(access_token)
        if record is None:
            return None
        return Session(
            user_id=record.user_id,
            email="",
            display_name=record.user_id,
            org_id="",
            provider=self.name,
            expires_at=record.access_expires_at,
            access_token=access_token,
            # The mobile device store does not round-trip the refresh token
            # through ``verify_access`` (it only stores the hash), so the
            # Session carries an empty string. The middleware does not
            # consume this field on the bearer path; the native client owns
            # its own refresh token and rotates it through
            # ``/auth/native/refresh``.
            refresh_token="",
        )

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError("Use the native refresh endpoint")

    def revoke_session(self, *, refresh_token: str) -> None:
        self._store_factory().revoke_session(refresh_token=refresh_token)
