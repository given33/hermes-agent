"""Account-scoped encrypted retention for complete hosted tool output."""

from __future__ import annotations

from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import time
from typing import Any
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SCHEMA_VERSION = 3
DEFAULT_RETENTION_SECONDS = 365 * 24 * 60 * 60


class EncryptedToolArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.data_root = self.root / "tool-output-artifacts"
        self.db_path = self.root / "tool-output-artifacts.db"
        self.key_path = self.root / "secrets" / "tool-output-artifact.key"

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            wal_deadline = time.monotonic() + 30.0
            while True:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= wal_deadline:
                        raise
                    # SQLite's busy_timeout is not consistently applied while
                    # two first-open connections both negotiate journal mode.
                    time.sleep(0.01)
            conn.execute("PRAGMA foreign_keys=ON")
            # Schema creation and migration are part of one SQLite writer
            # transaction. Independent store instances may first connect at
            # the same time; DDL outside this lock races DROP/CREATE INDEX.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tool_output_artifacts (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                account_generation TEXT NOT NULL DEFAULT 'legacy',
                conversation_id TEXT NOT NULL DEFAULT '',
                turn_id TEXT NOT NULL DEFAULT '',
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                stored_relpath TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                retained_until INTEGER NOT NULL
            )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tool_output_owner_directories (
                owner_id TEXT NOT NULL,
                account_generation TEXT NOT NULL,
                directory_name TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(owner_id, account_generation)
            )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tool_output_owner_tombstones (
                owner_id TEXT NOT NULL,
                account_generation TEXT NOT NULL,
                deleted_at INTEGER NOT NULL,
                PRIMARY KEY(owner_id, account_generation)
            )"""
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(tool_output_artifacts)").fetchall()
            }
            if "account_generation" not in columns:
                conn.execute(
                    "ALTER TABLE tool_output_artifacts "
                    "ADD COLUMN account_generation TEXT NOT NULL DEFAULT 'legacy'"
                )
            conn.execute("DROP INDEX IF EXISTS idx_tool_output_owner_call")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_output_owner_generation_call "
                "ON tool_output_artifacts(owner_id, account_generation, conversation_id, turn_id, tool_call_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_output_owner_created "
                "ON tool_output_artifacts(owner_id, account_generation, created_at DESC)"
            )
            for row in conn.execute(
                "SELECT DISTINCT owner_id,account_generation FROM tool_output_artifacts"
            ).fetchall():
                owner = str(row["owner_id"])
                generation = str(row["account_generation"] or "legacy")
                conn.execute(
                    "INSERT OR IGNORE INTO tool_output_owner_directories("
                    "owner_id,account_generation,directory_name,created_at) VALUES(?,?,?,?)",
                    (
                        owner,
                        generation,
                        self._owner_directory_name(owner, generation),
                        int(time.time()),
                    ),
                )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
            return conn
        except BaseException:
            try:
                conn.rollback()
            finally:
                conn.close()
            raise

    @staticmethod
    def _write_master_key_candidate(path: Path, candidate: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(candidate)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _master_key(self) -> bytes:
        self.key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging_pattern = f".{self.key_path.name}.*.tmp"
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                try:
                    key = self.key_path.read_bytes()
                except FileNotFoundError:
                    key = b""
                if key and len(key) != 32:
                    artifact_count = int(
                        conn.execute("SELECT COUNT(*) FROM tool_output_artifacts").fetchone()[0]
                    )
                    if artifact_count:
                        raise RuntimeError("tool output artifact master key is invalid")
                    self.key_path.unlink(missing_ok=True)
                    key = b""
                for stale in self.key_path.parent.glob(staging_pattern):
                    stale.unlink(missing_ok=True)
                if not key:
                    candidate = secrets.token_bytes(32)
                    temporary = self.key_path.parent / (
                        f".{self.key_path.name}.{uuid.uuid4().hex}.tmp"
                    )
                    try:
                        self._write_master_key_candidate(temporary, candidate)
                        os.replace(temporary, self.key_path)
                        self._fsync_directory(self.key_path.parent)
                    finally:
                        temporary.unlink(missing_ok=True)
                    key = self.key_path.read_bytes()
                if len(key) != 32:
                    raise RuntimeError("tool output artifact master key is invalid")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        return key

    def _owner_key(self, owner_id: str, account_generation: str) -> bytes:
        generation = str(account_generation or "legacy").strip() or "legacy"
        material = b"hermes-tool-output\0" + owner_id.encode("utf-8")
        if generation != "legacy":
            material += b"\0generation\0" + generation.encode("utf-8")
        return hmac.new(
            self._master_key(),
            material,
            hashlib.sha256,
        ).digest()

    @staticmethod
    def _owner_directory_name(owner_id: str, account_generation: str) -> str:
        generation = str(account_generation or "legacy").strip() or "legacy"
        boundary = owner_id if generation == "legacy" else f"{owner_id}\0{generation}"
        return hashlib.sha256(boundary.encode("utf-8")).hexdigest()

    def _register_owner_directory(
        self,
        owner_id: str,
        account_generation: str,
        directory_name: str,
    ) -> None:
        """Commit cleanup provenance before any ciphertext can be installed."""

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            tombstone = conn.execute(
                "SELECT 1 FROM tool_output_owner_tombstones "
                "WHERE owner_id=? AND account_generation=?",
                (owner_id, account_generation),
            ).fetchone()
            if tombstone is not None:
                conn.rollback()
                raise RuntimeError("account generation has been deleted")
            existing = conn.execute(
                "SELECT directory_name FROM tool_output_owner_directories "
                "WHERE owner_id=? AND account_generation=?",
                (owner_id, account_generation),
            ).fetchone()
            if existing is not None and not hmac.compare_digest(
                str(existing["directory_name"]), directory_name
            ):
                conn.rollback()
                raise RuntimeError("tool output owner directory boundary mismatch")
            conn.execute(
                "INSERT OR IGNORE INTO tool_output_owner_directories("
                "owner_id,account_generation,directory_name,created_at) VALUES(?,?,?,?)",
                (owner_id, account_generation, directory_name, int(time.time())),
            )
            conn.commit()

    def put(
        self,
        *,
        owner_id: str,
        account_generation: str = "legacy",
        conversation_id: str,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
    ) -> dict[str, Any]:
        owner = str(owner_id or "").strip()
        generation = str(account_generation or "").strip() or "legacy"
        call_id = str(tool_call_id or "").strip()
        if not owner or not call_id or "\x00" in owner + generation:
            raise ValueError("owner_id, account_generation, and tool_call_id are required")
        plaintext = str(content).encode("utf-8")
        digest = hashlib.sha256(plaintext).hexdigest()
        # One tool call owns one stable public artifact id. Ciphertext paths are
        # versioned below so an index switch never points at bytes installed by
        # a different concurrent writer.
        identity = "\0".join((owner, generation, conversation_id, turn_id, call_id))
        artifact_id = "toolout_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        owner_directory_name = self._owner_directory_name(owner, generation)
        owner_dir = self.data_root / owner_directory_name
        now = int(time.time())
        retained_until = now + max(3600, int(retention_seconds))
        record = {
            "id": artifact_id,
            "owner_id": owner,
            "account_generation": generation,
            "conversation_id": str(conversation_id or ""),
            "turn_id": str(turn_id or ""),
            "tool_call_id": call_id,
            "tool_name": str(tool_name or ""),
            "sha256": digest,
        }
        aad_record = _artifact_aad_record(record)
        aad = json.dumps(aad_record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._owner_key(owner, generation)).encrypt(nonce, plaintext, aad)
        self._register_owner_directory(owner, generation, owner_directory_name)
        # Keep the version component short enough for legacy Windows path
        # limits in deeply nested runtime/test roots while retaining 48 bits of
        # collision-resistant entropy under the serialized writer lock.
        write_token = uuid.uuid4().hex[:12]
        relpath = f"{owner_dir.name}/{artifact_id}.{write_token}.aesgcm"
        target = self.data_root / relpath
        temp = owner_dir / f".{artifact_id}.{write_token}.tmp"
        old_target: Path | None = None
        published = False
        try:
            with closing(self._connect()) as conn:
                # Keep the database write lock across file installation and
                # index publication. This serializes the same identity across
                # threads, processes, and independent store instances. The
                # versioned target means rollback leaves the previous row and
                # previous ciphertext paired.
                conn.execute("BEGIN IMMEDIATE")
                tombstone = conn.execute(
                    "SELECT 1 FROM tool_output_owner_tombstones "
                    "WHERE owner_id=? AND account_generation=?",
                    (owner, generation),
                ).fetchone()
                if tombstone is not None:
                    conn.rollback()
                    raise RuntimeError("account generation has been deleted")
                owner_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                existing = conn.execute(
                    "SELECT * FROM tool_output_artifacts WHERE id=?",
                    (artifact_id,),
                ).fetchone()
                self._remove_unindexed_versions_locked(
                    conn,
                    owner_dir=owner_dir,
                    artifact_id=artifact_id,
                )
                if existing is not None:
                    existing_target = self.data_root / str(existing["stored_relpath"])
                    if (
                        str(existing["state"]) == "available"
                        and existing_target.is_file()
                        and hmac.compare_digest(str(existing["sha256"]), digest)
                    ):
                        conn.rollback()
                        return dict(existing)
                    old_target = existing_target

                with temp.open("wb") as handle:
                    handle.write(nonce + ciphertext)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, target)

                conn.execute(
                    "INSERT INTO tool_output_artifacts("
                    "id,owner_id,account_generation,conversation_id,turn_id,tool_call_id,tool_name,"
                    "sha256,size_bytes,stored_relpath,state,created_at,retained_until"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?, 'available',?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "owner_id=excluded.owner_id,account_generation=excluded.account_generation,"
                    "conversation_id=excluded.conversation_id,turn_id=excluded.turn_id,"
                    "tool_call_id=excluded.tool_call_id,tool_name=excluded.tool_name,"
                    "sha256=excluded.sha256,size_bytes=excluded.size_bytes,"
                    "stored_relpath=excluded.stored_relpath,state='available',"
                    "created_at=excluded.created_at,retained_until=excluded.retained_until",
                    (
                        artifact_id,
                        owner,
                        generation,
                        str(conversation_id or ""),
                        str(turn_id or ""),
                        call_id,
                        str(tool_name or ""),
                        digest,
                        len(plaintext),
                        relpath,
                        now,
                        retained_until,
                    ),
                )
                conn.commit()
                published = True
        except BaseException:
            temp.unlink(missing_ok=True)
            if not published:
                target.unlink(missing_ok=True)
            raise
        finally:
            temp.unlink(missing_ok=True)

        if old_target is not None and old_target != target:
            old_target.unlink(missing_ok=True)
        return {**record, "size_bytes": len(plaintext), "state": "available", "created_at": now, "retained_until": retained_until}

    def _remove_unindexed_versions_locked(
        self,
        conn: sqlite3.Connection,
        *,
        owner_dir: Path,
        artifact_id: str,
    ) -> None:
        """Remove crash orphans while the SQLite writer lock excludes puts."""

        referenced = {
            str(row["stored_relpath"])
            for row in conn.execute(
                "SELECT stored_relpath FROM tool_output_artifacts WHERE id=?",
                (artifact_id,),
            ).fetchall()
        }
        for candidate in owner_dir.glob(f"{artifact_id}*.aesgcm"):
            relpath = candidate.relative_to(self.data_root).as_posix()
            if relpath not in referenced:
                candidate.unlink(missing_ok=True)
        for candidate in owner_dir.glob(f".{artifact_id}.*.tmp"):
            candidate.unlink(missing_ok=True)

    def read(
        self,
        owner_id: str,
        artifact_id: str,
        *,
        account_generation: str = "legacy",
    ) -> bytes:
        owner = str(owner_id or "").strip()
        generation = str(account_generation or "").strip() or "legacy"
        now = int(time.time())
        artifact = str(artifact_id or "")
        for attempt in range(3):
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT * FROM tool_output_artifacts WHERE id=? AND owner_id=? "
                    "AND account_generation=? AND state='available' AND retained_until>?",
                    (artifact, owner, generation, now),
                ).fetchone()
            if row is None:
                raise FileNotFoundError(artifact_id)
            record = dict(row)
            try:
                encrypted = (self.data_root / str(record["stored_relpath"])).read_bytes()
                if len(encrypted) < 13:
                    raise RuntimeError("encrypted tool output artifact is truncated")
                aad_record = _artifact_aad_record(record)
                aad = json.dumps(aad_record, sort_keys=True, separators=(",", ":")).encode("utf-8")
                plaintext = AESGCM(self._owner_key(owner, generation)).decrypt(
                    encrypted[:12], encrypted[12:], aad
                )
                if not hmac.compare_digest(
                    hashlib.sha256(plaintext).hexdigest(), str(record["sha256"])
                ):
                    raise RuntimeError("tool output artifact hash mismatch")
                return plaintext
            except (FileNotFoundError, RuntimeError):
                # A reader can capture the old row immediately before a writer
                # atomically publishes a new version and removes the old file.
                # Retry only when the authoritative index actually changed;
                # persistent corruption still fails closed.
                with closing(self._connect()) as conn:
                    current = conn.execute(
                        "SELECT stored_relpath,sha256,state FROM tool_output_artifacts "
                        "WHERE id=? AND owner_id=? AND account_generation=?",
                        (artifact, owner, generation),
                    ).fetchone()
                changed = (
                    current is not None
                    and str(current["state"]) == "available"
                    and (
                        str(current["stored_relpath"]) != str(record["stored_relpath"])
                        or str(current["sha256"]) != str(record["sha256"])
                    )
                )
                if not changed or attempt == 2:
                    raise
        raise FileNotFoundError(artifact_id)

    def list_owner(
        self,
        owner_id: str,
        *,
        account_generation: str = "legacy",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        owner = str(owner_id or "").strip()
        generation = str(account_generation or "").strip() or "legacy"
        now = int(time.time())
        self.purge_expired(now=now)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id,account_generation,conversation_id,turn_id,tool_call_id,tool_name,sha256,"
                "size_bytes,state,created_at,retained_until "
                "FROM tool_output_artifacts WHERE owner_id=? AND account_generation=? "
                "AND state='available' AND retained_until>? "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (
                    owner,
                    generation,
                    now,
                    min(1000, max(1, int(limit))),
                    max(0, int(offset)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_owner(
        self,
        owner_id: str,
        *,
        account_generation: str = "legacy",
    ) -> int:
        owner = str(owner_id or "").strip()
        generation = str(account_generation or "").strip() or "legacy"
        now = int(time.time())
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM tool_output_artifacts "
                "WHERE owner_id=? AND account_generation=? AND state='available' "
                "AND retained_until>?",
                (owner, generation, now),
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    def metadata(
        self,
        owner_id: str,
        artifact_id: str,
        *,
        account_generation: str = "legacy",
    ) -> dict[str, Any] | None:
        owner = str(owner_id or "").strip()
        generation = str(account_generation or "").strip() or "legacy"
        now = int(time.time())
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id,account_generation,conversation_id,turn_id,tool_call_id,tool_name,sha256,"
                "size_bytes,state,created_at,retained_until "
                "FROM tool_output_artifacts WHERE owner_id=? AND account_generation=? "
                "AND id=? AND state='available' AND retained_until>?",
                (owner, generation, str(artifact_id or ""), now),
            ).fetchone()
        return dict(row) if row is not None else None

    def delete(
        self,
        owner_id: str,
        artifact_id: str,
        *,
        account_generation: str = "legacy",
    ) -> bool:
        owner = str(owner_id or "").strip()
        generation = str(account_generation or "").strip() or "legacy"
        artifact = str(artifact_id or "").strip()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT stored_relpath FROM tool_output_artifacts WHERE id=? "
                "AND owner_id=? AND account_generation=? AND state='available'",
                (artifact, owner, generation),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE tool_output_artifacts SET state='deleting' WHERE id=? "
                "AND owner_id=? AND account_generation=? AND state='available'",
                (artifact, owner, generation),
            )
            conn.commit()

        path = self.data_root / str(row["stored_relpath"])
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM tool_output_artifacts WHERE id=? AND owner_id=? "
                "AND account_generation=? AND state='deleting'",
                (artifact, owner, generation),
            )
            conn.commit()
        return True

    def purge_expired(
        self,
        *,
        now: int | None = None,
        limit: int = 1000,
    ) -> dict[str, int]:
        """Remove expired artifacts after durably claiming their rows.

        Read queries also enforce ``retained_until``. A crash during file
        deletion therefore leaves an invisible ``deleting`` row that the next
        purge safely resumes instead of reviving expired output.
        """

        cutoff = int(time.time()) if now is None else int(now)
        bounded_limit = min(10_000, max(1, int(limit)))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id,stored_relpath FROM tool_output_artifacts "
                "WHERE ((retained_until<=? AND state IN ('available','staging')) "
                "OR state='deleting') ORDER BY retained_until ASC, id ASC LIMIT ?",
                (cutoff, bounded_limit),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE tool_output_artifacts SET state='deleting' "
                    f"WHERE id IN ({placeholders})",
                    ids,
                )
            conn.commit()

        for row in rows:
            path = self.data_root / str(row["stored_relpath"])
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass

        if ids:
            with closing(self._connect()) as conn:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM tool_output_artifacts "
                    f"WHERE id IN ({placeholders}) AND state='deleting'",
                    ids,
                )
                conn.commit()
        return {"artifacts": len(ids)}

    def delete_owner(
        self,
        owner_id: str,
        *,
        account_generation: str,
        include_known_generations: bool = False,
    ) -> dict[str, int]:
        owner = str(owner_id or "").strip()
        normalized_generation = str(account_generation or "").strip()
        if not owner or not normalized_generation or "\x00" in owner + normalized_generation:
            raise ValueError("owner_id and account_generation are required")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            generations = {normalized_generation}
            if include_known_generations:
                generations.update(
                    str(row[0])
                    for row in conn.execute(
                        "SELECT account_generation FROM tool_output_owner_directories "
                        "WHERE owner_id=? UNION SELECT account_generation "
                        "FROM tool_output_artifacts WHERE owner_id=? UNION SELECT account_generation "
                        "FROM tool_output_owner_tombstones WHERE owner_id=?",
                        (owner, owner, owner),
                    ).fetchall()
                    if str(row[0] or "")
                )
            now = int(time.time())
            conn.executemany(
                "INSERT INTO tool_output_owner_tombstones("
                "owner_id,account_generation,deleted_at) VALUES(?,?,?) "
                "ON CONFLICT(owner_id,account_generation) DO UPDATE SET "
                "deleted_at=excluded.deleted_at",
                [(owner, item, now) for item in sorted(generations)],
            )
            if include_known_generations:
                where = "owner_id=?"
                params: tuple[Any, ...] = (owner,)
            else:
                where = "owner_id=? AND account_generation=?"
                params = (owner, normalized_generation)
            rows = conn.execute(
                "SELECT account_generation,stored_relpath FROM tool_output_artifacts WHERE " + where,
                params,
            ).fetchall()
            directory_rows = conn.execute(
                "SELECT account_generation,directory_name FROM "
                "tool_output_owner_directories WHERE " + where,
                params,
            ).fetchall()
            conn.execute(
                "UPDATE tool_output_artifacts SET state='deleting' WHERE " + where,
                params,
            )
            conn.commit()

        directories = {
            (
                str(row["account_generation"] or "legacy"),
                str(row["directory_name"]),
            )
            for row in directory_rows
        }
        for row in rows:
            row_generation = str(row["account_generation"] or "legacy")
            relpath = Path(str(row["stored_relpath"]))
            if len(relpath.parts) >= 2:
                directories.add((row_generation, relpath.parts[0]))
            expected = self._owner_directory_name(owner, row_generation)
            if len(relpath.parts) < 2 or not hmac.compare_digest(
                relpath.parts[0], expected
            ):
                raise RuntimeError("tool output artifact path boundary mismatch")
            (self.data_root / relpath).unlink(missing_ok=True)
        for row_generation, directory_name in directories:
            expected = self._owner_directory_name(owner, row_generation)
            if not hmac.compare_digest(directory_name, expected):
                raise RuntimeError("tool output owner directory boundary mismatch")
            directory = self.data_root / directory_name
            if directory.is_symlink():
                raise RuntimeError("tool output owner directory is unsafe")
            if directory.exists():
                if not directory.is_dir():
                    raise RuntimeError("tool output owner directory is unsafe")
                shutil.rmtree(directory)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM tool_output_artifacts WHERE " + where + " AND state='deleting'",
                params,
            )
            conn.execute(
                "DELETE FROM tool_output_owner_directories WHERE " + where,
                params,
            )
            conn.commit()
        return {"artifacts": len(rows)}


def _artifact_aad_record(record: dict[str, Any]) -> dict[str, Any]:
    """Keep v1 legacy ciphertext readable while fencing every newer account."""

    aad = {
        key: record[key]
        for key in (
            "id",
            "owner_id",
            "conversation_id",
            "turn_id",
            "tool_call_id",
            "tool_name",
            "sha256",
        )
    }
    generation = str(record.get("account_generation") or "legacy").strip() or "legacy"
    if generation != "legacy":
        aad["account_generation"] = generation
    return aad
