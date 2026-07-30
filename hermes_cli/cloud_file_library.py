"""Durable, account-scoped file storage for dashboard and native clients.

The dashboard process is the cloud boundary for a self-hosted Hermes account:
files live under ``HERMES_HOME`` and SQLite is the durable source of metadata.
Conversation attachments and model-created artifacts use the same store so a
client reinstall only needs to sign back in and query the account library.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time as datetime_time, timezone
import hashlib
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator, Sequence
import uuid

from hermes_runtime.config import get_hermes_home
from hermes_cli.account_lifecycle import account_lifecycle_commit_guard
from hermes_cli.sqlite_util import write_txn


def _native_atomic_path(path: Path) -> str:
    """Return an extended Windows path for the final atomic filesystem call."""

    value = os.path.abspath(os.fspath(path))
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value.lstrip("\\")
    return "\\\\?\\" + value


SCHEMA_VERSION = 5
LOCAL_OWNER_ID = "local-owner"
LEGACY_ACCOUNT_GENERATION = "legacy"
FILE_SOURCES = frozenset({"user_upload", "model_output"})
FILE_STATUSES = frozenset({"uploading", "staged", "available", "failed"})
INSTALL_INTENT_RECOVERY_AGE_MS = 15 * 60 * 1000
UPLOAD_RESERVATION_LEASE_MS = 60 * 1000
_SOURCE_ALIASES = {
    "user": "user_upload",
    "upload": "user_upload",
    "uploads": "user_upload",
    "user_upload": "user_upload",
    "model": "model_output",
    "output": "model_output",
    "outputs": "model_output",
    "artifact": "model_output",
    "model_output": "model_output",
}
_DOCUMENT_EXTENSIONS = frozenset(
    {
        ".csv",
        ".doc",
        ".docx",
        ".epub",
        ".html",
        ".md",
        ".odp",
        ".ods",
        ".odt",
        ".pdf",
        ".ppt",
        ".pptx",
        ".rtf",
        ".tex",
        ".txt",
        ".xls",
        ".xlsx",
    }
)
_ARCHIVE_EXTENSIONS = frozenset(
    {".7z", ".bz2", ".gz", ".rar", ".tar", ".tgz", ".xz", ".zip"}
)
_CODE_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".json",
        ".kt",
        ".m",
        ".mm",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".xml",
        ".yaml",
        ".yml",
    }
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS account_files (
    id                  TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL,
    account_generation  TEXT NOT NULL DEFAULT 'legacy',
    name                TEXT NOT NULL,
    stored_relpath      TEXT NOT NULL DEFAULT '',
    sha256              TEXT NOT NULL DEFAULT '',
    mime_type           TEXT NOT NULL DEFAULT 'application/octet-stream',
    extension           TEXT NOT NULL DEFAULT '',
    file_type           TEXT NOT NULL DEFAULT 'other',
    size                INTEGER NOT NULL DEFAULT 0,
    source              TEXT NOT NULL,
    status              TEXT NOT NULL,
    conversation_id     TEXT NOT NULL DEFAULT '',
    message_id          TEXT NOT NULL DEFAULT '',
    turn_id             TEXT NOT NULL DEFAULT '',
    profile             TEXT NOT NULL DEFAULT '',
    origin_key          TEXT NOT NULL DEFAULT '',
    error               TEXT NOT NULL DEFAULT '',
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    available_at        INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_account_files_owner_origin
    ON account_files(owner_id, account_generation, origin_key)
    WHERE origin_key <> '';
CREATE INDEX IF NOT EXISTS idx_account_files_owner_created
    ON account_files(owner_id, account_generation, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_files_owner_updated
    ON account_files(owner_id, account_generation, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_files_owner_source
    ON account_files(owner_id, account_generation, source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_files_owner_type
    ON account_files(owner_id, account_generation, file_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_files_conversation
    ON account_files(owner_id, account_generation, conversation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS deleted_file_origins (
    owner_id        TEXT NOT NULL,
    account_generation TEXT NOT NULL DEFAULT 'legacy',
    origin_key      TEXT NOT NULL,
    sha256          TEXT NOT NULL DEFAULT '',
    deleted_at      INTEGER NOT NULL,
    PRIMARY KEY(owner_id, account_generation, origin_key)
);

CREATE TABLE IF NOT EXISTS deleted_file_owners (
    owner_id            TEXT NOT NULL,
    account_generation  TEXT NOT NULL DEFAULT 'legacy',
    deleted_at          INTEGER NOT NULL,
    PRIMARY KEY(owner_id, account_generation)
);

CREATE TABLE IF NOT EXISTS file_install_intents (
    id                  TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL,
    account_generation  TEXT NOT NULL DEFAULT 'legacy',
    file_id             TEXT NOT NULL,
    target_relpath      TEXT NOT NULL,
    expected_sha256     TEXT NOT NULL,
    reservation_token   TEXT NOT NULL DEFAULT '',
    created_at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_install_intents_owner
    ON file_install_intents(owner_id, account_generation, created_at);

CREATE TABLE IF NOT EXISTS file_upload_reservations (
    owner_id            TEXT NOT NULL,
    account_generation  TEXT NOT NULL,
    origin_key          TEXT NOT NULL,
    reservation_token   TEXT NOT NULL,
    file_id             TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    state               TEXT NOT NULL,
    temp_relpath        TEXT NOT NULL DEFAULT '',
    target_relpath      TEXT NOT NULL DEFAULT '',
    lease_expires_at    INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    PRIMARY KEY(owner_id, account_generation, origin_key)
);
CREATE INDEX IF NOT EXISTS idx_file_upload_reservation_lease
    ON file_upload_reservations(state, lease_expires_at);
"""


def normalize_owner_id(value: Any) -> str:
    owner_id = str(value or "").strip().replace("\x00", "")
    return owner_id[:512] or LOCAL_OWNER_ID


def normalize_account_generation(value: Any) -> str:
    generation = str(value or "").strip().replace("\x00", "")
    return generation[:512] or LEGACY_ACCOUNT_GENERATION


def account_generation_from_request(request: Any) -> str:
    """Return the generation authenticated for this request, if present."""

    state = getattr(request, "state", None)
    principal = getattr(state, "token_principal", None)
    generation = getattr(principal, "account_generation", "")
    if str(generation or "").strip():
        return normalize_account_generation(generation)
    session = getattr(state, "session", None)
    generation = getattr(session, "account_generation", "")
    return normalize_account_generation(generation)


def owner_id_from_request(request: Any) -> str:
    """Resolve the canonical account identity attached by dashboard auth.

    Cookie auth attaches ``Session`` and native bearer auth attaches
    ``TokenPrincipal``. Loopback dashboards intentionally fall back to one
    local owner, preserving the existing no-login desktop behavior.
    """

    state = getattr(request, "state", None)
    session = getattr(state, "session", None)
    user_id = getattr(session, "user_id", "")
    if str(user_id or "").strip():
        return normalize_owner_id(user_id)
    principal = getattr(state, "token_principal", None)
    principal_id = getattr(principal, "principal", "")
    if str(principal_id or "").strip():
        return normalize_owner_id(principal_id)
    return LOCAL_OWNER_ID


def safe_file_name(filename: str) -> str:
    """Return a single safe path component while preserving display text."""

    raw = str(filename or "").replace("\x00", "").strip()
    # Path.name on POSIX does not treat a backslash as a separator.
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name in {"", ".", ".."}:
        raise ValueError("File name is required")
    # Windows rejects trailing dots/spaces and several control characters.
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).rstrip(" .")
    if name in {"", ".", ".."}:
        raise ValueError("File name is invalid")
    stem = Path(name).stem.upper()
    if stem in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        name = f"_{name}"
    if len(name) > 240:
        suffix = Path(name).suffix[:32]
        name = name[: max(1, 240 - len(suffix))].rstrip() + suffix
    return name


def normalize_source(source: str) -> str:
    normalized = _SOURCE_ALIASES.get(str(source or "").strip().lower(), "")
    if normalized not in FILE_SOURCES:
        raise ValueError("File source must be user_upload or model_output")
    return normalized


def normalize_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized not in FILE_STATUSES:
        raise ValueError("File status must be uploading, staged, available, or failed")
    return normalized


def normalize_mime_type(value: str, filename: str) -> str:
    supplied = str(value or "").split(";", 1)[0].strip().lower()
    guessed = mimetypes.guess_type(filename)[0]
    if supplied == "application/octet-stream" and guessed:
        return guessed
    if supplied and re.fullmatch(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*", supplied):
        return supplied
    return guessed or "application/octet-stream"


def classify_file_type(mime_type: str, filename: str) -> str:
    top_level = str(mime_type or "").split("/", 1)[0].lower()
    if top_level in {"image", "audio", "video"}:
        return top_level
    extension = Path(filename).suffix.lower()
    if extension in _ARCHIVE_EXTENSIONS:
        return "archive"
    if extension in _CODE_EXTENSIONS:
        return "code"
    if extension in _DOCUMENT_EXTENSIONS or top_level == "text":
        return "document"
    return "other"


def parse_date_filter(value: Any, *, end_of_day: bool = False) -> int | None:
    """Parse epoch seconds/ms or ISO-8601 into UTC epoch milliseconds."""

    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    try:
        numeric = float(raw)
    except ValueError:
        numeric = None
    if numeric is not None:
        return int(numeric if abs(numeric) >= 100_000_000_000 else numeric * 1000)

    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw))
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("Date filters must be epoch seconds/ms or ISO-8601") from exc
    if date_only:
        parsed = datetime.combine(
            parsed.date(),
            datetime_time.max if end_of_day else datetime_time.min,
            tzinfo=timezone.utc,
        )
    elif parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


class CloudFileLibrary:
    """SQLite index plus path-confined durable object storage."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else (
            Path(get_hermes_home()) / "collaboration" / "account-files"
        )
        self.db_path = self.root / "library.sqlite3"
        self.objects_root = self.root / "objects"
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self._schema_ready = False
        self._last_temp_recovery_ms = 0

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _migrate_generation_schema(self, conn: sqlite3.Connection) -> None:
        """Fence pre-v4 rows into a non-reusable legacy generation."""

        account_columns = self._table_columns(conn, "account_files")
        if account_columns and "account_generation" not in account_columns:
            conn.execute(
                "ALTER TABLE account_files ADD COLUMN account_generation "
                "TEXT NOT NULL DEFAULT 'legacy'"
            )

        intent_columns = self._table_columns(conn, "file_install_intents")
        if intent_columns and "account_generation" not in intent_columns:
            conn.execute(
                "ALTER TABLE file_install_intents ADD COLUMN account_generation "
                "TEXT NOT NULL DEFAULT 'legacy'"
            )
        if intent_columns and "reservation_token" not in intent_columns:
            conn.execute(
                "ALTER TABLE file_install_intents ADD COLUMN reservation_token "
                "TEXT NOT NULL DEFAULT ''"
            )

        origin_columns = self._table_columns(conn, "deleted_file_origins")
        if origin_columns and "account_generation" not in origin_columns:
            conn.execute(
                "ALTER TABLE deleted_file_origins RENAME TO "
                "deleted_file_origins_v3"
            )
            conn.execute(
                """
                CREATE TABLE deleted_file_origins (
                    owner_id TEXT NOT NULL,
                    account_generation TEXT NOT NULL DEFAULT 'legacy',
                    origin_key TEXT NOT NULL,
                    sha256 TEXT NOT NULL DEFAULT '',
                    deleted_at INTEGER NOT NULL,
                    PRIMARY KEY(owner_id, account_generation, origin_key)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO deleted_file_origins (
                    owner_id, account_generation, origin_key, sha256, deleted_at
                )
                SELECT owner_id, 'legacy', origin_key, sha256, deleted_at
                FROM deleted_file_origins_v3
                """
            )
            conn.execute("DROP TABLE deleted_file_origins_v3")

        owner_columns = self._table_columns(conn, "deleted_file_owners")
        if owner_columns and "account_generation" not in owner_columns:
            conn.execute(
                "ALTER TABLE deleted_file_owners RENAME TO deleted_file_owners_v3"
            )
            conn.execute(
                """
                CREATE TABLE deleted_file_owners (
                    owner_id TEXT NOT NULL,
                    account_generation TEXT NOT NULL DEFAULT 'legacy',
                    deleted_at INTEGER NOT NULL,
                    PRIMARY KEY(owner_id, account_generation)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO deleted_file_owners (
                    owner_id, account_generation, deleted_at
                )
                SELECT owner_id, 'legacy', deleted_at
                FROM deleted_file_owners_v3
                """
            )
            conn.execute("DROP TABLE deleted_file_owners_v3")

        for index_name in (
            "idx_account_files_owner_origin",
            "idx_account_files_owner_created",
            "idx_account_files_owner_updated",
            "idx_account_files_owner_source",
            "idx_account_files_owner_type",
            "idx_account_files_conversation",
            "idx_file_install_intents_owner",
            "idx_file_upload_reservation_lease",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {index_name}")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects_root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                conn.close()
                raise RuntimeError(
                    "Cloud file library was created by a newer Hermes version "
                    f"(schema {current} > {SCHEMA_VERSION})"
                )
            conn.execute("PRAGMA journal_mode=WAL")
            if not self._schema_ready:
                if current < SCHEMA_VERSION:
                    with write_txn(conn):
                        self._migrate_generation_schema(conn)
                conn.executescript(_SCHEMA_SQL)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._schema_ready = True
            self._recover_install_intents(conn)
            self._recover_stale_temps()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _clean_metadata(value: Any, limit: int = 512) -> str:
        return str(value or "").replace("\x00", "").strip()[:limit]

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _owner_bucket(self, owner_id: str, account_generation: str) -> str:
        scope = f"{owner_id}\x00{account_generation}"
        return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]

    def _destination(
        self,
        owner_id: str,
        account_generation: str,
        file_id: str,
        filename: str,
        digest: str,
    ) -> tuple[Path, str]:
        # Immutable content revisions keep the prior indexed object valid if
        # the process exits after installing new bytes but before committing
        # the SQLite metadata switch.
        relative = (
            Path("objects")
            / self._owner_bucket(owner_id, account_generation)
            / file_id
            / f"{digest[:20]}-{filename}"
        )
        target = (self.root / relative).resolve()
        root = self.root.resolve()
        if not target.is_relative_to(root):
            raise ValueError("File storage path escapes the account library")
        return target, relative.as_posix()

    def _recover_install_intents(self, conn: sqlite3.Connection) -> None:
        """Remove unindexed installed objects left by a process exit."""

        intents = conn.execute(
            "SELECT * FROM file_install_intents WHERE created_at<=?",
            (self._clock_ms() - INSTALL_INTENT_RECOVERY_AGE_MS,),
        ).fetchall()
        objects_root = self.objects_root.resolve()
        for intent in intents:
            relative = str(intent["target_relpath"] or "")
            reservation_token = str(intent["reservation_token"] or "")
            target = (self.root / relative).resolve()
            if target.is_relative_to(objects_root):
                reservation = None
                if reservation_token:
                    reservation = conn.execute(
                        "SELECT reservation_token FROM file_upload_reservations "
                        "WHERE owner_id=? AND account_generation=? AND file_id=?",
                        (
                            str(intent["owner_id"]),
                            str(intent["account_generation"]),
                            str(intent["file_id"]),
                        ),
                    ).fetchone()
                token_still_owned = (
                    not reservation_token
                    or (
                        reservation is not None
                        and str(reservation["reservation_token"])
                        == reservation_token
                    )
                )
                indexed = conn.execute(
                    "SELECT stored_relpath,sha256 FROM account_files "
                    "WHERE id=? AND owner_id=? AND account_generation=?",
                    (
                        str(intent["file_id"]),
                        str(intent["owner_id"]),
                        str(intent["account_generation"]),
                    ),
                ).fetchone()
                installed_is_current = (
                    indexed is not None
                    and str(indexed["stored_relpath"] or "") == relative
                    and str(indexed["sha256"] or "")
                    == str(intent["expected_sha256"] or "")
                )
                if token_still_owned and not installed_is_current:
                    Path(_native_atomic_path(target)).unlink(missing_ok=True)
                    self._remove_empty_object_parents(target)
                    conn.execute(
                        "DELETE FROM file_upload_reservations "
                        "WHERE owner_id=? AND account_generation=? AND file_id=? "
                        "AND reservation_token=? AND state='pending'",
                        (
                            str(intent["owner_id"]),
                            str(intent["account_generation"]),
                            str(intent["file_id"]),
                            reservation_token,
                        ),
                    )
            conn.execute(
                "DELETE FROM file_install_intents WHERE id=?",
                (str(intent["id"]),),
            )

    def _recover_stale_temps(self) -> None:
        now = self._clock_ms()
        if now - self._last_temp_recovery_ms < INSTALL_INTENT_RECOVERY_AGE_MS:
            return
        self._last_temp_recovery_ms = now
        root = self.objects_root.resolve()
        if not root.exists():
            return
        cutoff_seconds = (now - INSTALL_INTENT_RECOVERY_AGE_MS) / 1000
        for candidate in root.rglob(".upload-*"):
            try:
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    continue
                relative = resolved.relative_to(self.root.resolve()).as_posix()
                active = False
                with sqlite3.connect(str(self.db_path), timeout=30.0) as check:
                    active = check.execute(
                        "SELECT 1 FROM file_upload_reservations "
                        "WHERE temp_relpath=? AND state='pending' "
                        "AND lease_expires_at>? LIMIT 1",
                        (relative, now),
                    ).fetchone() is not None
                if (
                    resolved.is_file()
                    and resolved.stat().st_mtime <= cutoff_seconds
                    and not active
                ):
                    resolved.unlink(missing_ok=True)
                    self._remove_empty_object_parents(resolved)
            except (FileNotFoundError, OSError):
                continue

    def _remove_empty_object_parents(self, target: Path) -> None:
        root = self.objects_root.resolve()
        parent = target.parent
        while parent != root and parent.is_relative_to(root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _abandon_install_intent(
        self,
        intent_id: str,
        relative: str,
        target: Path | None,
        reservation_origin: str,
        reservation_token: str,
    ) -> None:
        """Drop one failed install without deleting another writer's object."""

        with self.connection() as conn, write_txn(conn):
            reservation = conn.execute(
                "SELECT reservation_token FROM file_upload_reservations "
                "WHERE origin_key=? AND reservation_token=?",
                (reservation_origin, reservation_token),
            ).fetchone()
            owns_reservation = reservation is not None
            conn.execute(
                "DELETE FROM file_install_intents WHERE id=? "
                "AND reservation_token=?",
                (intent_id, reservation_token),
            )
            if owns_reservation:
                conn.execute(
                    "DELETE FROM file_upload_reservations WHERE origin_key=? "
                    "AND reservation_token=? AND state='pending'",
                    (reservation_origin, reservation_token),
                )
            indexed = conn.execute(
                "SELECT 1 FROM account_files WHERE stored_relpath=? LIMIT 1",
                (relative,),
            ).fetchone()
            pending = conn.execute(
                "SELECT 1 FROM file_install_intents WHERE target_relpath=? LIMIT 1",
                (relative,),
            ).fetchone()
            if (
                owns_reservation
                and target is not None
                and relative
                and indexed is None
                and pending is None
            ):
                target.unlink(missing_ok=True)
                self._remove_empty_object_parents(target)

    def _discard_staged_temp(self, temp: Path, reservation_token: str) -> None:
        """Remove this writer's temp only when no other token owns its path."""

        if not temp.exists():
            return
        relative = temp.resolve().relative_to(self.root.resolve()).as_posix()
        with self.connection() as conn:
            owner = conn.execute(
                "SELECT reservation_token FROM file_upload_reservations "
                "WHERE temp_relpath=? LIMIT 1",
                (relative,),
            ).fetchone()
        if owner is None or str(owner["reservation_token"]) == reservation_token:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _ensure_owner_active(
        conn: sqlite3.Connection,
        owner_id: str,
        account_generation: str,
    ) -> None:
        deleted = conn.execute(
            "SELECT 1 FROM deleted_file_owners "
            "WHERE owner_id=? AND account_generation=?",
            (owner_id, account_generation),
        ).fetchone()
        if deleted is not None:
            raise PermissionError("account file boundary was deleted")

    @staticmethod
    def _validate_source_path(
        source_path: Path | str,
        allowed_roots: Sequence[Path | str] | None,
    ) -> Path:
        source = Path(source_path).resolve(strict=True)
        if not source.is_file():
            raise ValueError("Artifact source is not a file")
        if allowed_roots:
            roots = [Path(root).resolve(strict=True) for root in allowed_roots]
            if not any(source.is_relative_to(root) for root in roots):
                raise ValueError("Artifact source is outside the allowed output directory")
        return source

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def _stage_source(self, source_path: Path) -> tuple[Path, str, int, str]:
        """Copy once while hashing so the reserved digest matches published bytes."""

        self.objects_root.mkdir(parents=True, exist_ok=True)
        reservation_token = uuid.uuid4().hex
        temp = self.objects_root / f".upload-{reservation_token}"
        digest = hashlib.sha256()
        size = 0
        try:
            with source_path.open("rb") as source_handle, temp.open("xb") as target_handle:
                for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    target_handle.write(chunk)
                target_handle.flush()
                os.fsync(target_handle.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return temp, digest.hexdigest(), size, reservation_token

    @staticmethod
    def _reservation_origin(origin_key: str, file_id: str) -> str:
        return origin_key or f"file-id:{file_id}"

    def _claim_upload_reservation(
        self,
        conn: sqlite3.Connection,
        *,
        owner_id: str,
        account_generation: str,
        origin_key: str,
        reservation_token: str,
        file_id: str,
        digest: str,
        temp_relpath: str,
        strict_bytes: bool,
        make_available: bool,
    ) -> tuple[str, sqlite3.Row | None]:
        """Reserve one upload origin and return ``(file_id, replay_row)``."""

        now = self._clock_ms()
        reservation = conn.execute(
            "SELECT * FROM file_upload_reservations WHERE owner_id=? "
            "AND account_generation=? AND origin_key=?",
            (owner_id, account_generation, origin_key),
        ).fetchone()
        if reservation is not None:
            reserved_digest = str(reservation["sha256"] or "")
            if strict_bytes and reserved_digest and reserved_digest != digest:
                raise FileExistsError(
                    "Upload id was already used with different content"
                )
            reserved_file_id = str(reservation["file_id"])
            if reservation["state"] == "completed" and reserved_digest == digest:
                replay = self._select_owned(
                    conn, owner_id, account_generation, reserved_file_id
                )
                if replay is not None:
                    try:
                        status_compatible = (
                            replay["status"] == "available"
                            if make_available
                            else replay["status"] in {"staged", "available"}
                        )
                        if (
                            status_compatible
                            and self._record_path(dict(replay)).is_file()
                        ):
                            return reserved_file_id, replay
                    except ValueError:
                        pass
            if (
                reservation["state"] == "pending"
                and int(reservation["lease_expires_at"] or 0) > now
            ):
                raise BlockingIOError("Upload is already in progress")
            file_id = reserved_file_id
            conn.execute(
                "UPDATE file_upload_reservations SET reservation_token=?, "
                "sha256=?, state='pending', temp_relpath=?, target_relpath='', "
                "lease_expires_at=?, updated_at=? WHERE owner_id=? "
                "AND account_generation=? AND origin_key=?",
                (
                    reservation_token,
                    digest,
                    temp_relpath,
                    now + UPLOAD_RESERVATION_LEASE_MS,
                    now,
                    owner_id,
                    account_generation,
                    origin_key,
                ),
            )
            return file_id, None

        conn.execute(
            "INSERT INTO file_upload_reservations("
            "owner_id,account_generation,origin_key,reservation_token,file_id,"
            "sha256,state,temp_relpath,lease_expires_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'pending',?,?,?)",
            (
                owner_id,
                account_generation,
                origin_key,
                reservation_token,
                file_id,
                digest,
                temp_relpath,
                now + UPLOAD_RESERVATION_LEASE_MS,
                now,
            ),
        )
        return file_id, None

    def _select_owned(
        self,
        conn: sqlite3.Connection,
        owner_id: str,
        account_generation: str,
        file_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM account_files "
            "WHERE id=? AND owner_id=? AND account_generation=?",
            (file_id, owner_id, account_generation),
        ).fetchone()

    def reserve_file(
        self,
        owner_id: str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
        name: str,
        source: str,
        conversation_id: str = "",
        message_id: str = "",
        turn_id: str = "",
        profile: str = "",
        origin_key: str = "",
        mime_type: str = "",
        file_id: str = "",
    ) -> dict[str, Any]:
        owner_id = normalize_owner_id(owner_id)
        account_generation = normalize_account_generation(account_generation)
        source = normalize_source(source)
        name = safe_file_name(name)
        mime_type = normalize_mime_type(mime_type, name)
        extension = Path(name).suffix.lower()[:32]
        file_type = classify_file_type(mime_type, name)
        now = self._clock_ms()
        metadata = {
            "conversation_id": self._clean_metadata(conversation_id),
            "message_id": self._clean_metadata(message_id),
            "turn_id": self._clean_metadata(turn_id),
            "profile": self._clean_metadata(profile, 128),
            "origin_key": self._clean_metadata(origin_key, 1024),
        }
        with account_lifecycle_commit_guard(), self._lock, self.connection() as conn, write_txn(conn):
            self._ensure_owner_active(conn, owner_id, account_generation)
            existing = None
            if file_id:
                existing = self._select_owned(
                    conn, owner_id, account_generation, file_id
                )
                if existing is None:
                    raise KeyError(file_id)
            elif metadata["origin_key"]:
                existing = conn.execute(
                    "SELECT * FROM account_files WHERE owner_id=? "
                    "AND account_generation=? AND origin_key=?",
                    (owner_id, account_generation, metadata["origin_key"]),
                ).fetchone()
            if existing is not None:
                file_id = str(existing["id"])
                conn.execute(
                    """
                    UPDATE account_files
                    SET name=?, mime_type=?, extension=?, file_type=?, source=?,
                        status='uploading', conversation_id=?, message_id=?,
                        turn_id=?, profile=?, origin_key=?, error='', updated_at=?
                    WHERE id=? AND owner_id=? AND account_generation=?
                    """,
                    (
                        name,
                        mime_type,
                        extension,
                        file_type,
                        source,
                        metadata["conversation_id"],
                        metadata["message_id"],
                        metadata["turn_id"],
                        metadata["profile"],
                        metadata["origin_key"],
                        now,
                        file_id,
                        owner_id,
                        account_generation,
                    ),
                )
            else:
                file_id = file_id or f"file_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO account_files (
                        id, owner_id, account_generation, name, mime_type, extension, file_type,
                        source, status, conversation_id, message_id, turn_id,
                        profile, origin_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'uploading', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        owner_id,
                        account_generation,
                        name,
                        mime_type,
                        extension,
                        file_type,
                        source,
                        metadata["conversation_id"],
                        metadata["message_id"],
                        metadata["turn_id"],
                        metadata["profile"],
                        metadata["origin_key"],
                        now,
                        now,
                    ),
                )
            if metadata["origin_key"]:
                conn.execute(
                    "DELETE FROM deleted_file_origins WHERE owner_id=? "
                    "AND account_generation=? AND origin_key=?",
                    (owner_id, account_generation, metadata["origin_key"]),
                )
            row = self._select_owned(conn, owner_id, account_generation, file_id)
        return dict(row)

    def ingest_file(
        self,
        owner_id: str,
        source_path: Path | str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
        name: str = "",
        source: str,
        conversation_id: str = "",
        message_id: str = "",
        turn_id: str = "",
        profile: str = "",
        origin_key: str = "",
        mime_type: str = "",
        file_id: str = "",
        allowed_roots: Sequence[Path | str] | None = None,
        restore_deleted: bool = True,
        make_available: bool = True,
    ) -> dict[str, Any] | None:
        owner_id = normalize_owner_id(owner_id)
        account_generation = normalize_account_generation(account_generation)
        normalized_source = normalize_source(source)
        source_path = self._validate_source_path(source_path, allowed_roots)
        name = safe_file_name(name or source_path.name)
        mime_type = normalize_mime_type(mime_type, name)
        extension = Path(name).suffix.lower()[:32]
        file_type = classify_file_type(mime_type, name)
        metadata = {
            "conversation_id": self._clean_metadata(conversation_id),
            "message_id": self._clean_metadata(message_id),
            "turn_id": self._clean_metadata(turn_id),
            "profile": self._clean_metadata(profile, 128),
            "origin_key": self._clean_metadata(origin_key, 1024),
        }
        temp, digest, size, reservation_token = self._stage_source(source_path)
        temp_relative = temp.resolve().relative_to(self.root.resolve()).as_posix()
        requested_file_id = file_id
        file_id = file_id or f"file_{uuid.uuid4().hex}"
        reservation_origin = self._reservation_origin(metadata["origin_key"], file_id)
        strict_bytes = normalized_source == "user_upload" and bool(metadata["origin_key"])
        target: Path | None = None
        relative = ""
        intent_id = f"install_{uuid.uuid4().hex}"
        reservation_claimed = False

        try:
            with account_lifecycle_commit_guard(), self._lock:
                with self.connection() as conn, write_txn(conn):
                    self._ensure_owner_active(conn, owner_id, account_generation)
                    if metadata["origin_key"]:
                        tombstone = conn.execute(
                            "SELECT sha256 FROM deleted_file_origins WHERE owner_id=? "
                            "AND account_generation=? AND origin_key=?",
                            (owner_id, account_generation, metadata["origin_key"]),
                        ).fetchone()
                        if (
                            tombstone is not None
                            and not restore_deleted
                            and str(tombstone["sha256"] or "") == digest
                        ):
                            return None

                    existing = None
                    if requested_file_id:
                        existing = self._select_owned(
                            conn, owner_id, account_generation, requested_file_id
                        )
                        if existing is None:
                            raise KeyError(requested_file_id)
                    elif metadata["origin_key"]:
                        existing = conn.execute(
                            "SELECT * FROM account_files WHERE owner_id=? "
                            "AND account_generation=? AND origin_key=?",
                            (owner_id, account_generation, metadata["origin_key"]),
                        ).fetchone()
                    if existing is not None:
                        file_id = str(existing["id"])
                        reservation_origin = self._reservation_origin(
                            metadata["origin_key"], file_id
                        )
                        if (
                            strict_bytes
                            and str(existing["sha256"] or "")
                            and str(existing["sha256"]) != digest
                        ):
                            raise FileExistsError(
                                "Upload id was already used with different content"
                            )

                    file_id, reservation_replay = self._claim_upload_reservation(
                        conn,
                        owner_id=owner_id,
                        account_generation=account_generation,
                        origin_key=reservation_origin,
                        reservation_token=reservation_token,
                        file_id=file_id,
                        digest=digest,
                        temp_relpath=temp_relative,
                        strict_bytes=strict_bytes,
                        make_available=make_available,
                    )
                    reservation_claimed = reservation_replay is None
                    if requested_file_id and file_id != requested_file_id:
                        raise FileExistsError("Upload origin belongs to another file")
                    if reservation_replay is not None:
                        return dict(reservation_replay)

                    if existing is None or str(existing["id"]) != file_id:
                        existing = self._select_owned(
                            conn, owner_id, account_generation, file_id
                        )
                    desired_status = "available" if make_available else "staged"
                    if existing is not None:
                        existing_path_ok = False
                        if existing["stored_relpath"]:
                            try:
                                existing_path_ok = self._record_path(dict(existing)).is_file()
                            except ValueError:
                                pass
                        status_compatible = (
                            existing["status"] == "available"
                            if make_available
                            else existing["status"] in {"staged", "available"}
                        )
                        metadata_unchanged = all(
                            str(existing[key]) == value for key, value in metadata.items()
                        ) and existing["source"] == normalized_source
                        origin_replay = bool(
                            metadata["origin_key"]
                            and existing_path_ok
                            and status_compatible
                            and existing["sha256"] == digest
                        )
                        unchanged = bool(
                            existing_path_ok
                            and status_compatible
                            and existing["sha256"] == digest
                            and existing["name"] == name
                            and metadata_unchanged
                        )
                        if origin_replay or unchanged:
                            conn.execute(
                                "UPDATE file_upload_reservations SET state='completed', "
                                "temp_relpath='', target_relpath=?, lease_expires_at=0, "
                                "updated_at=? WHERE owner_id=? AND account_generation=? "
                                "AND origin_key=? AND reservation_token=?",
                                (
                                    str(existing["stored_relpath"] or ""),
                                    self._clock_ms(),
                                    owner_id,
                                    account_generation,
                                    reservation_origin,
                                    reservation_token,
                                ),
                            )
                            reservation_claimed = False
                            return dict(existing)

                available_at = self._clock_ms() if make_available else None
                target, relative = self._destination(
                    owner_id, account_generation, file_id, name, digest
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                old_relative = str(existing["stored_relpath"] or "") if existing else ""

                with self.connection() as conn, write_txn(conn):
                    self._ensure_owner_active(conn, owner_id, account_generation)
                    owned = conn.execute(
                        "UPDATE file_upload_reservations SET target_relpath=?, "
                        "lease_expires_at=?, updated_at=? WHERE owner_id=? "
                        "AND account_generation=? AND origin_key=? "
                        "AND reservation_token=? AND state='pending'",
                        (
                            relative,
                            self._clock_ms() + UPLOAD_RESERVATION_LEASE_MS,
                            self._clock_ms(),
                            owner_id,
                            account_generation,
                            reservation_origin,
                            reservation_token,
                        ),
                    ).rowcount
                    if owned != 1:
                        raise PermissionError("Upload reservation was lost")
                    conn.execute(
                        "INSERT INTO file_install_intents("
                        "id,owner_id,account_generation,file_id,target_relpath,"
                        "expected_sha256,reservation_token,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            intent_id,
                            owner_id,
                            account_generation,
                            file_id,
                            relative,
                            digest,
                            reservation_token,
                            self._clock_ms(),
                        ),
                    )

                os.replace(_native_atomic_path(temp), _native_atomic_path(target))
                now = self._clock_ms()
                with self.connection() as conn, write_txn(conn):
                    self._ensure_owner_active(conn, owner_id, account_generation)
                    reservation = conn.execute(
                        "SELECT 1 FROM file_upload_reservations WHERE owner_id=? "
                        "AND account_generation=? AND origin_key=? "
                        "AND reservation_token=? AND state='pending'",
                        (
                            owner_id,
                            account_generation,
                            reservation_origin,
                            reservation_token,
                        ),
                    ).fetchone()
                    if reservation is None:
                        raise PermissionError("Upload reservation was lost")
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO account_files (
                                id, owner_id, account_generation, name, stored_relpath, sha256,
                                mime_type, extension, file_type, size, source,
                                status, conversation_id, message_id, turn_id,
                                profile, origin_key, error, created_at,
                                updated_at, available_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      ?, ?, ?, ?, ?, '', ?, ?, ?)
                            """,
                            (
                                file_id, owner_id, account_generation, name, relative,
                                digest, mime_type, extension, file_type, size,
                                normalized_source, desired_status,
                                metadata["conversation_id"], metadata["message_id"],
                                metadata["turn_id"], metadata["profile"],
                                metadata["origin_key"], now, now, available_at,
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE account_files
                            SET name=?, stored_relpath=?, sha256=?, mime_type=?,
                                extension=?, file_type=?, size=?, source=?,
                                status=?, conversation_id=?, message_id=?,
                                turn_id=?, profile=?, origin_key=?, error='',
                                updated_at=?, available_at=?
                            WHERE id=? AND owner_id=? AND account_generation=?
                            """,
                            (
                                name, relative, digest, mime_type, extension,
                                file_type, size, normalized_source, desired_status,
                                metadata["conversation_id"], metadata["message_id"],
                                metadata["turn_id"], metadata["profile"],
                                metadata["origin_key"], now, available_at, file_id,
                                owner_id, account_generation,
                            ),
                        )
                    if metadata["origin_key"]:
                        conn.execute(
                            "DELETE FROM deleted_file_origins WHERE owner_id=? "
                            "AND account_generation=? AND origin_key=?",
                            (owner_id, account_generation, metadata["origin_key"]),
                        )
                    conn.execute(
                        "UPDATE file_upload_reservations SET state='completed', "
                        "temp_relpath='', target_relpath=?, lease_expires_at=0, "
                        "updated_at=? WHERE owner_id=? AND account_generation=? "
                        "AND origin_key=? AND reservation_token=?",
                        (
                            relative, now, owner_id, account_generation,
                            reservation_origin, reservation_token,
                        ),
                    )
                    conn.execute(
                        "DELETE FROM file_install_intents WHERE id=? "
                        "AND reservation_token=?",
                        (intent_id, reservation_token),
                    )
                    row = self._select_owned(conn, owner_id, account_generation, file_id)
                reservation_claimed = False

                if old_relative and old_relative != relative:
                    self._remove_object_path(old_relative)
                return dict(row)
        except BaseException:
            if reservation_claimed:
                try:
                    self._abandon_install_intent(
                        intent_id,
                        relative,
                        target,
                        reservation_origin,
                        reservation_token,
                    )
                except Exception:
                    pass
            raise
        finally:
            self._discard_staged_temp(temp, reservation_token)

    def restore_file_record(self, previous: dict[str, Any]) -> dict[str, Any]:
        """Restore every field and object path from a pre-publication snapshot."""

        columns = (
            "id", "owner_id", "account_generation", "name", "stored_relpath", "sha256",
            "mime_type", "extension", "file_type", "size", "source",
            "status", "conversation_id", "message_id", "turn_id", "profile",
            "origin_key", "error", "created_at", "updated_at", "available_at",
        )
        if not isinstance(previous, dict) or any(key not in previous for key in columns):
            raise ValueError("Previous file record is incomplete")
        owner_id = normalize_owner_id(previous["owner_id"])
        account_generation = normalize_account_generation(
            previous.get("account_generation")
        )
        previous = dict(previous)
        previous["account_generation"] = account_generation
        file_id = str(previous["id"] or "").strip()
        if not file_id or owner_id != str(previous["owner_id"]):
            raise ValueError("Previous file record identity is invalid")
        previous_path = self._record_path(previous)

        with account_lifecycle_commit_guard(), self._lock:
            with self.connection() as conn:
                current = self._select_owned(
                    conn, owner_id, account_generation, file_id
                )
            current_record = dict(current) if current is not None else None
            current_path = self._record_path(current_record) if current_record else None

            if not previous_path.is_file():
                if current_path is None or not current_path.is_file():
                    raise FileNotFoundError(file_id)
                digest, size = self._hash_file(current_path)
                if digest != str(previous["sha256"] or "") or size != int(previous["size"]):
                    raise ValueError("Current object cannot restore previous record")
                previous_path.parent.mkdir(parents=True, exist_ok=True)
                temp = previous_path.with_name(f".restore-{uuid.uuid4().hex[:12]}")
                try:
                    with current_path.open("rb") as source_handle, temp.open("xb") as target_handle:
                        shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                        target_handle.flush()
                        os.fsync(target_handle.fileno())
                    os.replace(_native_atomic_path(temp), _native_atomic_path(previous_path))
                finally:
                    temp.unlink(missing_ok=True)
            else:
                digest, size = self._hash_file(previous_path)
                if digest != str(previous["sha256"] or "") or size != int(previous["size"]):
                    raise ValueError("Previous object no longer matches its record")

            placeholders = ",".join("?" for _ in columns)
            updates = ",".join(f"{column}=excluded.{column}" for column in columns[3:])
            with self.connection() as conn, write_txn(conn):
                self._ensure_owner_active(conn, owner_id, account_generation)
                conflicting_scope = conn.execute(
                    "SELECT owner_id,account_generation FROM account_files "
                    "WHERE id=?",
                    (file_id,),
                ).fetchone()
                if conflicting_scope is not None and (
                    str(conflicting_scope["owner_id"]) != owner_id
                    or str(conflicting_scope["account_generation"])
                    != account_generation
                ):
                    raise PermissionError("file id belongs to another account generation")
                conn.execute(
                    f"INSERT INTO account_files ({','.join(columns)}) VALUES ({placeholders}) "
                    f"ON CONFLICT(id) DO UPDATE SET {updates}",
                    tuple(previous[column] for column in columns),
                )
                if previous["origin_key"]:
                    conn.execute(
                        "DELETE FROM deleted_file_origins WHERE owner_id=? "
                        "AND account_generation=? AND origin_key=?",
                        (owner_id, account_generation, previous["origin_key"]),
                    )
                row = self._select_owned(
                    conn, owner_id, account_generation, file_id
                )

            if current_path is not None and current_path != previous_path:
                self._remove_object_path(str(current_record.get("stored_relpath") or ""))
        return dict(row)

    def publish_file(
        self,
        owner_id: str,
        file_id: str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
    ) -> dict[str, Any]:
        """Make staged bytes visible after their owning transaction commits."""

        owner_id = normalize_owner_id(owner_id)
        account_generation = normalize_account_generation(account_generation)
        now = self._clock_ms()
        with account_lifecycle_commit_guard(), self._lock, self.connection() as conn, write_txn(conn):
            self._ensure_owner_active(conn, owner_id, account_generation)
            row = self._select_owned(conn, owner_id, account_generation, file_id)
            if row is None:
                raise KeyError(file_id)
            if str(row["status"] or "") == "available":
                return dict(row)
            if str(row["status"] or "") != "staged":
                raise ValueError("Only staged files can be published")
            path = self._record_path(dict(row))
            if not path.is_file():
                raise FileNotFoundError(file_id)
            conn.execute(
                "UPDATE account_files SET status='available', available_at=?, "
                "updated_at=? WHERE id=? AND owner_id=? AND account_generation=? "
                "AND status='staged'",
                (now, now, file_id, owner_id, account_generation),
            )
            row = self._select_owned(conn, owner_id, account_generation, file_id)
        return dict(row)

    def set_status(
        self,
        owner_id: str,
        file_id: str,
        status: str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
        error: str = "",
    ) -> dict[str, Any]:
        owner_id = normalize_owner_id(owner_id)
        account_generation = normalize_account_generation(account_generation)
        status = normalize_status(status)
        if status == "available":
            raise ValueError("Complete an available file by ingesting its bytes")
        now = self._clock_ms()
        with account_lifecycle_commit_guard(), self.connection() as conn, write_txn(conn):
            row = self._select_owned(conn, owner_id, account_generation, file_id)
            if row is None:
                raise KeyError(file_id)
            conn.execute(
                "UPDATE account_files SET status=?, error=?, updated_at=? "
                "WHERE id=? AND owner_id=? AND account_generation=?",
                (
                    status,
                    self._clean_metadata(error, 2000),
                    now,
                    file_id,
                    owner_id,
                    account_generation,
                ),
            )
            row = self._select_owned(conn, owner_id, account_generation, file_id)
        return dict(row)

    def update_links(
        self,
        owner_id: str,
        file_ids: Sequence[str],
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
        conversation_id: str = "",
        message_id: str = "",
        turn_id: str = "",
        profile: str = "",
    ) -> int:
        owner_id = normalize_owner_id(owner_id)
        account_generation = normalize_account_generation(account_generation)
        ids = list(dict.fromkeys(str(item or "").strip() for item in file_ids if item))
        if not ids:
            return 0
        updates = {
            "conversation_id": self._clean_metadata(conversation_id),
            "message_id": self._clean_metadata(message_id),
            "turn_id": self._clean_metadata(turn_id),
            "profile": self._clean_metadata(profile, 128),
        }
        assignments = [f"{key}=?" for key, value in updates.items() if value]
        values = [value for value in updates.values() if value]
        if not assignments:
            return 0
        placeholders = ",".join("?" for _ in ids)
        now = self._clock_ms()
        with account_lifecycle_commit_guard(), self.connection() as conn, write_txn(conn):
            cursor = conn.execute(
                f"""
                UPDATE account_files
                SET {', '.join(assignments)}, updated_at=?
                WHERE owner_id=? AND account_generation=? AND id IN ({placeholders})
                """,
                (*values, now, owner_id, account_generation, *ids),
            )
        return int(cursor.rowcount)

    def get_file(
        self,
        owner_id: str,
        file_id: str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
    ) -> dict[str, Any] | None:
        owner_id = normalize_owner_id(owner_id)
        account_generation = normalize_account_generation(account_generation)
        with self.connection() as conn:
            return self._row_dict(
                self._select_owned(conn, owner_id, account_generation, file_id)
            )

    def get_file_by_origin(
        self,
        owner_id: str,
        origin_key: str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
    ) -> dict[str, Any] | None:
        owner_id = normalize_owner_id(owner_id)
        account_generation = normalize_account_generation(account_generation)
        normalized_origin = self._clean_metadata(origin_key, 1024)
        if not normalized_origin:
            return None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM account_files WHERE owner_id=? "
                "AND account_generation=? AND origin_key=?",
                (owner_id, account_generation, normalized_origin),
            ).fetchone()
        return self._row_dict(row)

    def list_files(
        self,
        owner_id: str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
        keyword: str = "",
        date_from: int | None = None,
        date_to: int | None = None,
        source: str = "",
        file_type: str = "",
        status: str = "",
        conversation_id: str = "",
        turn_id: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        owner_id = normalize_owner_id(owner_id)
        account_generation = normalize_account_generation(account_generation)
        # Staged connector objects are an internal two-phase publication
        # detail and are never visible through account-library listings.
        clauses = ["owner_id=?", "account_generation=?", "status<>'staged'"]
        values: list[Any] = [owner_id, account_generation]
        keyword = str(keyword or "").strip()[:300]
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(" + " OR ".join(
                    f"{column} LIKE ? ESCAPE '\\'"
                    for column in (
                        "name",
                        "conversation_id",
                        "message_id",
                        "turn_id",
                        "profile",
                    )
                ) + ")"
            )
            values.extend([pattern] * 5)
        if date_from is not None:
            clauses.append("created_at>=?")
            values.append(int(date_from))
        if date_to is not None:
            clauses.append("created_at<=?")
            values.append(int(date_to))
        if source:
            clauses.append("source=?")
            values.append(normalize_source(source))
        if status:
            clauses.append("status=?")
            values.append(normalize_status(status))
        type_filter = str(file_type or "").strip().lower()
        if type_filter:
            if type_filter.endswith("/*"):
                clauses.append("mime_type LIKE ?")
                values.append(type_filter[:-1] + "%")
            elif "/" in type_filter:
                clauses.append("mime_type=?")
                values.append(type_filter)
            elif type_filter in {"image", "audio", "video", "document", "archive", "code", "other"}:
                clauses.append("file_type=?")
                values.append(type_filter)
            else:
                clauses.append("extension=?")
                values.append("." + type_filter.lstrip("."))
        if conversation_id:
            clauses.append("conversation_id=?")
            values.append(self._clean_metadata(conversation_id))
        if turn_id:
            clauses.append("turn_id=?")
            values.append(self._clean_metadata(turn_id))
        where = " AND ".join(clauses)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with self.connection() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM account_files WHERE {where}", values
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT * FROM account_files
                WHERE {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*values, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows], total

    def _record_path(self, record: dict[str, Any]) -> Path:
        relative = str(record.get("stored_relpath") or "")
        if not relative:
            raise ValueError("File has no stored object")
        target = (self.root / relative).resolve()
        objects_root = self.objects_root.resolve()
        if not target.is_relative_to(objects_root):
            raise ValueError("Stored file path escapes the object directory")
        return Path(_native_atomic_path(target))

    def resolve_download(
        self,
        owner_id: str,
        file_id: str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
    ) -> tuple[dict[str, Any], Path]:
        record = self.get_file(
            owner_id,
            file_id,
            account_generation=account_generation,
        )
        if record is None:
            raise KeyError(file_id)
        if record["status"] != "available":
            raise FileNotFoundError(file_id)
        path = self._record_path(record)
        if not path.is_file():
            raise FileNotFoundError(file_id)
        return record, path

    def _remove_object_path(self, relative: str) -> None:
        if not relative:
            return
        try:
            target = self._record_path({"stored_relpath": relative})
        except ValueError:
            return
        target.unlink(missing_ok=True)
        self._remove_empty_object_parents(target)

    def delete_file(
        self,
        owner_id: str,
        file_id: str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
    ) -> bool:
        owner_id = normalize_owner_id(owner_id)
        account_generation = normalize_account_generation(account_generation)
        with account_lifecycle_commit_guard(), self._lock:
            with self.connection() as conn, write_txn(conn):
                row = self._select_owned(conn, owner_id, account_generation, file_id)
                if row is None:
                    return False
                if row["origin_key"]:
                    conn.execute(
                        """
                        INSERT INTO deleted_file_origins (
                            owner_id, account_generation, origin_key, sha256, deleted_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(owner_id, account_generation, origin_key) DO UPDATE SET
                            sha256=excluded.sha256,
                            deleted_at=excluded.deleted_at
                        """,
                        (
                            owner_id,
                            account_generation,
                            row["origin_key"],
                            row["sha256"],
                            self._clock_ms(),
                        ),
                    )
                conn.execute(
                    "DELETE FROM file_upload_reservations WHERE owner_id=? "
                    "AND account_generation=? AND file_id=?",
                    (owner_id, account_generation, file_id),
                )
                conn.execute(
                    "DELETE FROM account_files WHERE id=? AND owner_id=? "
                    "AND account_generation=?",
                    (file_id, owner_id, account_generation),
                )
            self._remove_object_path(str(row["stored_relpath"] or ""))
        return True

    def delete_conversation(
        self,
        owner_id: str,
        conversation_id: str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
    ) -> dict[str, int]:
        """Delete every indexed object produced by one owned conversation."""

        owner_id = normalize_owner_id(owner_id)
        account_generation = normalize_account_generation(account_generation)
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            raise ValueError("Conversation id is required")
        with account_lifecycle_commit_guard(), self._lock, self.connection() as conn, write_txn(conn):
            rows = conn.execute(
                """
                SELECT id, origin_key, sha256, stored_relpath
                FROM account_files
                WHERE owner_id=? AND account_generation=? AND conversation_id=?
                """,
                (owner_id, account_generation, conversation_id),
            ).fetchall()
            for row in rows:
                if row["origin_key"]:
                    conn.execute(
                        """
                        INSERT INTO deleted_file_origins (
                            owner_id, account_generation, origin_key, sha256, deleted_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(owner_id, account_generation, origin_key) DO UPDATE SET
                            sha256=excluded.sha256,
                            deleted_at=excluded.deleted_at
                        """,
                        (
                            owner_id,
                            account_generation,
                            row["origin_key"],
                            row["sha256"],
                            self._clock_ms(),
                        ),
                    )
            conn.execute(
                "DELETE FROM file_upload_reservations WHERE owner_id=? "
                "AND account_generation=? AND file_id IN ("
                "SELECT id FROM account_files WHERE owner_id=? "
                "AND account_generation=? AND conversation_id=?)",
                (
                    owner_id,
                    account_generation,
                    owner_id,
                    account_generation,
                    conversation_id,
                ),
            )
            conn.execute(
                "DELETE FROM account_files WHERE owner_id=? AND account_generation=? "
                "AND conversation_id=?",
                (owner_id, account_generation, conversation_id),
            )
        for row in rows:
            self._remove_object_path(str(row["stored_relpath"] or ""))
        return {"files": len(rows)}

    def delete_owner(
        self,
        owner_id: str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
    ) -> dict[str, int]:
        """Delete one immutable account generation without touching successors."""

        owner_id = normalize_owner_id(owner_id)
        account_generation = normalize_account_generation(account_generation)
        bucket = (
            self.objects_root / self._owner_bucket(owner_id, account_generation)
        ).resolve()
        objects_root = self.objects_root.resolve()
        if not bucket.is_relative_to(objects_root):
            raise ValueError("Account object bucket escapes the library")
        with account_lifecycle_commit_guard(), self._lock:
            objects_removed = int(bucket.exists())
            with self.connection() as conn, write_txn(conn):
                conn.execute(
                    "INSERT INTO deleted_file_owners("
                    "owner_id,account_generation,deleted_at) VALUES(?,?,?) "
                    "ON CONFLICT(owner_id,account_generation) DO UPDATE SET "
                    "deleted_at=excluded.deleted_at",
                    (owner_id, account_generation, self._clock_ms()),
                )
                file_count = int(conn.execute(
                    "SELECT COUNT(*) FROM account_files WHERE owner_id=? "
                    "AND account_generation=?",
                    (owner_id, account_generation),
                ).fetchone()[0])
                origin_count = int(conn.execute(
                    "SELECT COUNT(*) FROM deleted_file_origins WHERE owner_id=? "
                    "AND account_generation=?",
                    (owner_id, account_generation),
                ).fetchone()[0])
                conn.execute(
                    "DELETE FROM account_files WHERE owner_id=? "
                    "AND account_generation=?",
                    (owner_id, account_generation),
                )
                conn.execute(
                    "DELETE FROM deleted_file_origins WHERE owner_id=? "
                    "AND account_generation=?",
                    (owner_id, account_generation),
                )
                conn.execute(
                    "DELETE FROM file_install_intents WHERE owner_id=? "
                    "AND account_generation=?",
                    (owner_id, account_generation),
                )
                conn.execute(
                    "DELETE FROM file_upload_reservations WHERE owner_id=? "
                    "AND account_generation=?",
                    (owner_id, account_generation),
                )
            if bucket.exists():
                shutil.rmtree(bucket)
        return {
            "files": file_count,
            "deleted_origins": origin_count,
            "object_buckets": objects_removed,
        }

    def sync_directory(
        self,
        owner_id: str,
        directory: Path | str,
        *,
        account_generation: str = LEGACY_ACCOUNT_GENERATION,
        source: str,
        conversation_id: str,
        turn_id: str = "",
        profile: str = "",
        origin_prefix: str = "",
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        root = Path(directory)
        account_generation = normalize_account_generation(account_generation)
        if not root.exists():
            return []
        resolved_root = root.resolve(strict=True)
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        for candidate in sorted(root.rglob("*")):
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or (candidate.name.startswith(".") and candidate.name.endswith(".upload"))
            ):
                continue
            try:
                before = candidate.stat()
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(resolved_root):
                    continue
                relative = resolved.relative_to(resolved_root).as_posix()
                origin_key = f"{origin_prefix}:{relative}" if origin_prefix else relative
                effective_turn_id = str(turn_id or "").strip()
                if not effective_turn_id and origin_key:
                    existing = self.get_file_by_origin(
                        owner_id,
                        origin_key,
                        account_generation=account_generation,
                    )
                    if existing is not None:
                        effective_turn_id = str(existing.get("turn_id") or "").strip()
                record = self.ingest_file(
                    owner_id,
                    resolved,
                    account_generation=account_generation,
                    name=candidate.name,
                    source=source,
                    conversation_id=conversation_id,
                    turn_id=effective_turn_id,
                    profile=profile,
                    origin_key=origin_key,
                    allowed_roots=[resolved_root],
                    restore_deleted=False,
                )
                if record is not None:
                    records.append(record)
                after = candidate.stat()
                if (
                    before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                ):
                    errors.append(f"{candidate}: changed during indexing")
            except (OSError, ValueError) as exc:
                errors.append(f"{candidate}: {exc}")
                continue
        if strict and errors:
            raise OSError(
                "Directory indexing was incomplete: " + "; ".join(errors[:8])
            )
        return records
