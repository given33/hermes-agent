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


@pytest.mark.asyncio
async def test_conversation_upload_persists_ios_context_headers(tmp_path: Path):
    module = load_module()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    captured = {}

    class FakeLibrary:
        def get_file_by_origin(self, _owner_id, _origin_key):
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
