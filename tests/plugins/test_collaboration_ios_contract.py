"""Focused iOS-to-collaboration HTTP contract regression tests."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "collaboration"
    / "dashboard"
    / "plugin_api.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "collaboration_plugin_ios_contract",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_account_file_type_query_accepts_ios_type_alias():
    module = load_module()
    captured = []
    library = SimpleNamespace(
        list_files=lambda _owner_id, **kwargs: (
            captured.append(kwargs) or ([], 0)
        ),
    )
    module.owner_id_from_request = lambda _request: "owner-a"
    module._migrate_account_conversation_files_once = lambda _owner_id: None
    module._file_library = lambda: library

    response = module.list_account_files(
        SimpleNamespace(query_params={"type": "document"}),
    )

    assert response["total"] == 0
    assert captured[-1]["file_type"] == "document"


def test_explicit_file_type_takes_precedence_over_legacy_alias():
    module = load_module()
    captured = []
    module.owner_id_from_request = lambda _request: "owner-a"
    module._migrate_account_conversation_files_once = lambda _owner_id: None
    module._file_library = lambda: SimpleNamespace(
        list_files=lambda _owner_id, **kwargs: (
            captured.append(kwargs) or ([], 0)
        ),
    )

    module.list_account_files(
        SimpleNamespace(query_params={"type": "document"}),
        file_type="image",
    )

    assert captured[-1]["file_type"] == "image"


def test_collaboration_room_projection_supports_incremental_message_pages():
    module = load_module()
    messages = [
        {"id": f"message-{index}", "role": "assistant", "content": str(index)}
        for index in range(5)
    ]
    projection = module._room_projection(
        {
            "id": "room-page",
            "owner_id": "owner-a",
            "conversation_id": "chat_room_page",
            "messages": [],
        },
        {
            "conversations": [{
                "id": "chat_room_page",
                "owner_id": "owner-a",
                "messages": messages,
            }],
        },
        summary=False,
        offset=2,
        limit=2,
    )

    assert [item["id"] for item in projection["messages"]] == ["message-2", "message-3"]
    assert projection["message_count"] == 5
    assert projection["offset"] == 2
    assert projection["limit"] == 2
    assert projection["has_more"] is True


def test_listing_migrates_legacy_room_transcript_into_mobile_conversation_index(
    monkeypatch,
):
    module = load_module()
    room = {
        "id": "room_legacy",
        "name": "Legacy room",
        "profiles": ["default"],
        "owner_id": "owner-a",
        "account_generation": "generation-a",
        "messages": [{"id": "message-1", "role": "assistant", "content": "saved"}],
    }
    room_state = {"rooms": [room]}
    single_state = {"conversations": []}
    single_saves = []
    room_saves = []
    monkeypatch.setattr(module, "owner_id_from_request", lambda _request: "owner-a")
    monkeypatch.setattr(
        module,
        "_account_generation_for_request",
        lambda _request, _owner: "generation-a",
    )
    monkeypatch.setattr(module, "_claim_legacy_rooms_in_state", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(module, "load_state", lambda: room_state)
    monkeypatch.setattr(module, "load_single_state", lambda: single_state)
    monkeypatch.setattr(module, "save_state", lambda state: room_saves.append(state))
    monkeypatch.setattr(module, "save_single_state", lambda state: single_saves.append(state))

    response = module.get_rooms(SimpleNamespace())

    conversation_id = response["rooms"][0]["conversation_id"]
    assert conversation_id == "chat_room_legacy"
    assert response["rooms"][0]["messages"][0]["content"] == "saved"
    assert room["messages"] == []
    assert single_state["conversations"][0]["id"] == conversation_id
    assert single_state["conversations"][0]["source"] == "collaboration_room"
    assert len(single_saves) == len(room_saves) == 1


def test_full_history_sidecar_survives_hot_state_trim_and_drives_mobile_pages(
    tmp_path: Path,
    monkeypatch,
):
    module = load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    messages = [
        {
            "id": f"message-{index}",
            "role": "assistant",
            "content": ("x" * 10_000) if index == 0 else str(index),
        }
        for index in range(55)
    ]
    conversation = {
        "id": "chat_room_complete",
        "owner_id": "owner-a",
        "account_generation": "generation-a",
        "messages": messages,
        "session_entries": [],
        "updated_at": 55,
    }
    state = {"conversations": [conversation]}

    module._persist_conversation_histories(state)
    module._trim_hosted_state(state)

    assert len(conversation["messages"]) == 40
    summary = module._room_projection(
        {
            "id": "room_complete",
            "owner_id": "owner-a",
            "conversation_id": conversation["id"],
        },
        state,
        summary=True,
    )
    detail = module._room_projection(
        {
            "id": "room_complete",
            "owner_id": "owner-a",
            "conversation_id": conversation["id"],
        },
        state,
        summary=False,
        offset=0,
        limit=20,
    )

    assert summary["message_count"] == 55
    assert summary["messages"][0]["id"] == "message-54"
    assert [item["id"] for item in detail["messages"]] == [
        f"message-{index}" for index in range(20)
    ]
    assert detail["has_more"] is True
    assert len(module._read_conversation_history(conversation["id"])["messages"][0]["content"]) == 8_000
    assert module._read_conversation_history_meta(conversation["id"])["message_count"] == 55


def test_sidecar_failure_prevents_hot_history_trim(tmp_path: Path, monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    state = {
        "conversations": [{
            "id": "chat_room_unwritten",
            "owner_id": "owner-a",
            "account_generation": "generation-a",
            "messages": [{"id": f"message-{index}"} for index in range(41)],
            "session_entries": [],
        }],
    }
    monkeypatch.setattr(
        module,
        "_write_conversation_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        module.save_single_state(state)

    assert len(state["conversations"][0]["messages"]) == 41


def test_archived_conversation_keeps_index_count_and_restores_room_detail(
    tmp_path: Path,
    monkeypatch,
):
    module = load_module()
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "save_single_state", lambda _state: None)
    conversation = module.create_single_conversation("default", "Archived history")
    conversation.update(
        {
            "owner_id": "owner-a",
            "account_generation": "generation-a",
            "room_id": "room_archived",
            "updated_at": 1,
            "messages": [
                {"id": f"message-{index}", "role": "assistant", "content": str(index)}
                for index in range(55)
            ],
        }
    )
    state = {"conversations": [conversation]}

    module._archive_completed_conversations(state)

    placeholder = state["conversations"][0]
    assert placeholder["archived"] is True
    assert placeholder["message_count"] == 55
    assert placeholder["messages"][-1]["id"] == "message-54"

    room = {
        "id": "room_archived",
        "owner_id": "owner-a",
        "conversation_id": conversation["id"],
        "messages": [],
    }
    summary = module._room_projection(room, state, summary=True)
    assert summary["message_count"] == 55
    assert summary["messages"][0]["id"] == "message-54"

    detail = module._room_projection(
        room,
        state,
        summary=False,
        offset=0,
        limit=20,
    )
    assert [item["id"] for item in detail["messages"]] == [
        f"message-{index}" for index in range(20)
    ]
    assert detail["has_more"] is True
    assert state["conversations"][0].get("archived") is not True


def test_account_files_v1_contract_is_echoed_and_selects_exact_semantics():
    module = load_module()
    captured = []
    module.owner_id_from_request = lambda _request: "owner-a"
    module._file_library = lambda: SimpleNamespace(
        list_files=lambda _owner_id, **kwargs: (
            captured.append(kwargs) or ([], 0)
        ),
    )

    response = module.list_account_files(
        SimpleNamespace(query_params={}),
        filter_contract="account-files-v1",
    )

    assert response["filter_contract"] == "account-files-v1"
    assert captured[-1]["account_files_contract"] is True


@pytest.mark.asyncio
async def test_conversation_upload_persists_ios_context_headers(tmp_path: Path):
    module = load_module()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    captured = {}

    class FakeLibrary:
        def get_file_by_origin(self, _owner_id, _origin_key, **_kwargs):
            return None

        def ingest_file(self, owner_id, source_path, **kwargs):
            captured.update(owner_id=owner_id, source_path=source_path, **kwargs)
            return {
                "id": "file_contract",
                "name": kwargs["name"],
                "status": "available",
                "source": "user_upload",
                "message_id": kwargs["message_id"],
                "turn_id": kwargs["turn_id"],
                "profile": kwargs["profile"],
            }

    class FakeRequest:
        headers = {
            "content-type": "text/plain",
            "x-filename": "notes.txt",
            "x-message-id": "message-1",
            "x-profile": "reviewer",
            "x-turn-id": "turn-1",
            "x-upload-id": "upload-contract-1",
        }

        async def stream(self):
            yield b"contract payload"

    module._owned_conversation = lambda _request, _conversation_id: (
        "owner-a",
        {"profile": "default"},
    )
    module._conversation_file_dir = lambda _conversation_id, _bucket: uploads
    module._file_library = lambda: FakeLibrary()

    response = await module.upload_conversation_attachment(
        "chat_contract",
        FakeRequest(),
    )

    assert response["attachment"]["id"] == "file_contract"
    assert captured["message_id"] == "message-1"
    assert captured["turn_id"] == "turn-1"
    assert captured["profile"] == "reviewer"
    assert Path(captured["source_path"]).name.startswith(".notes.txt.")
    assert not list(uploads.glob("*.upload"))
