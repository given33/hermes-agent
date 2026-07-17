from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.cloud_file_library import (
    CloudFileLibrary,
    LOCAL_OWNER_ID,
    owner_id_from_request,
    parse_date_filter,
    safe_file_name,
)


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_ingest_is_durable_and_records_delivery_metadata(tmp_path):
    source = _write(tmp_path / "incoming" / "report.pdf", b"%PDF-test-content")
    root = tmp_path / "cloud"
    now = 1_750_000_000_123
    library = CloudFileLibrary(root, clock_ms=lambda: now)

    record = library.ingest_file(
        "account-a",
        source,
        source="model_output",
        conversation_id="chat-1",
        message_id="msg-1",
        turn_id="turn-1",
        profile="dbb3-worker",
        allowed_roots=[source.parent],
    )

    assert record["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert record["mime_type"] == "application/pdf"
    assert record["file_type"] == "document"
    assert record["size"] == len(source.read_bytes())
    assert record["source"] == "model_output"
    assert record["status"] == "available"
    assert record["conversation_id"] == "chat-1"
    assert record["message_id"] == "msg-1"
    assert record["turn_id"] == "turn-1"
    assert record["profile"] == "dbb3-worker"
    assert record["created_at"] == now
    assert record["available_at"] == now
    assert "expires_at" not in record

    reopened = CloudFileLibrary(root)
    persisted, path = reopened.resolve_download("account-a", record["id"])
    assert persisted["sha256"] == record["sha256"]
    assert path.read_bytes() == source.read_bytes()


def test_owner_scope_applies_to_read_list_download_and_delete(tmp_path):
    source = _write(tmp_path / "source.txt", b"owner-a-only")
    library = CloudFileLibrary(tmp_path / "cloud")
    record = library.ingest_file("account-a", source, source="user_upload")

    assert library.get_file("account-b", record["id"]) is None
    assert library.list_files("account-b")[0] == []
    assert library.delete_file("account-b", record["id"]) is False
    with pytest.raises(KeyError):
        library.resolve_download("account-b", record["id"])

    assert library.delete_file("account-a", record["id"]) is True
    assert library.get_file("account-a", record["id"]) is None
    assert library.delete_file("account-a", record["id"]) is False


def test_keyword_date_source_and_type_filters(tmp_path):
    now = [parse_date_filter("2026-07-15T10:00:00Z")]
    assert now[0] is not None
    library = CloudFileLibrary(tmp_path / "cloud", clock_ms=lambda: int(now[0]))
    first = library.ingest_file(
        "account",
        _write(tmp_path / "quarterly-report.pdf", b"report"),
        source="user_upload",
        conversation_id="finance-chat",
        profile="default",
    )
    now[0] = parse_date_filter("2026-07-16T12:00:00Z")
    second = library.ingest_file(
        "account",
        _write(tmp_path / "preview.png", b"not-a-real-png"),
        source="model_output",
        conversation_id="design-chat",
        profile="pc-worker",
    )

    assert [item["id"] for item in library.list_files("account", keyword="quarterly")[0]] == [first["id"]]
    assert [item["id"] for item in library.list_files("account", keyword="design-chat")[0]] == [second["id"]]
    assert [item["id"] for item in library.list_files("account", keyword="pc-worker")[0]] == [second["id"]]
    assert [item["id"] for item in library.list_files("account", source="model")[0]] == [second["id"]]
    assert [item["id"] for item in library.list_files("account", file_type="image")[0]] == [second["id"]]
    assert [item["id"] for item in library.list_files("account", file_type="pdf")[0]] == [first["id"]]
    assert [item["id"] for item in library.list_files("account", date_from=parse_date_filter("2026-07-16"))[0]] == [second["id"]]
    assert [item["id"] for item in library.list_files("account", date_to=parse_date_filter("2026-07-15", end_of_day=True))[0]] == [first["id"]]


def test_artifact_lifecycle_reserves_completes_fails_and_links(tmp_path):
    output_root = tmp_path / "outputs"
    artifact = _write(output_root / "deck.pptx", b"presentation")
    library = CloudFileLibrary(tmp_path / "cloud")

    reserved = library.reserve_file(
        "account",
        name="deck.pptx",
        source="model_output",
        conversation_id="chat-2",
        turn_id="turn-2",
        origin_key="chat-2:deck.pptx",
    )
    assert reserved["status"] == "uploading"

    completed = library.ingest_file(
        "account",
        artifact,
        name="deck.pptx",
        source="model_output",
        conversation_id="chat-2",
        turn_id="turn-2",
        origin_key="chat-2:deck.pptx",
        file_id=reserved["id"],
        allowed_roots=[output_root],
    )
    assert completed["id"] == reserved["id"]
    assert completed["status"] == "available"
    assert library.update_links(
        "account",
        [completed["id"]],
        message_id="msg-final",
        profile="reporter",
    ) == 1
    assert library.get_file("account", completed["id"])["message_id"] == "msg-final"

    failed = library.reserve_file(
        "account",
        name="failed.zip",
        source="model_output",
    )
    failed = library.set_status(
        "account",
        failed["id"],
        "failed",
        error="upload interrupted",
    )
    assert failed["status"] == "failed"
    assert failed["error"] == "upload interrupted"
    with pytest.raises(ValueError):
        library.set_status("account", failed["id"], "available")


def test_sync_outputs_is_idempotent_and_updates_changed_artifact(tmp_path):
    output_root = tmp_path / "outputs"
    artifact = _write(output_root / "nested" / "result.csv", b"a,b\n1,2\n")
    library = CloudFileLibrary(tmp_path / "cloud")

    first = library.sync_directory(
        "account",
        output_root,
        source="model_output",
        conversation_id="chat-sync",
        turn_id="turn-sync",
        origin_prefix="chat-sync:outputs",
    )
    second = library.sync_directory(
        "account",
        output_root,
        source="model_output",
        conversation_id="chat-sync",
        origin_prefix="chat-sync:outputs",
    )
    assert len(first) == len(second) == 1
    assert first[0]["id"] == second[0]["id"]
    assert first[0]["turn_id"] == second[0]["turn_id"] == "turn-sync"
    assert library.list_files("account", turn_id="turn-sync")[1] == 1
    assert library.list_files("account")[1] == 1

    artifact.write_bytes(b"a,b\n3,4\n")
    changed = library.sync_directory(
        "account",
        output_root,
        source="model_output",
        conversation_id="chat-sync",
        origin_prefix="chat-sync:outputs",
    )[0]
    assert changed["id"] == first[0]["id"]
    assert changed["sha256"] != first[0]["sha256"]
    assert library.resolve_download("account", changed["id"])[1].read_bytes() == artifact.read_bytes()

    assert library.delete_file("account", changed["id"]) is True
    assert library.sync_directory(
        "account",
        output_root,
        source="model_output",
        conversation_id="chat-sync",
        origin_prefix="chat-sync:outputs",
    ) == []
    assert library.list_files("account")[1] == 0

    # A genuinely new output at the same path is discoverable again.
    artifact.write_bytes(b"a,b\n5,6\n")
    recreated = library.sync_directory(
        "account",
        output_root,
        source="model_output",
        conversation_id="chat-sync",
        origin_prefix="chat-sync:outputs",
    )
    assert len(recreated) == 1
    assert recreated[0]["sha256"] != changed["sha256"]


def test_sync_ignores_in_progress_upload_temps(tmp_path):
    upload_root = tmp_path / "uploads"
    _write(upload_root / ".report.pdf.abc.upload", b"partial")
    library = CloudFileLibrary(tmp_path / "cloud")

    assert library.sync_directory(
        "account",
        upload_root,
        source="user_upload",
        conversation_id="chat-upload",
        origin_prefix="chat-upload:uploads",
    ) == []


def test_source_and_stored_paths_are_confined(tmp_path):
    allowed = tmp_path / "outputs"
    allowed.mkdir()
    outside = _write(tmp_path / "secret.txt", b"secret")
    library = CloudFileLibrary(tmp_path / "cloud")

    with pytest.raises(ValueError, match="outside"):
        library.ingest_file(
            "account",
            outside,
            source="model_output",
            allowed_roots=[allowed],
        )
    with pytest.raises(ValueError, match="escapes"):
        library._record_path({"stored_relpath": "../secret.txt"})
    assert safe_file_name("../../report.pdf") == "report.pdf"
    assert safe_file_name(r"..\..\report.pdf") == "report.pdf"


def test_request_owner_prefers_authenticated_session_then_token_principal():
    session_request = SimpleNamespace(
        state=SimpleNamespace(session=SimpleNamespace(user_id=" owner-a "))
    )
    token_request = SimpleNamespace(
        state=SimpleNamespace(
            session=None,
            token_principal=SimpleNamespace(principal="mobile-owner"),
        )
    )
    local_request = SimpleNamespace(state=SimpleNamespace())

    assert owner_id_from_request(session_request) == "owner-a"
    assert owner_id_from_request(token_request) == "mobile-owner"
    assert owner_id_from_request(local_request) == LOCAL_OWNER_ID


def test_date_parser_accepts_epoch_and_rejects_invalid_input():
    assert parse_date_filter("1750000000") == 1_750_000_000_000
    assert parse_date_filter("1750000000123") == 1_750_000_000_123
    assert parse_date_filter("2026-07-16") < parse_date_filter(
        "2026-07-16", end_of_day=True
    )
    with pytest.raises(ValueError, match="ISO-8601"):
        parse_date_filter("next thursday")
