from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from plugins.workflows.dashboard import plugin_api
from plugins.workflows.models import WorkflowScope
from plugins.workflows.store import WorkflowStore


def _request(owner: str, generation: str, provider: str = "owner-mobile"):
    return SimpleNamespace(
        state=SimpleNamespace(
            session=None,
            token_principal=SimpleNamespace(
                principal=owner,
                provider=provider,
                account_generation=generation,
            ),
        )
    )


def _spec() -> dict:
    return {"nodes": [{"id": "first", "type": "agent"}]}


def test_workflow_scope_uses_authenticated_live_generation(monkeypatch):
    class FakeMobileStore:
        def account_generation(self, owner_id: str, *, create: bool):
            assert owner_id == "alice"
            assert create is True
            return "acctgen-live"

    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.mobile_device_store.MobileDeviceStore",
        FakeMobileStore,
    )

    scope = plugin_api._scope(_request("alice", "acctgen-live"), "default")

    assert scope == WorkflowScope("alice", "acctgen-live", "default")


def test_workflow_scope_rejects_stale_mobile_generation(monkeypatch):
    class FakeMobileStore:
        def account_generation(self, _owner_id: str, *, create: bool):
            return "acctgen-new"

    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.mobile_device_store.MobileDeviceStore",
        FakeMobileStore,
    )

    with pytest.raises(plugin_api.HTTPException) as error:
        plugin_api._scope(_request("alice", "acctgen-old"), "default")

    assert error.value.status_code == 410


def test_workflow_scope_preserves_local_legacy_generation():
    request = SimpleNamespace(state=SimpleNamespace(session=None, token_principal=None))

    scope = plugin_api._scope(request, "default")

    assert scope == WorkflowScope("local-owner", "1", "default")


def test_workflow_delete_is_generation_scoped(tmp_path):
    store = WorkflowStore(tmp_path / "workflows.db", audit_key=b"a" * 32)
    old_scope = WorkflowScope("alice", "acctgen-old", "default")
    new_scope = WorkflowScope("alice", "acctgen-new", "default")
    store.create_definition(
        old_scope,
        name="old",
        description="",
        spec=_spec(),
        idempotency_key="old-create",
    )
    store.create_definition(
        new_scope,
        name="new",
        description="",
        spec=_spec(),
        idempotency_key="new-create",
    )

    deleted = store.delete_account("alice", account_generation="acctgen-old")

    assert deleted["definitions"] == 1
    assert all(isinstance(value, int) for value in deleted.values())
    assert store.list_definitions(old_scope) == []
    assert [item["name"] for item in store.list_definitions(new_scope)] == ["new"]


def test_workflow_store_migrates_v1_account_tombstones(tmp_path):
    path = tmp_path / "workflows.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE workflow_schema(version INTEGER NOT NULL)")
        conn.execute("INSERT INTO workflow_schema(version) VALUES (1)")
        conn.execute(
            "CREATE TABLE workflow_account_deletions("
            "account_id TEXT PRIMARY KEY, deleted_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO workflow_account_deletions(account_id,deleted_at) VALUES (?,?)",
            ("alice", 123),
        )

    store = WorkflowStore(path, audit_key=b"a" * 32)

    with store.connect() as conn:
        columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(workflow_account_deletions)"
            ).fetchall()
        }
        tombstone = conn.execute(
            "SELECT account_id,account_generation,deleted_at "
            "FROM workflow_account_deletions"
        ).fetchone()
        version = conn.execute("SELECT version FROM workflow_schema").fetchone()[0]
    assert columns == {"account_id", "account_generation", "deleted_at"}
    assert dict(tombstone) == {
        "account_id": "alice",
        "account_generation": "legacy",
        "deleted_at": 123,
    }
    assert version == 3


def test_legacy_generation_rows_rekey_once_to_live_generation(tmp_path, monkeypatch):
    """Pre-fence "1" workflows move to the account's live generation exactly once."""
    store = WorkflowStore(tmp_path / "workflows.db", audit_key=b"a" * 32)
    legacy_scope = WorkflowScope("alice", "1", "default")
    store.create_definition(
        legacy_scope,
        name="legacy",
        description="",
        spec=_spec(),
        idempotency_key="legacy-1",
    )
    assert store.list_definitions(WorkflowScope("alice", "acctgen-live", "default")) == []
    assert store.legacy_generation_accounts() == ["alice"]
    assert not store.legacy_generation_migration_done("alice")

    generations = iter(["acctgen-live", "acctgen-live"])

    class FakeMobileStore:
        def account_generation(self, owner_id: str, *, create: bool):
            assert owner_id == "alice"
            return next(generations)

    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.mobile_device_store.MobileDeviceStore",
        FakeMobileStore,
    )
    plugin_api._STORE = None
    monkeypatch.setattr(plugin_api, "workflow_store", lambda: store)

    scope = plugin_api._scope(_request("alice", "acctgen-live"), "default")
    assert scope.account_generation == "acctgen-live"
    migrated = store.list_definitions(WorkflowScope("alice", "acctgen-live", "default"))
    assert [d["name"] for d in migrated] == ["legacy"]
    assert store.list_definitions(legacy_scope) == []
    assert store.legacy_generation_migration_done("alice")

    # After the marker commits, a later era (account reset → new generation)
    # must never resurrect or re-key the old rows again.
    generations = iter(["acctgen-newera", "acctgen-newera"])
    plugin_api._scope(_request("alice", "acctgen-newera"), "default")
    assert store.list_definitions(WorkflowScope("alice", "acctgen-live", "default"))
    assert store.list_definitions(WorkflowScope("alice", "acctgen-newera", "default")) == []


def test_legacy_generation_conflict_migrates_per_row_and_stays_visible(tmp_path):
    """A UNIQUE collision must not strand the rest of the legacy era."""
    store = WorkflowStore(tmp_path / "workflows.db", audit_key=b"a" * 32)
    legacy_scope = WorkflowScope("alice", "1", "default")
    live_scope = WorkflowScope("alice", "acctgen-live", "default")
    store.create_definition(legacy_scope, name="shared", description="", spec=_spec(), idempotency_key="k1")
    store.create_definition(legacy_scope, name="only-legacy", description="", spec=_spec(), idempotency_key="k2")
    # The live era re-created "shared": rekeying it would collide on
    # UNIQUE(account_id, account_generation, profile_id, name).
    store.create_definition(live_scope, name="shared", description="", spec=_spec(), idempotency_key="k3")

    # The bulk rekey raises IntegrityError; the fallback moves what fits.
    with pytest.raises(sqlite3.IntegrityError):
        store.rekey_account_generation("alice", "1", "acctgen-live")
    moved, conflicts = store.rekey_account_generation_per_row("alice", "1", "acctgen-live")

    assert moved >= 1
    assert conflicts == [
        {
            "table": "workflow_definitions",
            "key": "default/shared",
            "detail": "definition 'shared' already exists in the live generation",
        }
    ]
    store.mark_legacy_generation_migration_partial("alice", conflicts)

    # Partial counts as done (no per-request retries) but is distinguishable.
    assert store.legacy_generation_migration_done("alice") is True
    status = store.legacy_generation_migration_status("alice")
    assert status["status"] == "partial"
    assert status["conflicts"][0]["key"] == "default/shared"

    # The non-conflicting legacy row reached the live era...
    live_names = {d["name"] for d in store.list_definitions(live_scope)}
    assert "only-legacy" in live_names
    # ...while the conflicted workflow stays visible and flagged.
    conflict_defs = store.legacy_conflict_definitions("alice")
    assert [d["name"] for d in conflict_defs] == ["shared"]
