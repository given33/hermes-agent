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

from hermes_cli.config import get_hermes_home
from hermes_cli.sqlite_util import write_txn


SCHEMA_VERSION = 2
LOCAL_OWNER_ID = "local-owner"
FILE_SOURCES = frozenset({"user_upload", "model_output"})
FILE_STATUSES = frozenset({"uploading", "available", "failed"})
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
    ON account_files(owner_id, origin_key)
    WHERE origin_key <> '';
CREATE INDEX IF NOT EXISTS idx_account_files_owner_created
    ON account_files(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_files_owner_updated
    ON account_files(owner_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_files_owner_source
    ON account_files(owner_id, source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_files_owner_type
    ON account_files(owner_id, file_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_files_conversation
    ON account_files(owner_id, conversation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS deleted_file_origins (
    owner_id        TEXT NOT NULL,
    origin_key      TEXT NOT NULL,
    sha256          TEXT NOT NULL DEFAULT '',
    deleted_at      INTEGER NOT NULL,
    PRIMARY KEY(owner_id, origin_key)
);
"""


def normalize_owner_id(value: Any) -> str:
    owner_id = str(value or "").strip().replace("\x00", "")
    return owner_id[:512] or LOCAL_OWNER_ID


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
        raise ValueError("File status must be uploading, available, or failed")
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

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects_root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            if not self._schema_ready:
                conn.executescript(_SCHEMA_SQL)
                current = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if current > SCHEMA_VERSION:
                    conn.close()
                    raise RuntimeError(
                        "Cloud file library was created by a newer Hermes version "
                        f"(schema {current} > {SCHEMA_VERSION})"
                    )
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._schema_ready = True
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

    def _owner_bucket(self, owner_id: str) -> str:
        return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32]

    def _destination(self, owner_id: str, file_id: str, filename: str) -> tuple[Path, str]:
        relative = Path("objects") / self._owner_bucket(owner_id) / file_id / filename
        target = (self.root / relative).resolve()
        root = self.root.resolve()
        if not target.is_relative_to(root):
            raise ValueError("File storage path escapes the account library")
        return target, relative.as_posix()

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

    def _select_owned(
        self,
        conn: sqlite3.Connection,
        owner_id: str,
        file_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM account_files WHERE id=? AND owner_id=?",
            (file_id, owner_id),
        ).fetchone()

    def reserve_file(
        self,
        owner_id: str,
        *,
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
        with self._lock, self.connection() as conn, write_txn(conn):
            existing = None
            if file_id:
                existing = self._select_owned(conn, owner_id, file_id)
                if existing is None:
                    raise KeyError(file_id)
            elif metadata["origin_key"]:
                existing = conn.execute(
                    "SELECT * FROM account_files WHERE owner_id=? AND origin_key=?",
                    (owner_id, metadata["origin_key"]),
                ).fetchone()
            if existing is not None:
                file_id = str(existing["id"])
                conn.execute(
                    """
                    UPDATE account_files
                    SET name=?, mime_type=?, extension=?, file_type=?, source=?,
                        status='uploading', conversation_id=?, message_id=?,
                        turn_id=?, profile=?, origin_key=?, error='', updated_at=?
                    WHERE id=? AND owner_id=?
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
                    ),
                )
            else:
                file_id = file_id or f"file_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO account_files (
                        id, owner_id, name, mime_type, extension, file_type,
                        source, status, conversation_id, message_id, turn_id,
                        profile, origin_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'uploading', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        owner_id,
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
                    "DELETE FROM deleted_file_origins WHERE owner_id=? AND origin_key=?",
                    (owner_id, metadata["origin_key"]),
                )
            row = self._select_owned(conn, owner_id, file_id)
        return dict(row)

    def ingest_file(
        self,
        owner_id: str,
        source_path: Path | str,
        *,
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
    ) -> dict[str, Any] | None:
        owner_id = normalize_owner_id(owner_id)
        normalized_source = normalize_source(source)
        source_path = self._validate_source_path(source_path, allowed_roots)
        name = safe_file_name(name or source_path.name)
        digest, size = self._hash_file(source_path)
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

        with self._lock:
            with self.connection() as conn:
                if metadata["origin_key"]:
                    tombstone = conn.execute(
                        """
                        SELECT sha256 FROM deleted_file_origins
                        WHERE owner_id=? AND origin_key=?
                        """,
                        (owner_id, metadata["origin_key"]),
                    ).fetchone()
                    if (
                        tombstone is not None
                        and not restore_deleted
                        and str(tombstone["sha256"] or "") == digest
                    ):
                        return None
                existing = None
                if file_id:
                    existing = self._select_owned(conn, owner_id, file_id)
                    if existing is None:
                        raise KeyError(file_id)
                elif metadata["origin_key"]:
                    existing = conn.execute(
                        "SELECT * FROM account_files WHERE owner_id=? AND origin_key=?",
                        (owner_id, metadata["origin_key"]),
                    ).fetchone()
                if existing is not None:
                    file_id = str(existing["id"])
                    existing_path_ok = False
                    if existing["stored_relpath"]:
                        try:
                            existing_path_ok = self._record_path(dict(existing)).is_file()
                        except ValueError:
                            existing_path_ok = False
                    unchanged = (
                        existing_path_ok
                        and existing["status"] == "available"
                        and existing["sha256"] == digest
                        and existing["name"] == name
                    )
                    metadata_unchanged = all(
                        str(existing[key]) == value for key, value in metadata.items()
                    ) and existing["source"] == normalized_source
                    if unchanged and metadata_unchanged:
                        return dict(existing)
                else:
                    file_id = file_id or f"file_{uuid.uuid4().hex}"

            target, relative = self._destination(owner_id, file_id, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Keep the atomic sibling temp short enough for Windows MAX_PATH
            # when pytest or a user chooses a deeply nested HERMES_HOME.
            temp = target.with_name(f".upload-{uuid.uuid4().hex[:12]}")
            old_relative = str(existing["stored_relpath"] or "") if existing else ""
            try:
                with source_path.open("rb") as source_handle, temp.open("xb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                    target_handle.flush()
                    os.fsync(target_handle.fileno())
                os.replace(temp, target)
                now = self._clock_ms()
                with self.connection() as conn, write_txn(conn):
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO account_files (
                                id, owner_id, name, stored_relpath, sha256,
                                mime_type, extension, file_type, size, source,
                                status, conversation_id, message_id, turn_id,
                                profile, origin_key, error, created_at,
                                updated_at, available_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available',
                                      ?, ?, ?, ?, ?, '', ?, ?, ?)
                            """,
                            (
                                file_id,
                                owner_id,
                                name,
                                relative,
                                digest,
                                mime_type,
                                extension,
                                file_type,
                                size,
                                normalized_source,
                                metadata["conversation_id"],
                                metadata["message_id"],
                                metadata["turn_id"],
                                metadata["profile"],
                                metadata["origin_key"],
                                now,
                                now,
                                now,
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE account_files
                            SET name=?, stored_relpath=?, sha256=?, mime_type=?,
                                extension=?, file_type=?, size=?, source=?,
                                status='available', conversation_id=?, message_id=?,
                                turn_id=?, profile=?, origin_key=?, error='',
                                updated_at=?, available_at=?
                            WHERE id=? AND owner_id=?
                            """,
                            (
                                name,
                                relative,
                                digest,
                                mime_type,
                                extension,
                                file_type,
                                size,
                                normalized_source,
                                metadata["conversation_id"],
                                metadata["message_id"],
                                metadata["turn_id"],
                                metadata["profile"],
                                metadata["origin_key"],
                                now,
                                now,
                                file_id,
                                owner_id,
                            ),
                        )
                    if metadata["origin_key"]:
                        conn.execute(
                            "DELETE FROM deleted_file_origins WHERE owner_id=? AND origin_key=?",
                            (owner_id, metadata["origin_key"]),
                        )
                    row = self._select_owned(conn, owner_id, file_id)
            finally:
                temp.unlink(missing_ok=True)

            if old_relative and old_relative != relative:
                self._remove_object_path(old_relative)
            return dict(row)

    def set_status(
        self,
        owner_id: str,
        file_id: str,
        status: str,
        *,
        error: str = "",
    ) -> dict[str, Any]:
        owner_id = normalize_owner_id(owner_id)
        status = normalize_status(status)
        if status == "available":
            raise ValueError("Complete an available file by ingesting its bytes")
        now = self._clock_ms()
        with self.connection() as conn, write_txn(conn):
            row = self._select_owned(conn, owner_id, file_id)
            if row is None:
                raise KeyError(file_id)
            conn.execute(
                "UPDATE account_files SET status=?, error=?, updated_at=? WHERE id=? AND owner_id=?",
                (status, self._clean_metadata(error, 2000), now, file_id, owner_id),
            )
            row = self._select_owned(conn, owner_id, file_id)
        return dict(row)

    def update_links(
        self,
        owner_id: str,
        file_ids: Sequence[str],
        *,
        conversation_id: str = "",
        message_id: str = "",
        turn_id: str = "",
        profile: str = "",
    ) -> int:
        owner_id = normalize_owner_id(owner_id)
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
        with self.connection() as conn, write_txn(conn):
            cursor = conn.execute(
                f"""
                UPDATE account_files
                SET {', '.join(assignments)}, updated_at=?
                WHERE owner_id=? AND id IN ({placeholders})
                """,
                (*values, now, owner_id, *ids),
            )
        return int(cursor.rowcount)

    def get_file(self, owner_id: str, file_id: str) -> dict[str, Any] | None:
        owner_id = normalize_owner_id(owner_id)
        with self.connection() as conn:
            return self._row_dict(self._select_owned(conn, owner_id, file_id))

    def get_file_by_origin(
        self,
        owner_id: str,
        origin_key: str,
    ) -> dict[str, Any] | None:
        owner_id = normalize_owner_id(owner_id)
        normalized_origin = self._clean_metadata(origin_key, 1024)
        if not normalized_origin:
            return None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM account_files WHERE owner_id=? AND origin_key=?",
                (owner_id, normalized_origin),
            ).fetchone()
        return self._row_dict(row)

    def list_files(
        self,
        owner_id: str,
        *,
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
        clauses = ["owner_id=?"]
        values: list[Any] = [owner_id]
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
        return target

    def resolve_download(self, owner_id: str, file_id: str) -> tuple[dict[str, Any], Path]:
        record = self.get_file(owner_id, file_id)
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
        parent = target.parent
        if parent != self.objects_root.resolve():
            try:
                parent.rmdir()
            except OSError:
                pass

    def delete_file(self, owner_id: str, file_id: str) -> bool:
        owner_id = normalize_owner_id(owner_id)
        with self._lock, self.connection() as conn, write_txn(conn):
            row = self._select_owned(conn, owner_id, file_id)
            if row is None:
                return False
            if row["origin_key"]:
                conn.execute(
                    """
                    INSERT INTO deleted_file_origins (
                        owner_id, origin_key, sha256, deleted_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(owner_id, origin_key) DO UPDATE SET
                        sha256=excluded.sha256,
                        deleted_at=excluded.deleted_at
                    """,
                    (
                        owner_id,
                        row["origin_key"],
                        row["sha256"],
                        self._clock_ms(),
                    ),
                )
            conn.execute(
                "DELETE FROM account_files WHERE id=? AND owner_id=?",
                (file_id, owner_id),
            )
        self._remove_object_path(str(row["stored_relpath"] or ""))
        return True

    def sync_directory(
        self,
        owner_id: str,
        directory: Path | str,
        *,
        source: str,
        conversation_id: str,
        turn_id: str = "",
        profile: str = "",
        origin_prefix: str = "",
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        root = Path(directory)
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
                    existing = self.get_file_by_origin(owner_id, origin_key)
                    if existing is not None:
                        effective_turn_id = str(existing.get("turn_id") or "").strip()
                record = self.ingest_file(
                    owner_id,
                    resolved,
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
