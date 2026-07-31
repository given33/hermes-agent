from __future__ import annotations

import hashlib
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
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


def _multiprocess_ingest_worker(root, source, barrier, results):
    try:
        barrier.wait(timeout=20)
        record = CloudFileLibrary(root).ingest_file(
            "account-a",
            source,
            account_generation="generation-a",
            source="user_upload",
            origin_key="account-upload:shared-upload-id",
        )
        results.put(("ok", record["id"], record["sha256"]))
    except (FileExistsError, BlockingIOError) as exc:
        results.put(("conflict", type(exc).__name__, str(exc)))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _crash_after_replace_worker(root, source):
    import hermes_cli.cloud_file_library as cloud_files

    original_replace = cloud_files.os.replace

    def replace_then_exit(source_path, target_path):
        original_replace(source_path, target_path)
        os._exit(77)

    cloud_files.os.replace = replace_then_exit
    CloudFileLibrary(root).ingest_file(
        "account-a",
        source,
        account_generation="generation-a",
        source="user_upload",
        origin_key="account-upload:crash-upload-id",
    )


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


def test_ingest_supports_a_long_final_object_path(tmp_path):
    source = _write(tmp_path / "source.bin", b"long-path-content")
    library = CloudFileLibrary(tmp_path / "cloud")
    display_name = f"{'report-' * 30}final.txt"

    record = library.ingest_file(
        "account-long-path",
        source,
        name=display_name,
        source="user_upload",
    )

    assert record is not None
    persisted, path = library.resolve_download("account-long-path", record["id"])
    assert persisted["name"] == display_name
    assert path.read_bytes() == b"long-path-content"


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


def test_generation_scope_fences_reused_owner_and_late_writers(tmp_path):
    library = CloudFileLibrary(tmp_path / "cloud")
    old = library.ingest_file(
        "account-a",
        _write(tmp_path / "old.txt", b"old-generation"),
        account_generation="generation-old",
        source="user_upload",
        origin_key="upload-1",
    )
    new = library.ingest_file(
        "account-a",
        _write(tmp_path / "new.txt", b"new-generation"),
        account_generation="generation-new",
        source="user_upload",
        origin_key="upload-1",
    )

    assert old["id"] != new["id"]
    assert old["stored_relpath"].split("/")[1] != new["stored_relpath"].split("/")[1]
    assert library.get_file(
        "account-a", old["id"], account_generation="generation-new"
    ) is None
    assert library.list_files(
        "account-a", account_generation="generation-new"
    )[0] == [new]

    assert library.delete_owner(
        "account-a", account_generation="generation-old"
    )["files"] == 1
    assert library.resolve_download(
        "account-a", new["id"], account_generation="generation-new"
    )[1].read_bytes() == b"new-generation"
    with pytest.raises(PermissionError, match="deleted"):
        library.ingest_file(
            "account-a",
            _write(tmp_path / "late.txt", b"late-old-writer"),
            account_generation="generation-old",
            source="model_output",
        )


def test_v3_schema_migration_fences_existing_rows_into_legacy_generation(tmp_path):
    root = tmp_path / "cloud"
    root.mkdir()
    with sqlite3.connect(root / "library.sqlite3") as conn:
        conn.executescript(
            """
            CREATE TABLE account_files (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                name TEXT NOT NULL,
                stored_relpath TEXT NOT NULL DEFAULT '',
                sha256 TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                extension TEXT NOT NULL DEFAULT '',
                file_type TEXT NOT NULL DEFAULT 'other',
                size INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                conversation_id TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL DEFAULT '',
                turn_id TEXT NOT NULL DEFAULT '',
                profile TEXT NOT NULL DEFAULT '',
                origin_key TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                available_at INTEGER
            );
            CREATE UNIQUE INDEX idx_account_files_owner_origin
                ON account_files(owner_id, origin_key) WHERE origin_key <> '';
            CREATE TABLE deleted_file_origins (
                owner_id TEXT NOT NULL,
                origin_key TEXT NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '',
                deleted_at INTEGER NOT NULL,
                PRIMARY KEY(owner_id, origin_key)
            );
            CREATE TABLE deleted_file_owners (
                owner_id TEXT PRIMARY KEY,
                deleted_at INTEGER NOT NULL
            );
            CREATE TABLE file_install_intents (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                file_id TEXT NOT NULL,
                target_relpath TEXT NOT NULL,
                expected_sha256 TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            INSERT INTO account_files (
                id, owner_id, name, source, status, origin_key,
                created_at, updated_at, available_at
            ) VALUES (
                'legacy-file', 'recycled-owner', 'old.txt', 'user_upload',
                'available', 'upload-1', 1, 1, 1
            );
            PRAGMA user_version=3;
            """
        )

    library = CloudFileLibrary(root)
    legacy_files, legacy_total = library.list_files("recycled-owner")

    assert legacy_total == 1
    assert legacy_files[0]["account_generation"] == "legacy"
    assert library.list_files(
        "recycled-owner", account_generation="generation-new"
    ) == ([], 0)
    with sqlite3.connect(root / "library.sqlite3") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5


def test_same_origin_same_bytes_is_strictly_idempotent_across_filename_changes(tmp_path):
    library = CloudFileLibrary(tmp_path / "cloud")
    old_source = _write(tmp_path / "old.bin", b"stable-artifact")
    original = library.ingest_file(
        "account-a",
        old_source,
        name="old.bin",
        source="model_output",
        conversation_id="conversation-old",
        message_id="message-old",
        turn_id="turn-old",
        profile="worker-old",
        origin_key="remote:run:path:sha",
    )
    original_path = library.resolve_download("account-a", original["id"])[1]

    replay = library.ingest_file(
        "account-a",
        _write(tmp_path / "new.bin", b"stable-artifact"),
        name="new.bin",
        source="model_output",
        conversation_id="conversation-new",
        message_id="message-new",
        turn_id="turn-new",
        profile="worker-new",
        origin_key="remote:run:path:sha",
        make_available=False,
    )

    assert replay == original
    assert library.get_file("account-a", original["id"]) == original
    persisted, persisted_path = library.resolve_download("account-a", original["id"])
    assert persisted == original
    assert persisted_path == original_path
    assert persisted_path.read_bytes() == b"stable-artifact"
    assert not list(library.root.rglob("new.bin"))


@pytest.mark.parametrize(
    ("second_bytes", "expected_statuses"),
    [
        (b"same bytes", ["ok", "ok"]),
        (b"different bytes", ["conflict", "ok"]),
    ],
)
def test_upload_reservation_is_idempotent_across_processes(
    tmp_path,
    second_bytes,
    expected_statuses,
):
    ctx = multiprocessing.get_context("spawn")
    root = tmp_path / "cloud"
    first = _write(tmp_path / "first.bin", b"same bytes")
    second = _write(tmp_path / "second.bin", second_bytes)
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    workers = [
        ctx.Process(
            target=_multiprocess_ingest_worker,
            args=(root, source, barrier, results),
        )
        for source in (first, second)
    ]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    outcomes = [results.get(timeout=5) for _ in workers]
    assert sorted(item[0] for item in outcomes) == expected_statuses
    assert not [item for item in outcomes if item[0] == "error"]
    successful_ids = {item[1] for item in outcomes if item[0] == "ok"}
    assert len(successful_ids) == 1
    files, total = CloudFileLibrary(root).list_files(
        "account-a",
        account_generation="generation-a",
    )
    assert total == 1
    assert files[0]["id"] in successful_ids


def test_expired_crash_reservation_reclaims_without_stale_intent_deleting_publish(
    tmp_path,
):
    ctx = multiprocessing.get_context("spawn")
    root = tmp_path / "cloud"
    source = _write(tmp_path / "crash.bin", b"crash-safe bytes")
    worker = ctx.Process(target=_crash_after_replace_worker, args=(root, source))
    worker.start()
    worker.join(timeout=30)
    assert worker.exitcode == 77

    with sqlite3.connect(root / "library.sqlite3") as conn:
        old_token = conn.execute(
            "SELECT reservation_token FROM file_upload_reservations"
        ).fetchone()[0]
        conn.execute("UPDATE file_upload_reservations SET lease_expires_at=0")

    recovered = CloudFileLibrary(root).ingest_file(
        "account-a",
        source,
        account_generation="generation-a",
        source="user_upload",
        origin_key="account-upload:crash-upload-id",
    )
    downloaded = CloudFileLibrary(root).resolve_download(
        "account-a",
        recovered["id"],
        account_generation="generation-a",
    )[1]
    assert downloaded.read_bytes() == b"crash-safe bytes"

    with sqlite3.connect(root / "library.sqlite3") as conn:
        current_token = conn.execute(
            "SELECT reservation_token FROM file_upload_reservations"
        ).fetchone()[0]
        assert current_token != old_token
        conn.execute("UPDATE file_install_intents SET created_at=0")

    reopened = CloudFileLibrary(root, clock_ms=lambda: 10**12)
    record, path = reopened.resolve_download(
        "account-a",
        recovered["id"],
        account_generation="generation-a",
    )
    assert record["sha256"] == hashlib.sha256(b"crash-safe bytes").hexdigest()
    assert path.read_bytes() == b"crash-safe bytes"
    with sqlite3.connect(root / "library.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM file_install_intents").fetchone()[0] == 0
        assert conn.execute("SELECT state FROM file_upload_reservations").fetchone()[0] == "completed"
    assert not list(root.rglob(".upload-*"))


def test_delete_and_upload_interleave_without_orphan_or_cross_generation_file(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "cloud"
    source = _write(tmp_path / "interleave.bin", b"interleave")
    deleting = CloudFileLibrary(root)
    original = deleting.ingest_file(
        "account-a",
        source,
        account_generation="generation-a",
        source="user_upload",
        origin_key="account-upload:delete-race-id",
    )
    removal_started = threading.Event()
    allow_removal = threading.Event()
    original_remove = deleting._remove_object_path

    def paused_remove(relative):
        removal_started.set()
        assert allow_removal.wait(timeout=10)
        original_remove(relative)

    monkeypatch.setattr(deleting, "_remove_object_path", paused_remove)
    delete_result = []
    upload_result = []
    delete_thread = threading.Thread(
        target=lambda: delete_result.append(
            deleting.delete_file(
                "account-a",
                original["id"],
                account_generation="generation-a",
            )
        )
    )
    upload_thread = threading.Thread(
        target=lambda: upload_result.append(
            CloudFileLibrary(root).ingest_file(
                "account-a",
                source,
                account_generation="generation-a",
                source="user_upload",
                origin_key="account-upload:delete-race-id",
            )
        )
    )

    delete_thread.start()
    assert removal_started.wait(timeout=10)
    upload_thread.start()
    assert upload_thread.is_alive()
    allow_removal.set()
    delete_thread.join(timeout=10)
    upload_thread.join(timeout=10)

    assert delete_result == [True]
    assert len(upload_result) == 1
    assert upload_result[0]["id"] != original["id"]
    files, total = CloudFileLibrary(root).list_files(
        "account-a",
        account_generation="generation-a",
    )
    assert total == 1
    assert files[0]["id"] == upload_result[0]["id"]
    assert not list(root.rglob(f"*/{original['id']}/*"))


def test_delete_owner_removes_index_tombstones_and_objects_without_touching_peers(tmp_path):
    library = CloudFileLibrary(tmp_path / "cloud")
    owner_a = library.ingest_file(
        "account-a",
        _write(tmp_path / "a.txt", b"owner-a"),
        source="user_upload",
        origin_key="account-a:upload",
    )
    owner_b = library.ingest_file(
        "account-b",
        _write(tmp_path / "b.txt", b"owner-b"),
        source="user_upload",
        origin_key="account-b:upload",
    )
    assert library.delete_file("account-a", owner_a["id"]) is True
    library.ingest_file(
        "account-a",
        _write(tmp_path / "a-new.txt", b"owner-a-new"),
        source="model_output",
    )

    deleted = library.delete_owner("account-a")

    assert deleted == {"files": 1, "deleted_origins": 1, "object_buckets": 1}
    assert library.list_files("account-a")[0] == []
    assert library.get_file("account-b", owner_b["id"]) is not None
    assert library.delete_owner("account-a") == {
        "files": 0,
        "deleted_origins": 0,
        "object_buckets": 0,
    }


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

    contract_kwargs = {"account_files_contract": True}
    assert library.list_files("account", keyword="design-chat", **contract_kwargs)[0] == []
    assert [item["id"] for item in library.list_files(
        "account", keyword="document", **contract_kwargs
    )[0]] == [first["id"]]
    assert library.list_files("account", file_type="pdf", **contract_kwargs)[0] == []
    assert [item["id"] for item in library.list_files(
        "account", file_type="document", **contract_kwargs
    )[0]] == [first["id"]]

    with library.connection() as conn:
        conn.execute(
            "UPDATE account_files SET created_at=? WHERE id IN (?,?)",
            (int(now[0]), first["id"], second["id"]),
        )
    tied = library.list_files("account", account_files_contract=True)[0]
    assert [item["id"] for item in tied] == sorted([first["id"], second["id"]])


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
    for value in ("nan", "inf", "-inf"):
        with pytest.raises(ValueError, match="finite"):
            parse_date_filter(value)
