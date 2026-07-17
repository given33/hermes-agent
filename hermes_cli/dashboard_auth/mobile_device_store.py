"""Durable, revocable mobile device sessions and APNs registrations.

The native app keeps only opaque credentials in Keychain.  The server is the
source of truth for device sessions, token rotation, revocation, and APNs
registrations.  Business data remains in the existing ``HERMES_HOME`` stores;
this database contains authentication and delivery metadata only.
"""
from __future__ import annotations

import contextlib
import hashlib
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli.dashboard_auth.base import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
)
from hermes_cli.sqlite_util import write_txn
from hermes_constants import get_hermes_home


ACCESS_TTL_SECONDS = 15 * 60
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60
SCHEMA_VERSION = 2

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mobile_devices (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
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
"""


def mobile_auth_db_path() -> Path:
    return get_hermes_home() / "dashboard" / "mobile-auth.db"


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
    access_expires_at: int
    refresh_expires_at: int


@dataclass(frozen=True)
class MobileTokenPair:
    access_token: str
    refresh_token: str
    session: MobileSessionRecord


class MobileDeviceStore:
    """Small per-HERMES_HOME SQLite store with one connection per operation."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        clock: Callable[[], int] = _now,
    ) -> None:
        self.db_path = db_path if db_path is not None else mobile_auth_db_path()
        self._clock = clock

    def connect(self) -> sqlite3.Connection:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._restrict_permissions(path.parent, 0o700)
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            from hermes_state import apply_wal_with_fallback

            apply_wal_with_fallback(conn, db_label="mobile-auth.db")
            current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    "mobile-auth.db was created by a newer Hermes version "
                    f"(schema {current_version} > {SCHEMA_VERSION})"
                )
            conn.executescript(_SCHEMA_SQL)
            if current_version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
            self._restrict_permissions(path, 0o600)
            return conn
        except Exception:
            conn.close()
            raise

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
        now = self._clock()
        normalized = self._normalize_device(device)
        access_token = _new_access_token()
        refresh_token = _new_refresh_token()
        session_id = "ms_" + uuid.uuid4().hex
        record = MobileSessionRecord(
            session_id=session_id,
            device_id=normalized.id,
            user_id=user_id,
            access_expires_at=now + ACCESS_TTL_SECONDS,
            refresh_expires_at=now + REFRESH_TTL_SECONDS,
        )
        with self.connection() as conn, write_txn(conn):
            conn.execute(
                """
                INSERT INTO mobile_devices (
                    id, user_id, name, model, os_version, app_version,
                    created_at, updated_at, last_seen_at, revoked_at, revoke_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '')
                ON CONFLICT(id) DO UPDATE SET
                    user_id=excluded.user_id,
                    name=excluded.name,
                    model=excluded.model,
                    os_version=excluded.os_version,
                    app_version=excluded.app_version,
                    updated_at=excluded.updated_at,
                    last_seen_at=excluded.last_seen_at,
                    revoked_at=NULL,
                    revoke_reason=''
                """,
                (
                    normalized.id,
                    user_id,
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
                WHERE device_id=? AND revoked_at IS NULL
                """,
                (now, now, normalized.id),
            )
            conn.execute(
                """
                INSERT INTO mobile_sessions (
                    id, device_id, user_id, access_token_hash,
                    refresh_token_hash, access_expires_at, refresh_expires_at,
                    created_at, updated_at, last_seen_at, revoked_at, revoke_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '')
                """,
                (
                    session_id,
                    normalized.id,
                    user_id,
                    _token_hash(access_token),
                    _token_hash(refresh_token),
                    record.access_expires_at,
                    record.refresh_expires_at,
                    now,
                    now,
                    now,
                ),
            )
        return MobileTokenPair(access_token, refresh_token, record)

    def verify_access(
        self,
        token: str,
        *,
        touch: bool = True,
    ) -> Optional[MobileSessionRecord]:
        if not token or not token.startswith("hma_"):
            return None
        now = self._clock()
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT s.*
                FROM mobile_sessions AS s
                JOIN mobile_devices AS d ON d.id=s.device_id
                WHERE s.access_token_hash=?
                  AND s.revoked_at IS NULL
                  AND d.revoked_at IS NULL
                  AND s.access_expires_at>?
                """,
                (_token_hash(token), now),
            ).fetchone()
            if row is None:
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
            return self._session_from_row(row)

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
                JOIN mobile_devices AS d ON d.id=s.device_id
                WHERE s.refresh_token_hash=?
                  AND s.revoked_at IS NULL
                  AND d.revoked_at IS NULL
                  AND s.refresh_expires_at>?
                """,
                (old_hash, now),
            ).fetchone()
            if row is None:
                replayed = conn.execute(
                    "SELECT session_id FROM mobile_refresh_history WHERE token_hash=?",
                    (old_hash,),
                ).fetchone()
                if replayed is not None:
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
            record = MobileSessionRecord(
                session_id=str(row["id"]),
                device_id=str(row["device_id"]),
                user_id=str(row["user_id"]),
                access_expires_at=access_expires_at,
                refresh_expires_at=refresh_expires_at,
            )
        return MobileTokenPair(next_access, next_refresh, record)

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
        return result.rowcount > 0

    def list_devices(self, *, current_device_id: str = "") -> list[dict[str, Any]]:
        now = self._clock()
        with self.connection() as conn:
            device_rows = conn.execute(
                """
                SELECT d.*,
                       COUNT(CASE WHEN s.revoked_at IS NULL
                                      AND s.refresh_expires_at>? THEN 1 END) AS active_sessions
                FROM mobile_devices AS d
                LEFT JOIN mobile_sessions AS s ON s.device_id=d.id
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

    def revoke_device(self, device_id: str, *, reason: str = "device_revoked") -> bool:
        now = self._clock()
        with self.connection() as conn, write_txn(conn):
            device = conn.execute(
                "SELECT id FROM mobile_devices WHERE id=?",
                (device_id,),
            ).fetchone()
            if device is None:
                return False
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
            "EXISTS (SELECT 1 FROM mobile_sessions AS s "
            "WHERE s.device_id=d.id AND s.revoked_at IS NULL "
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
                       p.created_at, p.updated_at
                FROM mobile_apns_tokens AS p
                JOIN mobile_devices AS d ON d.id=p.device_id
                WHERE {' AND '.join(clauses)}
                ORDER BY p.updated_at DESC
                """,
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]

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
            access_expires_at=int(row["access_expires_at"]),
            refresh_expires_at=int(row["refresh_expires_at"]),
        )


class OwnerMobileTokenProvider(DashboardAuthProvider):
    """Token-only provider backed by :class:`MobileDeviceStore`."""

    name = "owner-mobile"
    display_name = "Hermes mobile device"
    supports_session = False
    supports_token = True

    def __init__(
        self,
        store_factory: Callable[[], MobileDeviceStore] = MobileDeviceStore,
    ) -> None:
        self._store_factory = store_factory

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        record = self._store_factory().verify_access(token)
        if record is None:
            return None
        return TokenPrincipal(
            principal=record.user_id,
            provider=self.name,
            scopes=("dashboard:admin",),
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
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError("Use the native refresh endpoint")

    def revoke_session(self, *, refresh_token: str) -> None:
        self._store_factory().revoke_session(refresh_token=refresh_token)
