"""Account-scoped durable store for Memory and Skill write approvals.

The original Write Gate pending store used one directory of JSON files per
Hermes home.  That was adequate for the local CLI, but it did not provide an
account boundary, compare-and-swap decisions, or an auditable terminal state.
This store is deliberately profile-local and still records both ``owner_id``
and ``profile`` on every row so a mobile/dashboard caller can never enumerate
another account's pending writes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from hermes_constants import get_hermes_home


_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_write_approval_deletions (
    owner_id TEXT PRIMARY KEY,
    deleted_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS account_write_approvals (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    subsystem TEXT NOT NULL CHECK(subsystem IN ('memory','skills')),
    action TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'foreground',
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN
        ('pending','applying','applied','rejected','expired','failed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decision_token TEXT,
    decision_by TEXT,
    decided_at REAL,
    applied_at REAL,
    last_error TEXT,
    decision_action TEXT,
    decision_revision INTEGER,
    idempotency_key TEXT,
    effect_key TEXT,
    apply_lease_expires_at REAL,
    apply_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_account_write_approvals_scope
    ON account_write_approvals(owner_id, profile, state, created_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_account_write_approvals_scope_id
    ON account_write_approvals(owner_id, profile, id);

CREATE TABLE IF NOT EXISTS account_write_approval_migrations (
    owner_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    subsystem TEXT NOT NULL,
    source_path TEXT NOT NULL,
    migrated_at REAL NOT NULL,
    PRIMARY KEY(owner_id, profile, subsystem, source_path)
);

CREATE TABLE IF NOT EXISTS account_write_approval_effects (
    owner_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    effect_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('prepared','succeeded','failed')),
    plan_json TEXT,
    result_json TEXT,
    execution_token TEXT,
    execution_lease_expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(owner_id, profile, effect_key)
);
"""

_APPLY_LEASE_SECONDS = 60.0
_MAX_OWNER_ID_LENGTH = 512
_MAX_PROFILE_LENGTH = 64
_MIGRATION_COLUMNS = {
    "decision_action": "decision_action TEXT",
    "decision_revision": "decision_revision INTEGER",
    "idempotency_key": "idempotency_key TEXT",
    "effect_key": "effect_key TEXT",
    "apply_lease_expires_at": "apply_lease_expires_at REAL",
    "apply_attempts": "apply_attempts INTEGER NOT NULL DEFAULT 0",
}

_EFFECT_MIGRATION_COLUMNS = {
    "plan_json": "plan_json TEXT",
    "execution_token": "execution_token TEXT",
    "execution_lease_expires_at": "execution_lease_expires_at REAL",
}


class ApprovalConflict(RuntimeError):
    """The requested revision/state no longer owns the approval row."""


class ApprovalAccountDeleted(ApprovalConflict):
    """The account approval scope is permanently fenced."""


class ApprovalDeletionInProgress(RuntimeError):
    """Account cleanup must retry after an active effect lease drains."""


class AccountWriteApprovalStore:
    """SQLite-backed, account-scoped pending write store."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or (get_hermes_home() / "write-approvals.db"))
        self._lock = threading.RLock()
        self._initialize()

    @staticmethod
    def _owner(owner_id: str) -> str:
        if not isinstance(owner_id, str):
            raise ValueError("owner_id must be a string")
        owner = owner_id.strip()
        if not owner:
            raise ValueError("owner_id is required")
        if len(owner) > _MAX_OWNER_ID_LENGTH:
            raise ValueError(
                f"owner_id must be at most {_MAX_OWNER_ID_LENGTH} characters"
            )
        return owner

    @classmethod
    def _scope(cls, owner_id: str, profile: str) -> tuple[str, str]:
        owner = cls._owner(owner_id)
        if not isinstance(profile, str):
            raise ValueError("profile must be a string")
        profile_name = profile.strip().lower()
        if not profile_name:
            raise ValueError("profile is required")
        if len(profile_name) > _MAX_PROFILE_LENGTH:
            raise ValueError(
                f"profile must be at most {_MAX_PROFILE_LENGTH} characters"
            )
        return owner, profile_name

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @staticmethod
    def _assert_owner_active(conn: sqlite3.Connection, owner_id: str) -> None:
        if conn.execute(
            "SELECT 1 FROM account_write_approval_deletions WHERE owner_id=?",
            (owner_id,),
        ).fetchone() is not None:
            raise ApprovalAccountDeleted("account approval scope was deleted")

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(account_write_approvals)"
                ).fetchall()
            }
            for column, ddl in _MIGRATION_COLUMNS.items():
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE account_write_approvals ADD COLUMN {ddl}"
                    )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_account_write_approvals_idempotency "
                "ON account_write_approvals(owner_id,profile,idempotency_key) "
                "WHERE idempotency_key IS NOT NULL"
            )
            effect_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(account_write_approval_effects)"
                ).fetchall()
            }
            for column, ddl in _EFFECT_MIGRATION_COLUMNS.items():
                if column not in effect_columns:
                    conn.execute(
                        f"ALTER TABLE account_write_approval_effects ADD COLUMN {ddl}"
                    )

    @staticmethod
    def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        try:
            payload = json.loads(result.pop("payload_json"))
            result["payload"] = payload if isinstance(payload, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            result["payload"] = {}
            result.pop("payload_json", None)
        return result

    def stage(
        self,
        *,
        owner_id: str,
        profile: str,
        subsystem: str,
        payload: dict[str, Any],
        summary: str,
        origin: str,
        expires_in: float = 30 * 24 * 60 * 60,
        approval_id: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        owner, profile_name = self._scope(owner_id, profile)
        if subsystem not in {"memory", "skills"}:
            raise ValueError("unsupported write approval subsystem")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        now = float(created_at if created_at is not None else time.time())
        expiry = now + max(60.0, float(expires_in))
        item_id = str(approval_id or uuid.uuid4().hex).strip()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_owner_active(conn, owner)
            conn.execute(
                "INSERT INTO account_write_approvals("
                "id,owner_id,profile,subsystem,action,summary,origin,payload_json,"
                "state,revision,created_at,updated_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'pending',1,?,?,?)",
                (
                    item_id,
                    owner,
                    profile_name,
                    subsystem,
                    str(payload.get("action") or "")[:128],
                    str(summary or "").strip()[:2000],
                    str(origin or "foreground")[:128],
                    encoded,
                    now,
                    now,
                    expiry,
                ),
            )
            row = conn.execute(
                "SELECT * FROM account_write_approvals "
                "WHERE owner_id=? AND profile=? AND id=?",
                (owner, profile_name, item_id),
            ).fetchone()
            conn.commit()
        return self._record(row) or {}

    def expire_due(
        self, *, owner_id: str, profile: str, now: float | None = None
    ) -> int:
        owner, profile_name = self._scope(owner_id, profile)
        timestamp = float(time.time() if now is None else now)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE account_write_approvals SET state='expired',revision=revision+1,"
                "updated_at=?,decided_at=?,decision_token=NULL "
                "WHERE owner_id=? AND profile=? AND state='pending' AND expires_at<=?",
                (timestamp, timestamp, owner, profile_name, timestamp),
            )
            conn.commit()
            return int(cursor.rowcount)

    def list(
        self,
        *,
        owner_id: str,
        profile: str,
        subsystem: str | None = None,
        states: Iterable[str] = ("pending",),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        owner, profile_name = self._scope(owner_id, profile)
        self.expire_due(owner_id=owner, profile=profile_name)
        clean_states = tuple(dict.fromkeys(str(value) for value in states))
        if not clean_states:
            return []
        clauses = ["owner_id=?", "profile=?"]
        params: list[Any] = [owner, profile_name]
        clauses.append("state IN (" + ",".join("?" for _ in clean_states) + ")")
        params.extend(clean_states)
        if subsystem is not None:
            if subsystem not in {"memory", "skills"}:
                raise ValueError("unsupported write approval subsystem")
            clauses.append("subsystem=?")
            params.append(subsystem)
        params.append(max(1, min(int(limit), 500)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM account_write_approvals WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at,id LIMIT ?",
                params,
            ).fetchall()
        return [self._record(row) or {} for row in rows]

    def get(
        self, *, owner_id: str, profile: str, approval_id: str
    ) -> dict[str, Any] | None:
        owner, profile_name = self._scope(owner_id, profile)
        self.expire_due(owner_id=owner, profile=profile_name)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM account_write_approvals "
                "WHERE owner_id=? AND profile=? AND id=?",
                (owner, profile_name, str(approval_id)),
            ).fetchone()
        return self._record(row)

    def claim_decision(
        self,
        *,
        owner_id: str,
        profile: str,
        approval_id: str,
        expected_revision: int,
        decision: str,
        decision_by: str,
        idempotency_key: str | None = None,
        lease_seconds: float = _APPLY_LEASE_SECONDS,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Claim or replay one exact decision using a durable request key.

        An approval claim owns a bounded lease, while ``effect_key`` remains
        stable across every recovery attempt.  A caller that loses its process
        after claiming can therefore reacquire the same operation after the
        lease expires without inventing a second side effect identity.
        """

        owner, profile_name = self._scope(owner_id, profile)
        normalized = str(decision).strip().lower()
        if normalized not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        timestamp = float(time.time() if now is None else now)
        lease_duration = max(1.0, float(lease_seconds))
        supplied_key = idempotency_key is not None
        request_key = str(idempotency_key or "").strip()
        if supplied_key and (not request_key or len(request_key) > 256):
            raise ValueError(
                "Idempotency-Key is required and must be at most 256 characters"
            )
        if not request_key:
            request_key = f"legacy:{uuid.uuid4().hex}"
        target_state = "applying" if normalized == "approve" else "rejected"
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_owner_active(conn, owner)
            row = conn.execute(
                "SELECT * FROM account_write_approvals "
                "WHERE owner_id=? AND profile=? AND id=?",
                (owner, profile_name, approval_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise ApprovalConflict("approval revision or state changed")

            stored_key = str(row["idempotency_key"] or "")
            if stored_key:
                if stored_key != request_key:
                    conn.rollback()
                    raise ApprovalConflict("approval already has a different decision")
                if str(row["decision_action"] or "") != normalized or int(
                    row["decision_revision"] or -1
                ) != int(expected_revision):
                    conn.rollback()
                    raise ApprovalConflict(
                        "Idempotency-Key was already used with a different decision"
                    )
                if str(row["state"]) != "applying":
                    conn.commit()
                    record = self._record(row) or {}
                    record["_claim_acquired"] = False
                    record["_idempotent_replay"] = True
                    return record

                lease_expires = float(row["apply_lease_expires_at"] or 0)
                if lease_expires > timestamp:
                    conn.commit()
                    record = self._record(row) or {}
                    record["_claim_acquired"] = False
                    record["_idempotent_replay"] = True
                    return record

                token = uuid.uuid4().hex
                cursor = conn.execute(
                    "UPDATE account_write_approvals SET revision=revision+1,"
                    "updated_at=?,decision_token=?,apply_lease_expires_at=?,"
                    "apply_attempts=apply_attempts+1,last_error=NULL "
                    "WHERE owner_id=? AND profile=? AND id=? AND state='applying' "
                    "AND idempotency_key=? AND apply_lease_expires_at<=?",
                    (
                        timestamp,
                        token,
                        timestamp + lease_duration,
                        owner,
                        profile_name,
                        approval_id,
                        request_key,
                        timestamp,
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise ApprovalConflict("approval apply lease was not acquired")
                row = conn.execute(
                    "SELECT * FROM account_write_approvals "
                    "WHERE owner_id=? AND profile=? AND id=?",
                    (owner, profile_name, approval_id),
                ).fetchone()
                conn.commit()
                record = self._record(row) or {}
                record["_claim_acquired"] = True
                record["_idempotent_replay"] = True
                return record

            key_owner = conn.execute(
                "SELECT id FROM account_write_approvals "
                "WHERE owner_id=? AND profile=? AND idempotency_key=?",
                (owner, profile_name, request_key),
            ).fetchone()
            if key_owner is not None:
                conn.rollback()
                raise ApprovalConflict(
                    "Idempotency-Key was already used for another approval"
                )

            expired = conn.execute(
                "UPDATE account_write_approvals SET state='expired',revision=revision+1,"
                "updated_at=?,decided_at=?,decision_token=NULL "
                "WHERE owner_id=? AND profile=? AND id=? AND state='pending' "
                "AND expires_at<=?",
                (timestamp, timestamp, owner, profile_name, approval_id, timestamp),
            )
            token = uuid.uuid4().hex if normalized == "approve" else None
            effect_key = uuid.uuid4().hex if normalized == "approve" else None
            cursor = conn.execute(
                "UPDATE account_write_approvals SET state=?,revision=revision+1,"
                "updated_at=?,decided_at=?,decision_by=?,decision_token=?,"
                "decision_action=?,decision_revision=?,idempotency_key=?,effect_key=?,"
                "apply_lease_expires_at=?,apply_attempts=? "
                "WHERE owner_id=? AND profile=? AND id=? AND state='pending' "
                "AND revision=? AND expires_at>?",
                (
                    target_state,
                    timestamp,
                    timestamp,
                    str(decision_by or owner)[:512],
                    token,
                    normalized,
                    int(expected_revision),
                    request_key,
                    effect_key,
                    timestamp + lease_duration if normalized == "approve" else None,
                    1 if normalized == "approve" else 0,
                    owner,
                    profile_name,
                    approval_id,
                    int(expected_revision),
                    timestamp,
                ),
            )
            if cursor.rowcount != 1:
                if expired.rowcount:
                    conn.commit()
                else:
                    conn.rollback()
                raise ApprovalConflict("approval revision or state changed")
            row = conn.execute(
                "SELECT * FROM account_write_approvals "
                "WHERE owner_id=? AND profile=? AND id=?",
                (owner, profile_name, approval_id),
            ).fetchone()
            conn.commit()
        record = self._record(row) or {}
        record["_claim_acquired"] = True
        record["_idempotent_replay"] = False
        return record

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def execute_effect(
        self,
        *,
        owner_id: str,
        profile: str,
        approval_id: str,
        decision_token: str,
        effect_key: str,
        payload: dict[str, Any],
        prepare: Callable[[], dict[str, Any]],
        apply: Callable[[dict[str, Any]], tuple[bool, str]],
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Run one claimed side effect once, with a durable replay receipt.

        Once the callback result is committed, a lease recovery returns that
        receipt without invoking the callback again.  The callback and SQLite
        receipt are not one atomic transaction, so adapters must remain
        convergent across a process exit in the narrow post-write/pre-receipt
        window.
        """

        owner, profile_name = self._scope(owner_id, profile)
        timestamp = float(time.time() if now is None else now)
        normalized_effect_key = str(effect_key or "").strip()
        if not normalized_effect_key:
            raise ValueError("effect_key is required")
        payload_hash = self._payload_hash(payload)

        # Persist preparation before entering the external side-effect window.
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_owner_active(conn, owner)
            approval = conn.execute(
                "SELECT state,decision_token,effect_key,apply_lease_expires_at "
                "FROM account_write_approvals "
                "WHERE owner_id=? AND profile=? AND id=?",
                (owner, profile_name, approval_id),
            ).fetchone()
            if (
                approval is None
                or str(approval["state"]) != "applying"
                or str(approval["decision_token"] or "") != str(decision_token)
                or str(approval["effect_key"] or "") != normalized_effect_key
                or float(approval["apply_lease_expires_at"] or 0) <= timestamp
            ):
                conn.rollback()
                raise ApprovalConflict("approval apply claim was lost")
            existing = conn.execute(
                "SELECT approval_id,payload_hash,plan_json,result_json "
                "FROM account_write_approval_effects "
                "WHERE owner_id=? AND profile=? AND effect_key=?",
                (owner, profile_name, normalized_effect_key),
            ).fetchone()
            if existing is not None and str(existing["approval_id"]) != str(
                approval_id
            ):
                conn.rollback()
                raise ApprovalConflict("effect_key belongs to another approval")
            if existing is not None and str(existing["payload_hash"]) != payload_hash:
                conn.rollback()
                raise ApprovalConflict(
                    "effect_key was already used with another payload"
                )
            conn.execute(
                "INSERT OR IGNORE INTO account_write_approval_effects("
                "owner_id,profile,approval_id,effect_key,payload_hash,state,"
                "plan_json,result_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'prepared',NULL,NULL,?,?)",
                (
                    owner,
                    profile_name,
                    approval_id,
                    normalized_effect_key,
                    payload_hash,
                    timestamp,
                    timestamp,
                ),
            )
            receipt = conn.execute(
                "SELECT payload_hash,plan_json,result_json "
                "FROM account_write_approval_effects "
                "WHERE owner_id=? AND profile=? AND approval_id=? AND effect_key=?",
                (owner, profile_name, approval_id, normalized_effect_key),
            ).fetchone()
            conn.commit()

        if receipt is None or str(receipt["payload_hash"]) != payload_hash:
            raise ApprovalConflict("approval effect receipt was lost")
        if receipt["result_json"] is not None:
            try:
                stored = json.loads(str(receipt["result_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ApprovalConflict("approval effect result is invalid") from exc
            if not isinstance(stored, dict):
                raise ApprovalConflict("approval effect result is invalid")
            return bool(stored.get("success")), str(stored.get("error") or "")

        plan_json = str(receipt["plan_json"] or "")
        lease_checked_at = timestamp
        if not plan_json:
            plan = prepare()
            if not isinstance(plan, dict):
                raise ApprovalConflict("approval effect plan must be an object")
            plan_json = json.dumps(
                plan,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            checked_at = float(time.time() if now is None else now)
            with self._lock, self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._assert_owner_active(conn, owner)
                approval = conn.execute(
                    "SELECT state,decision_token,effect_key,apply_lease_expires_at "
                    "FROM account_write_approvals "
                    "WHERE owner_id=? AND profile=? AND id=?",
                    (owner, profile_name, approval_id),
                ).fetchone()
                if (
                    approval is None
                    or str(approval["state"]) != "applying"
                    or str(approval["decision_token"] or "") != str(decision_token)
                    or str(approval["effect_key"] or "") != normalized_effect_key
                    or float(approval["apply_lease_expires_at"] or 0) <= checked_at
                ):
                    conn.rollback()
                    raise ApprovalConflict("approval apply claim was lost")
                conn.execute(
                    "UPDATE account_write_approval_effects SET plan_json=?,updated_at=? "
                    "WHERE owner_id=? AND profile=? AND effect_key=? AND plan_json IS NULL",
                    (
                        plan_json,
                        checked_at,
                        owner,
                        profile_name,
                        normalized_effect_key,
                    ),
                )
                stored_plan = conn.execute(
                    "SELECT plan_json FROM account_write_approval_effects "
                    "WHERE owner_id=? AND profile=? AND approval_id=? AND effect_key=?",
                    (owner, profile_name, approval_id, normalized_effect_key),
                ).fetchone()
                if (
                    stored_plan is None
                    or str(stored_plan["plan_json"] or "") != plan_json
                ):
                    conn.rollback()
                    raise ApprovalConflict("approval effect plan changed")
                conn.commit()
            lease_checked_at = checked_at
        else:
            try:
                plan = json.loads(plan_json)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ApprovalConflict("approval effect plan is invalid") from exc
            if not isinstance(plan, dict):
                raise ApprovalConflict("approval effect plan is invalid")

        # Publish a durable execution lease immediately before entering the
        # filesystem side effect. Account deletion fences new executions, then
        # waits for this lease to be cleared or expire after a crashed worker.
        execution_token = uuid.uuid4().hex
        execution_expires_at = 0.0
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_owner_active(conn, owner)
            approval = conn.execute(
                "SELECT state,decision_token,effect_key,apply_lease_expires_at "
                "FROM account_write_approvals "
                "WHERE owner_id=? AND profile=? AND id=?",
                (owner, profile_name, approval_id),
            ).fetchone()
            if (
                approval is None
                or str(approval["state"]) != "applying"
                or str(approval["decision_token"] or "") != str(decision_token)
                or str(approval["effect_key"] or "") != normalized_effect_key
                or float(approval["apply_lease_expires_at"] or 0)
                <= lease_checked_at
            ):
                conn.rollback()
                raise ApprovalConflict("approval apply claim was lost")
            execution_expires_at = float(
                approval["apply_lease_expires_at"] or 0
            )
            changed = conn.execute(
                "UPDATE account_write_approval_effects SET execution_token=?,"
                "execution_lease_expires_at=?,updated_at=? "
                "WHERE owner_id=? AND profile=? AND approval_id=? AND effect_key=? "
                "AND result_json IS NULL",
                (
                    execution_token,
                    execution_expires_at,
                    lease_checked_at,
                    owner,
                    profile_name,
                    approval_id,
                    normalized_effect_key,
                ),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise ApprovalConflict("approval effect execution was not acquired")
            conn.commit()

        heartbeat_stop = threading.Event()
        remaining_lease = max(0.1, execution_expires_at - lease_checked_at)
        heartbeat_interval = max(0.05, min(5.0, remaining_lease / 3.0))

        def heartbeat() -> None:
            while not heartbeat_stop.wait(heartbeat_interval):
                renewed_at = time.time()
                renewed_until = renewed_at + _APPLY_LEASE_SECONDS
                try:
                    with self._connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        approval_changed = conn.execute(
                            "UPDATE account_write_approvals SET "
                            "apply_lease_expires_at=?,updated_at=? "
                            "WHERE owner_id=? AND profile=? AND id=? "
                            "AND state='applying' AND decision_token=? AND effect_key=?",
                            (
                                renewed_until,
                                renewed_at,
                                owner,
                                profile_name,
                                approval_id,
                                str(decision_token),
                                normalized_effect_key,
                            ),
                        ).rowcount
                        effect_changed = conn.execute(
                            "UPDATE account_write_approval_effects SET "
                            "execution_lease_expires_at=?,updated_at=? "
                            "WHERE owner_id=? AND profile=? AND approval_id=? "
                            "AND effect_key=? AND execution_token=? "
                            "AND result_json IS NULL",
                            (
                                renewed_until,
                                renewed_at,
                                owner,
                                profile_name,
                                approval_id,
                                normalized_effect_key,
                                execution_token,
                            ),
                        ).rowcount
                        if approval_changed != 1 or effect_changed != 1:
                            conn.rollback()
                            return
                        conn.commit()
                except sqlite3.Error:
                    # The original lease remains authoritative; a transient
                    # busy error does not transfer ownership to another worker.
                    continue

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"approval-effect-{approval_id[:12]}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            # The adapter converges from either its recorded precondition or
            # postcondition, preserving crash recovery around the receipt.
            success, error = apply(plan)
        except BaseException:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=max(1.0, heartbeat_interval * 2))
            with self._lock, self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE account_write_approval_effects SET execution_token=NULL,"
                    "execution_lease_expires_at=NULL,updated_at=? "
                    "WHERE owner_id=? AND profile=? AND approval_id=? "
                    "AND effect_key=? AND execution_token=?",
                    (
                        float(time.time() if now is None else now),
                        owner,
                        profile_name,
                        approval_id,
                        normalized_effect_key,
                        execution_token,
                    ),
                )
                conn.commit()
            raise
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=max(1.0, heartbeat_interval * 2))
        completed_at = float(time.time() if now is None else now)
        result = {"success": bool(success), "error": str(error or "")}
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            approval = conn.execute(
                "SELECT state,decision_token,effect_key,apply_lease_expires_at "
                "FROM account_write_approvals "
                "WHERE owner_id=? AND profile=? AND id=?",
                (owner, profile_name, approval_id),
            ).fetchone()
            lease_was_lost = (
                approval is not None
                and str(approval["state"]) == "applying"
                and str(approval["decision_token"] or "") == str(decision_token)
                and str(approval["effect_key"] or "") == normalized_effect_key
                and float(approval["apply_lease_expires_at"] or 0) <= completed_at
            )
            if (
                approval is None
                or str(approval["state"]) != "applying"
                or str(approval["decision_token"] or "") != str(decision_token)
                or str(approval["effect_key"] or "") != normalized_effect_key
                or float(approval["apply_lease_expires_at"] or 0) <= completed_at
            ):
                conn.rollback()
                if lease_was_lost:
                    raise ApprovalConflict("approval apply lease was lost")
                raise ApprovalConflict("approval apply claim was lost")
            changed = conn.execute(
                "UPDATE account_write_approval_effects SET state=?,result_json=?,"
                "execution_token=NULL,execution_lease_expires_at=NULL,updated_at=? "
                "WHERE owner_id=? AND profile=? AND approval_id=? "
                "AND effect_key=? AND result_json IS NULL AND plan_json=? "
                "AND execution_token=?",
                (
                    "succeeded" if success else "failed",
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    completed_at,
                    owner,
                    profile_name,
                    approval_id,
                    normalized_effect_key,
                    plan_json,
                    execution_token,
                ),
            ).rowcount
            if changed != 1:
                stored = conn.execute(
                    "SELECT result_json FROM account_write_approval_effects "
                    "WHERE owner_id=? AND profile=? AND approval_id=? AND effect_key=?",
                    (owner, profile_name, approval_id, normalized_effect_key),
                ).fetchone()
                if stored is None or stored["result_json"] is None:
                    conn.rollback()
                    raise ApprovalConflict("approval effect receipt was lost")
                try:
                    result = json.loads(str(stored["result_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    conn.rollback()
                    raise ApprovalConflict("approval effect result is invalid") from exc
                if not isinstance(result, dict):
                    conn.rollback()
                    raise ApprovalConflict("approval effect result is invalid")
            conn.commit()
        return bool(result.get("success")), str(result.get("error") or "")

    def claim_recoverable_applies(
        self,
        *,
        owner_id: str,
        profile: str,
        limit: int = 20,
        lease_seconds: float = _APPLY_LEASE_SECONDS,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Claim one scope's expired applies without changing effect identity."""

        owner, profile_name = self._scope(owner_id, profile)
        timestamp = float(time.time() if now is None else now)
        lease_duration = max(1.0, float(lease_seconds))
        bounded = max(1, min(int(limit), 100))
        claimed: list[dict[str, Any]] = []
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_owner_active(conn, owner)
            rows = conn.execute(
                "SELECT id FROM account_write_approvals "
                "WHERE owner_id=? AND profile=? AND state='applying' "
                "AND idempotency_key IS NOT NULL AND effect_key IS NOT NULL "
                "AND (apply_lease_expires_at IS NULL OR apply_lease_expires_at<=?) "
                "ORDER BY updated_at,id LIMIT ?",
                (owner, profile_name, timestamp, bounded),
            ).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                changed = conn.execute(
                    "UPDATE account_write_approvals SET revision=revision+1,updated_at=?,"
                    "decision_token=?,apply_lease_expires_at=?,"
                    "apply_attempts=apply_attempts+1,last_error=NULL "
                    "WHERE owner_id=? AND profile=? AND id=? AND state='applying' "
                    "AND (apply_lease_expires_at IS NULL OR apply_lease_expires_at<=?)",
                    (
                        timestamp,
                        token,
                        timestamp + lease_duration,
                        owner,
                        profile_name,
                        row["id"],
                        timestamp,
                    ),
                ).rowcount
                if changed != 1:
                    continue
                refreshed = conn.execute(
                    "SELECT * FROM account_write_approvals "
                    "WHERE owner_id=? AND profile=? AND id=?",
                    (owner, profile_name, row["id"]),
                ).fetchone()
                record = self._record(refreshed) or {}
                record["_claim_acquired"] = True
                record["_idempotent_replay"] = True
                claimed.append(record)
            conn.commit()
        return claimed

    def recoverable_apply_scopes(
        self, *, now: float | None = None
    ) -> list[tuple[str, str]]:
        """List owner/profile scopes with an expired apply lease."""

        timestamp = float(time.time() if now is None else now)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT owner_id,profile FROM account_write_approvals "
                "WHERE state='applying' "
                "AND (apply_lease_expires_at IS NULL OR apply_lease_expires_at<=?) "
                "AND NOT EXISTS (SELECT 1 FROM account_write_approval_deletions d "
                "WHERE d.owner_id=account_write_approvals.owner_id) "
                "ORDER BY owner_id,profile",
                (timestamp,),
            ).fetchall()
        return [(str(row["owner_id"]), str(row["profile"])) for row in rows]

    def finish_apply(
        self,
        *,
        owner_id: str,
        profile: str,
        approval_id: str,
        decision_token: str,
        success: bool,
        error: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        owner, profile_name = self._scope(owner_id, profile)
        timestamp = float(time.time() if now is None else now)
        state = "applied" if success else "failed"
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE account_write_approvals SET state=?,revision=revision+1,"
                "updated_at=?,applied_at=?,last_error=?,decision_token=NULL "
                "WHERE owner_id=? AND profile=? AND id=? AND state='applying' "
                "AND decision_token=? AND apply_lease_expires_at>?",
                (
                    state,
                    timestamp,
                    timestamp if success else None,
                    None if success else str(error or "apply failed")[:4000],
                    owner,
                    profile_name,
                    approval_id,
                    str(decision_token),
                    timestamp,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise ApprovalConflict("approval apply claim was lost")
            row = conn.execute(
                "SELECT * FROM account_write_approvals "
                "WHERE owner_id=? AND profile=? AND id=?",
                (owner, profile_name, approval_id),
            ).fetchone()
            conn.commit()
        return self._record(row) or {}

    def discard_pending(self, *, owner_id: str, profile: str, approval_id: str) -> bool:
        """Compatibility reject used by the existing CLI command."""

        record = self.get(owner_id=owner_id, profile=profile, approval_id=approval_id)
        if record is None or record.get("state") != "pending":
            return False
        try:
            self.claim_decision(
                owner_id=owner_id,
                profile=profile,
                approval_id=approval_id,
                expected_revision=int(record["revision"]),
                decision="reject",
                decision_by=owner_id,
            )
            return True
        except ApprovalConflict:
            return False

    def delete_owner(
        self, owner_id: str, *, now: float | None = None
    ) -> dict[str, int]:
        """Delete one owner's approvals and migration markers idempotently."""

        owner = self._owner(owner_id)
        timestamp = float(time.time() if now is None else now)
        active_effects = 0
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO account_write_approval_deletions("
                "owner_id,deleted_at) VALUES(?,?)",
                (owner, timestamp),
            )
            active_effects = int(
                conn.execute(
                    "SELECT COUNT(*) FROM account_write_approval_effects "
                    "WHERE owner_id=? AND execution_token IS NOT NULL "
                    "AND execution_lease_expires_at>?",
                    (owner, timestamp),
                ).fetchone()[0]
            )
            if active_effects:
                conn.commit()
            else:
                conn.execute(
                    "DELETE FROM account_write_approval_effects WHERE owner_id=?",
                    (owner,),
                )
                rows = conn.execute(
                    "DELETE FROM account_write_approvals WHERE owner_id=?", (owner,)
                ).rowcount
                migrations = conn.execute(
                    "DELETE FROM account_write_approval_migrations WHERE owner_id=?",
                    (owner,),
                ).rowcount
                conn.commit()
        if active_effects:
            raise ApprovalDeletionInProgress(
                f"{active_effects} approval effect(s) are still executing"
            )
        return {"rows": int(rows), "migrations": int(migrations)}

    def migrate_legacy_json(
        self,
        *,
        owner_id: str,
        profile: str,
        subsystem: str,
        pending_dir: Path,
    ) -> int:
        """Bind legacy JSON records to one explicit owner/profile exactly once."""

        owner, profile_name = self._scope(owner_id, profile)
        if subsystem not in {"memory", "skills"}:
            raise ValueError("unsupported write approval subsystem")
        directory = Path(pending_dir)
        if not directory.is_dir():
            return 0
        migrated = 0
        for path in sorted(directory.glob("*.json")):
            source = str(path.resolve())
            with self._lock, self._connect() as conn:
                seen = conn.execute(
                    "SELECT 1 FROM account_write_approval_migrations "
                    "WHERE owner_id=? AND profile=? AND subsystem=? AND source_path=?",
                    (owner, profile_name, subsystem, source),
                ).fetchone()
            if seen:
                continue
            try:
                decoded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(decoded, dict):
                    legacy_records = [decoded]
                elif isinstance(decoded, list):
                    legacy_records = decoded
                else:
                    legacy_records = []
                for index, legacy in enumerate(legacy_records):
                    if not isinstance(legacy, dict):
                        continue
                    payload = legacy.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    deterministic_id = uuid.uuid5(
                        uuid.NAMESPACE_URL, f"{source}#{index}"
                    ).hex
                    legacy_id = str(legacy.get("id") or deterministic_id)
                    try:
                        self.stage(
                            owner_id=owner,
                            profile=profile_name,
                            subsystem=subsystem,
                            payload=payload,
                            summary=str(legacy.get("summary") or ""),
                            origin=str(legacy.get("origin") or "foreground"),
                            approval_id=legacy_id,
                            created_at=float(legacy.get("created_at") or time.time()),
                        )
                        migrated += 1
                    except sqlite3.IntegrityError:
                        pass
                with self._lock, self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._assert_owner_active(conn, owner)
                    conn.execute(
                        "INSERT OR IGNORE INTO account_write_approval_migrations("
                        "owner_id,profile,subsystem,source_path,migrated_at) VALUES(?,?,?,?,?)",
                        (owner, profile_name, subsystem, source, time.time()),
                    )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return migrated
