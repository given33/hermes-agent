from __future__ import annotations

import asyncio
import time
import json
import sqlite3

import pytest

from hermes_cli import managed_installations
from hermes_cli import cloud_file_library
from hermes_cli.dashboard_auth import mobile_device_store
from plugins.collaboration.dashboard import plugin_api
from hermes_services.worker_channel import WorkerChannelRegistry
from tools.managed_installation_tool import managed_installation


def _complete(db, *, owner: str, generation: str, identifier: str):
    managed_installations.create_managed_installation(
        kind="skill",
        identifier=identifier,
        request_id=f"install-{owner}",
        targets=["server"],
        db_path=db,
        owner_id=owner,
        account_generation=generation,
    )
    claim = managed_installations._claim_target(db, now=time.time(), lease_seconds=30)
    assert claim is not None
    assert managed_installations._finish_target(db, claim, state="completed", detail={})
    managed_installations._release_execution_fence(claim)


def test_connector_deployment_health_proves_release_and_database_schemas(
    tmp_path,
    monkeypatch,
):
    managed_db = tmp_path / "managed-installations.db"
    mobile_db = tmp_path / "mobile-auth.db"
    library = cloud_file_library.CloudFileLibrary(tmp_path / "cloud-files")
    monkeypatch.setattr(plugin_api, "_file_library", lambda: library)
    monkeypatch.setattr(
        mobile_device_store,
        "mobile_auth_db_path",
        lambda: mobile_db,
    )
    monkeypatch.setattr(
        managed_installations,
        "managed_installations_db_path",
        lambda: managed_db,
    )
    monkeypatch.setattr(
        plugin_api,
        "connector_health",
        lambda _request: {
            "ok": True,
            "connector_id": "dbb3-primary",
            "contract_version": 2,
            "capabilities": ["artifact-upload", "attachment-download"],
        },
    )
    registry = WorkerChannelRegistry()
    registry.connect(
        "dbb3-worker",
        connection_generation="deployment-generation",
        release={
            "node_id": "dbb3",
            "commit": "a" * 40,
            "version": "1.2.3",
        },
    )
    monkeypatch.setattr(plugin_api, "_WORKER_CHANNEL", registry)

    result = plugin_api.connector_deployment_health(object())

    assert result["ok"] is True
    assert result["managed_catalog_readable"] is True
    assert result["worker_channel"] == {
        "node_id": "dbb3-worker",
        "managed_node_id": "dbb3",
        "online": True,
        "fresh": True,
        "connection_generation": "deployment-generation",
        "observed_at": result["worker_channel"]["observed_at"],
        "version": "1.2.3",
        "release": {
            "node_id": "dbb3",
            "commit": "a" * 40,
            "version": "1.2.3",
        },
    }
    assert result["manifest_version"]
    assert len(result["manifest_sha256"]) == 64
    assert set(result["databases"]) == {
        "cloud_files",
        "mobile_auth",
        "managed_resources",
    }
    for status in result["databases"].values():
        assert status["ok"] is True
        assert status["db_user_version"] == status["code_schema_version"]
        assert status["integrity_check"] == "ok"
        assert len(status["schema_sha256"]) == 64
    assert result["databases"]["managed_resources"]["catalog_rows"] == 0


def test_managed_installation_schema_rejects_a_newer_database(tmp_path):
    db = tmp_path / "managed-installations.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"PRAGMA user_version={managed_installations.MANAGED_INSTALLATIONS_SCHEMA_VERSION + 1}"
        )

    with pytest.raises(RuntimeError, match="newer Hermes version"):
        managed_installations._connect(db)


def test_authenticated_catalog_endpoint_filters_owner_and_generation(tmp_path, monkeypatch):
    db = tmp_path / "managed-installations.db"
    _complete(db, owner="alice", generation="alice-gen", identifier="alice-skill")
    _complete(db, owner="bob", generation="bob-gen", identifier="bob-skill")
    monkeypatch.setattr(
        managed_installations, "managed_installations_db_path", lambda: db
    )
    monkeypatch.setattr(plugin_api, "owner_id_from_request", lambda _request: "alice")
    monkeypatch.setattr(
        plugin_api, "_account_generation_for_owner", lambda _owner: "alice-gen"
    )

    result = plugin_api.list_mobile_managed_resources(object())

    assert result["account_generation"] == "alice-gen"
    assert [item["name"] for item in result["resources"]] == ["alice-skill"]
    assert len(result["events"]) == 1
    assert result["events"][0]["resource"]["name"] == "alice-skill"


def test_collaboration_account_deletion_invokes_managed_resource_cleanup(monkeypatch):
    monkeypatch.setattr(
        plugin_api,
        "load_single_state",
        lambda **_kwargs: {"conversations": []},
    )
    monkeypatch.setattr(plugin_api, "save_single_state", lambda _state: None)
    monkeypatch.setattr(plugin_api, "load_state", lambda: {"rooms": []})
    monkeypatch.setattr(plugin_api, "save_state", lambda _state: None)

    class Library:
        def delete_owner(self, owner, *, account_generation):
            assert owner == "alice"
            assert account_generation == "alice-generation"
            return {"deleted": 0}

    class ArtifactStore:
        def __init__(self, _root):
            pass

        def delete_owner(
            self,
            owner,
            *,
            account_generation,
            include_known_generations=False,
        ):
            assert owner == "alice"
            assert account_generation == "alice-generation"
            assert include_known_generations is False
            return {"deleted": 0}

    cleanup_calls = []
    monkeypatch.setattr(plugin_api, "_file_library", lambda: Library())
    monkeypatch.setattr(plugin_api, "EncryptedToolArtifactStore", ArtifactStore)
    monkeypatch.setattr(
        managed_installations,
        "delete_owner_managed_resources",
        lambda owner, *, account_generation, include_known_generations=False: cleanup_calls.append(
            (owner, account_generation, include_known_generations)
        )
        or {"resources": 1, "events": 1, "operations": 1},
    )

    result = plugin_api.delete_owner_account_data(
        "alice",
        account_generation="alice-generation",
    )

    assert cleanup_calls == [("alice", "alice-generation", False)]
    assert result["managed_resources"] == {
        "resources": 1,
        "events": 1,
        "operations": 1,
    }


def test_agent_managed_installation_status_cannot_read_another_account(tmp_path, monkeypatch):
    db = tmp_path / "managed-installations.db"
    bob = managed_installations.create_managed_installation(
        kind="skill",
        identifier="bob-private-skill",
        request_id="bob-private-install",
        targets=["server"],
        db_path=db,
        owner_id="bob",
        account_generation="bob-gen",
    )
    monkeypatch.setattr(
        managed_installations, "managed_installations_db_path", lambda: db
    )
    monkeypatch.setenv("HERMES_TOOL_ARTIFACT_OWNER", "alice")
    monkeypatch.setenv("HERMES_ACCOUNT_GENERATION", "alice-gen")

    result = managed_installation(
        {"action": "status", "operation_id": bob["id"]}
    )

    assert '"error": "installation_not_found"' in result
    assert "bob-private-skill" not in result


def test_hosted_agent_install_tool_persists_authenticated_owner_generation(tmp_path, monkeypatch):
    db = tmp_path / "managed-installations.db"
    monkeypatch.setattr(
        managed_installations, "managed_installations_db_path", lambda: db
    )
    monkeypatch.setattr(
        managed_installations,
        "require_managed_installation_topology",
        lambda _targets, **_kwargs: None,
    )
    monkeypatch.setenv("HERMES_TOOL_ARTIFACT_OWNER", "alice")
    monkeypatch.setenv("HERMES_ACCOUNT_GENERATION", "alice-gen")

    result = json.loads(
        managed_installation(
            {
                "action": "install",
                "kind": "skill",
                "identifier": "account-skill",
                "request_id": "mobile-install-1",
                "targets": ["server"],
            }
        )
    )

    assert result["accepted"] is True
    operation = result["operation"]
    assert operation["owner_id"] == "alice"
    assert operation["account_generation"] == "alice-gen"
    persisted = managed_installations.get_managed_installation(
        operation["id"],
        db_path=db,
        owner_id="alice",
        account_generation="alice-gen",
    )
    assert persisted["request_id"] == "mobile-install-1"


def test_mobile_install_routes_create_and_list_only_current_account(tmp_path, monkeypatch):
    db = tmp_path / "managed-installations.db"
    monkeypatch.setattr(
        managed_installations, "managed_installations_db_path", lambda: db
    )
    monkeypatch.setattr(
        managed_installations,
        "require_managed_installation_topology",
        lambda _targets, **_kwargs: None,
    )
    monkeypatch.setattr(plugin_api, "owner_id_from_request", lambda _request: "alice")
    monkeypatch.setattr(
        plugin_api, "_account_generation_for_owner", lambda _owner: "alice-gen"
    )
    _complete(db, owner="bob", generation="bob-gen", identifier="bob-skill")

    created = plugin_api.create_mobile_managed_installation(
        plugin_api.MobileManagedInstallationBody(
            kind="skill",
            identifier="alice-skill",
            request_id="mobile-install-1",
            targets=["server"],
        ),
        object(),
    )
    listing = plugin_api.list_mobile_managed_installations(object())

    assert created["accepted"] is True
    assert created["operation"]["owner_id"] == "alice"
    assert created["operation"]["account_generation"] == "alice-gen"
    assert [item["identifier"] for item in listing["operations"]] == ["alice-skill"]


def test_mobile_install_detail_returns_not_found_for_another_account(tmp_path, monkeypatch):
    db = tmp_path / "managed-installations.db"
    bob = managed_installations.create_managed_installation(
        kind="skill",
        identifier="bob-private-skill",
        request_id="bob-private-install",
        targets=["server"],
        db_path=db,
        owner_id="bob",
        account_generation="bob-gen",
    )
    monkeypatch.setattr(
        managed_installations, "managed_installations_db_path", lambda: db
    )
    monkeypatch.setattr(plugin_api, "owner_id_from_request", lambda _request: "alice")
    monkeypatch.setattr(
        plugin_api, "_account_generation_for_owner", lambda _owner: "alice-gen"
    )

    try:
        plugin_api.get_mobile_managed_installation(bob["id"], object())
    except plugin_api.HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("another account's installation detail was exposed")


def test_managed_resource_sse_is_account_scoped_and_emits_cursor_snapshot(
    tmp_path, monkeypatch,
):
    db = tmp_path / "managed-installations.db"
    _complete(db, owner="alice", generation="alice-gen", identifier="alice-skill")
    monkeypatch.setattr(
        managed_installations, "managed_installations_db_path", lambda: db
    )
    monkeypatch.setattr(plugin_api, "owner_id_from_request", lambda _request: "alice")
    monkeypatch.setattr(
        plugin_api, "_account_generation_for_request", lambda _request, _owner: "alice-gen"
    )
    monkeypatch.setattr(
        plugin_api, "_account_generation_for_owner", lambda _owner: "alice-gen"
    )

    class Request:
        query_params = {"cursor": "0"}
        headers = {}

        async def is_disconnected(self):
            return False

    async def first_frame():
        response = await plugin_api.stream_mobile_managed_resources(Request())
        assert response.headers["content-type"].startswith("text/event-stream")
        iterator = response.body_iterator
        payload = await anext(iterator)
        await iterator.aclose()
        return payload

    raw = asyncio.run(first_frame())
    text = raw.decode() if isinstance(raw, bytes) else raw
    assert "event: managed-resources" in text
    data = json.loads(next(
        line.removeprefix("data: ")
        for line in text.splitlines()
        if line.startswith("data: ")
    ))
    assert data["account_generation"] == "alice-gen"
    assert [item["name"] for item in data["resources"]] == ["alice-skill"]
