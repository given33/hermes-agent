"""SQLite authority for definitions, runs, dispatch intents, and audit data."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hermes_constants import get_hermes_home
from plugins.workflows.models import (
    NodeState,
    SecurityVerdict,
    TERMINAL_NODE_STATES,
    TERMINAL_RUN_STATES,
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowScope,
    WorkflowSecurityError,
)
from plugins.workflows.validator import canonical_digest, canonical_json, condition_matches, validate_definition
from plugins.workflows.workspace_audit import redact_secrets

SCHEMA_VERSION = 1
MAX_WORKSPACE_FILES = 80
MAX_WORKSPACE_FILE_BYTES = 1_048_576
SENSITIVE_PARTS = {".env", ".git", "auth.json", "credentials", "secrets"}


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS workflow_schema(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_account_deletions(
 account_id TEXT PRIMARY KEY, deleted_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_deleting_definitions(
 id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS workflow_definitions(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_generation INTEGER NOT NULL,
 profile_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 current_version INTEGER NOT NULL, revision INTEGER NOT NULL, created_at INTEGER NOT NULL,
 updated_at INTEGER NOT NULL,
 UNIQUE(account_id, account_generation, profile_id, name)
);
CREATE TABLE IF NOT EXISTS workflow_versions(
 id TEXT PRIMARY KEY, definition_id TEXT NOT NULL REFERENCES workflow_definitions(id) ON DELETE CASCADE,
 version_number INTEGER NOT NULL, spec_json TEXT NOT NULL, digest TEXT NOT NULL,
 created_at INTEGER NOT NULL, UNIQUE(definition_id, version_number)
);
CREATE TRIGGER IF NOT EXISTS workflow_versions_no_update
 BEFORE UPDATE ON workflow_versions BEGIN SELECT RAISE(ABORT, 'workflow versions are immutable'); END;
DROP TRIGGER IF EXISTS workflow_versions_no_delete;
CREATE TRIGGER workflow_versions_no_delete
 BEFORE DELETE ON workflow_versions
 WHEN NOT EXISTS(SELECT 1 FROM workflow_deleting_definitions WHERE id=OLD.definition_id)
 BEGIN SELECT RAISE(ABORT, 'workflow versions are immutable'); END;
CREATE TABLE IF NOT EXISTS workflow_runs(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_generation INTEGER NOT NULL,
 profile_id TEXT NOT NULL, definition_id TEXT NOT NULL REFERENCES workflow_definitions(id),
 version_id TEXT NOT NULL REFERENCES workflow_versions(id), state TEXT NOT NULL,
 revision INTEGER NOT NULL, input_json TEXT NOT NULL, cancel_reason TEXT NOT NULL DEFAULT '',
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS workflow_runs_recovery_idx ON workflow_runs(state, updated_at);
CREATE TABLE IF NOT EXISTS workflow_node_runs(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
 node_key TEXT NOT NULL, iteration INTEGER NOT NULL, attempt INTEGER NOT NULL,
 state TEXT NOT NULL, revision INTEGER NOT NULL, spec_json TEXT NOT NULL,
 output_json TEXT, error TEXT NOT NULL DEFAULT '', external_ref TEXT NOT NULL DEFAULT '',
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, finished_at INTEGER,
 UNIQUE(run_id, node_key, iteration, attempt)
);
CREATE INDEX IF NOT EXISTS workflow_node_runs_run_idx
 ON workflow_node_runs(run_id, node_key, iteration, attempt DESC);
CREATE TABLE IF NOT EXISTS workflow_edge_events(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
 edge_key TEXT NOT NULL, source_node_run_id TEXT NOT NULL REFERENCES workflow_node_runs(id),
 target_node_key TEXT NOT NULL, target_iteration INTEGER NOT NULL, matched INTEGER NOT NULL,
 event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at INTEGER NOT NULL,
 UNIQUE(source_node_run_id, edge_key, target_iteration, event_type)
);
CREATE TABLE IF NOT EXISTS workflow_dispatch_intents(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_generation INTEGER NOT NULL,
 profile_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
 node_run_id TEXT NOT NULL REFERENCES workflow_node_runs(id) ON DELETE CASCADE,
 state TEXT NOT NULL, payload_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 available_at INTEGER NOT NULL, lease_token TEXT, lease_expires_at INTEGER,
 external_ref TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(node_run_id)
);
CREATE INDEX IF NOT EXISTS workflow_dispatch_claim_idx
 ON workflow_dispatch_intents(state, available_at, lease_expires_at);
CREATE TABLE IF NOT EXISTS workflow_run_artifacts(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
 node_run_id TEXT REFERENCES workflow_node_runs(id) ON DELETE SET NULL, kind TEXT NOT NULL,
 name TEXT NOT NULL, opaque_ref TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_workspace_change_sets(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_generation INTEGER NOT NULL,
 profile_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
 turn_id TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_workspace_change_files(
 id TEXT PRIMARY KEY, change_set_id TEXT NOT NULL REFERENCES workflow_workspace_change_sets(id) ON DELETE CASCADE,
 relative_path TEXT NOT NULL, change_type TEXT NOT NULL, sha256 TEXT NOT NULL,
 nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, byte_count INTEGER NOT NULL,
 created_at INTEGER NOT NULL, UNIQUE(change_set_id, relative_path)
);
CREATE TABLE IF NOT EXISTS workflow_workspace_baselines(
 node_run_id TEXT PRIMARY KEY REFERENCES workflow_node_runs(id) ON DELETE CASCADE,
 account_id TEXT NOT NULL, account_generation INTEGER NOT NULL, profile_id TEXT NOT NULL,
 run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
 state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', file_count INTEGER NOT NULL DEFAULT 0,
 byte_count INTEGER NOT NULL DEFAULT 0, nonce BLOB, ciphertext BLOB,
 change_set_id TEXT REFERENCES workflow_workspace_change_sets(id) ON DELETE SET NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, finalized_at INTEGER
);
CREATE INDEX IF NOT EXISTS workflow_workspace_baselines_scope_idx
 ON workflow_workspace_baselines(account_id, account_generation, profile_id, run_id);
CREATE TABLE IF NOT EXISTS workflow_tool_approval_requests(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_generation INTEGER NOT NULL,
 profile_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
 node_run_id TEXT NOT NULL REFERENCES workflow_node_runs(id) ON DELETE CASCADE,
 tool_name TEXT NOT NULL, args_hash TEXT NOT NULL, state TEXT NOT NULL,
 revision INTEGER NOT NULL, expires_at INTEGER NOT NULL, remaining_uses INTEGER NOT NULL DEFAULT 0,
 token_hash TEXT, requested_at INTEGER NOT NULL, decided_at INTEGER, consumed_at INTEGER,
 UNIQUE(run_id, node_run_id, tool_name, args_hash, state)
);
CREATE TABLE IF NOT EXISTS workflow_idempotency(
 account_id TEXT NOT NULL, account_generation INTEGER NOT NULL, profile_id TEXT NOT NULL,
 operation TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
 response_json TEXT NOT NULL, created_at INTEGER NOT NULL,
 PRIMARY KEY(account_id, account_generation, profile_id, operation, idempotency_key)
);
"""


def default_store_path() -> Path:
    return Path(get_hermes_home()) / "workflows.db"


def _now() -> int:
    return int(time.time())


def _json_load(value: str | bytes | None, default: Any = None) -> Any:
    if value is None:
        return default
    loaded = json.loads(value)
    if isinstance(default, dict) and not isinstance(loaded, dict):
        return default
    if isinstance(default, list) and not isinstance(loaded, list):
        return default
    return loaded


class WorkflowStore:
    def __init__(self, path: str | Path | None = None, *, audit_key: bytes | None = None):
        self.path = Path(path) if path else default_store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._audit_key = audit_key or self._load_audit_key()
        if len(self._audit_key) != 32:
            raise ValueError("workflow audit key must be 32 bytes")
        self._init_schema()

    def _load_audit_key(self) -> bytes:
        configured = os.getenv("HERMES_WORKFLOW_AUDIT_KEY", "").strip()
        if configured:
            try:
                key = base64.urlsafe_b64decode(configured + "=" * (-len(configured) % 4))
            except Exception as exc:
                raise ValueError("HERMES_WORKFLOW_AUDIT_KEY must be urlsafe base64") from exc
            if len(key) != 32:
                raise ValueError("HERMES_WORKFLOW_AUDIT_KEY must decode to 32 bytes")
            return key
        key_path = self.path.with_suffix(".audit-key")
        try:
            return key_path.read_bytes()
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(str(key_path), flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            return key

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            row = conn.execute("SELECT version FROM workflow_schema LIMIT 1").fetchone()
            if row is None:
                conn.execute("INSERT INTO workflow_schema(version) VALUES (?)", (SCHEMA_VERSION,))
            elif int(row["version"]) > SCHEMA_VERSION:
                raise RuntimeError("workflow database was created by a newer Hermes version")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _scope(scope: WorkflowScope) -> WorkflowScope:
        return scope.validate()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _replay(self, conn: sqlite3.Connection, scope: WorkflowScope, operation: str,
                key: str, payload: Any) -> dict[str, Any] | None:
        if conn.execute(
            "SELECT 1 FROM workflow_account_deletions WHERE account_id=?",
            (scope.account_id,),
        ).fetchone() is not None:
            raise WorkflowSecurityError("account has been deleted")
        key = str(key or "").strip()
        if not key or len(key) > 256:
            raise ValueError("Idempotency-Key is required and must be at most 256 characters")
        row = conn.execute(
            "SELECT payload_hash,response_json FROM workflow_idempotency WHERE "
            "account_id=? AND account_generation=? AND profile_id=? AND operation=? AND idempotency_key=?",
            (scope.account_id, scope.account_generation, scope.profile_id, operation, key),
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != canonical_digest(payload):
            raise WorkflowConflict("Idempotency-Key was already used with a different payload")
        return _json_load(row["response_json"], {})

    def _remember(
        self,
        conn: sqlite3.Connection,
        scope: WorkflowScope,
        operation: str,
        key: str,
        payload: Any,
        response: dict[str, Any],
        *,
        now: int | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO workflow_idempotency VALUES(?,?,?,?,?,?,?,?)",
            (scope.account_id, scope.account_generation, scope.profile_id, operation, key,
             canonical_digest(payload), canonical_json(response),
             int(_now() if now is None else now)),
        )

    @staticmethod
    def _require_definition(conn: sqlite3.Connection, scope: WorkflowScope, definition_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM workflow_definitions WHERE id=? AND account_id=? AND account_generation=? AND profile_id=?",
            (definition_id, scope.account_id, scope.account_generation, scope.profile_id),
        ).fetchone()
        if row is None:
            raise WorkflowNotFound("workflow definition was not found in this account/profile")
        return row

    @staticmethod
    def _require_run(conn: sqlite3.Connection, scope: WorkflowScope, run_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM workflow_runs WHERE id=? AND account_id=? AND account_generation=? AND profile_id=?",
            (run_id, scope.account_id, scope.account_generation, scope.profile_id),
        ).fetchone()
        if row is None:
            raise WorkflowNotFound("workflow run was not found in this account/profile")
        return row

    @staticmethod
    def _definition_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        version = conn.execute(
            "SELECT * FROM workflow_versions WHERE definition_id=? AND version_number=?",
            (row["id"], row["current_version"]),
        ).fetchone()
        result = dict(row)
        result["version_id"] = version["id"]
        result["digest"] = version["digest"]
        result["spec"] = _json_load(version["spec_json"], {})
        return result

    def create_definition(self, scope: WorkflowScope, *, name: str, spec: dict[str, Any],
                          description: str = "", idempotency_key: str) -> dict[str, Any]:
        scope = self._scope(scope)
        name = str(name or "").strip()
        if not name or len(name) > 160:
            raise ValueError("name is required and must be at most 160 characters")
        snapshot = validate_definition(spec)
        payload = {"name": name, "description": description, "spec": snapshot}
        with self.transaction() as conn:
            replay = self._replay(conn, scope, "definition.create", idempotency_key, payload)
            if replay is not None:
                return replay
            now, definition_id, version_id = _now(), str(uuid.uuid4()), str(uuid.uuid4())
            try:
                conn.execute(
                    "INSERT INTO workflow_definitions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (definition_id, scope.account_id, scope.account_generation, scope.profile_id,
                     name, str(description or "")[:2000], 1, 1, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkflowConflict("a workflow with this name already exists in the profile") from exc
            conn.execute(
                "INSERT INTO workflow_versions VALUES(?,?,?,?,?,?)",
                (version_id, definition_id, 1, canonical_json(snapshot), canonical_digest(snapshot), now),
            )
            row = conn.execute("SELECT * FROM workflow_definitions WHERE id=?", (definition_id,)).fetchone()
            response = self._definition_payload(conn, row)
            self._remember(conn, scope, "definition.create", idempotency_key, payload, response)
            return response

    def get_definition(self, scope: WorkflowScope, definition_id: str) -> dict[str, Any]:
        scope = self._scope(scope)
        with self.connect() as conn:
            return self._definition_payload(conn, self._require_definition(conn, scope, definition_id))

    def list_definitions(self, scope: WorkflowScope) -> list[dict[str, Any]]:
        scope = self._scope(scope)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_definitions WHERE account_id=? AND account_generation=? "
                "AND profile_id=? ORDER BY updated_at DESC",
                (scope.account_id, scope.account_generation, scope.profile_id),
            ).fetchall()
            return [self._definition_payload(conn, row) for row in rows]

    def add_version(self, scope: WorkflowScope, definition_id: str, *, spec: dict[str, Any],
                    expected_revision: int, idempotency_key: str) -> dict[str, Any]:
        scope, snapshot = self._scope(scope), validate_definition(spec)
        payload = {"definition_id": definition_id, "spec": snapshot,
                   "expected_revision": expected_revision}
        with self.transaction() as conn:
            replay = self._replay(conn, scope, "definition.version", idempotency_key, payload)
            if replay is not None:
                return replay
            current = self._require_definition(conn, scope, definition_id)
            if int(current["revision"]) != int(expected_revision):
                raise WorkflowConflict("definition revision changed")
            next_version, now = int(current["current_version"]) + 1, _now()
            conn.execute(
                "INSERT INTO workflow_versions VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), definition_id, next_version, canonical_json(snapshot),
                 canonical_digest(snapshot), now),
            )
            changed = conn.execute(
                "UPDATE workflow_definitions SET current_version=?,revision=revision+1,updated_at=? "
                "WHERE id=? AND revision=?", (next_version, now, definition_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise WorkflowConflict("definition revision changed")
            row = conn.execute("SELECT * FROM workflow_definitions WHERE id=?", (definition_id,)).fetchone()
            response = self._definition_payload(conn, row)
            self._remember(conn, scope, "definition.version", idempotency_key, payload, response)
            return response

    def _insert_dispatch_intent(self, conn: sqlite3.Connection, scope: WorkflowScope, run_id: str,
                                node_run_id: str, node: dict[str, Any], now: int) -> str:
        if bool(node.get("requires_approval")):
            tool_name = str(node.get("tool_name") or node.get("type") or "agent")[:256]
            arguments = node.get("tool_args", node.get("config", {}))
            conn.execute(
                "INSERT OR IGNORE INTO workflow_tool_approval_requests(id,account_id,"
                "account_generation,profile_id,run_id,node_run_id,tool_name,args_hash,state,"
                "revision,expires_at,requested_at) VALUES(?,?,?,?,?,?,?,?,'pending',1,?,?)",
                (
                    str(uuid.uuid4()), scope.account_id, scope.account_generation,
                    scope.profile_id, run_id, node_run_id, tool_name,
                    canonical_digest(arguments), now + 900, now,
                ),
            )
            return ""
        intent_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO workflow_dispatch_intents(id,account_id,account_generation,profile_id,"
            "run_id,node_run_id,state,payload_json,available_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'pending',?,?,?,?)",
            (intent_id, scope.account_id, scope.account_generation, scope.profile_id, run_id,
             node_run_id, canonical_json({"node": node}), now, now, now),
        )
        return intent_id

    def release_node_after_approval(
        self, scope: WorkflowScope, run_id: str, node_run_id: str
    ) -> bool:
        scope = self._scope(scope)
        with self.transaction() as conn:
            self._require_run(conn, scope, run_id)
            node = conn.execute(
                "SELECT * FROM workflow_node_runs WHERE id=? AND run_id=? AND state='ready'",
                (node_run_id, run_id),
            ).fetchone()
            if node is None:
                return False
            consumed = conn.execute(
                "SELECT 1 FROM workflow_tool_approval_requests WHERE run_id=? AND node_run_id=? "
                "AND state='consumed' LIMIT 1",
                (run_id, node_run_id),
            ).fetchone()
            if consumed is None:
                raise WorkflowSecurityError("node approval has not been consumed")
            exists = conn.execute(
                "SELECT 1 FROM workflow_dispatch_intents WHERE node_run_id=?", (node_run_id,)
            ).fetchone()
            if exists is not None:
                return False
            spec = _json_load(node["spec_json"], {})
            spec["requires_approval"] = False
            self._insert_dispatch_intent(conn, scope, run_id, node_run_id, spec, _now())
            return True

    @staticmethod
    def _workspace_change_summaries(
        conn: sqlite3.Connection, run_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT s.*,COUNT(f.id) AS file_count,COALESCE(SUM(f.byte_count),0) AS byte_count,"
            "COALESCE(SUM(CASE WHEN f.change_type='added' THEN 1 ELSE 0 END),0) AS added_count,"
            "COALESCE(SUM(CASE WHEN f.change_type='modified' THEN 1 ELSE 0 END),0) AS modified_count,"
            "COALESCE(SUM(CASE WHEN f.change_type='deleted' THEN 1 ELSE 0 END),0) AS deleted_count,"
            "COALESCE(SUM(CASE WHEN f.change_type='renamed' THEN 1 ELSE 0 END),0) AS renamed_count "
            "FROM workflow_workspace_change_sets s "
            "LEFT JOIN workflow_workspace_change_files f ON f.change_set_id=s.id "
            "WHERE s.run_id=? GROUP BY s.id ORDER BY s.created_at DESC,s.id DESC LIMIT ?",
            (run_id, max(1, min(int(limit), 500))),
        ).fetchall()
        summaries: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["change_counts"] = {
                "added": int(item.pop("added_count")),
                "modified": int(item.pop("modified_count")),
                "deleted": int(item.pop("deleted_count")),
                "renamed": int(item.pop("renamed_count")),
            }
            item["file_count"] = int(item["file_count"])
            item["byte_count"] = int(item["byte_count"])
            summaries.append(item)
        return summaries

    @staticmethod
    def _workspace_audit_summaries(
        conn: sqlite3.Connection, run_id: str
    ) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            "SELECT node_run_id,state,reason,file_count,byte_count,change_set_id,"
            "created_at,updated_at,finalized_at FROM workflow_workspace_baselines "
            "WHERE run_id=? ORDER BY created_at,node_run_id",
            (run_id,),
        ).fetchall()
        return {str(row["node_run_id"]): dict(row) for row in rows}

    @classmethod
    def _run_payload(cls, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["inputs"] = _json_load(result.pop("input_json"), {})
        result["workspace_change_sets"] = cls._workspace_change_summaries(
            conn, str(row["id"]), limit=500
        )
        workspace_audits = cls._workspace_audit_summaries(conn, str(row["id"]))
        node_rows = conn.execute(
            "SELECT * FROM workflow_node_runs WHERE run_id=? ORDER BY created_at,id", (row["id"],)
        ).fetchall()
        result["node_runs"] = []
        for node in node_rows:
            item = dict(node)
            item["spec"] = _json_load(item.pop("spec_json"), {})
            item["output"] = _json_load(item.pop("output_json"), None)
            item["workspace_audit"] = workspace_audits.get(str(item["id"]))
            result["node_runs"].append(item)
        return result

    def start_run(
        self,
        scope: WorkflowScope,
        definition_id: str,
        *,
        version: int | None = None,
        inputs: dict[str, Any] | None = None,
        idempotency_key: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(scope)
        payload = {"definition_id": definition_id, "version": version, "inputs": inputs or {}}
        with self.transaction() as conn:
            replay = self._replay(conn, scope, "run.start", idempotency_key, payload)
            if replay is not None:
                return replay
            definition = self._require_definition(conn, scope, definition_id)
            version_number = int(version or definition["current_version"])
            version_row = conn.execute(
                "SELECT * FROM workflow_versions WHERE definition_id=? AND version_number=?",
                (definition_id, version_number),
            ).fetchone()
            if version_row is None:
                raise WorkflowNotFound("workflow version was not found")
            spec = _json_load(version_row["spec_json"], {})
            incoming = {edge["target"] for edge in spec["edges"] if not edge.get("loop")}
            run_id = str(uuid.uuid4())
            current = int(_now() if now is None else now)
            conn.execute(
                "INSERT INTO workflow_runs(id,account_id,account_generation,profile_id,definition_id,"
                "version_id,state,revision,input_json,created_at,updated_at) VALUES(?,?,?,?,?,?,'running',1,?,?,?)",
                (run_id, scope.account_id, scope.account_generation, scope.profile_id, definition_id,
                 version_row["id"], canonical_json(inputs or {}), current, current),
            )
            for node in spec["nodes"]:
                node_run_id = str(uuid.uuid4())
                state = "pending" if node["id"] in incoming else "ready"
                conn.execute(
                    "INSERT INTO workflow_node_runs(id,run_id,node_key,iteration,attempt,state,revision,"
                    "spec_json,created_at,updated_at) VALUES(?,?,?,0,1,?,1,?,?,?)",
                    (node_run_id, run_id, node["id"], state, canonical_json(node), current, current),
                )
                if state == "ready":
                    self._insert_dispatch_intent(conn, scope, run_id, node_run_id, node, current)
            row = conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
            response = self._run_payload(conn, row)
            response["version"] = version_number
            self._remember(
                conn,
                scope,
                "run.start",
                idempotency_key,
                payload,
                response,
                now=current,
            )
            return response

    def get_run(self, scope: WorkflowScope, run_id: str) -> dict[str, Any]:
        scope = self._scope(scope)
        with self.connect() as conn:
            return self._run_payload(conn, self._require_run(conn, scope, run_id))

    def list_runs(self, scope: WorkflowScope, *, limit: int = 100) -> list[dict[str, Any]]:
        scope = self._scope(scope)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_runs WHERE account_id=? AND account_generation=? AND profile_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (scope.account_id, scope.account_generation, scope.profile_id, max(1, min(limit, 500))),
            ).fetchall()
            return [self._run_payload(conn, row) for row in rows]

    def cancel_run(
        self,
        scope: WorkflowScope,
        run_id: str,
        *,
        expected_revision: int,
        reason: str = "",
        idempotency_key: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(scope)
        payload = {"run_id": run_id, "expected_revision": expected_revision, "reason": reason}
        with self.transaction() as conn:
            replay = self._replay(conn, scope, "run.cancel", idempotency_key, payload)
            if replay is not None:
                return replay
            run = self._require_run(conn, scope, run_id)
            if run["state"] in TERMINAL_RUN_STATES:
                raise WorkflowConflict("run is already terminal")
            current = int(_now() if now is None else now)
            changed = conn.execute(
                "UPDATE workflow_runs SET state='cancelled',revision=revision+1,cancel_reason=?,"
                "updated_at=?,finished_at=? WHERE id=? AND revision=?",
                (str(reason or "")[:2000], current, current, run_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise WorkflowConflict("run revision changed")

            # Any claimed dispatch may already have created its external task.  Preserve it as a
            # durable cancellation outbox row before invalidating the dispatch lease.  Intents
            # never claimed cannot have crossed the adapter boundary and can be closed directly.
            placeholders = ",".join("?" for _ in TERMINAL_NODE_STATES)
            conn.execute(
                f"UPDATE workflow_dispatch_intents SET state=CASE WHEN "
                f"(attempts>0 OR external_ref!='') AND EXISTS("
                f"SELECT 1 FROM workflow_node_runs n WHERE n.id=node_run_id "
                f"AND n.state NOT IN ({placeholders})) "
                f"THEN 'cancel_pending' ELSE 'cancelled' END,"
                f"available_at=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? "
                f"WHERE run_id=? AND state!='cancelled'",
                (*sorted(TERMINAL_NODE_STATES), current, current, run_id),
            )
            conn.execute(
                f"UPDATE workflow_node_runs SET state='cancelled',revision=revision+1,updated_at=?,"
                f"finished_at=? WHERE run_id=? AND state NOT IN ({placeholders})",
                (current, current, run_id, *sorted(TERMINAL_NODE_STATES)),
            )
            response = self._run_payload(
                conn, conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
            )
            self._remember(
                conn,
                scope,
                "run.cancel",
                idempotency_key,
                payload,
                response,
                now=current,
            )
            return response

    def retry_node(self, scope: WorkflowScope, run_id: str, node_run_id: str, *,
                   expected_revision: int, idempotency_key: str) -> dict[str, Any]:
        scope = self._scope(scope)
        payload = {"run_id": run_id, "node_run_id": node_run_id,
                   "expected_revision": expected_revision}
        with self.transaction() as conn:
            replay = self._replay(conn, scope, "node.retry", idempotency_key, payload)
            if replay is not None:
                return replay
            run = self._require_run(conn, scope, run_id)
            if run["state"] == "cancelled":
                raise WorkflowConflict("cancelled runs cannot be retried")
            node = conn.execute(
                "SELECT * FROM workflow_node_runs WHERE id=? AND run_id=?", (node_run_id, run_id)
            ).fetchone()
            if node is None:
                raise WorkflowNotFound("node run was not found")
            if node["state"] not in {"failed", "cancelled"} or int(node["revision"]) != int(expected_revision):
                raise WorkflowConflict("node is not retryable or its revision changed")
            latest = conn.execute(
                "SELECT MAX(attempt) AS value FROM workflow_node_runs WHERE run_id=? AND node_key=? AND iteration=?",
                (run_id, node["node_key"], node["iteration"]),
            ).fetchone()["value"]
            now, new_id = _now(), str(uuid.uuid4())
            conn.execute(
                "INSERT INTO workflow_node_runs(id,run_id,node_key,iteration,attempt,state,revision,spec_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,'ready',1,?,?,?)",
                (new_id, run_id, node["node_key"], node["iteration"], int(latest) + 1,
                 node["spec_json"], now, now),
            )
            spec = _json_load(node["spec_json"], {})
            self._insert_dispatch_intent(conn, scope, run_id, new_id, spec, now)
            conn.execute(
                "UPDATE workflow_runs SET state='running',revision=revision+1,updated_at=?,finished_at=NULL WHERE id=?",
                (now, run_id),
            )
            response = {"node_run_id": new_id, "node_key": node["node_key"],
                        "iteration": node["iteration"], "attempt": int(latest) + 1,
                        "state": "ready", "revision": 1}
            self._remember(conn, scope, "node.retry", idempotency_key, payload, response)
            return response

    def claim_dispatch_intents(
        self,
        *,
        limit: int = 20,
        lease_seconds: int = 120,
        now: int | None = None,
    ) -> list[dict[str, Any]]:
        """Claim ready nodes for an execution adapter using expiring CAS leases."""

        current = int(_now() if now is None else now)
        lease = max(30, min(int(lease_seconds), 3600))
        bounded = max(1, min(int(limit), 100))
        claimed: list[dict[str, Any]] = []
        with self.transaction() as conn:
            conn.execute(
                "UPDATE workflow_dispatch_intents SET state='retry',lease_token=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE state='delivering' "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
                (current, current),
            )
            rows = conn.execute(
                "SELECT i.* FROM workflow_dispatch_intents i "
                "JOIN workflow_runs r ON r.id=i.run_id "
                "JOIN workflow_node_runs n ON n.id=i.node_run_id "
                "WHERE i.state IN ('pending','retry') AND i.available_at<=? "
                "AND r.state='running' AND n.state='ready' "
                "ORDER BY i.available_at,i.created_at,i.id LIMIT ?",
                (current, bounded),
            ).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(32)
                changed = conn.execute(
                    "UPDATE workflow_dispatch_intents SET state='delivering',lease_token=?,"
                    "lease_expires_at=?,attempts=attempts+1,updated_at=? "
                    "WHERE id=? AND state IN ('pending','retry')",
                    (token, current + lease, current, row["id"]),
                ).rowcount
                if changed != 1:
                    continue
                item = dict(row)
                item.update(
                    state="delivering",
                    lease_token=token,
                    lease_expires_at=current + lease,
                    attempts=int(row["attempts"]) + 1,
                )
                item["payload"] = _json_load(item.pop("payload_json"), {})
                claimed.append(item)
        return claimed

    def renew_dispatch_claim(
        self,
        intent_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 120,
        now: int | None = None,
    ) -> bool:
        current = int(_now() if now is None else now)
        lease = max(30, min(int(lease_seconds), 3600))
        with self.transaction() as conn:
            return conn.execute(
                "UPDATE workflow_dispatch_intents SET lease_expires_at=?,updated_at=? "
                "WHERE id=? AND state='delivering' AND lease_token=? "
                "AND lease_expires_at>?",
                (current + lease, current, intent_id, lease_token, current),
            ).rowcount == 1

    def complete_dispatch_claim(
        self,
        intent_id: str,
        lease_token: str,
        external_ref: str,
        *,
        now: int | None = None,
    ) -> bool:
        current = int(_now() if now is None else now)
        reference = str(external_ref or "").strip()[:512]
        if not reference:
            raise ValueError("external_ref is required")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT node_run_id FROM workflow_dispatch_intents WHERE id=? "
                "AND state='delivering' AND lease_token=? AND lease_expires_at>?",
                (intent_id, lease_token, current),
            ).fetchone()
            if row is None:
                return False
            changed = conn.execute(
                "UPDATE workflow_dispatch_intents SET state='dispatched',external_ref=?,"
                "lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE id=? "
                "AND state='delivering' AND lease_token=?",
                (reference, current, intent_id, lease_token),
            ).rowcount
            if changed != 1:
                return False
            conn.execute(
                "UPDATE workflow_node_runs SET state='dispatched',external_ref=?,"
                "revision=revision+1,updated_at=? WHERE id=? AND state='ready'",
                (reference, current, row["node_run_id"]),
            )
            return True

    def fail_dispatch_claim(
        self,
        intent_id: str,
        lease_token: str,
        error: str,
        *,
        retry_after: int = 30,
        permanent: bool = False,
        now: int | None = None,
    ) -> bool:
        current = int(_now() if now is None else now)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT run_id,node_run_id FROM workflow_dispatch_intents WHERE id=? "
                "AND state='delivering' AND lease_token=? AND lease_expires_at>?",
                (intent_id, lease_token, current),
            ).fetchone()
            if row is None:
                return False
            state = "failed" if permanent else "retry"
            changed = conn.execute(
                "UPDATE workflow_dispatch_intents SET state=?,error=?,available_at=?,"
                "lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE id=? "
                "AND state='delivering' AND lease_token=?",
                (
                    state,
                    str(error or "")[:4000],
                    current + max(1, int(retry_after)),
                    current,
                    intent_id,
                    lease_token,
                ),
            ).rowcount
            if changed != 1:
                return False
            if permanent:
                conn.execute(
                    "UPDATE workflow_node_runs SET state='failed',error=?,revision=revision+1,"
                    "updated_at=?,finished_at=? WHERE id=? AND state='ready'",
                    (str(error or "")[:4000], current, current, row["node_run_id"]),
                )
                conn.execute(
                    "UPDATE workflow_runs SET state='failed',revision=revision+1,updated_at=?,"
                    "finished_at=? WHERE id=? AND state='running'",
                    (current, current, row["run_id"]),
                )
            return True

    def claim_cancel_intents(
        self,
        *,
        limit: int = 1,
        lease_seconds: int = 120,
        now: int | None = None,
    ) -> list[dict[str, Any]]:
        """Claim durable external-task cancellations with expiring CAS leases."""

        current = int(_now() if now is None else now)
        lease = max(30, min(int(lease_seconds), 3600))
        bounded = max(1, min(int(limit), 100))
        claimed: list[dict[str, Any]] = []
        with self.transaction() as conn:
            conn.execute(
                "UPDATE workflow_dispatch_intents SET state='cancel_retry',lease_token=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE state='cancel_delivering' "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
                (current, current),
            )
            rows = conn.execute(
                "SELECT * FROM workflow_dispatch_intents "
                "WHERE state IN ('cancel_pending','cancel_retry') AND available_at<=? "
                "ORDER BY available_at,created_at,id LIMIT ?",
                (current, bounded),
            ).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(32)
                changed = conn.execute(
                    "UPDATE workflow_dispatch_intents SET state='cancel_delivering',"
                    "lease_token=?,lease_expires_at=?,attempts=attempts+1,updated_at=? "
                    "WHERE id=? AND state IN ('cancel_pending','cancel_retry')",
                    (token, current + lease, current, row["id"]),
                ).rowcount
                if changed != 1:
                    continue
                item = dict(row)
                item.update(
                    state="cancel_delivering",
                    lease_token=token,
                    lease_expires_at=current + lease,
                    attempts=int(row["attempts"]) + 1,
                )
                item["payload"] = _json_load(item.pop("payload_json"), {})
                claimed.append(item)
        return claimed

    def renew_cancel_claim(
        self,
        intent_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 120,
        now: int | None = None,
    ) -> bool:
        current = int(_now() if now is None else now)
        lease = max(30, min(int(lease_seconds), 3600))
        with self.transaction() as conn:
            return conn.execute(
                "UPDATE workflow_dispatch_intents SET lease_expires_at=?,updated_at=? "
                "WHERE id=? AND state='cancel_delivering' AND lease_token=? "
                "AND lease_expires_at>?",
                (current + lease, current, intent_id, lease_token, current),
            ).rowcount == 1

    def record_cancel_external_ref(
        self,
        intent_id: str,
        lease_token: str,
        external_ref: str,
        *,
        now: int | None = None,
    ) -> bool:
        """Persist recovery's external reference before cancellation crosses the adapter."""

        current = int(_now() if now is None else now)
        reference = str(external_ref or "").strip()[:512]
        if not reference:
            raise ValueError("external_ref is required")
        with self.transaction() as conn:
            return conn.execute(
                "UPDATE workflow_dispatch_intents SET external_ref=?,updated_at=? "
                "WHERE id=? AND state='cancel_delivering' AND lease_token=? "
                "AND lease_expires_at>?",
                (reference, current, intent_id, lease_token, current),
            ).rowcount == 1

    def complete_cancel_claim(
        self,
        intent_id: str,
        lease_token: str,
        *,
        now: int | None = None,
    ) -> bool:
        current = int(_now() if now is None else now)
        with self.transaction() as conn:
            return conn.execute(
                "UPDATE workflow_dispatch_intents SET state='cancelled',error='',"
                "lease_token=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE id=? AND state='cancel_delivering' AND lease_token=? "
                "AND lease_expires_at>? AND external_ref!=''",
                (current, intent_id, lease_token, current),
            ).rowcount == 1

    def fail_cancel_claim(
        self,
        intent_id: str,
        lease_token: str,
        error: str,
        *,
        retry_after: int = 30,
        now: int | None = None,
    ) -> bool:
        current = int(_now() if now is None else now)
        with self.transaction() as conn:
            return conn.execute(
                "UPDATE workflow_dispatch_intents SET state='cancel_retry',error=?,"
                "available_at=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE id=? AND state='cancel_delivering' AND lease_token=? "
                "AND lease_expires_at>?",
                (
                    str(error or "")[:4000],
                    current + max(1, int(retry_after)),
                    current,
                    intent_id,
                    lease_token,
                    current,
                ),
            ).rowcount == 1

    def list_external_node_runs(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT n.*,r.account_id,r.account_generation,r.profile_id "
                "FROM workflow_node_runs n JOIN workflow_runs r ON r.id=n.run_id "
                "WHERE r.state='running' AND n.state IN ('dispatched','running') "
                "AND n.external_ref!='' ORDER BY n.updated_at,n.id LIMIT ?",
                (max(1, min(int(limit), 2000)),),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _version_spec(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT v.spec_json FROM workflow_runs r JOIN workflow_versions v ON v.id=r.version_id "
            "WHERE r.id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise WorkflowNotFound("workflow run version was not found")
        return _json_load(row["spec_json"], {})

    @staticmethod
    def _edge_key(index: int, edge: dict[str, Any]) -> str:
        return f"e{index}:{edge['source']}:{edge['target']}:{1 if edge.get('loop') else 0}"

    @staticmethod
    def _ensure_node_instance(
        conn: sqlite3.Connection,
        run_id: str,
        node: dict[str, Any],
        iteration: int,
        now: int,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM workflow_node_runs WHERE run_id=? AND node_key=? AND iteration=? "
            "ORDER BY attempt DESC LIMIT 1",
            (run_id, node["id"], iteration),
        ).fetchone()
        if row is not None:
            return row
        conn.execute(
            "INSERT INTO workflow_node_runs(id,run_id,node_key,iteration,attempt,state,revision,"
            "spec_json,created_at,updated_at) VALUES(?,?,?, ?,1,'pending',1,?,?,?)",
            (str(uuid.uuid4()), run_id, node["id"], iteration, canonical_json(node), now, now),
        )
        return conn.execute(
            "SELECT * FROM workflow_node_runs WHERE run_id=? AND node_key=? AND iteration=?",
            (run_id, node["id"], iteration),
        ).fetchone()

    def _emit_outgoing_locked(
        self,
        conn: sqlite3.Connection,
        scope: WorkflowScope,
        run_id: str,
        source: sqlite3.Row,
        spec: dict[str, Any],
        *,
        source_matched: bool,
        now: int,
    ) -> None:
        nodes = {node["id"]: node for node in spec["nodes"]}
        output = _json_load(source["output_json"], None)
        for index, edge in enumerate(spec["edges"]):
            if edge["source"] != source["node_key"]:
                continue
            matched = source_matched and condition_matches(edge.get("condition"), output)
            target_iteration = int(source["iteration"]) + (1 if edge.get("loop") else 0)
            if edge.get("loop"):
                limit = int(edge.get("max_iterations") or 1)
                if target_iteration >= limit:
                    matched = False
                if not matched:
                    continue
            target = nodes[edge["target"]]
            self._ensure_node_instance(conn, run_id, target, target_iteration, now)
            conn.execute(
                "INSERT OR IGNORE INTO workflow_edge_events(id,run_id,edge_key,source_node_run_id,"
                "target_node_key,target_iteration,matched,event_type,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,'resolved',?,?)",
                (
                    str(uuid.uuid4()),
                    run_id,
                    self._edge_key(index, edge),
                    source["id"],
                    edge["target"],
                    target_iteration,
                    1 if matched else 0,
                    canonical_json({"output": output}),
                    now,
                ),
            )

    def _advance_locked(
        self,
        conn: sqlite3.Connection,
        scope: WorkflowScope,
        run_id: str,
        spec: dict[str, Any],
        now: int,
    ) -> None:
        nodes = {node["id"]: node for node in spec["nodes"]}
        edges = list(spec["edges"])
        changed = True
        while changed:
            changed = False
            pending = conn.execute(
                "SELECT * FROM workflow_node_runs WHERE run_id=? AND state='pending' "
                "ORDER BY iteration,created_at,id",
                (run_id,),
            ).fetchall()
            for node_run in pending:
                iteration = int(node_run["iteration"])
                relevant: list[tuple[str, dict[str, Any]]] = []
                for index, edge in enumerate(edges):
                    if edge["target"] != node_run["node_key"]:
                        continue
                    if iteration == 0 and edge.get("loop"):
                        continue
                    if iteration > 0 and edge.get("loop") is False:
                        source_exists = conn.execute(
                            "SELECT 1 FROM workflow_node_runs WHERE run_id=? AND node_key=? "
                            "AND iteration=? LIMIT 1",
                            (run_id, edge["source"], iteration),
                        ).fetchone()
                        if source_exists is None:
                            continue
                    relevant.append((self._edge_key(index, edge), edge))
                if not relevant:
                    target_state = "ready"
                else:
                    event_rows = conn.execute(
                        "SELECT edge_key,matched FROM workflow_edge_events WHERE run_id=? "
                        "AND target_node_key=? AND target_iteration=? AND event_type='resolved'",
                        (run_id, node_run["node_key"], iteration),
                    ).fetchall()
                    events = {str(row["edge_key"]): bool(row["matched"]) for row in event_rows}
                    policy = str(nodes[node_run["node_key"]].get("join_policy") or "all")
                    matched_count = sum(1 for key, _edge in relevant if events.get(key) is True)
                    resolved_count = sum(1 for key, _edge in relevant if key in events)
                    if policy == "all" and resolved_count == len(relevant):
                        target_state = "ready" if matched_count == len(relevant) else "skipped"
                    elif policy in {"any", "first"} and matched_count:
                        target_state = "ready"
                    elif resolved_count == len(relevant):
                        target_state = "skipped"
                    else:
                        continue
                updated = conn.execute(
                    "UPDATE workflow_node_runs SET state=?,revision=revision+1,updated_at=?,"
                    "finished_at=? WHERE id=? AND state='pending'",
                    (
                        target_state,
                        now,
                        now if target_state == "skipped" else None,
                        node_run["id"],
                    ),
                ).rowcount
                if updated != 1:
                    continue
                changed = True
                current = conn.execute(
                    "SELECT * FROM workflow_node_runs WHERE id=?", (node_run["id"],)
                ).fetchone()
                if target_state == "ready":
                    self._insert_dispatch_intent(
                        conn, scope, run_id, current["id"], nodes[current["node_key"]], now
                    )
                else:
                    self._emit_outgoing_locked(
                        conn, scope, run_id, current, spec, source_matched=False, now=now
                    )

        states = {
            str(row["state"])
            for row in conn.execute(
                "SELECT state FROM workflow_node_runs WHERE run_id=?", (run_id,)
            ).fetchall()
        }
        if "failed" in states:
            final = "failed"
        elif states and states.issubset(TERMINAL_NODE_STATES):
            final = "succeeded"
        else:
            final = "running"
        finished = now if final in TERMINAL_RUN_STATES else None
        conn.execute(
            "UPDATE workflow_runs SET state=?,revision=revision+1,updated_at=?,finished_at=? "
            "WHERE id=? AND state!='cancelled'",
            (final, now, finished, run_id),
        )

    def finish_node(
        self,
        scope: WorkflowScope,
        run_id: str,
        node_run_id: str,
        *,
        succeeded: bool,
        output: Any = None,
        error: str = "",
        external_ref: str = "",
        now: int | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(scope)
        current = int(_now() if now is None else now)
        with self.transaction() as conn:
            run = self._require_run(conn, scope, run_id)
            if run["state"] != "running":
                raise WorkflowConflict("run is not active")
            node = conn.execute(
                "SELECT * FROM workflow_node_runs WHERE id=? AND run_id=?",
                (node_run_id, run_id),
            ).fetchone()
            if node is None:
                raise WorkflowNotFound("node run was not found")
            if node["state"] in TERMINAL_NODE_STATES:
                return self._run_payload(conn, run)
            if node["state"] not in {"ready", "dispatched", "running"}:
                raise WorkflowConflict("node run is not finishable")
            state = "succeeded" if succeeded else "failed"
            conn.execute(
                "UPDATE workflow_node_runs SET state=?,output_json=?,error=?,external_ref=CASE "
                "WHEN ?!='' THEN ? ELSE external_ref END,revision=revision+1,updated_at=?,finished_at=? "
                "WHERE id=?",
                (
                    state,
                    canonical_json(output) if output is not None else None,
                    str(error or "")[:4000],
                    external_ref,
                    str(external_ref or "")[:512],
                    current,
                    current,
                    node_run_id,
                ),
            )
            current_node = conn.execute(
                "SELECT * FROM workflow_node_runs WHERE id=?", (node_run_id,)
            ).fetchone()
            spec = self._version_spec(conn, run_id)
            if succeeded:
                self._emit_outgoing_locked(
                    conn, scope, run_id, current_node, spec, source_matched=True, now=current
                )
            self._advance_locked(conn, scope, run_id, spec, current)
            refreshed = conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
            return self._run_payload(conn, refreshed)

    def request_tool_approval(
        self,
        scope: WorkflowScope,
        run_id: str,
        node_run_id: str,
        *,
        tool_name: str,
        arguments: Any,
        expires_in: int = 900,
    ) -> dict[str, Any]:
        scope = self._scope(scope)
        name = str(tool_name or "").strip()[:256]
        if not name:
            raise ValueError("tool_name is required")
        args_hash = canonical_digest(arguments)
        with self.transaction() as conn:
            self._require_run(conn, scope, run_id)
            node = conn.execute(
                "SELECT 1 FROM workflow_node_runs WHERE id=? AND run_id=?",
                (node_run_id, run_id),
            ).fetchone()
            if node is None:
                raise WorkflowNotFound("node run was not found")
            existing = conn.execute(
                "SELECT * FROM workflow_tool_approval_requests WHERE run_id=? AND node_run_id=? "
                "AND tool_name=? AND args_hash=? AND state IN ('pending','approved','consumed') "
                "ORDER BY requested_at DESC, id DESC LIMIT 1",
                (run_id, node_run_id, name, args_hash),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            now = _now()
            approval_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO workflow_tool_approval_requests(id,account_id,account_generation,"
                "profile_id,run_id,node_run_id,tool_name,args_hash,state,revision,expires_at,"
                "requested_at) VALUES(?,?,?,?,?,?,?,?,'pending',1,?,?)",
                (
                    approval_id,
                    scope.account_id,
                    scope.account_generation,
                    scope.profile_id,
                    run_id,
                    node_run_id,
                    name,
                    args_hash,
                    now + max(30, min(int(expires_in), 86400)),
                    now,
                ),
            )
            return dict(conn.execute(
                "SELECT * FROM workflow_tool_approval_requests WHERE id=?", (approval_id,)
            ).fetchone())

    def get_tool_approval(
        self,
        scope: WorkflowScope,
        approval_id: str,
    ) -> dict[str, Any]:
        scope = self._scope(scope)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_tool_approval_requests WHERE id=? "
                "AND account_id=? AND account_generation=? AND profile_id=?",
                (
                    str(approval_id or ""),
                    scope.account_id,
                    scope.account_generation,
                    scope.profile_id,
                ),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound("tool approval was not found")
        return dict(row)

    def decide_tool_approval(
        self,
        scope: WorkflowScope,
        approval_id: str,
        *,
        expected_revision: int,
        decision: str,
        verdict: SecurityVerdict | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        scope = self._scope(scope)
        normalized = str(decision or "").strip().lower()
        if normalized not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        payload = {
            "approval_id": approval_id,
            "expected_revision": int(expected_revision),
            "decision": normalized,
        }
        with self.transaction() as conn:
            replay = self._replay(conn, scope, "tool-approval.decision", idempotency_key, payload)
            if replay is not None:
                return replay
            row = conn.execute(
                "SELECT * FROM workflow_tool_approval_requests WHERE id=? AND account_id=? "
                "AND account_generation=? AND profile_id=?",
                (approval_id, scope.account_id, scope.account_generation, scope.profile_id),
            ).fetchone()
            if row is None:
                raise WorkflowNotFound("tool approval was not found")
            now = _now()
            if row["state"] != "pending" or int(row["revision"]) != int(expected_revision):
                raise WorkflowConflict("tool approval revision or state changed")
            if int(row["expires_at"]) <= now:
                conn.execute(
                    "UPDATE workflow_tool_approval_requests SET state='expired',revision=revision+1,"
                    "decided_at=? WHERE id=? AND state='pending'",
                    (now, approval_id),
                )
                raise WorkflowConflict("tool approval expired")
            if normalized == "approve" and not (verdict or SecurityVerdict()).allows_one_shot:
                raise WorkflowSecurityError("security enforcement rejected this tool call")
            grant = secrets.token_urlsafe(32) if normalized == "approve" else ""
            token_hash = hashlib.sha256(grant.encode("utf-8")).hexdigest() if grant else None
            state = "approved" if normalized == "approve" else "rejected"
            conn.execute(
                "UPDATE workflow_tool_approval_requests SET state=?,revision=revision+1,"
                "remaining_uses=?,token_hash=?,decided_at=? WHERE id=? AND state='pending' "
                "AND revision=?",
                (
                    state,
                    1 if grant else 0,
                    token_hash,
                    now,
                    approval_id,
                    expected_revision,
                ),
            )
            response = dict(conn.execute(
                "SELECT * FROM workflow_tool_approval_requests WHERE id=?", (approval_id,)
            ).fetchone())
            if grant:
                response["grant_token"] = grant
            self._remember(
                conn, scope, "tool-approval.decision", idempotency_key, payload, response
            )
            return response

    def consume_tool_approval(
        self,
        scope: WorkflowScope,
        approval_id: str,
        *,
        run_id: str,
        node_run_id: str,
        tool_name: str,
        arguments: Any,
        grant_token: str,
        now: int | None = None,
    ) -> bool:
        scope = self._scope(scope)
        current = int(_now() if now is None else now)
        token_hash = hashlib.sha256(str(grant_token or "").encode("utf-8")).hexdigest()
        args_hash = canonical_digest(arguments)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_tool_approval_requests WHERE id=? AND account_id=? "
                "AND account_generation=? AND profile_id=? AND run_id=? AND node_run_id=? "
                "AND tool_name=? AND args_hash=?",
                (
                    approval_id,
                    scope.account_id,
                    scope.account_generation,
                    scope.profile_id,
                    run_id,
                    node_run_id,
                    str(tool_name or "").strip(),
                    args_hash,
                ),
            ).fetchone()
            if row is None or row["state"] != "approved" or int(row["remaining_uses"]) != 1:
                return False
            if int(row["expires_at"]) <= current:
                conn.execute(
                    "UPDATE workflow_tool_approval_requests SET state='expired',revision=revision+1,"
                    "remaining_uses=0 WHERE id=? AND state='approved'",
                    (approval_id,),
                )
                return False
            if not hmac.compare_digest(str(row["token_hash"] or ""), token_hash):
                return False
            return conn.execute(
                "UPDATE workflow_tool_approval_requests SET state='consumed',revision=revision+1,"
                "remaining_uses=0,consumed_at=? WHERE id=? AND state='approved' "
                "AND remaining_uses=1",
                (current, approval_id),
            ).rowcount == 1

    @staticmethod
    def _safe_change_path(value: str) -> str:
        raw = str(value or "").replace("\\", "/").strip()
        path = PurePosixPath(raw)
        lowered = {part.lower() for part in path.parts}
        name = path.name.lower()
        if (
            not raw
            or path.is_absolute()
            or (len(raw) >= 3 and raw[0].isalpha() and raw[1:3] == ":/")
            or ".." in path.parts
            or any(ord(character) < 32 for character in raw)
            or any(part in SENSITIVE_PARTS for part in lowered)
            or name.startswith(".env.")
            or PurePosixPath(name).suffix in {".key", ".p12", ".pem", ".pfx"}
        ):
            raise WorkflowSecurityError("workspace change path is not eligible for audit storage")
        return str(path)

    def _prepare_workspace_changes(
        self, files: Sequence[dict[str, Any]]
    ) -> list[tuple[str, str, bytes]]:
        if len(files) > MAX_WORKSPACE_FILES:
            raise ValueError(f"workspace change set exceeds {MAX_WORKSPACE_FILES} files")
        prepared: list[tuple[str, str, bytes]] = []
        seen: set[str] = set()
        total_bytes = 0
        for item in files:
            path = self._safe_change_path(str(item.get("path") or ""))
            if path in seen:
                raise ValueError(f"duplicate workspace change path: {path}")
            seen.add(path)
            change_type = str(item.get("change_type") or "modified").strip().lower()
            if change_type not in {"added", "modified", "deleted", "renamed"}:
                raise ValueError("invalid workspace change type")
            raw = item.get("patch", "")
            data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
            try:
                data = redact_secrets(data.decode("utf-8")).encode("utf-8")
            except UnicodeDecodeError:
                pass
            total_bytes += len(data)
            if total_bytes > MAX_WORKSPACE_FILE_BYTES:
                raise ValueError(
                    f"workspace change set patch exceeds {MAX_WORKSPACE_FILE_BYTES} bytes"
                )
            prepared.append((path, change_type, data))
        return prepared

    def _insert_workspace_changes_locked(
        self,
        conn: sqlite3.Connection,
        scope: WorkflowScope,
        run_id: str,
        *,
        turn_id: str,
        prepared: Sequence[tuple[str, str, bytes]],
        summary: str,
        now: int,
    ) -> str:
        change_set_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO workflow_workspace_change_sets(id,account_id,account_generation,"
            "profile_id,run_id,turn_id,summary,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                change_set_id,
                scope.account_id,
                scope.account_generation,
                scope.profile_id,
                run_id,
                str(turn_id or "")[:512],
                str(summary or "")[:2000],
                now,
            ),
        )
        aes = AESGCM(self._audit_key)
        for path, change_type, data in prepared:
            nonce = secrets.token_bytes(12)
            aad = canonical_json(
                {
                    "account": scope.account_id,
                    "generation": scope.account_generation,
                    "profile": scope.profile_id,
                    "run": run_id,
                    "change_set": change_set_id,
                    "path": path,
                }
            ).encode("utf-8")
            ciphertext = aes.encrypt(nonce, data, aad)
            conn.execute(
                "INSERT INTO workflow_workspace_change_files(id,change_set_id,relative_path,"
                "change_type,sha256,nonce,ciphertext,byte_count,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    change_set_id,
                    path,
                    change_type,
                    hashlib.sha256(data).hexdigest(),
                    nonce,
                    ciphertext,
                    len(data),
                    now,
                ),
            )
        return change_set_id

    def record_workspace_changes(
        self,
        scope: WorkflowScope,
        run_id: str,
        *,
        turn_id: str,
        files: Sequence[dict[str, Any]],
        summary: str = "",
    ) -> dict[str, Any]:
        scope = self._scope(scope)
        prepared = self._prepare_workspace_changes(files)
        with self.transaction() as conn:
            self._require_run(conn, scope, run_id)
            current = _now()
            change_set_id = self._insert_workspace_changes_locked(
                conn,
                scope,
                run_id,
                turn_id=turn_id,
                prepared=prepared,
                summary=summary,
                now=current,
            )
            return {
                "id": change_set_id,
                "run_id": run_id,
                "file_count": len(prepared),
                "byte_count": sum(len(item[2]) for item in prepared),
            }

    def list_workspace_changes(
        self, scope: WorkflowScope, run_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        scope = self._scope(scope)
        with self.connect() as conn:
            self._require_run(conn, scope, run_id)
            return self._workspace_change_summaries(conn, run_id, limit=limit)

    def list_workspace_audits(
        self, scope: WorkflowScope, run_id: str
    ) -> list[dict[str, Any]]:
        scope = self._scope(scope)
        with self.connect() as conn:
            self._require_run(conn, scope, run_id)
            return list(self._workspace_audit_summaries(conn, run_id).values())

    def get_workspace_changes(
        self, scope: WorkflowScope, run_id: str, change_set_id: str
    ) -> dict[str, Any]:
        scope = self._scope(scope)
        with self.connect() as conn:
            self._require_run(conn, scope, run_id)
            change_set = conn.execute(
                "SELECT * FROM workflow_workspace_change_sets WHERE id=? AND run_id=? "
                "AND account_id=? AND account_generation=? AND profile_id=?",
                (
                    change_set_id,
                    run_id,
                    scope.account_id,
                    scope.account_generation,
                    scope.profile_id,
                ),
            ).fetchone()
            if change_set is None:
                raise WorkflowNotFound("workspace change set was not found")
            aes = AESGCM(self._audit_key)
            files: list[dict[str, Any]] = []
            for row in conn.execute(
                "SELECT * FROM workflow_workspace_change_files WHERE change_set_id=? "
                "ORDER BY relative_path",
                (change_set_id,),
            ).fetchall():
                aad = canonical_json(
                    {
                        "account": scope.account_id,
                        "generation": scope.account_generation,
                        "profile": scope.profile_id,
                        "run": run_id,
                        "change_set": change_set_id,
                        "path": row["relative_path"],
                    }
                ).encode("utf-8")
                patch = aes.decrypt(row["nonce"], row["ciphertext"], aad)
                files.append(
                    {
                        "path": row["relative_path"],
                        "change_type": row["change_type"],
                        "sha256": row["sha256"],
                        "byte_count": row["byte_count"],
                        "patch": patch.decode("utf-8", errors="replace"),
                    }
                )
            result = dict(change_set)
            result["files"] = files
            return result

    @staticmethod
    def _workspace_baseline_aad(
        scope: WorkflowScope, run_id: str, node_run_id: str
    ) -> bytes:
        return canonical_json(
            {
                "account": scope.account_id,
                "generation": scope.account_generation,
                "profile": scope.profile_id,
                "run": run_id,
                "node_run": node_run_id,
                "kind": "workspace-baseline",
            }
        ).encode("utf-8")

    @staticmethod
    def _require_node_run(
        conn: sqlite3.Connection, run_id: str, node_run_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM workflow_node_runs WHERE id=? AND run_id=?",
            (node_run_id, run_id),
        ).fetchone()
        if row is None:
            raise WorkflowNotFound("node run was not found")
        return row

    @staticmethod
    def _workspace_baseline_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "node_run_id": row["node_run_id"],
            "run_id": row["run_id"],
            "state": row["state"],
            "reason": row["reason"],
            "file_count": int(row["file_count"]),
            "byte_count": int(row["byte_count"]),
            "change_set_id": row["change_set_id"],
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
            "finalized_at": row["finalized_at"],
        }

    def save_workspace_baseline(
        self,
        scope: WorkflowScope,
        run_id: str,
        node_run_id: str,
        *,
        workspace_kind: str,
        workspace_path: str,
        snapshot: dict[str, Any],
        now: int | None = None,
    ) -> dict[str, Any]:
        """Encrypt a pre-execution snapshot exactly once for a node run."""

        scope = self._scope(scope)
        current = int(_now() if now is None else now)
        payload = canonical_json(
            {
                "workspace_kind": str(workspace_kind or "")[:32],
                "workspace_path": str(workspace_path or ""),
                "snapshot": snapshot,
            }
        ).encode("utf-8")
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._audit_key).encrypt(
            nonce,
            payload,
            self._workspace_baseline_aad(scope, run_id, node_run_id),
        )
        file_count = max(0, int(snapshot.get("file_count") or 0))
        byte_count = max(0, int(snapshot.get("captured_bytes") or 0))
        with self.transaction() as conn:
            self._require_run(conn, scope, run_id)
            self._require_node_run(conn, run_id, node_run_id)
            existing = conn.execute(
                "SELECT * FROM workflow_workspace_baselines WHERE node_run_id=? "
                "AND account_id=? AND account_generation=? AND profile_id=? AND run_id=?",
                (
                    node_run_id,
                    scope.account_id,
                    scope.account_generation,
                    scope.profile_id,
                    run_id,
                ),
            ).fetchone()
            if existing is not None and existing["state"] != "pending":
                return self._workspace_baseline_summary(existing)
            if existing is None:
                conn.execute(
                    "INSERT INTO workflow_workspace_baselines(node_run_id,account_id,"
                    "account_generation,profile_id,run_id,state,file_count,byte_count,nonce,"
                    "ciphertext,created_at,updated_at) VALUES(?,?,?,?,?,'captured',?,?,?,?,?,?)",
                    (
                        node_run_id,
                        scope.account_id,
                        scope.account_generation,
                        scope.profile_id,
                        run_id,
                        file_count,
                        byte_count,
                        nonce,
                        ciphertext,
                        current,
                        current,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE workflow_workspace_baselines SET state='captured',reason='',"
                    "file_count=?,byte_count=?,nonce=?,ciphertext=?,updated_at=? "
                    "WHERE node_run_id=? AND state='pending'",
                    (file_count, byte_count, nonce, ciphertext, current, node_run_id),
                )
            row = conn.execute(
                "SELECT * FROM workflow_workspace_baselines WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            return self._workspace_baseline_summary(row)

    def get_workspace_baseline(
        self, scope: WorkflowScope, run_id: str, node_run_id: str
    ) -> dict[str, Any] | None:
        scope = self._scope(scope)
        with self.connect() as conn:
            self._require_run(conn, scope, run_id)
            self._require_node_run(conn, run_id, node_run_id)
            row = conn.execute(
                "SELECT * FROM workflow_workspace_baselines WHERE node_run_id=? "
                "AND account_id=? AND account_generation=? AND profile_id=? AND run_id=?",
                (
                    node_run_id,
                    scope.account_id,
                    scope.account_generation,
                    scope.profile_id,
                    run_id,
                ),
            ).fetchone()
            if row is None:
                return None
            result = self._workspace_baseline_summary(row)
            if row["state"] == "captured":
                if row["nonce"] is None or row["ciphertext"] is None:
                    raise WorkflowSecurityError("workspace baseline ciphertext is missing")
                plaintext = AESGCM(self._audit_key).decrypt(
                    row["nonce"],
                    row["ciphertext"],
                    self._workspace_baseline_aad(scope, run_id, node_run_id),
                )
                result["payload"] = _json_load(plaintext, {})
            return result

    def mark_workspace_audit_unavailable(
        self,
        scope: WorkflowScope,
        run_id: str,
        node_run_id: str,
        *,
        reason: str,
        discard_baseline: bool = False,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Persist an explicit no-audit result without overwriting a baseline."""

        scope = self._scope(scope)
        current = int(_now() if now is None else now)
        bounded_reason = str(reason or "workspace audit is unavailable")[:1000]
        with self.transaction() as conn:
            self._require_run(conn, scope, run_id)
            self._require_node_run(conn, run_id, node_run_id)
            row = conn.execute(
                "SELECT * FROM workflow_workspace_baselines WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO workflow_workspace_baselines(node_run_id,account_id,"
                    "account_generation,profile_id,run_id,state,reason,created_at,updated_at,"
                    "finalized_at) VALUES(?,?,?,?,?,'unavailable',?,?,?,?)",
                    (
                        node_run_id,
                        scope.account_id,
                        scope.account_generation,
                        scope.profile_id,
                        run_id,
                        bounded_reason,
                        current,
                        current,
                        current,
                    ),
                )
            elif row["state"] == "pending" or (
                discard_baseline and row["state"] == "captured"
            ):
                conn.execute(
                    "UPDATE workflow_workspace_baselines SET state='unavailable',reason=?,"
                    "nonce=NULL,ciphertext=NULL,updated_at=?,finalized_at=? WHERE node_run_id=?",
                    (bounded_reason, current, current, node_run_id),
                )
            row = conn.execute(
                "SELECT * FROM workflow_workspace_baselines WHERE node_run_id=?",
                (node_run_id,),
            ).fetchone()
            return self._workspace_baseline_summary(row)

    def finalize_workspace_audit(
        self,
        scope: WorkflowScope,
        run_id: str,
        node_run_id: str,
        *,
        files: Sequence[dict[str, Any]],
        summary: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Atomically store the final Diff and consume the encrypted baseline."""

        scope = self._scope(scope)
        prepared = self._prepare_workspace_changes(files)
        current = int(_now() if now is None else now)
        with self.transaction() as conn:
            self._require_run(conn, scope, run_id)
            self._require_node_run(conn, run_id, node_run_id)
            baseline = conn.execute(
                "SELECT * FROM workflow_workspace_baselines WHERE node_run_id=? "
                "AND account_id=? AND account_generation=? AND profile_id=? AND run_id=?",
                (
                    node_run_id,
                    scope.account_id,
                    scope.account_generation,
                    scope.profile_id,
                    run_id,
                ),
            ).fetchone()
            if baseline is None:
                raise WorkflowConflict("workspace baseline was not captured")
            if baseline["state"] == "recorded" and baseline["change_set_id"]:
                summaries = self._workspace_change_summaries(conn, run_id, limit=500)
                return next(
                    item for item in summaries if item["id"] == baseline["change_set_id"]
                )
            if baseline["state"] != "captured":
                raise WorkflowConflict("workspace baseline is not finalizable")
            change_set_id = self._insert_workspace_changes_locked(
                conn,
                scope,
                run_id,
                turn_id=node_run_id,
                prepared=prepared,
                summary=summary,
                now=current,
            )
            changed = conn.execute(
                "UPDATE workflow_workspace_baselines SET state='recorded',reason='',"
                "change_set_id=?,nonce=NULL,ciphertext=NULL,updated_at=?,finalized_at=? "
                "WHERE node_run_id=? AND state='captured'",
                (change_set_id, current, current, node_run_id),
            ).rowcount
            if changed != 1:
                raise WorkflowConflict("workspace baseline changed during finalization")
            return next(
                item
                for item in self._workspace_change_summaries(conn, run_id, limit=500)
                if item["id"] == change_set_id
            )

    @staticmethod
    def _purge_account_rows(conn: sqlite3.Connection, account: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO workflow_deleting_definitions(id) "
            "SELECT id FROM workflow_definitions WHERE account_id=?",
            (account,),
        )
        conn.execute("DELETE FROM workflow_idempotency WHERE account_id=?", (account,))
        conn.execute(
            "DELETE FROM workflow_tool_approval_requests WHERE account_id=?",
            (account,),
        )
        conn.execute(
            "DELETE FROM workflow_workspace_change_sets WHERE account_id=?",
            (account,),
        )
        conn.execute(
            "DELETE FROM workflow_dispatch_intents WHERE account_id=?",
            (account,),
        )
        conn.execute("DELETE FROM workflow_runs WHERE account_id=?", (account,))
        conn.execute("DELETE FROM workflow_definitions WHERE account_id=?", (account,))
        conn.execute(
            "DELETE FROM workflow_deleting_definitions WHERE id NOT IN "
            "(SELECT id FROM workflow_definitions)"
        )

    def finalize_account_deletions(self, *, limit: int = 20) -> int:
        """Purge tombstoned accounts only after every cancel outbox row is terminal."""

        bounded = max(1, min(int(limit), 100))
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT d.account_id FROM workflow_account_deletions d "
                "WHERE (EXISTS(SELECT 1 FROM workflow_definitions w "
                "WHERE w.account_id=d.account_id) OR EXISTS("
                "SELECT 1 FROM workflow_runs r WHERE r.account_id=d.account_id)) "
                "AND NOT EXISTS(SELECT 1 FROM workflow_dispatch_intents i "
                "WHERE i.account_id=d.account_id AND i.state!='cancelled') "
                "ORDER BY d.deleted_at,d.account_id LIMIT ?",
                (bounded,),
            ).fetchall()
            for row in rows:
                self._purge_account_rows(conn, str(row["account_id"]))
            return len(rows)

    def delete_account(
        self,
        account_id: str,
        *,
        now: int | None = None,
    ) -> dict[str, int]:
        account = str(account_id or "").strip()
        if not account:
            raise ValueError("account_id is required")
        with self.transaction() as conn:
            current = int(_now() if now is None else now)
            conn.execute(
                "INSERT OR IGNORE INTO workflow_account_deletions(account_id,deleted_at) VALUES(?,?)",
                (account, current),
            )
            run_count = int(conn.execute(
                "SELECT COUNT(*) FROM workflow_runs WHERE account_id=?", (account,)
            ).fetchone()[0])
            definition_count = int(conn.execute(
                "SELECT COUNT(*) FROM workflow_definitions WHERE account_id=?", (account,)
            ).fetchone()[0])

            conn.execute(
                "UPDATE workflow_runs SET state='cancelled',revision=revision+1,"
                "cancel_reason=CASE WHEN cancel_reason='' THEN 'account_deleted' "
                "ELSE cancel_reason END,updated_at=?,finished_at=? "
                "WHERE account_id=? AND state='running'",
                (current, current, account),
            )
            placeholders = ",".join("?" for _ in TERMINAL_NODE_STATES)
            conn.execute(
                f"UPDATE workflow_node_runs SET state='cancelled',revision=revision+1,"
                f"updated_at=?,finished_at=? WHERE run_id IN ("
                f"SELECT id FROM workflow_runs WHERE account_id=?) "
                f"AND state NOT IN ({placeholders})",
                (current, current, account, *sorted(TERMINAL_NODE_STATES)),
            )
            # A claimed intent may have crossed the external create boundary
            # even when its reference was never recorded. Keep those rows as a
            # cancellation outbox; only never-claimed intents are locally final.
            conn.execute(
                "UPDATE workflow_dispatch_intents SET state=CASE WHEN "
                "attempts>0 OR external_ref!='' THEN 'cancel_pending' "
                "ELSE 'cancelled' END,available_at=?,lease_token=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE account_id=? "
                "AND state NOT IN ('cancelled','cancel_pending','cancel_retry',"
                "'cancel_delivering')",
                (current, current, account),
            )
            pending = int(conn.execute(
                "SELECT COUNT(*) FROM workflow_dispatch_intents "
                "WHERE account_id=? AND state!='cancelled'",
                (account,),
            ).fetchone()[0])
            if pending:
                return {
                    "definitions": definition_count,
                    "runs": run_count,
                    "pending_cancellations": pending,
                }

            self._purge_account_rows(conn, account)
            return {"definitions": definition_count, "runs": run_count}
