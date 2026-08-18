from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "collaboration"
    / "dashboard"
    / "plugin_api.py"
)


def _load_module():
    module_name = f"collaboration_cloud_files_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _client(module, owner: str = "owner-a") -> TestClient:
    # Production migrations are bound only by explicit HERMES_LEGACY_OWNER_ID.
    # Most fixtures model that configured primary account.
    module._configured_legacy_owner_id = lambda: owner
    app = FastAPI()

    @app.middleware("http")
    async def attach_identity(request: Request, call_next):
        request.state.session = SimpleNamespace(
            user_id=request.headers.get("x-test-owner", owner)
        )
        return await call_next(request)

    app.include_router(module.router, prefix="/api/plugins/collaboration")
    return TestClient(app)


def test_delete_owner_account_data_removes_only_owned_collaboration_state(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    owner_conversation = {
        "id": "conversation-owner-a",
        "owner_id": "owner-a",
        "account_generation": "owner-a-generation",
        "runtime_sessions": {"default": "session-owner-a"},
        "hosted_turns": {"turn-a": {"state": "running"}},
    }
    replacement_conversation = {
        "id": "conversation-owner-a-replacement",
        "owner_id": "owner-a",
        "account_generation": "owner-a-new-generation",
        "runtime_sessions": {"default": "session-owner-a-new"},
        "hosted_turns": {},
    }
    peer_conversation = {
        "id": "conversation-owner-b",
        "owner_id": "owner-b",
        "runtime_sessions": {},
        "hosted_turns": {},
    }
    single_state = {
        "conversations": [
            owner_conversation,
            replacement_conversation,
            peer_conversation,
        ]
    }
    room_state = {"rooms": [
        {
            "id": "room-owner-a",
            "owner_id": "owner-a",
            "account_generation": "owner-a-generation",
        },
        {
            "id": "room-owner-a-replacement",
            "owner_id": "owner-a",
            "account_generation": "owner-a-new-generation",
        },
        {"id": "room-owner-b", "owner_id": "owner-b"},
    ]}
    conversation_root = tmp_path / "conversation-owner-a"
    conversation_root.mkdir()
    (conversation_root / "attachment.txt").write_text("delete", encoding="utf-8")
    sessions = []
    file_owners = []

    monkeypatch.setattr(module, "load_single_state", lambda **_kwargs: single_state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    monkeypatch.setattr(module, "load_state", lambda: room_state)
    monkeypatch.setattr(module, "save_state", lambda _state: None)
    monkeypatch.setattr(
        module,
        "conversation_files_root",
        lambda conversation_id: tmp_path / conversation_id,
    )
    monkeypatch.setattr(
        module,
        "_delete_runtime_session",
        lambda profile, session_id: sessions.append((profile, session_id)) or True,
    )
    monkeypatch.setattr(
        module,
        "_file_library",
        lambda: SimpleNamespace(
            delete_owner=lambda owner_id, *, account_generation: file_owners.append(
                (owner_id, account_generation)
            )
            or {"files": 1}
        ),
    )

    result = module.delete_owner_account_data(
        "owner-a",
        account_generation="owner-a-generation",
    )

    assert result == {
        "conversations": 1,
        "rooms": 1,
        "runtime_sessions": 1,
        "files": {"files": 1},
        "tool_output_artifacts": {"artifacts": 0},
        "managed_resources": {"resources": 0, "events": 0, "operations": 0},
    }
    assert single_state["conversations"] == [
        replacement_conversation,
        peer_conversation,
    ]
    assert room_state["rooms"] == [
        {
            "id": "room-owner-a-replacement",
            "owner_id": "owner-a",
            "account_generation": "owner-a-new-generation",
        },
        {"id": "room-owner-b", "owner_id": "owner-b"},
    ]
    assert sessions == [("default", "session-owner-a")]
    assert file_owners == [("owner-a", "owner-a-generation")]
    assert conversation_root.exists() is False


def test_account_deletion_removes_owned_archive_but_preserves_other_generation(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    owner_archive = {
        "id": "chat-owner-archived",
        "owner_id": "owner-a",
        "account_generation": "generation-a",
        "messages": [{"id": "old", "content": "private"}],
    }
    peer_archive = {
        "id": "chat-peer-archived",
        "owner_id": "owner-b",
        "account_generation": "generation-b",
        "messages": [{"id": "peer", "content": "keep"}],
    }
    archive_root = tmp_path / "collaboration" / "archive"
    archive_root.mkdir(parents=True)
    (archive_root / f"{owner_archive['id']}.json").write_text(
        json.dumps(owner_archive), encoding="utf-8"
    )
    peer_target = archive_root / f"{peer_archive['id']}.json"
    peer_target.write_text(json.dumps(peer_archive), encoding="utf-8")
    state = {
        "conversations": [
            {
                "id": owner_archive["id"],
                "owner_id": "owner-a",
                "account_generation": "generation-a",
                "archived": True,
            },
            {
                "id": "chat-owner-hot",
                "owner_id": "owner-a",
                "account_generation": "generation-a",
            },
            {
                "id": "chat-peer-hot",
                "owner_id": "owner-b",
                "account_generation": "generation-b",
            },
        ]
    }
    rooms = {"rooms": []}
    monkeypatch.setattr(module, "load_single_state", lambda **_kwargs: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    monkeypatch.setattr(module, "load_state", lambda: rooms)
    monkeypatch.setattr(module, "save_state", lambda _state: None)
    monkeypatch.setattr(
        module,
        "_file_library",
        lambda: SimpleNamespace(
            delete_owner=lambda *_args, **_kwargs: {"files": 0},
        ),
    )

    module.delete_owner_account_data(
        "owner-a",
        account_generation="generation-a",
    )

    assert not (archive_root / f"{owner_archive['id']}.json").exists()
    assert peer_target.exists()
    assert [item["id"] for item in state["conversations"]] == ["chat-peer-hot"]


def test_account_file_routes_cover_upload_artifact_link_download_and_delete(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    conversation = module.create_single_conversation("default", "File test")
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    prefix = "/api/plugins/collaboration"

    with _client(module) as client:
        rejected_hash = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/attachments",
            content=b"tampered upload",
            headers={
                "x-filename": "tampered.txt",
                "content-type": "text/plain",
                "x-upload-id": "upload-tampered-001",
                "x-content-sha256": "0" * 64,
            },
        )
        assert rejected_hash.status_code == 422
        assert "does not match" in rejected_hash.json()["detail"]

        upload = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/attachments",
            content=b"user upload",
            headers={
                "x-filename": "notes.txt",
                "content-type": "text/plain",
                "x-turn-id": "turn-upload",
                "x-upload-id": "upload-notes-001",
            },
        )
        assert upload.status_code == 200
        uploaded = upload.json()["attachment"]
        assert "output_dir" not in upload.json()
        assert uploaded["source"] == "user_upload"
        assert uploaded["bucket"] == "uploads"
        assert uploaded["status"] == "available"
        assert uploaded["sha256"]
        assert "path" not in uploaded
        download = client.get(f"{prefix}/files/{uploaded['id']}/download")
        assert download.content == b"user upload"
        assert download.headers["x-content-sha256"] == uploaded["sha256"]

        replayed_upload = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/attachments",
            content=b"user upload",
            headers={
                "x-filename": "notes.txt",
                "content-type": "text/plain",
                "x-turn-id": "turn-upload",
                "x-upload-id": "upload-notes-001",
            },
        )
        assert replayed_upload.status_code == 200
        assert replayed_upload.json()["attachment"]["id"] == uploaded["id"]
        conflicting_upload = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/attachments",
            content=b"different bytes",
            headers={
                "x-filename": "notes.txt",
                "content-type": "text/plain",
                "x-turn-id": "turn-upload",
                "x-upload-id": "upload-notes-001",
            },
        )
        assert conflicting_upload.status_code == 409
        assert client.get(f"{prefix}/files/{uploaded['id']}/download").content == b"user upload"

        own_list = client.get(f"{prefix}/files", params={"q": "notes"})
        assert own_list.status_code == 200
        assert own_list.json()["total"] == 1
        assert own_list.json()["files"][0]["id"] == uploaded["id"]

        other_list = client.get(
            f"{prefix}/files",
            headers={"x-test-owner": "owner-b"},
        )
        assert other_list.status_code == 200
        assert other_list.json()["files"] == []
        assert client.get(
            f"{prefix}/files/{uploaded['id']}",
            headers={"x-test-owner": "owner-b"},
        ).status_code == 404

        reserve = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/artifacts",
            json={
                "name": "result.pdf",
                "relative_path": "result.pdf",
                "status": "uploading",
                "turn_id": "turn-model",
                "profile": "dbb3-worker",
            },
        )
        assert reserve.status_code == 200
        reserved = reserve.json()["file"]
        assert reserved["status"] == "uploading"
        assert "path" not in reserved

        output_path = module._conversation_file_dir(conversation["id"], "outputs") / "result.pdf"
        output_path.write_bytes(b"%PDF-model-result")
        publish = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/artifacts",
            json={
                "artifact_id": reserved["id"],
                "relative_path": "result.pdf",
                "status": "available",
                "turn_id": "turn-model",
                "profile": "dbb3-worker",
            },
        )
        assert publish.status_code == 200
        artifact = publish.json()["file"]
        assert artifact["id"] == reserved["id"]
        assert artifact["source"] == "model_output"
        assert artifact["status"] == "available"

        recorded = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/record",
            json={
                "role": "assistant",
                "name": "dbb3-worker",
                "content": "Artifact is ready",
                "meta": {
                    "runtime_turn_id": "turn-model",
                    "attachments": [artifact],
                },
            },
        )
        assert recorded.status_code == 200
        message_id = recorded.json()["message"]["id"]
        detail = client.get(f"{prefix}/files/{artifact['id']}")
        assert detail.status_code == 200
        assert detail.json()["file"]["message_id"] == message_id
        assert detail.json()["file"]["turn_id"] == "turn-model"

        model_files = client.get(
            f"{prefix}/files",
            params={"source": "model", "type": "document"},
        )
        assert model_files.status_code == 200
        assert [item["id"] for item in model_files.json()["files"]] == [artifact["id"]]

        download = client.get(f"{prefix}/files/{artifact['id']}/download")
        assert download.status_code == 200
        assert download.content == b"%PDF-model-result"
        assert download.headers["etag"] == f'"{artifact["sha256"]}"'

        traversal = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/artifacts",
            json={"relative_path": "../../secret.txt", "status": "available"},
        )
        assert traversal.status_code == 403

        deleted = client.delete(f"{prefix}/files/{artifact['id']}")
        assert deleted.status_code == 200
        assert client.get(f"{prefix}/files/{artifact['id']}").status_code == 404
        assert client.get(f"{prefix}/files/{artifact['id']}/download").status_code == 404
        # Automatic outputs discovery must respect an account deletion even
        # while the original worker output still exists on disk.
        after_delete = client.get(
            f"{prefix}/files",
            params={"source": "model"},
        )
        assert after_delete.status_code == 200
        assert after_delete.json()["files"] == []

        deleted_conversation = client.delete(
            f"{prefix}/single/conversations/{conversation['id']}"
        )
        assert deleted_conversation.status_code == 200
        assert client.get(f"{prefix}/files").json()["files"] == []
        assert client.get(
            f"{prefix}/single/conversations/{conversation['id']}"
        ).status_code == 404


def test_account_file_listing_uses_only_the_index_and_hides_server_paths(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "_sync_account_conversations",
        lambda _owner_id: (_ for _ in ()).throw(AssertionError("unexpected file scan")),
    )
    library = module._file_library()
    incoming = tmp_path / "incoming-fixture"
    incoming.mkdir()
    source = incoming / "indexed.txt"
    source.write_text("indexed", encoding="utf-8")
    generation = module._account_generation_for_owner("owner-index")
    record = library.ingest_file(
        "owner-index",
        source,
        account_generation=generation,
        name="indexed.txt",
        source="user_upload",
        allowed_roots=[incoming],
    )
    marker = module._account_file_migration_marker("owner-index")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("done\n", encoding="utf-8")
    prefix = "/api/plugins/collaboration"

    with _client(module, owner="owner-index") as client:
        response = client.get(f"{prefix}/files")

    assert response.status_code == 200
    assert response.json()["files"][0]["id"] == record["id"]
    assert "path" not in response.json()["files"][0]


def test_legacy_conversation_files_require_explicit_one_time_migration(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    conversation = module.create_single_conversation("default", "Legacy files")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    output = module._conversation_file_dir(conversation["id"], "outputs") / "legacy.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("legacy bytes", encoding="utf-8")
    prefix = "/api/plugins/collaboration"

    with _client(module, owner="owner-migration") as client:
        first = client.get(f"{prefix}/files")
        assert first.status_code == 200
        assert first.json()["files"] == []

        generation = module._account_generation_for_owner("owner-migration")
        module._migrate_account_conversation_files_once(
            "owner-migration",
            generation,
        )
        migrated = client.get(f"{prefix}/files")
        assert [item["name"] for item in migrated.json()["files"]] == ["legacy.txt"]
        assert "path" not in migrated.json()["files"][0]
        assert conversation["owner_id"] == "owner-migration"
        assert conversation["account_generation"] == generation
        assert module._account_file_migration_marker(
            "owner-migration",
            generation,
        ).is_file()

        monkeypatch.setattr(
            module,
            "_sync_account_conversations",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("migration repeated")
            ),
        )
        module._migrate_account_conversation_files_once(
            "owner-migration",
            generation,
        )
        second = client.get(f"{prefix}/files")

    assert second.status_code == 200
    assert second.json()["files"][0]["id"] == migrated.json()["files"][0]["id"]


def test_unconfigured_account_cannot_claim_legacy_conversations_or_rooms(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_configured_legacy_owner_id", lambda: "")
    conversation = module.create_single_conversation("default", "Legacy private")
    room = module.create_room_record("Legacy room", ["default"])
    state = {"conversations": [conversation]}
    room_state = {"rooms": [room]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)

    module._sync_account_conversations("owner-unbound", strict=True)

    assert conversation.get("owner_id", "") != "owner-unbound"
    with pytest.raises(HTTPException) as denied:
        module._owned_conversation_in_state(
            state,
            conversation["id"],
            "owner-unbound",
        )
    assert getattr(denied.value, "status_code", None) == 404
    assert not module._claim_legacy_rooms_in_state(room_state, "owner-unbound")
    assert room["owner_id"] == module.LOCAL_OWNER_ID


def test_owner_email_never_implicitly_authorizes_legacy_claim(monkeypatch):
    module = _load_module()
    monkeypatch.delenv("HERMES_LEGACY_OWNER_ID", raising=False)
    monkeypatch.setenv("HERMES_OWNER_EMAIL", "mobile-owner@example.com")

    assert module._configured_legacy_owner_id() == ""
    assert not module._legacy_owner_claim_allowed(
        module.LOCAL_OWNER_ID,
        "mobile-owner@example.com",
    )

    monkeypatch.setenv("HERMES_LEGACY_OWNER_ID", "explicit-owner")
    assert module._configured_legacy_owner_id() == "explicit-owner"
    assert module._legacy_owner_claim_allowed(
        module.LOCAL_OWNER_ID,
        "explicit-owner",
    )


def test_mobile_conversation_list_does_not_claim_owner_email_legacy_data(
    monkeypatch,
):
    module = _load_module()
    monkeypatch.delenv("HERMES_LEGACY_OWNER_ID", raising=False)
    monkeypatch.setenv("HERMES_OWNER_EMAIL", "mobile-owner@example.com")
    conversation = module.create_single_conversation("default", "Old E2E")
    conversation["owner_id"] = module.LOCAL_OWNER_ID
    state = {"conversations": [conversation]}
    saves = []
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda value: saves.append(value))
    request = SimpleNamespace(
        state=SimpleNamespace(
            session=SimpleNamespace(user_id="mobile-owner@example.com")
        )
    )

    response = module.get_single_conversations(request)

    assert response["conversations"] == []
    # Deletion tombstones ride with the list (remote deletion propagation);
    # this account has none.
    assert response["deleted"] == []
    assert conversation["owner_id"] == module.LOCAL_OWNER_ID
    assert saves == []


def test_incomplete_legacy_file_migration_does_not_write_completion_marker(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    attempts = []

    def fail_once(owner_id, account_generation, *, strict=False):
        attempts.append((owner_id, account_generation, strict))
        raise OSError("file changed during indexing")

    monkeypatch.setattr(module, "_sync_account_conversations", fail_once)
    marker = module._account_file_migration_marker("owner-retry")

    with pytest.raises(OSError, match="changed during indexing"):
        module._migrate_account_conversation_files_once("owner-retry")

    generation = module._account_generation_for_owner("owner-retry")
    assert attempts == [("owner-retry", generation, True)]
    assert not marker.exists()

    monkeypatch.setattr(
        module,
        "_sync_account_conversations",
        lambda owner_id, account_generation, *, strict=False: attempts.append(
            (owner_id, account_generation, strict)
        ),
    )
    module._migrate_account_conversation_files_once("owner-retry")
    assert attempts[-1] == ("owner-retry", generation, True)
    assert marker.is_file()


def test_rooms_are_account_scoped_and_enqueue_the_durable_hosted_workflow(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    rooms_state = {"rooms": []}
    single_state = {"conversations": []}
    monkeypatch.setattr(module, "load_state", lambda: rooms_state)
    monkeypatch.setattr(module, "save_state", lambda _state: None)
    monkeypatch.setattr(module, "load_single_state", lambda: single_state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    monkeypatch.setattr(
        module,
        "available_profiles",
        lambda: [
            {"name": "default"},
            {"name": "pc-worker"},
            {"name": "reviewer"},
        ],
    )
    started = []
    monkeypatch.setattr(
        module,
        "start_hosted_workflow",
        lambda conversation_id, turn_id: started.append((conversation_id, turn_id)),
    )
    monkeypatch.setattr(module, "_notify_hosted_update", lambda _conversation_id="": 1)
    prefix = "/api/plugins/collaboration"

    with _client(module, owner="owner-a") as client:
        created = client.post(
            f"{prefix}/rooms",
            json={
                "name": "Account A room",
                "profiles": ["default", "pc-worker", "reviewer"],
            },
        )
        assert created.status_code == 200
        room = created.json()["room"]
        stored_room = rooms_state["rooms"][0]
        assert "owner_id" not in room
        assert room["conversation_id"].startswith("chat_room_")

        other_headers = {"x-test-owner": "owner-b"}
        assert client.get(f"{prefix}/rooms", headers=other_headers).json()["rooms"] == []
        assert client.get(
            f"{prefix}/rooms/{room['id']}",
            headers=other_headers,
        ).status_code == 404

        sent = client.post(
            f"{prefix}/rooms/{room['id']}/messages",
            json={
                "content": "Run the PC checks",
                "profiles": ["pc-worker", "reviewer", "default"],
                "request_id": "room-request-stable-001",
                "turn_id": "room-turn-stable-001",
            },
        )
        assert sent.status_code == 200
        body = sent.json()
        assert body["accepted"] is True
        assert body["replayed"] is False
        assert started == [(room["conversation_id"], "room-turn-stable-001")]
        conversation = single_state["conversations"][0]
        assert conversation["owner_id"] == "owner-a"
        assert conversation["messages"][-1]["content"] == "Run the PC checks"
        assert conversation["hosted_turns"]["room-turn-stable-001"]["status"] == "queued"
        assert "room_request" not in body["hosted_turn"]

        # Simulate a stop after single.json committed but before the separate
        # room index commit. Replaying must reconstruct hosted_requests from the
        # durable turn instead of appending the user message a second time.
        rooms_state["rooms"][0]["hosted_requests"] = {}

        replayed = client.post(
            f"{prefix}/rooms/{room['id']}/messages",
            json={
                "content": "Run the PC checks",
                "profiles": ["pc-worker", "reviewer", "default"],
                "request_id": "room-request-stable-001",
                "turn_id": "room-turn-stable-001",
            },
        )
        assert replayed.status_code == 200
        assert replayed.json()["replayed"] is True
        assert len(conversation["messages"]) == 1

        cancelled = client.post(
            f"{prefix}/rooms/{room['id']}/hosted-turns/room-turn-stable-001/cancel",
            json={"reason": "stop"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["hosted_turn"]["cancel_requested"] is True

        deleted = client.delete(
            f"{prefix}/single/conversations/{room['conversation_id']}"
        )
        assert deleted.status_code == 503
        assert rooms_state["rooms"] == []
        assert conversation["delete_requested"] is True
        assert single_state["conversations"] == [conversation]
        assert started[-1] == (room["conversation_id"], "room-turn-stable-001")
        assert client.get(f"{prefix}/rooms/{room['id']}").status_code == 404


def test_first_account_claims_legacy_rooms_once_and_other_accounts_stay_isolated(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    legacy = module.create_room_record("Legacy room", ["default"])
    legacy.pop("owner_id")
    legacy.pop("account_generation")
    legacy["messages"] = [
        {"role": "assistant", "name": "default", "content": "legacy message"}
    ]
    local_room = module.create_room_record("Local room", ["default"])
    local_room.pop("account_generation")
    rooms_state = {"rooms": [legacy, local_room]}
    single_state = {"conversations": []}
    saves = []
    monkeypatch.setattr(module, "load_state", lambda: rooms_state)
    monkeypatch.setattr(module, "save_state", lambda state: saves.append(state))
    monkeypatch.setattr(module, "load_single_state", lambda: single_state)
    prefix = "/api/plugins/collaboration"

    with _client(module, owner="owner-a") as client:
        listed = client.get(f"{prefix}/rooms")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["rooms"]] == [
            legacy["id"],
            local_room["id"],
        ]
        assert listed.json()["rooms"][0]["messages"][0]["content"] == "legacy message"
        assert rooms_state["rooms"][0]["owner_id"] == "owner-a"
        assert rooms_state["rooms"][1]["owner_id"] == "owner-a"
        assert len(saves) == 1

        other_headers = {"x-test-owner": "owner-b"}
        assert client.get(f"{prefix}/rooms", headers=other_headers).json()["rooms"] == []
        assert client.get(
            f"{prefix}/rooms/{legacy['id']}",
            headers=other_headers,
        ).status_code == 404
        assert client.get(f"{prefix}/rooms/{legacy['id']}").status_code == 200
        assert rooms_state["rooms"][0]["owner_id"] == "owner-a"
        assert rooms_state["rooms"][1]["owner_id"] == "owner-a"


def test_client_conversation_identity_is_idempotent_and_account_scoped(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    state = {"conversations": []}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    monkeypatch.setattr(
        module,
        "available_profiles",
        lambda: [{"name": "default"}],
    )
    prefix = "/api/plugins/collaboration"
    payload = {
        "client_id": "chat_client-stable-001",
        "profile": "default",
        "title": "Durable send",
    }

    with _client(module, owner="owner-a") as client:
        first = client.post(f"{prefix}/single/conversations", json=payload)
        state["conversations"][0]["hosted_turns"] = {
            "turn-private": {
                "turn_id": "turn-private",
                "output_dir": str(tmp_path / "private-output"),
                "output_baseline": {"secret.txt": "hash"},
                "room_request": {"fingerprint": "private"},
            }
        }
        replay = client.post(f"{prefix}/single/conversations", json=payload)
        other = client.post(
            f"{prefix}/single/conversations",
            json=payload,
            headers={"x-test-owner": "owner-b"},
        )

    assert first.status_code == 200
    assert first.json()["created"] is True
    assert replay.status_code == 200
    assert replay.json()["created"] is False
    assert replay.json()["conversation"]["id"] == "chat_client-stable-001"
    replayed_turn = replay.json()["conversation"]["hosted_turns"]["turn-private"]
    assert "output_dir" not in replayed_turn
    assert "output_baseline" not in replayed_turn
    assert "room_request" not in replayed_turn
    assert other.status_code == 404
    assert len(state["conversations"]) == 1


def test_attachment_get_auto_registers_outputs(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    conversation = module.create_single_conversation("default", "Output sync")
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    output_dir = module._conversation_file_dir(conversation["id"], "outputs")
    (output_dir / "nested").mkdir(parents=True)
    (output_dir / "nested" / "summary.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    prefix = "/api/plugins/collaboration"

    with _client(module) as client:
        response = client.get(
            f"{prefix}/single/conversations/{conversation['id']}/attachments"
        )
        assert response.status_code == 200
        outputs = [
            item
            for item in response.json()["attachments"]
            if item["bucket"] == "outputs"
        ]
        assert len(outputs) == 1
        assert "uploads_dir" not in response.json()
        assert "output_dir" not in response.json()
        assert outputs[0]["name"] == "summary.csv"
        assert outputs[0]["source"] == "model_output"

        # Repeated discovery updates the same indexed object instead of
        # duplicating it in the account library.
        second = client.get(
            f"{prefix}/single/conversations/{conversation['id']}/attachments"
        )
        assert second.status_code == 200
        assert [item["id"] for item in second.json()["attachments"]] == [outputs[0]["id"]]

        hosted_collection = module._list_conversation_attachments(conversation["id"])
        assert [item["id"] for item in hosted_collection] == [outputs[0]["id"]]
        assert hosted_collection[0]["download_url"].startswith(
            f"{prefix}/files/"
        )


def test_attachment_sync_failure_never_falls_back_to_server_paths(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    conversation = module.create_single_conversation("default", "Output failure")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    output_dir = module._conversation_file_dir(conversation["id"], "outputs")
    (output_dir / "private.txt").write_text("private", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_sync_conversation_files",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index unavailable")),
    )

    attachments = module._list_conversation_attachments(conversation["id"])

    assert attachments == []
    assert str(output_dir.resolve()) not in json.dumps(attachments)


def test_account_file_upload_does_not_require_a_conversation(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "load_single_state", lambda: {"conversations": []})
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    prefix = "/api/plugins/collaboration"

    with _client(module, owner="owner-upload") as client:
        response = client.post(
            f"{prefix}/files",
            content=b"standalone account file",
            headers={
                "x-filename": "Account Report.txt",
                "x-upload-id": "account-upload-test-001",
                "content-type": "text/plain",
            },
        )

        assert response.status_code == 200
        uploaded = response.json()["file"]
        assert uploaded["name"] == "Account Report.txt"
        assert uploaded["source"] == "user_upload"
        assert uploaded["status"] == "available"
        assert uploaded["conversation_id"] == ""
        listed = client.get(f"{prefix}/files", params={"q": "Account Report"})
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["files"]] == [uploaded["id"]]


def test_single_conversation_routes_are_private_to_the_account(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    conversation = module.create_single_conversation("default", "Private chat")
    conversation["owner_id"] = "owner-a"
    conversation["runtime_sessions"] = {"default": "session-private"}
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-private",
        content="private task",
        title="Private task",
        profiles=["default"],
        artifact_required=False,
        attachment_context="",
        delivery_context="",
        mode="chat",
        route_metadata={},
    )
    hosted["status"] = "running"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    monkeypatch.setattr(module, "available_profiles", lambda: [{"name": "default"}])
    prefix = "/api/plugins/collaboration"
    conversation_path = f"{prefix}/single/conversations/{conversation['id']}"
    other = {"x-test-owner": "owner-b"}

    with _client(module, owner="owner-a") as client:
        assert client.get(conversation_path).status_code == 200
        renamed = client.patch(conversation_path, json={"title": "Owned rename"})
        assert renamed.status_code == 200
        assert renamed.json()["conversation"]["title"] == "Owned rename"

        requests = (
            ("GET", conversation_path, {}),
            ("PATCH", conversation_path, {"json": {"title": "Cross-account"}}),
            ("GET", f"{conversation_path}/attachments", {}),
            (
                "POST",
                f"{conversation_path}/attachments",
                {
                    "content": b"cross account bytes",
                    "headers": {"x-filename": "cross.txt", **other},
                },
            ),
            (
                "GET",
                f"{conversation_path}/attachments/uploads/cross.txt",
                {},
            ),
            (
                "POST",
                f"{conversation_path}/record",
                {"json": {"role": "user", "name": "User", "content": "secret"}},
            ),
            ("GET", f"{conversation_path}/hosted-events", {}),
            (
                "POST",
                f"{conversation_path}/runtime-session",
                {
                    "json": {
                        "profile": "default",
                        "session_id": "session-cross",
                        "turn_id": "turn-cross",
                        "status": "running",
                    }
                },
            ),
            (
                "POST",
                f"{conversation_path}/hosted-turns",
                {
                    "json": {
                        "turn_id": "turn-cross",
                        "content": "cross account task",
                        "title": "cross",
                        "profiles": ["default"],
                        "mode": "chat",
                    }
                },
            ),
            (
                "POST",
                f"{conversation_path}/hosted-turns/turn-private/cancel",
                {"json": {"reason": "cross"}},
            ),
            ("POST", f"{conversation_path}/messages", {"json": {"content": "cross"}}),
            (
                "POST",
                f"{conversation_path}/artifacts",
                {"json": {"name": "cross.txt", "status": "uploading"}},
            ),
            ("DELETE", conversation_path, {}),
        )
        for method, path, kwargs in requests:
            request_headers = dict(other)
            request_headers.update(kwargs.pop("headers", {}))
            response = client.request(method, path, headers=request_headers, **kwargs)
            assert response.status_code == 404, (method, path, response.text)

        adopted = client.post(
            f"{prefix}/single/conversations/adopt",
            headers=other,
            json={"profile": "default", "session_id": "session-private"},
        )
        assert adopted.status_code == 404
        assert client.get(
            f"{prefix}/single/conversations",
            headers=other,
        ).json()["conversations"] == []
        assert state["conversations"][0]["owner_id"] == "owner-a"


def test_official_session_adoption_is_keyed_by_profile_and_session(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    state = {"conversations": []}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    monkeypatch.setattr(
        module,
        "available_profiles",
        lambda: [{"name": "default"}, {"name": "reviewer"}],
    )
    prefix = "/api/plugins/collaboration"

    with _client(module, owner="owner-a") as client:
        first = client.post(
            f"{prefix}/single/conversations/adopt",
            json={
                "profile": "default",
                "session_id": "shared-session",
                "title": "Default history",
                "messages": [{"role": "user", "content": "default message"}],
            },
        )
        second = client.post(
            f"{prefix}/single/conversations/adopt",
            json={
                "profile": "reviewer",
                "session_id": "shared-session",
                "title": "Reviewer history",
                "messages": [{"role": "user", "content": "reviewer message"}],
            },
        )
        replay = client.post(
            f"{prefix}/single/conversations/adopt",
            json={
                "profile": "default",
                "session_id": "shared-session",
                "title": "Ignored replay",
            },
        )

    assert first.status_code == second.status_code == replay.status_code == 200
    assert first.json()["created"] is True
    assert second.json()["created"] is True
    assert replay.json()["created"] is False
    assert first.json()["conversation"]["id"] != second.json()["conversation"]["id"]
    assert first.json()["conversation"]["runtime_sessions"] == {
        "default": "shared-session"
    }
    assert second.json()["conversation"]["runtime_sessions"] == {
        "reviewer": "shared-session"
    }
    assert first.json()["conversation"]["messages"][0]["content"] == "default message"
    assert second.json()["conversation"]["messages"][0]["content"] == "reviewer message"


def test_connector_downloads_only_files_bound_to_its_remote_run(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_configured_connector_token", lambda: "connector-secret")
    conversation = module.create_single_conversation("default", "Attachment relay")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    prefix = "/api/plugins/collaboration"

    with _client(module, owner="owner-a") as client:
        upload = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/attachments",
            content=b"connector input bytes",
            headers={
                "x-filename": "input.txt",
                "content-type": "text/plain",
                "x-upload-id": "upload-connector-input-001",
            },
        )
        assert upload.status_code == 200
        attachment = upload.json()["attachment"]
        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-attachment",
            content="read attachment",
            title="Read attachment",
            profiles=["dbb3-worker"],
            artifact_required=False,
            attachment_ids=[attachment["id"]],
        )
        remote = module._ensure_remote_run(
            conversation["id"],
            "turn-attachment",
            role_stage="worker",
            profile="dbb3-worker",
            title="Read attachment",
            objective="Read the file",
            local_task_id="task-worker",
            artifact_required=False,
            delivery_context="",
            attachment_context="input.txt",
            attachment_ids=[attachment["id"]],
        )
        run_path = f"{prefix}/connector/runs/{remote['id']}"
        auth = {
            "authorization": "Bearer connector-secret",
            "x-connector-id": "dbb3-primary",
        }

        assert client.get(f"{run_path}/attachments").status_code == 401
        listed = client.get(f"{run_path}/attachments", headers=auth)
        assert listed.status_code == 200
        [record] = listed.json()["attachments"]
        assert record["id"] == attachment["id"]
        assert record["sha256"] == attachment["sha256"]
        assert "path" not in record

        downloaded = client.get(
            f"{run_path}/attachments/{attachment['id']}",
            headers=auth,
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"connector input bytes"
        assert downloaded.headers["etag"] == f'"{attachment["sha256"]}"'
        assert client.get(
            f"{run_path}/attachments/file_not_bound",
            headers=auth,
        ).status_code == 404


def test_connector_cancellation_includes_current_cursor_and_reaches_terminal_state(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_configured_connector_token", lambda: "connector-secret")
    conversation = module.create_single_conversation("default", "Cancellation relay")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-cancel",
        content="cancel this work",
        title="Cancel work",
        profiles=["dbb3-worker"],
        artifact_required=False,
    )
    hosted["status"] = "running"
    remote = module._ensure_remote_run(
        conversation["id"],
        "turn-cancel",
        role_stage="worker",
        profile="dbb3-worker",
        title="Cancel work",
        objective="cancel this work",
        local_task_id="task-cancel",
        artifact_required=False,
        delivery_context="",
        attachment_context="",
    )
    persisted_remote = hosted["remote_runs"]["worker"]
    persisted_remote.update(
        {
            "status": "running",
            "checkpoint_cursor": 7,
            "root_task_id": "task-cancel",
        }
    )
    module.request_hosted_turn_cancellation(
        conversation["id"],
        "turn-cancel",
        reason="user cancelled",
    )
    prefix = "/api/plugins/collaboration"
    auth = {
        "authorization": "Bearer connector-secret",
        "x-connector-id": "dbb3-primary",
    }

    with _client(module, owner="owner-a") as client:
        pulled = client.post(
            f"{prefix}/connector/cancellations/pull",
            headers=auth,
            json={"connector_id": "dbb3-primary", "limit": 5, "lease_seconds": 30},
        )
        assert pulled.status_code == 200
        cancellation = next(
            item
            for item in pulled.json()["cancellations"]
            if item["remote_run_id"] == remote["id"]
        )
        assert cancellation["remote_run_id"] == remote["id"]
        assert cancellation["checkpoint_cursor"] == 7

        acknowledged = client.post(
            f"{prefix}/connector/runs/{remote['id']}/cancel-ack",
            headers=auth,
            json={
                "connector_id": "dbb3-primary",
                "claim_token": cancellation["claim_token"],
                "checkpoint_cursor": 8,
                "summary": "Cancellation applied",
            },
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json()["applied"] is True
        assert acknowledged.json()["run"]["status"] == "cancelled"
        assert persisted_remote["status"] == "cancelled"
        assert persisted_remote["checkpoint_cursor"] == 8


def test_deleting_active_remote_conversation_retains_cancellation_until_ack(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_configured_connector_token", lambda: "connector-secret")
    conversation = module.create_single_conversation("default", "Delete active work")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    starts = []
    monkeypatch.setattr(
        module,
        "start_hosted_workflow",
        lambda *args: starts.append(args),
    )
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-delete-active",
        content="long remote work",
        title="Long remote work",
        profiles=["dbb3-worker"],
        artifact_required=False,
    )
    hosted["status"] = "running"
    remote = module._ensure_remote_run(
        conversation["id"],
        "turn-delete-active",
        role_stage="worker",
        profile="dbb3-worker",
        title="Long remote work",
        objective="long remote work",
        local_task_id="task-delete-active",
        artifact_required=False,
        delivery_context="",
        attachment_context="",
    )
    hosted["remote_runs"]["worker"]["status"] = "running"
    starts.clear()
    prefix = "/api/plugins/collaboration"
    auth = {
        "authorization": "Bearer connector-secret",
        "x-connector-id": "dbb3-primary",
    }

    with _client(module, owner="owner-a") as client:
        starts.clear()
        deleted = client.delete(
            f"{prefix}/single/conversations/{conversation['id']}"
        )
        assert deleted.status_code == 503
        assert deleted.json()["detail"]["reason"] == "conversation_deletion_pending"
        assert state["conversations"] == [conversation]
        assert conversation["delete_requested"] is True
        assert hosted["cancel_requested"] is True
        assert starts == [(conversation["id"], "turn-delete-active")]
        assert client.get(
            f"{prefix}/single/conversations/{conversation['id']}"
        ).status_code == 404

        pulled = client.post(
            f"{prefix}/connector/cancellations/pull",
            headers=auth,
            json={"connector_id": "dbb3-primary", "limit": 5, "lease_seconds": 30},
        )
        assert pulled.status_code == 200
        cancellation = pulled.json()["cancellations"][0]
        assert cancellation["remote_run_id"] == remote["id"]

        acknowledged = client.post(
            f"{prefix}/connector/runs/{remote['id']}/cancel-ack",
            headers=auth,
            json={
                "connector_id": "dbb3-primary",
                "claim_token": cancellation["claim_token"],
                "checkpoint_cursor": 1,
                "summary": "cancelled before deletion",
            },
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json()["run"]["status"] == "cancelled"
        assert state["conversations"] == [conversation]

        hosted["status"] = "cancelled"
        assert module._finalize_pending_conversation_deletion(conversation["id"])

    assert state["conversations"] == []


def test_connector_releases_running_run_after_lease_for_terminal_poll(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_configured_connector_token", lambda: "connector-secret")
    conversation = module.create_single_conversation("default", "Terminal poll")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-terminal-poll",
        content="finish remote work",
        title="Finish remote work",
        profiles=["dbb3-worker"],
        artifact_required=False,
    )
    hosted["status"] = "running"
    remote = module._ensure_remote_run(
        conversation["id"],
        "turn-terminal-poll",
        role_stage="worker",
        profile="dbb3-worker",
        title="Finish remote work",
        objective="finish remote work",
        local_task_id="task-terminal-poll",
        artifact_required=False,
        delivery_context="",
        attachment_context="",
    )
    persisted_remote = hosted["remote_runs"]["worker"]
    persisted_remote.update(
        {
            "status": "running",
            "lease_owner": "dbb3-primary",
            "lease_until": 1,
            "checkpoint_cursor": 4,
        }
    )
    prefix = "/api/plugins/collaboration"
    auth = {
        "authorization": "Bearer connector-secret",
        "x-connector-id": "dbb3-primary",
    }

    with _client(module, owner="owner-a") as client:
        pulled = client.post(
            f"{prefix}/connector/runs/pull",
            headers=auth,
            json={"connector_id": "dbb3-primary", "limit": 5, "lease_seconds": 30},
        )
        assert pulled.status_code == 200
        leased = next(
            run
            for run in pulled.json()["runs"]
            if run["remote_run_id"] == remote["id"]
        )
        assert leased["remote_run_id"] == remote["id"]
        assert leased["status"] == "running"
        assert persisted_remote["status"] == "running"
        assert persisted_remote["lease_until"] > 1

        still_leased = client.post(
            f"{prefix}/connector/runs/pull",
            headers=auth,
            json={"connector_id": "dbb3-primary", "limit": 5, "lease_seconds": 30},
        )
        assert still_leased.status_code == 200
        assert still_leased.json()["runs"] == []


def test_remote_artifact_instruction_uses_kanban_workspace_not_public_output(
    tmp_path,
):
    module = _load_module()
    run = {
        "artifact_required": True,
        "user_delivery_context": "交付 UTF-8 文本报告。",
        "delivery_context": (
            "交付 UTF-8 文本报告。\n"
            f"Absolute output directory: `{tmp_path / 'public-output'}`.\n"
            "Write every generated deliverable to this exact directory and report its absolute path."
        ),
    }

    remote = module.hosted_artifact_instruction(run, remote_workers=True)
    assert "交付 UTF-8 文本报告" in remote
    assert "$HERMES_KANBAN_WORKSPACE" in remote
    assert "kanban_complete(artifacts=[...])" in remote
    assert str(tmp_path / "public-output") not in remote

    local = module.hosted_artifact_instruction(run, remote_workers=False)
    assert str(tmp_path / "public-output") in local


def test_atomic_enqueue_is_idempotent_and_persists_message_route_and_turn_together(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    conversation = module.create_single_conversation("default", "新对话")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    saves = []
    starts = []
    routing_starts = []
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda value: saves.append(value))
    monkeypatch.setattr(
        module,
        "start_hosted_workflow",
        lambda conversation_id, turn_id: starts.append((conversation_id, turn_id)),
    )
    monkeypatch.setattr(
        module,
        "start_hosted_routing",
        lambda conversation_id, turn_id: routing_starts.append(
            (conversation_id, turn_id)
        ),
    )
    monkeypatch.setattr(
        module,
        "route_message",
        lambda _payload: {
            "mode": "work",
            "label": "群聊 + 工作流",
            "reason": "需要远程执行",
            "title": "检查项目",
            "profiles": ["default", "dbb3-worker", "reviewer"],
            "artifact_required": False,
            "artifact": {"decision": "none"},
            "confidence": 0.98,
            "source": "test",
            "targets": ["dbb3"],
        },
    )
    body = {
        "request_id": "message-atomic-1",
        "turn_id": "turn-atomic-1",
        "message": {
            "id": "message-atomic-1",
            "role": "user",
            "name": "你",
            "kind": "message",
            "status": "completed",
            "content": "检查项目并汇报",
            "created_at": 1234,
        },
        "recent_messages": [],
        "profiles": ["default"],
        "attachment_ids": [],
        "attachment_context": "",
        "delivery_context": "由服务端判断交付范围",
    }
    prefix = "/api/plugins/collaboration"

    with _client(module) as client:
        first = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/enqueue",
            json=body,
        )
        assert first.status_code == 202
        first_payload = first.json()
        assert first_payload["accepted"] is True
        assert first_payload["replayed"] is False
        assert first_payload["message"]["id"] == "message-atomic-1"
        assert first_payload["route"]["mode"] == "pending"
        assert first_payload["hosted_turn"]["turn_id"] == "turn-atomic-1"
        assert len(conversation["messages"]) == 1
        accepted_save_count = len(saves)
        assert accepted_save_count >= 1

        assert module._complete_pending_hosted_route(
            conversation["id"], "turn-atomic-1"
        ) is True
        assert conversation["messages"][-1]["kind"] == "route"
        assert len(conversation["messages"]) == 2
        routed_save_count = len(saves)
        assert routed_save_count > accepted_save_count
        assert module._hosted_update_revision(conversation["id"]) >= 1

        replay = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/enqueue",
            json=body,
        )
        assert replay.status_code == 202
        assert replay.json()["replayed"] is True
        assert len(conversation["messages"]) == 2
        assert len(conversation["hosted_turns"]) == 1
        assert len(saves) == routed_save_count

        changed = dict(body)
        changed["message"] = {**body["message"], "content": "different"}
        conflict = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/enqueue",
            json=changed,
        )
        assert conflict.status_code == 409

    assert starts
    assert set(starts) == {(conversation["id"], "turn-atomic-1")}
    assert routing_starts == [
        (conversation["id"], "turn-atomic-1"),
    ]


def test_atomic_chat_enqueue_falls_back_to_the_authenticated_conversation_profile(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    conversation = module.create_single_conversation("reviewer", "Reviewer chat")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    monkeypatch.setattr(module, "start_hosted_workflow", lambda *_args: None)
    monkeypatch.setattr(module, "start_hosted_routing", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "available_profiles",
        lambda: [{"name": "default"}, {"name": "reviewer"}],
    )
    monkeypatch.setattr(
        module,
        "route_message",
        lambda _payload: {
            "mode": "chat",
            "label": "简单任务",
            "reason": "single profile",
            "confidence": 1.0,
            "source": "test",
            "profiles": ["default"],
            "artifact_required": False,
        },
    )
    prefix = "/api/plugins/collaboration"
    body = {
        "request_id": "request-reviewer-chat",
        "turn_id": "turn-reviewer-chat",
        "message": {
            "id": "message-reviewer-chat",
            "role": "user",
            "name": "User",
            "content": "continue reviewer chat",
        },
        "recent_messages": [],
        "profiles": ["default"],
        "attachment_ids": [],
    }

    with _client(module, owner="owner-a") as client:
        response = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/enqueue",
            json=body,
        )

    assert response.status_code == 202
    assert module._complete_pending_hosted_route(
        conversation["id"], "turn-reviewer-chat"
    ) is True
    assert conversation["hosted_turns"]["turn-reviewer-chat"]["profiles"] == [
        "reviewer"
    ]
    replay_body = {**body, "profiles": ["reviewer"]}
    with _client(module, owner="owner-a") as client:
        replay = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/enqueue",
            json=replay_body,
        )
    assert replay.status_code == 202
    assert replay.json()["replayed"] is True
    calls = []

    def runner(profile, prompt, **kwargs):
        calls.append((profile, prompt, kwargs))
        return "reviewer response"

    monkeypatch.setattr(module, "_schedule_mobile_completion_notification", lambda *_args: None)
    module.execute_hosted_workflow(
        conversation["id"],
        "turn-reviewer-chat",
        runner=runner,
    )
    assert [profile for profile, _prompt, _kwargs in calls] == ["reviewer"]


def test_hosted_chat_rejects_a_recent_unbound_output_when_this_turn_creates_nothing(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "_schedule_mobile_completion_notification",
        lambda *_args: None,
    )
    conversation = module.create_single_conversation("default", "Artifact isolation")
    conversation["owner_id"] = "owner-a"
    conversation["account_generation"] = module._account_generation_for_owner(
        "owner-a"
    )
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    output_dir = module._conversation_file_dir(conversation["id"], "outputs")
    (output_dir / "old-report.pdf").write_bytes(b"previous turn")
    module._sync_conversation_files("owner-a", conversation)
    unbound, total = module._file_library().list_files(
        "owner-a",
        account_generation=conversation["account_generation"],
        conversation_id=conversation["id"],
    )
    assert total == 1
    assert unbound[0]["turn_id"] == ""

    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-no-current-file",
        content="Create a fresh report",
        title="Fresh report",
        profiles=["default"],
        artifact_required=True,
        mode="chat",
        output_dir=str(output_dir),
    )
    module.execute_hosted_chat(
        conversation["id"],
        "turn-no-current-file",
        runner=lambda _profile, _prompt, **_kwargs: "No file was created",
    )

    assert hosted["status"] == "failed"
    assert module._file_library().list_files(
        "owner-a",
        account_generation=conversation["account_generation"],
        conversation_id=conversation["id"],
        turn_id="turn-no-current-file",
    )[1] == 0
    final_message = next(
        item
        for item in reversed(conversation["messages"])
        if item.get("meta", {}).get("message_key")
        == "turn-no-current-file:chat:completed"
    )
    assert final_message["meta"]["attachments"] == []


def test_hosted_turn_output_directories_are_stable_and_isolated(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    conversation_id = "chat-output-isolation"

    first = module._hosted_turn_output_dir(conversation_id, "turn-one").resolve()
    replay = module._hosted_turn_output_dir(conversation_id, "turn-one").resolve()
    second = module._hosted_turn_output_dir(conversation_id, "turn-two").resolve()

    assert first == replay
    assert first != second
    assert first.parent == second.parent
    assert first.is_relative_to(
        module._conversation_file_dir(conversation_id, "outputs").resolve()
    )


def test_hosted_chat_delivers_only_the_file_created_after_its_output_baseline(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "_schedule_mobile_completion_notification",
        lambda *_args: None,
    )
    conversation = module.create_single_conversation("default", "Artifact isolation")
    conversation["owner_id"] = "owner-a"
    conversation["account_generation"] = module._account_generation_for_owner(
        "owner-a"
    )
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    output_dir = module._conversation_file_dir(conversation["id"], "outputs")
    (output_dir / "old-report.pdf").write_bytes(b"previous turn")
    module._sync_conversation_files("owner-a", conversation)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-current-file",
        content="Create a fresh report",
        title="Fresh report",
        profiles=["default"],
        artifact_required=True,
        mode="chat",
        output_dir=str(output_dir),
    )

    def runner(_profile, _prompt, **_kwargs):
        (output_dir / "current-report.pdf").write_bytes(b"current turn")
        return "The current report is ready"

    module.execute_hosted_chat(
        conversation["id"],
        "turn-current-file",
        runner=runner,
    )

    assert hosted["status"] == "completed"
    current_files, total = module._file_library().list_files(
        "owner-a",
        account_generation=conversation["account_generation"],
        conversation_id=conversation["id"],
        turn_id="turn-current-file",
    )
    assert total == 1
    assert [item["name"] for item in current_files] == ["current-report.pdf"]
    final_message = next(
        item
        for item in reversed(conversation["messages"])
        if item.get("meta", {}).get("message_key") == "turn-current-file:chat:completed"
    )
    assert [item["name"] for item in final_message["meta"]["attachments"]] == [
        "current-report.pdf"
    ]

    module._sync_conversation_files("owner-a", conversation)
    preserved, preserved_total = module._file_library().list_files(
        "owner-a",
        account_generation=conversation["account_generation"],
        conversation_id=conversation["id"],
        turn_id="turn-current-file",
    )
    assert preserved_total == 1
    assert preserved[0]["name"] == "current-report.pdf"


def test_public_hosted_turn_projection_never_exposes_server_execution_paths():
    module = _load_module()
    internal = {
        "turn_id": "turn-private-paths",
        "status": "running",
        "delivery_context": "Absolute output directory: /srv/hermes/private/turn.",
        "user_delivery_context": "Please provide a PDF.",
        "output_dir": "/srv/hermes/private/turn",
        "output_baseline": {"old.pdf": "a" * 64},
        "output_baseline_captured_at": 1234,
        "remote_runs": {
            "worker": {
                "id": "remote-1",
                "status": "running",
                "delivery_context": "Write to /opt/worker/private",
                "attachment_context": "Read /opt/worker/input",
            }
        },
    }

    projected = module._public_hosted_turn(internal)
    encoded = json.dumps(projected, ensure_ascii=False)

    assert projected["delivery_context"] == "Please provide a PDF."
    assert projected["remote_runs"]["worker"] == {
        "id": "remote-1",
        "status": "running",
    }
    assert "output_dir" not in projected
    assert "output_baseline" not in projected
    assert "output_baseline_captured_at" not in projected
    assert "/srv/hermes" not in encoded
    assert "/opt/worker" not in encoded
    assert internal["output_dir"] == "/srv/hermes/private/turn"

    conversation = {"id": "conversation-1", "hosted_turns": {"turn-1": internal}}
    public_conversation = module._public_conversation(conversation)
    assert public_conversation["hosted_turns"]["turn-1"] == projected
    assert conversation["hosted_turns"]["turn-1"] is internal


def test_connector_credentials_and_profiles_are_bound_to_devices(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "_configured_connector_tokens",
        lambda: {"dbb3-primary": "dbb3-secret", "pc-primary": "pc-secret"},
    )
    conversation = module.create_single_conversation("default", "Device routing")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    for turn_id, profile in (("turn-dbb3", "dbb3-worker"), ("turn-pc", "pc-worker")):
        module.create_hosted_turn_record(
            conversation,
            turn_id=turn_id,
            content=f"run {profile}",
            title=profile,
            profiles=[profile],
            artifact_required=False,
        )
        module._ensure_remote_run(
            conversation["id"],
            turn_id,
            role_stage=f"worker:{profile}",
            profile=profile,
            title=profile,
            objective=f"run {profile}",
            local_task_id=f"task-{profile}",
            artifact_required=False,
            delivery_context="",
            attachment_context="",
        )
    prefix = "/api/plugins/collaboration"
    dbb3_auth = {
        "authorization": "Bearer dbb3-secret",
        "x-connector-id": "dbb3-primary",
    }
    pc_auth = {
        "authorization": "Bearer pc-secret",
        "x-connector-id": "pc-primary",
    }

    with _client(module) as client:
        dbb3 = client.post(
            f"{prefix}/connector/runs/pull",
            headers=dbb3_auth,
            json={"connector_id": "dbb3-primary", "limit": 5},
        )
        assert dbb3.status_code == 200
        assert [run["profile"] for run in dbb3.json()["runs"]] == [
            "dbb3-worker",
            "dbb3-manager",
        ]

        pc = client.post(
            f"{prefix}/connector/runs/pull",
            headers=pc_auth,
            json={"connector_id": "pc-primary", "limit": 5},
        )
        assert pc.status_code == 200
        assert [run["profile"] for run in pc.json()["runs"]] == ["pc-worker"]

        forged = client.post(
            f"{prefix}/connector/runs/pull",
            headers=dbb3_auth,
            json={"connector_id": "pc-primary", "limit": 5},
        )
        assert forged.status_code == 403

        pc_remote = next(
            remote
            for hosted in conversation["hosted_turns"].values()
            for remote in hosted["remote_runs"].values()
            if remote["profile"] == "pc-worker"
        )
        hidden = client.get(
            f"{prefix}/connector/runs/{pc_remote['id']}/attachments",
            headers=dbb3_auth,
        )
        assert hidden.status_code == 404


def test_connector_identity_rejects_a_secret_bound_to_multiple_devices(monkeypatch):
    """A duplicated connector secret must not be selectable by header."""

    module = _load_module()
    monkeypatch.setattr(
        module,
        "_configured_connector_token_records",
        lambda: {
            "dbb3-primary": {"tokens": ["shared-secret"]},
            "pc-primary": {"tokens": ["shared-secret"]},
        },
    )
    monkeypatch.setattr(
        module,
        "_configured_connector_tokens",
        lambda: {
            "dbb3-primary": "shared-secret",
            "pc-primary": "shared-secret",
        },
    )

    request = SimpleNamespace(
        headers={
            "authorization": "Bearer shared-secret",
            "x-connector-id": "pc-primary",
        }
    )
    assert module._connector_identity(request) == ""


def test_sliding_window_rate_limiter_bounds_unique_keys():
    module = _load_module()
    limiter = module._SlidingWindowRateLimiter(10, 60)
    limiter._MAX_KEYS = 2
    assert limiter.allow("first")
    assert limiter.allow("second")
    assert limiter.allow("third")
    assert len(limiter._hits) <= 2


def test_terminal_checkpoint_seals_claim_and_rejects_conflicting_old_token(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_configured_connector_token", lambda: "connector-secret")
    conversation = module.create_single_conversation("default", "Terminal race")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-terminal-race",
        content="finish",
        title="finish",
        profiles=["dbb3-worker"],
        artifact_required=False,
    )
    remote = module._ensure_remote_run(
        conversation["id"],
        "turn-terminal-race",
        role_stage="worker",
        profile="dbb3-worker",
        title="finish",
        objective="finish",
        local_task_id="task-finish",
        artifact_required=False,
        delivery_context="",
        attachment_context="",
    )
    auth = {
        "authorization": "Bearer connector-secret",
        "x-connector-id": "dbb3-primary",
    }
    prefix = "/api/plugins/collaboration"

    with _client(module) as client:
        claimed = client.post(
            f"{prefix}/connector/runs/pull",
            headers=auth,
            json={"connector_id": "dbb3-primary", "limit": 1, "lease_seconds": 30},
        ).json()["runs"][0]
        first = client.post(
            f"{prefix}/connector/runs/{remote['id']}/status",
            headers=auth,
            json={
                "connector_id": "dbb3-primary",
                "claim_token": claimed["claim_token"],
                "checkpoint_cursor": 8,
                "status": "completed",
                "terminal": True,
                "result": "original result",
            },
        )
        assert first.status_code == 200
        response = client.post(
            f"{prefix}/connector/runs/{remote['id']}/cancel-ack",
            headers=auth,
            json={
                "connector_id": "dbb3-primary",
                "claim_token": claimed["claim_token"],
                "checkpoint_cursor": 9,
                "summary": "cancel applied locally",
            },
        )
    assert response.status_code == 409
    assert hosted["remote_runs"]["worker"]["result"] == "original result"
    assert "claim_token" not in hosted["remote_runs"]["worker"]
    assert "lease_owner" not in hosted["remote_runs"]["worker"]
    assert hosted["remote_runs"]["worker"]["lease_until"] == 0


def test_required_remote_artifact_must_arrive_before_completed_checkpoint(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_configured_connector_token", lambda: "connector-secret")
    conversation = module.create_single_conversation("default", "Artifact gate")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-artifact-gate",
        content="deliver file",
        title="deliver file",
        profiles=["dbb3-worker"],
        artifact_required=True,
    )
    remote = module._ensure_remote_run(
        conversation["id"],
        "turn-artifact-gate",
        role_stage="worker",
        profile="dbb3-worker",
        title="deliver file",
        objective="deliver file",
        local_task_id="task-artifact",
        artifact_required=True,
        delivery_context="",
        attachment_context="",
    )
    auth = {
        "authorization": "Bearer connector-secret",
        "x-connector-id": "dbb3-primary",
    }
    prefix = "/api/plugins/collaboration"
    body = {
        "connector_id": "dbb3-primary",
        "checkpoint_cursor": 1,
        "status": "completed",
        "terminal": True,
        "summary": "done",
    }

    with _client(module) as client:
        claimed = client.post(
            f"{prefix}/connector/runs/pull",
            headers=auth,
            json={"connector_id": "dbb3-primary", "limit": 1, "lease_seconds": 30},
        ).json()["runs"][0]
        body["claim_token"] = claimed["claim_token"]
        rejected = client.post(
            f"{prefix}/connector/runs/{remote['id']}/status",
            headers=auth,
            json=body,
        )
        assert rejected.status_code == 409
        hosted["remote_runs"]["worker"]["artifacts"] = [{"id": "file-output"}]
        accepted = client.post(
            f"{prefix}/connector/runs/{remote['id']}/status",
            headers=auth,
            json=body,
        )
        assert accepted.status_code == 200
        assert accepted.json()["run"]["status"] == "completed"


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
def test_remote_artifact_rejects_late_upload_for_every_terminal_status(
    tmp_path,
    monkeypatch,
    terminal_status,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_require_connector", lambda _request: "dbb3-primary")
    conversation = module.create_single_conversation("default", "Late artifact")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id=f"turn-late-artifact-{terminal_status}",
        content="deliver file",
        title="deliver file",
        profiles=["dbb3-worker"],
        artifact_required=False,
    )
    remote = module._ensure_remote_run(
        conversation["id"],
        hosted["turn_id"],
        role_stage="worker",
        profile="dbb3-worker",
        title="deliver file",
        objective="deliver file",
        local_task_id="task-late-artifact",
        artifact_required=False,
        delivery_context="",
        attachment_context="",
    )
    claim = module.connector_pull_runs(
        module.ConnectorPullBody(
            connector_id="dbb3-primary", limit=1, lease_seconds=30
        ),
        SimpleNamespace(),
    )["runs"][0]
    current = hosted["remote_runs"]["worker"]
    # Reproduce the original bug exactly: a terminal row still retains a
    # matching, live execution claim and lease.
    current["status"] = terminal_status
    current["claim_token"] = claim["claim_token"]
    current["lease_owner"] = "dbb3-primary"
    current["lease_until"] = int(module.time.time() * 1000) + 30_000
    body = f"late {terminal_status}".encode()
    streamed = False

    class LateRequest:
        headers = {
            "x-claim-token": claim["claim_token"],
            "x-remote-run-id": remote["id"],
            "x-relative-path": f"report/{terminal_status}.txt",
            "x-filename": f"{terminal_status}.txt",
            "x-content-sha256": hashlib.sha256(body).hexdigest(),
            "content-length": str(len(body)),
            "content-type": "text/plain",
        }

        async def stream(self):
            nonlocal streamed
            streamed = True
            yield body

    with pytest.raises(module.HTTPException) as raised:
        asyncio.run(module.connector_upload_artifact(remote["id"], LateRequest()))
    assert raised.value.status_code == 409
    assert streamed is False
    library = module._file_library()
    origin_key = (
        f"remote:{remote['id']}:report/{terminal_status}.txt:"
        f"{hashlib.sha256(body).hexdigest()}"
    )
    assert library.get_file_by_origin("owner-a", origin_key) is None
    assert library.list_files("owner-a") == ([], 0)
    assert current.get("artifacts") in (None, [])
    assert not hosted.get("artifact_upload_intents")
    assert not any(
        str((message.get("meta") or {}).get("message_key") or "").startswith(
            f"{remote['id']}:artifact:"
        )
        for message in conversation["messages"]
        if isinstance(message, dict)
    )


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
def test_remote_terminal_checkpoint_seals_execution_claim(
    tmp_path,
    monkeypatch,
    terminal_status,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_require_connector", lambda _request: "dbb3-primary")
    conversation = module.create_single_conversation("default", "Seal claim")
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id=f"turn-seal-{terminal_status}",
        content="finish",
        title="finish",
        profiles=["dbb3-worker"],
        artifact_required=False,
    )
    remote = module._ensure_remote_run(
        conversation["id"],
        hosted["turn_id"],
        role_stage="worker",
        profile="dbb3-worker",
        title="finish",
        objective="finish",
        local_task_id="task-seal",
        artifact_required=False,
        delivery_context="",
        attachment_context="",
    )
    claim = module.connector_pull_runs(
        module.ConnectorPullBody(
            connector_id="dbb3-primary", limit=1, lease_seconds=30
        ),
        SimpleNamespace(),
    )["runs"][0]
    persisted, applied = module._apply_remote_checkpoint(
        remote["id"],
        {
            "connector_id": "dbb3-primary",
            "claim_token": claim["claim_token"],
            "checkpoint_cursor": 1,
            "status": terminal_status,
            "terminal": True,
            "result": "done" if terminal_status == "completed" else "",
            "error": "failed" if terminal_status == "failed" else "",
        },
    )
    assert applied is True
    assert persisted["status"] == terminal_status
    assert "claim_token" not in persisted
    assert "lease_owner" not in persisted
    assert persisted["lease_until"] == 0
    assert persisted["cancel_lease_until"] == 0
    assert persisted["sealed_claim_sha256"] == hashlib.sha256(
        claim["claim_token"].encode()
    ).hexdigest()
    assert persisted["claim_sealed_at"] > 0


def test_remote_artifact_stream_turns_terminal_before_second_claim_cas(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_require_connector", lambda _request: "dbb3-primary")
    conversation = module.create_single_conversation("default", "Artifact terminal race")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-artifact-terminal-race",
        content="deliver file",
        title="deliver file",
        profiles=["dbb3-worker"],
        artifact_required=False,
    )
    remote = module._ensure_remote_run(
        conversation["id"],
        hosted["turn_id"],
        role_stage="worker",
        profile="dbb3-worker",
        title="deliver file",
        objective="deliver file",
        local_task_id="task-terminal-race",
        artifact_required=False,
        delivery_context="",
        attachment_context="",
    )
    claim = module.connector_pull_runs(
        module.ConnectorPullBody(
            connector_id="dbb3-primary", limit=1, lease_seconds=30
        ),
        SimpleNamespace(),
    )["runs"][0]
    first = b"first half;"
    second = b"second half"
    body = first + second
    streamed = asyncio.Event()
    resume = asyncio.Event()

    class PausedRequest:
        headers = {
            "x-claim-token": claim["claim_token"],
            "x-remote-run-id": remote["id"],
            "x-relative-path": "report/terminal-race.txt",
            "x-filename": "terminal-race.txt",
            "x-content-sha256": hashlib.sha256(body).hexdigest(),
            "content-length": str(len(body)),
            "content-type": "text/plain",
        }

        async def stream(self):
            yield first
            streamed.set()
            await resume.wait()
            yield second

    async def race_upload():
        upload = asyncio.create_task(
            module.connector_upload_artifact(remote["id"], PausedRequest())
        )
        await streamed.wait()
        current = hosted["remote_runs"]["worker"]
        current["status"] = "completed"
        current["completed_at"] = int(module.time.time() * 1000)
        # Keep the old claim live to prove status, not only token rotation,
        # closes the second publication CAS.
        assert current["claim_token"] == claim["claim_token"]
        resume.set()
        with pytest.raises(module.HTTPException) as raised:
            await upload
        assert raised.value.status_code == 409

    asyncio.run(race_upload())
    library = module._file_library()
    origin_key = (
        f"remote:{remote['id']}:report/terminal-race.txt:"
        f"{hashlib.sha256(body).hexdigest()}"
    )
    assert library.get_file_by_origin("owner-a", origin_key) is None
    assert library.list_files("owner-a") == ([], 0)
    assert hosted["remote_runs"]["worker"].get("artifacts") in (None, [])
    assert not hosted.get("artifact_upload_intents")
    assert not any(
        str((message.get("meta") or {}).get("message_key") or "").startswith(
            f"{remote['id']}:artifact:"
        )
        for message in conversation["messages"]
        if isinstance(message, dict)
    )


def test_remote_artifact_final_publish_cas_rolls_back_staged_links_on_terminal(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_require_connector", lambda _request: "dbb3-primary")
    conversation = module.create_single_conversation("default", "Final publish CAS")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-final-publish-cas",
        content="deliver file",
        title="deliver file",
        profiles=["dbb3-worker"],
        artifact_required=False,
    )
    remote = module._ensure_remote_run(
        conversation["id"],
        hosted["turn_id"],
        role_stage="worker",
        profile="dbb3-worker",
        title="deliver file",
        objective="deliver file",
        local_task_id="task-final-publish-cas",
        artifact_required=False,
        delivery_context="",
        attachment_context="",
    )
    claim = module.connector_pull_runs(
        module.ConnectorPullBody(
            connector_id="dbb3-primary", limit=1, lease_seconds=30
        ),
        SimpleNamespace(),
    )["runs"][0]
    body = b"staged then terminal"
    origin_key = (
        f"remote:{remote['id']}:report/final-cas.txt:"
        f"{hashlib.sha256(body).hexdigest()}"
    )

    class CompleteRequest:
        headers = {
            "x-claim-token": claim["claim_token"],
            "x-remote-run-id": remote["id"],
            "x-relative-path": "report/final-cas.txt",
            "x-filename": "final-cas.txt",
            "x-content-sha256": hashlib.sha256(body).hexdigest(),
            "content-length": str(len(body)),
            "content-type": "text/plain",
        }

        async def stream(self):
            yield body

    real_gate = module._require_active_remote_artifact_claim
    gate_calls = 0

    def terminal_at_publish(hosted_record, remote_record, **kwargs):
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == 4:
            # The fourth gate runs after durable artifact/message linkage and
            # immediately before staged -> available publication.
            remote_record["status"] = "failed"
            remote_record["completed_at"] = int(module.time.time() * 1000)
        return real_gate(hosted_record, remote_record, **kwargs)

    monkeypatch.setattr(
        module,
        "_require_active_remote_artifact_claim",
        terminal_at_publish,
    )
    with pytest.raises(module.HTTPException) as raised:
        asyncio.run(module.connector_upload_artifact(remote["id"], CompleteRequest()))
    assert raised.value.status_code == 409
    assert gate_calls == 4
    library = module._file_library()
    assert library.get_file_by_origin("owner-a", origin_key) is None
    assert library.list_files("owner-a") == ([], 0)
    assert hosted["remote_runs"]["worker"].get("artifacts") in (None, [])
    assert not hosted.get("artifact_upload_intents")
    assert not any(
        str((message.get("meta") or {}).get("message_key") or "").startswith(
            f"{remote['id']}:artifact:"
        )
        for message in conversation["messages"]
        if isinstance(message, dict)
    )


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("terminal_phase", ["stream", "final_publish_cas"])
def test_remote_artifact_rejected_replay_preserves_existing_origin_and_message(
    tmp_path,
    monkeypatch,
    terminal_status,
    terminal_phase,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_require_connector", lambda _request: "dbb3-primary")
    conversation = module.create_single_conversation("default", "Existing artifact replay")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id=f"turn-existing-{terminal_status}-{terminal_phase}",
        content="deliver file",
        title="deliver file",
        profiles=["dbb3-worker"],
        artifact_required=False,
    )
    remote = module._ensure_remote_run(
        conversation["id"],
        hosted["turn_id"],
        role_stage="worker",
        profile="dbb3-worker",
        title="deliver file",
        objective="deliver file",
        local_task_id=f"task-existing-{terminal_status}-{terminal_phase}",
        artifact_required=False,
        delivery_context="",
        attachment_context="",
    )
    claim = module.connector_pull_runs(
        module.ConnectorPullBody(connector_id="dbb3-primary", limit=1, lease_seconds=30),
        SimpleNamespace(),
    )["runs"][0]
    body = b"already-published-content"
    digest = hashlib.sha256(body).hexdigest()
    relative_path = "report/stable.bin"
    origin_key = f"remote:{remote['id']}:{relative_path}:{digest}"
    source = tmp_path / "old-source.bin"
    source.write_bytes(body)
    library = module._file_library()
    original_record = library.ingest_file(
        "owner-a",
        source,
        name="old.bin",
        source="model_output",
        conversation_id=conversation["id"],
        turn_id=hosted["turn_id"],
        profile="dbb3-worker",
        origin_key=origin_key,
    )
    original_attachment = module._library_attachment(original_record)
    current = hosted["remote_runs"]["worker"]
    current["artifacts"] = [json.loads(json.dumps(original_attachment))]
    message_key = f"{remote['id']}:artifact:{original_record['id']}"
    module._append_message(
        conversation,
        role="assistant",
        name="dbb3-worker",
        content="original artifact message",
        status="completed",
        kind="message",
        meta={
            "message_key": message_key,
            "attachments": [json.loads(json.dumps(original_attachment))],
            "custom": {"preserve": True},
        },
    )
    original_messages = json.loads(json.dumps(conversation["messages"]))
    original_artifacts = json.loads(json.dumps(current["artifacts"]))
    original_path = library.resolve_download("owner-a", original_record["id"])[1]

    class ReplayRequest:
        headers = {
            "x-claim-token": claim["claim_token"],
            "x-remote-run-id": remote["id"],
            "x-relative-path": relative_path,
            "x-filename": "new.bin",
            "x-content-sha256": digest,
            "content-length": str(len(body)),
            "content-type": "application/octet-stream",
        }

        async def stream(self):
            if terminal_phase == "stream":
                split = len(body) // 2
                yield body[:split]
                current["status"] = terminal_status
                current["completed_at"] = int(module.time.time() * 1000)
                yield body[split:]
            else:
                yield body

    if terminal_phase == "final_publish_cas":
        real_gate = module._require_active_remote_artifact_claim
        gate_calls = 0

        def terminal_at_publish(hosted_record, remote_record, **kwargs):
            nonlocal gate_calls
            gate_calls += 1
            if gate_calls == 4:
                remote_record["status"] = terminal_status
                remote_record["completed_at"] = int(module.time.time() * 1000)
            return real_gate(hosted_record, remote_record, **kwargs)

        monkeypatch.setattr(module, "_require_active_remote_artifact_claim", terminal_at_publish)

    with pytest.raises(module.HTTPException) as raised:
        asyncio.run(module.connector_upload_artifact(remote["id"], ReplayRequest()))
    assert raised.value.status_code == 409

    persisted = library.get_file_by_origin("owner-a", origin_key)
    assert persisted == original_record
    downloaded, persisted_path = library.resolve_download("owner-a", original_record["id"])
    assert downloaded == original_record
    assert persisted_path == original_path
    assert persisted_path.read_bytes() == body
    assert conversation["messages"] == original_messages
    assert current["artifacts"] == original_artifacts
    assert not hosted.get("artifact_upload_intents")
    assert library.list_files("owner-a") == ([original_record], 1)
    assert not list(library.root.rglob("new.bin"))
    upload_temp = tmp_path / "collaboration" / "connector-upload-tmp"
    assert not list(upload_temp.glob("*.upload")) if upload_temp.exists() else True


def test_remote_artifact_stream_loses_claim_before_publish_without_any_visible_file(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_require_connector", lambda _request: "dbb3-primary")
    conversation = module.create_single_conversation("default", "Artifact claim race")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-artifact-claim-race",
        content="deliver file",
        title="deliver file",
        profiles=["dbb3-worker"],
        artifact_required=True,
    )
    remote = module._ensure_remote_run(
        conversation["id"],
        "turn-artifact-claim-race",
        role_stage="worker",
        profile="dbb3-worker",
        title="deliver file",
        objective="deliver file",
        local_task_id="task-artifact-race",
        artifact_required=True,
        delivery_context="",
        attachment_context="",
    )
    first_claim = module.connector_pull_runs(
        module.ConnectorPullBody(
            connector_id="dbb3-primary", limit=1, lease_seconds=30
        ),
        SimpleNamespace(),
    )["runs"][0]
    first = b"first half;"
    second = b"second half"
    body = first + second
    streamed = asyncio.Event()
    resume = asyncio.Event()

    class PausedRequest:
        headers = {
            "x-claim-token": first_claim["claim_token"],
            "x-remote-run-id": remote["id"],
            "x-relative-path": "report/output.txt",
            "x-filename": "output.txt",
            "x-content-sha256": hashlib.sha256(body).hexdigest(),
            "content-length": str(len(body)),
            "content-type": "text/plain",
        }

        async def stream(self):
            yield first
            streamed.set()
            await resume.wait()
            yield second

    async def race_upload():
        upload = asyncio.create_task(
            module.connector_upload_artifact(remote["id"], PausedRequest())
        )
        await streamed.wait()
        current = hosted["remote_runs"]["worker"]
        current["lease_until"] = 0
        replacement = module.connector_pull_runs(
            module.ConnectorPullBody(
                connector_id="dbb3-primary", limit=1, lease_seconds=30
            ),
            SimpleNamespace(),
        )["runs"][0]
        assert replacement["claim_token"] != first_claim["claim_token"]
        resume.set()
        with pytest.raises(module.HTTPException) as raised:
            await upload
        assert raised.value.status_code == 409

    asyncio.run(race_upload())
    files, total = module._file_library().list_files("owner-a")
    assert total == 0
    assert files == []
    assert hosted["remote_runs"]["worker"].get("artifacts") in (None, [])
    assert not any(
        str((message.get("meta") or {}).get("message_key") or "").startswith(
            f"{remote['id']}:artifact:"
        )
        for message in conversation["messages"]
        if isinstance(message, dict)
    )


def test_remote_artifact_process_exit_after_ingest_recovers_staged_orphan(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_require_connector", lambda _request: "dbb3-primary")
    conversation = module.create_single_conversation("default", "Artifact crash")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id="turn-artifact-crash",
        content="deliver file",
        title="deliver file",
        profiles=["dbb3-worker"],
        artifact_required=True,
    )
    remote = module._ensure_remote_run(
        conversation["id"],
        "turn-artifact-crash",
        role_stage="worker",
        profile="dbb3-worker",
        title="deliver file",
        objective="deliver file",
        local_task_id="task-artifact-crash",
        artifact_required=True,
        delivery_context="",
        attachment_context="",
    )
    claim = module.connector_pull_runs(
        module.ConnectorPullBody(
            connector_id="dbb3-primary", limit=1, lease_seconds=30
        ),
        SimpleNamespace(),
    )["runs"][0]
    body = b"durable staged artifact"
    origin_key = (
        f"remote:{remote['id']}:report/crash.txt:{hashlib.sha256(body).hexdigest()}"
    )

    class CompleteRequest:
        headers = {
            "x-claim-token": claim["claim_token"],
            "x-remote-run-id": remote["id"],
            "x-relative-path": "report/crash.txt",
            "x-filename": "crash.txt",
            "x-content-sha256": hashlib.sha256(body).hexdigest(),
            "content-length": str(len(body)),
            "content-type": "text/plain",
        }

        async def stream(self):
            yield body

    class SimulatedProcessExit(BaseException):
        pass

    library = module._file_library()
    real_ingest = library.ingest_file

    def install_then_exit(*args, **kwargs):
        real_ingest(*args, **kwargs)
        raise SimulatedProcessExit()

    monkeypatch.setattr(library, "ingest_file", install_then_exit)
    with pytest.raises(SimulatedProcessExit):
        asyncio.run(
            module.connector_upload_artifact(remote["id"], CompleteRequest())
        )

    generation = conversation["account_generation"]
    staged = library.get_file_by_origin(
        "owner-a",
        origin_key,
        account_generation=generation,
    )
    assert staged is not None
    assert staged["status"] == "staged"
    staged_path = library._record_path(staged)
    assert staged_path.is_file()
    assert library.list_files(
        "owner-a",
        account_generation=generation,
    ) == ([], 0)
    assert hosted.get("artifact_upload_intents")
    assert hosted["remote_runs"]["worker"].get("artifacts") in (None, [])

    # A new process constructs a new library instance. Its first access
    # resolves the durable intent before returning any account-file data.
    module._FILE_LIBRARY = None
    recovered_library = module._file_library()
    assert recovered_library.get_file_by_origin(
        "owner-a",
        origin_key,
        account_generation=generation,
    ) is None
    assert recovered_library.list_files(
        "owner-a",
        account_generation=generation,
    ) == ([], 0)
    assert not staged_path.exists()
    assert not hosted.get("artifact_upload_intents")
    assert not any(
        str((message.get("meta") or {}).get("message_key") or "").startswith(
            f"{remote['id']}:artifact:"
        )
        for message in conversation["messages"]
        if isinstance(message, dict)
    )


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
def test_artifact_intent_recovery_never_deletes_preexisting_origin(
    tmp_path,
    monkeypatch,
    terminal_status,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_require_connector", lambda _request: "dbb3-primary")
    conversation = module.create_single_conversation("default", "Recover existing artifact")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    hosted = module.create_hosted_turn_record(
        conversation,
        turn_id=f"turn-recover-existing-{terminal_status}",
        content="deliver file",
        title="deliver file",
        profiles=["dbb3-worker"],
        artifact_required=False,
    )
    remote = module._ensure_remote_run(
        conversation["id"],
        hosted["turn_id"],
        role_stage="worker",
        profile="dbb3-worker",
        title="deliver file",
        objective="deliver file",
        local_task_id=f"task-recover-existing-{terminal_status}",
        artifact_required=False,
        delivery_context="",
        attachment_context="",
    )
    claim = module.connector_pull_runs(
        module.ConnectorPullBody(connector_id="dbb3-primary", limit=1, lease_seconds=30),
        SimpleNamespace(),
    )["runs"][0]
    body = b"preexisting-recovery-content"
    digest = hashlib.sha256(body).hexdigest()
    relative_path = "report/recovery.bin"
    origin_key = f"remote:{remote['id']}:{relative_path}:{digest}"
    source = tmp_path / "recovery-old.bin"
    source.write_bytes(body)
    library = module._file_library()
    original_record = library.ingest_file(
        "owner-a",
        source,
        account_generation=conversation["account_generation"],
        name="old.bin",
        source="model_output",
        conversation_id=conversation["id"],
        turn_id=hosted["turn_id"],
        profile="dbb3-worker",
        origin_key=origin_key,
    )
    original_path = library.resolve_download(
        "owner-a",
        original_record["id"],
        account_generation=conversation["account_generation"],
    )[1]
    original_attachment = module._library_attachment(original_record)
    current = hosted["remote_runs"]["worker"]
    current["artifacts"] = [json.loads(json.dumps(original_attachment))]
    module._append_message(
        conversation,
        role="assistant",
        name="dbb3-worker",
        content="existing message remains exact",
        status="completed",
        kind="message",
        meta={
            "message_key": f"{remote['id']}:artifact:{original_record['id']}",
            "attachments": [json.loads(json.dumps(original_attachment))],
            "custom": "unchanged",
        },
    )
    original_messages = json.loads(json.dumps(conversation["messages"]))
    original_artifacts = json.loads(json.dumps(current["artifacts"]))

    class SimulatedProcessExit(BaseException):
        pass

    real_ingest = library.ingest_file

    def replay_then_exit(*args, **kwargs):
        replayed = real_ingest(*args, **kwargs)
        assert replayed == original_record
        raise SimulatedProcessExit()

    monkeypatch.setattr(library, "ingest_file", replay_then_exit)

    class ReplayRequest:
        headers = {
            "x-claim-token": claim["claim_token"],
            "x-remote-run-id": remote["id"],
            "x-relative-path": relative_path,
            "x-filename": "new.bin",
            "x-content-sha256": digest,
            "content-length": str(len(body)),
            "content-type": "application/octet-stream",
        }

        async def stream(self):
            yield body

    with pytest.raises(SimulatedProcessExit):
        asyncio.run(module.connector_upload_artifact(remote["id"], ReplayRequest()))
    assert hosted.get("artifact_upload_intents")
    current["status"] = terminal_status
    current["completed_at"] = int(module.time.time() * 1000)

    module._FILE_LIBRARY = None
    recovered_library = module._file_library()
    assert recovered_library.get_file_by_origin(
        "owner-a",
        origin_key,
        account_generation=conversation["account_generation"],
    ) == original_record
    downloaded, recovered_path = recovered_library.resolve_download(
        "owner-a",
        original_record["id"],
        account_generation=conversation["account_generation"],
    )
    assert downloaded == original_record
    assert recovered_path == original_path
    assert recovered_path.read_bytes() == body
    assert conversation["messages"] == original_messages
    assert current["artifacts"] == original_artifacts
    assert recovered_library.list_files(
        "owner-a",
        account_generation=conversation["account_generation"],
    ) == ([original_record], 1)
    assert not hosted.get("artifact_upload_intents")
    assert not list(recovered_library.root.rglob("new.bin"))


def test_remote_progress_creates_semantic_milestones_and_redacts_activity_secrets(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    conversation = module.create_single_conversation("default", "Milestones")
    conversation["owner_id"] = "owner-a"
    state = {"conversations": [conversation]}
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    module.create_hosted_turn_record(
        conversation,
        turn_id="turn-milestones",
        content="do work",
        title="do work",
        profiles=["dbb3-worker"],
        artifact_required=False,
    )
    remote = module._ensure_remote_run(
        conversation["id"],
        "turn-milestones",
        role_stage="worker",
        profile="dbb3-worker",
        title="do work",
        objective="do work",
        local_task_id="task-work",
        artifact_required=False,
        delivery_context="",
        attachment_context="",
    )
    activity = {
        "id": "tool-1",
        "kind": "tool",
        "name": "terminal",
        "tool_name": "terminal",
        "input": {"authorization": "Bearer private-token", "token_count": 12},
        "output": "Cookie: session=private-cookie",
        "status": "completed",
    }
    hosted_remote = state["conversations"][0]["hosted_turns"][
        "turn-milestones"
    ]["remote_runs"]["worker"]
    hosted_remote.update(
        {
            "lease_owner": "dbb3-primary",
            "lease_until": int(module.time.time() * 1000) + 60_000,
            "claim_token": "claim-milestones",
        }
    )
    for cursor, summary in ((1, "完成环境检查"), (2, "完成代码修改"), (3, "完成代码修改")):
        persisted, applied = module._apply_remote_checkpoint(
            remote["id"],
            {
                "connector_id": "dbb3-primary",
                "claim_token": "claim-milestones",
                "checkpoint_cursor": cursor,
                "status": "running",
                "terminal": False,
                "summary": summary,
                "activities": [activity],
            },
        )
        assert applied is True
        assert persisted["summary"] == summary

    milestone_messages = [
        message
        for message in conversation["messages"]
        if (message.get("meta") or {}).get("phase") == "milestone"
    ]
    assert [message["content"] for message in milestone_messages] == [
        "完成环境检查",
        "完成代码修改",
    ]
    encoded = str(conversation)
    assert "private-token" not in encoded
    assert "private-cookie" not in encoded
    assert "[REDACTED]" in encoded
    for raw_secret in (
        "OPENAI_API_KEY=private-openai-key",
        "DATABASE_PASSWORD='private-database-password'",
        "TOKEN: private-token-value",
        'password="private secret password"',
        "credential='private credential phrase'",
    ):
        redacted = module._redact_sensitive(raw_secret)
        assert "private-" not in redacted
        assert "private secret password" not in redacted
        assert "private credential phrase" not in redacted
        assert "[REDACTED]" in redacted
    stored_activity = conversation["hosted_turns"]["turn-milestones"]["remote_runs"]["worker"]["activities"][0]
    assert stored_activity["input"]["token_count"] == 12


def test_delete_cleanup_failure_returns_retryable_status_and_keeps_tombstone(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        module, "_account_generation_for_owner", lambda _owner: "generation-a"
    )
    conversation = module.create_single_conversation("default", "Retry delete")
    conversation.update({"owner_id": "owner-a", "account_generation": "generation-a"})
    single_state = {"conversations": [conversation]}
    room_state = {"rooms": []}
    monkeypatch.setattr(module, "load_single_state", lambda: single_state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    monkeypatch.setattr(module, "load_state", lambda: room_state)
    monkeypatch.setattr(module, "save_state", lambda _state: None)
    monkeypatch.setattr(module, "_notify_hosted_update", lambda *_args: None)

    class FailingLibrary:
        def delete_conversation(self, *_args, **_kwargs):
            raise OSError("account file store unavailable")

    monkeypatch.setattr(module, "_file_library", lambda: FailingLibrary())
    prefix = "/api/plugins/collaboration"

    with _client(module, owner="owner-a") as client:
        response = client.delete(
            f"{prefix}/single/conversations/{conversation['id']}"
        )

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "conversation_deletion_pending"
    assert conversation["delete_requested"] is True
    assert single_state["conversations"] == [conversation]


def test_room_delete_retry_finds_tombstone_after_room_alias_was_removed(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        module, "_account_generation_for_owner", lambda _owner: "generation-a"
    )
    conversation = module.create_single_conversation("default", "Retry room")
    conversation.update({"owner_id": "owner-a", "account_generation": "generation-a"})
    room = module.create_room_record(
        "Retry room", ["default"], "owner-a", "generation-a"
    )
    room["conversation_id"] = conversation["id"]
    conversation["room_id"] = room["id"]
    single_state = {"conversations": [conversation]}
    room_state = {"rooms": [room]}
    monkeypatch.setattr(module, "load_single_state", lambda: single_state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    monkeypatch.setattr(module, "load_state", lambda: room_state)
    monkeypatch.setattr(module, "save_state", lambda _state: None)
    monkeypatch.setattr(module, "_notify_hosted_update", lambda *_args: None)

    attempts = []

    class FlakyLibrary:
        def delete_conversation(self, *_args, **_kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError("transient cloud failure")

    monkeypatch.setattr(module, "_file_library", lambda: FlakyLibrary())
    prefix = "/api/plugins/collaboration"

    with _client(module, owner="owner-a") as client:
        first = client.delete(f"{prefix}/rooms/{room['id']}")
        assert first.status_code == 503
        assert room_state["rooms"] == []
        assert conversation["delete_requested"] is True

        second = client.delete(f"{prefix}/rooms/{room['id']}")

    assert second.status_code == 200
    assert second.json() == {"ok": True}
    assert single_state["conversations"] == []
    assert len(attempts) == 2


def test_sidecar_cleanup_failure_returns_retryable_status_and_preserves_files(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        module, "_account_generation_for_owner", lambda _owner: "generation-a"
    )
    conversation = module.create_single_conversation("default", "Sidecar retry")
    conversation.update({"owner_id": "owner-a", "account_generation": "generation-a"})
    single_state = {"conversations": [conversation]}
    room_state = {"rooms": []}
    files_root = module.conversation_files_root(conversation["id"])
    files_root.mkdir(parents=True)
    (files_root / "history.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "load_single_state", lambda: single_state)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    monkeypatch.setattr(module, "load_state", lambda: room_state)
    monkeypatch.setattr(module, "save_state", lambda _state: None)
    monkeypatch.setattr(module, "_notify_hosted_update", lambda *_args: None)
    monkeypatch.setattr(module, "_file_library", lambda: SimpleNamespace(
        delete_conversation=lambda *_args, **_kwargs: None,
    ))
    monkeypatch.setattr(
        module.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk busy")),
    )

    prefix = "/api/plugins/collaboration"
    with _client(module, owner="owner-a") as client:
        response = client.delete(
            f"{prefix}/single/conversations/{conversation['id']}"
        )

    assert response.status_code == 503
    assert conversation["delete_requested"] is True
    assert files_root.exists()
    assert single_state["conversations"] == [conversation]


@pytest.mark.parametrize(
    "conversation_id",
    [
        "",
        ".",
        "..",
        "../escape",
        r"..\escape",
        "chat:bad",
        "chat*bad",
        "chat?bad",
        "chat.bad.",
        "chat.bad ",
        "CON",
        "CON.txt",
        "NUL",
        "LPT1.log",
    ],
)
def test_conversation_storage_rejects_traversal_and_windows_dangerous_ids(
    tmp_path,
    monkeypatch,
    conversation_id,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)

    with pytest.raises(ValueError):
        module.conversation_files_root(conversation_id)
    assert module._conversation_history_path(conversation_id) is None
    assert module._conversation_archive_path(conversation_id) is None


def test_enqueue_rejects_an_invalid_conversation_id_before_side_effects():
    module = _load_module()
    payload = module.EnqueueHostedTurnBody(
        request_id="request-1",
        turn_id="turn-1",
        message={"role": "user", "content": "hello"},
    )

    with pytest.raises(HTTPException) as error:
        module.enqueue_hosted_turn(r"..\escape", payload, None)

    assert error.value.status_code == 422


@pytest.mark.parametrize(
    "client_id",
    [
        "chat_namespace:12345678",
        "chat_bad?12345678",
        "chat_client-stable-001 ",
    ],
)
def test_create_rejects_ids_that_are_not_cross_platform_path_components(
    tmp_path,
    monkeypatch,
    client_id,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "available_profiles", lambda: [{"name": "default"}])
    monkeypatch.setattr(module, "load_single_state", lambda: {"conversations": []})
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)

    with _client(module, owner="owner-a") as client:
        response = client.post(
            "/api/plugins/collaboration/single/conversations",
            json={"client_id": client_id, "profile": "default"},
        )

    assert response.status_code == 422
