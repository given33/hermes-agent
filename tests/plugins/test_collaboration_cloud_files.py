from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, Request
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
    app = FastAPI()

    @app.middleware("http")
    async def attach_identity(request: Request, call_next):
        request.state.session = SimpleNamespace(
            user_id=request.headers.get("x-test-owner", owner)
        )
        return await call_next(request)

    app.include_router(module.router, prefix="/api/plugins/collaboration")
    return TestClient(app)


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
        assert uploaded["source"] == "user_upload"
        assert uploaded["bucket"] == "uploads"
        assert uploaded["status"] == "available"
        assert uploaded["sha256"]
        assert Path(uploaded["path"]).read_bytes() == b"user upload"

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
        assert Path(uploaded["path"]).read_bytes() == b"user upload"

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
        assert reserved["path"] == ""

        output_path = Path(upload.json()["output_dir"]) / "result.pdf"
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
    monkeypatch.setattr(module, "_notify_hosted_update", lambda: 1)
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


def test_first_account_claims_legacy_rooms_once_and_other_accounts_stay_isolated(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    legacy = module.create_room_record("Legacy room", ["default"])
    legacy.pop("owner_id")
    legacy["messages"] = [
        {"role": "assistant", "name": "default", "content": "legacy message"}
    ]
    local_room = module.create_room_record("Local room", ["default"])
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
        [cancellation] = pulled.json()["cancellations"]
        assert cancellation["remote_run_id"] == remote["id"]
        assert cancellation["checkpoint_cursor"] == 7

        acknowledged = client.post(
            f"{prefix}/connector/runs/{remote['id']}/cancel-ack",
            headers=auth,
            json={
                "connector_id": "dbb3-primary",
                "checkpoint_cursor": 8,
                "summary": "Cancellation applied",
            },
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json()["applied"] is True
        assert acknowledged.json()["run"]["status"] == "cancelled"
        assert persisted_remote["status"] == "cancelled"
        assert persisted_remote["checkpoint_cursor"] == 8


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
        [leased] = pulled.json()["runs"]
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
    monkeypatch.setattr(module, "load_single_state", lambda: state)
    monkeypatch.setattr(module, "save_single_state", lambda value: saves.append(value))
    monkeypatch.setattr(
        module,
        "start_hosted_workflow",
        lambda conversation_id, turn_id: starts.append((conversation_id, turn_id)),
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
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["accepted"] is True
        assert first_payload["replayed"] is False
        assert first_payload["message"]["id"] == "message-atomic-1"
        assert first_payload["route_message"]["kind"] == "route"
        assert first_payload["hosted_turn"]["turn_id"] == "turn-atomic-1"
        assert len(conversation["messages"]) == 2
        assert len(saves) == 1

        replay = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/enqueue",
            json=body,
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert len(conversation["messages"]) == 2
        assert len(conversation["hosted_turns"]) == 1
        assert len(saves) == 1

        changed = dict(body)
        changed["message"] = {**body["message"], "content": "different"}
        conflict = client.post(
            f"{prefix}/single/conversations/{conversation['id']}/enqueue",
            json=changed,
        )
        assert conflict.status_code == 409

    assert starts == [
        (conversation["id"], "turn-atomic-1"),
        (conversation["id"], "turn-atomic-1"),
    ]


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
        assert [run["profile"] for run in dbb3.json()["runs"]] == ["dbb3-worker"]

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


def test_terminal_checkpoint_conflict_is_idempotent_and_keeps_original_result(
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
    hosted["remote_runs"]["worker"].update(
        {
            "status": "completed",
            "checkpoint_cursor": 8,
            "result": "original result",
        }
    )
    auth = {
        "authorization": "Bearer connector-secret",
        "x-connector-id": "dbb3-primary",
    }
    prefix = "/api/plugins/collaboration"

    with _client(module) as client:
        response = client.post(
            f"{prefix}/connector/runs/{remote['id']}/cancel-ack",
            headers=auth,
            json={
                "connector_id": "dbb3-primary",
                "checkpoint_cursor": 9,
                "summary": "cancel applied locally",
            },
        )
    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert response.json()["run"]["status"] == "completed"
    assert hosted["remote_runs"]["worker"]["result"] == "original result"


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
    for cursor, summary in ((1, "完成环境检查"), (2, "完成代码修改"), (3, "完成代码修改")):
        persisted, applied = module._apply_remote_checkpoint(
            remote["id"],
            {
                "connector_id": "dbb3-primary",
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
    ):
        redacted = module._redact_sensitive(raw_secret)
        assert "private-" not in redacted
        assert "[REDACTED]" in redacted
    stored_activity = conversation["hosted_turns"]["turn-milestones"]["remote_runs"]["worker"]["activities"][0]
    assert stored_activity["input"]["token_count"] == 12
