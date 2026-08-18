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
