from __future__ import annotations

import os
import sqlite3
import hashlib
import json
from pathlib import Path
import threading
import time

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hermes_services.tool_output_artifacts import EncryptedToolArtifactStore
from tools.budget_config import BudgetConfig
from tools.tool_result_storage import maybe_persist_tool_result


def test_master_key_partial_staging_write_is_cleaned_and_restart_recovers(
    tmp_path,
    monkeypatch,
):
    store = EncryptedToolArtifactStore(tmp_path)

    def partial_write(path, _candidate):
        path.write_bytes(b"fail")
        raise OSError("forced partial key write")

    monkeypatch.setattr(store, "_write_master_key_candidate", partial_write)
    with pytest.raises(OSError, match="partial key write"):
        store._master_key()

    assert not store.key_path.exists()
    assert not list(store.key_path.parent.glob(f".{store.key_path.name}.*.tmp"))
    restarted = EncryptedToolArtifactStore(tmp_path)
    assert len(restarted._master_key()) == 32


def test_master_key_replace_then_process_exit_is_recoverable(tmp_path, monkeypatch):
    store = EncryptedToolArtifactStore(tmp_path)
    real_replace = os.replace

    def replace_then_exit(source, target):
        real_replace(source, target)
        if Path(target) == store.key_path:
            raise SystemExit("forced exit after key publication")

    monkeypatch.setattr(os, "replace", replace_then_exit)
    with pytest.raises(SystemExit, match="key publication"):
        store._master_key()

    assert store.key_path.stat().st_size == 32
    monkeypatch.setattr(os, "replace", real_replace)
    assert EncryptedToolArtifactStore(tmp_path)._master_key() == store.key_path.read_bytes()


def test_concurrent_master_key_initialization_has_one_stable_winner(tmp_path):
    stores = [EncryptedToolArtifactStore(tmp_path) for _ in range(16)]
    keys: list[bytes] = []
    errors: list[BaseException] = []
    ready = threading.Barrier(len(stores))

    def initialize(store):
        try:
            ready.wait(timeout=5)
            keys.append(store._master_key())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=initialize, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(keys) == len(stores)
    assert set(keys) == {stores[0].key_path.read_bytes()}


def test_concurrent_first_artifact_writes_share_schema_and_key(tmp_path):
    stores = [EncryptedToolArtifactStore(tmp_path) for _ in range(12)]
    ready = threading.Barrier(len(stores))
    results: list[dict] = []
    errors: list[BaseException] = []

    def publish(index, store):
        try:
            ready.wait(timeout=5)
            results.append(
                store.put(
                    owner_id="alice",
                    account_generation="generation-1",
                    conversation_id="conversation-1",
                    turn_id="turn-1",
                    tool_call_id=f"tool-{index}",
                    tool_name="terminal",
                    content=f"output-{index}",
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=publish, args=(index, store))
        for index, store in enumerate(stores)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == len(stores)
    reader = EncryptedToolArtifactStore(tmp_path)
    for record in results:
        index = int(record["tool_call_id"].removeprefix("tool-"))
        assert reader.read(
            "alice",
            record["id"],
            account_generation="generation-1",
        ) == f"output-{index}".encode()


def test_invalid_legacy_master_key_recovers_only_without_artifacts(tmp_path):
    store = EncryptedToolArtifactStore(tmp_path)
    store.key_path.parent.mkdir(parents=True)
    store.key_path.write_bytes(b"fail")

    assert len(store._master_key()) == 32

    with store._connect() as conn:
        conn.execute(
            "INSERT INTO tool_output_artifacts("
            "id,owner_id,account_generation,conversation_id,turn_id,tool_call_id,"
            "tool_name,sha256,size_bytes,stored_relpath,state,created_at,retained_until"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "artifact-1", "alice", "generation-1", "conversation-1", "turn-1",
                "tool-1", "terminal", "0" * 64, 1, "missing", "available", 1, 2,
            ),
        )
        conn.commit()
    store.key_path.write_bytes(b"fail")

    with pytest.raises(RuntimeError, match="master key is invalid"):
        store._master_key()
    assert store.key_path.read_bytes() == b"fail"


def test_encrypted_artifact_round_trip_and_owner_isolation(tmp_path):
    store = EncryptedToolArtifactStore(tmp_path)
    content = "完整输出\n" * 500

    record = store.put(
        owner_id="alice",
        conversation_id="conversation-1",
        turn_id="turn-1",
        tool_call_id="tool-1",
        tool_name="terminal",
        content=content,
    )

    encrypted_files = list((tmp_path / "tool-output-artifacts").rglob("*.aesgcm"))
    assert len(encrypted_files) == 1
    assert content.encode("utf-8") not in encrypted_files[0].read_bytes()
    assert store.read("alice", record["id"]) == content.encode("utf-8")
    with pytest.raises(FileNotFoundError):
        store.read("bob", record["id"])
    assert store.list_owner("alice")[0]["sha256"] == record["sha256"]


def test_owner_listing_filters_metadata_dates_and_filtered_total(tmp_path, monkeypatch):
    store = EncryptedToolArtifactStore(tmp_path)
    now = [1_800_000_000]
    monkeypatch.setattr(
        "hermes_services.tool_output_artifacts.time.time", lambda: now[0]
    )

    records = []
    for index, (created_at, tool_name) in enumerate(
        (
            (1_800_000_000, "DeployX100Y"),
            (1_800_000_060, "Deploy_100%"),
            (1_800_000_120, "Deploy_100%"),
            (1_800_000_180, "Deploy_100%"),
        )
    ):
        now[0] = created_at
        records.append(
            store.put(
                owner_id="alice",
                account_generation="generation-1",
                conversation_id=f"conversation-{index}",
                turn_id=f"turn-{index}",
                tool_call_id=f"tool-{index}",
                tool_name=tool_name,
                content=f"output-{index}",
            )
        )
    now[0] = 1_800_000_200

    filters = {
        "account_generation": "generation-1",
        "q": "deploy_100",
        "date_from": 1_800_000_060_000,
        "date_to": 1_800_000_120_999,
    }
    page = store.list_owner("alice", limit=1, **filters)

    assert [item["id"] for item in page] == [records[2]["id"]]
    assert store.count_owner("alice", **filters) == 2
    assert [
        item["id"] for item in store.list_owner("alice", limit=100, **filters)
    ] == [records[2]["id"], records[1]["id"]]
    assert store.count_owner(
        "alice", account_generation="generation-1", q="tool_output"
    ) == 4
    assert store.count_owner(
        "alice", account_generation="generation-1", q="deploy-100"
    ) == 0


def test_owner_listing_uses_id_ascending_tie_break(tmp_path, monkeypatch):
    store = EncryptedToolArtifactStore(tmp_path)
    monkeypatch.setattr(
        "hermes_services.tool_output_artifacts.time.time", lambda: 1_800_000_000
    )
    records = [
        store.put(
            owner_id="alice",
            account_generation="generation-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            tool_call_id=tool_call_id,
            tool_name="terminal",
            content=tool_call_id,
        )
        for tool_call_id in ("tool-b", "tool-a")
    ]

    listed = store.list_owner("alice", account_generation="generation-1")

    assert [item["id"] for item in listed] == sorted(item["id"] for item in records)


def test_same_tool_call_replaces_one_stable_encrypted_artifact(tmp_path):
    store = EncryptedToolArtifactStore(tmp_path)
    first = store.put(
        owner_id="alice",
        account_generation="generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        tool_call_id="tool-1",
        tool_name="terminal",
        content="first output",
    )
    second = store.put(
        owner_id="alice",
        account_generation="generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        tool_call_id="tool-1",
        tool_name="terminal",
        content="replacement output",
    )

    assert second["id"] == first["id"]
    assert len(list((tmp_path / "tool-output-artifacts").rglob("*.aesgcm"))) == 1
    assert store.read(
        "alice", second["id"], account_generation="generation-1"
    ) == b"replacement output"
    assert [item["id"] for item in store.list_owner(
        "alice", account_generation="generation-1"
    )] == [second["id"]]


def test_concurrent_same_identity_publishes_one_matching_version(tmp_path, monkeypatch):
    first_store = EncryptedToolArtifactStore(tmp_path)
    second_store = EncryptedToolArtifactStore(tmp_path)
    first_installed = threading.Event()
    release_first = threading.Event()
    real_replace = os.replace
    results = {}
    errors = []
    first_store._master_key()

    def interleaved_replace(source, target):
        real_replace(source, target)
        if threading.current_thread().name == "artifact-first":
            first_installed.set()
            assert release_first.wait(5)

    monkeypatch.setattr(os, "replace", interleaved_replace)

    def publish(label, store, content):
        try:
            results[label] = store.put(
                owner_id="alice",
                account_generation="generation-1",
                conversation_id="conversation-1",
                turn_id="turn-1",
                tool_call_id="tool-1",
                tool_name="terminal",
                content=content,
            )
        except BaseException as exc:  # surfaced below with both thread results
            errors.append(exc)

    first = threading.Thread(
        target=publish,
        args=("first", first_store, "first output"),
        name="artifact-first",
    )
    second = threading.Thread(
        target=publish,
        args=("second", second_store, "second output"),
        name="artifact-second",
    )
    first.start()
    assert first_installed.wait(5)
    second.start()
    time.sleep(0.1)
    assert second.is_alive(), "second publisher must wait on the SQLite writer lock"
    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert results["first"]["id"] == results["second"]["id"]
    assert second_store.read(
        "alice",
        results["second"]["id"],
        account_generation="generation-1",
    ) == b"second output"
    encrypted_files = list((tmp_path / "tool-output-artifacts").rglob("*.aesgcm"))
    assert len(encrypted_files) == 1
    with second_store._connect() as conn:
        row = conn.execute(
            "SELECT sha256,stored_relpath,state FROM tool_output_artifacts WHERE id=?",
            (results["second"]["id"],),
        ).fetchone()
    assert row["state"] == "available"
    assert row["sha256"] == hashlib.sha256(b"second output").hexdigest()
    assert (second_store.data_root / row["stored_relpath"]) == encrypted_files[0]


def test_failed_concurrent_publisher_cannot_delete_successful_version(tmp_path, monkeypatch):
    first_store = EncryptedToolArtifactStore(tmp_path)
    second_store = EncryptedToolArtifactStore(tmp_path)
    first_installed = threading.Event()
    release_failure = threading.Event()
    real_replace = os.replace
    results = {}
    errors = []
    first_store._master_key()

    def fail_first_after_install(source, target):
        real_replace(source, target)
        if threading.current_thread().name == "artifact-failing":
            first_installed.set()
            assert release_failure.wait(5)
            raise SystemExit("first writer exits after installing its private version")

    monkeypatch.setattr(os, "replace", fail_first_after_install)

    def publish(label, store, content):
        try:
            results[label] = store.put(
                owner_id="alice",
                account_generation="generation-1",
                conversation_id="conversation-1",
                turn_id="turn-1",
                tool_call_id="tool-1",
                tool_name="terminal",
                content=content,
            )
        except BaseException as exc:
            errors.append(exc)

    failing = threading.Thread(
        target=publish,
        args=("failing", first_store, "discarded output"),
        name="artifact-failing",
    )
    successful = threading.Thread(
        target=publish,
        args=("successful", second_store, "committed output"),
        name="artifact-successful",
    )
    failing.start()
    assert first_installed.wait(5)
    successful.start()
    time.sleep(0.1)
    assert successful.is_alive()
    release_failure.set()
    failing.join(5)
    successful.join(5)

    assert not failing.is_alive() and not successful.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], SystemExit)
    artifact = results["successful"]
    assert second_store.read(
        "alice", artifact["id"], account_generation="generation-1"
    ) == b"committed output"
    assert len(list(second_store.data_root.rglob("*.aesgcm"))) == 1


def test_replace_failure_after_install_is_cleaned_up(tmp_path, monkeypatch):
    store = EncryptedToolArtifactStore(tmp_path)
    store._master_key()
    real_replace = os.replace

    def install_then_fail(source, target):
        real_replace(source, target)
        raise SystemExit("crash after replace")

    monkeypatch.setattr(os, "replace", install_then_fail)
    with pytest.raises(SystemExit):
        store.put(
            owner_id="alice",
            conversation_id="conversation-1",
            turn_id="turn-1",
            tool_call_id="tool-1",
            tool_name="terminal",
            content="secret",
        )

    assert not list((tmp_path / "tool-output-artifacts").rglob("*.aesgcm"))
    assert store.list_owner("alice") == []


def test_owner_cleanup_removes_unindexed_ciphertext_after_hard_exit_window(
    tmp_path, monkeypatch
):
    store = EncryptedToolArtifactStore(tmp_path)
    store._master_key()
    real_replace = os.replace
    real_unlink = Path.unlink

    def install_then_exit(source, target):
        real_replace(source, target)
        raise SystemExit("process exited after ciphertext installation")

    def leave_installed_ciphertext(path, *args, **kwargs):
        if path.suffix == ".aesgcm":
            return None
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", install_then_exit)
    monkeypatch.setattr(Path, "unlink", leave_installed_ciphertext)
    with pytest.raises(SystemExit, match="process exited"):
        store.put(
            owner_id="alice",
            account_generation="generation-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            tool_call_id="tool-1",
            tool_name="terminal",
            content="secret",
        )

    assert list(store.data_root.rglob("*.aesgcm"))
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert store.delete_owner(
        "alice", account_generation="generation-1"
    ) == {"artifacts": 0}
    assert not list(store.data_root.rglob("*.aesgcm"))


def test_deleted_generation_rejects_late_artifact_put(tmp_path):
    store = EncryptedToolArtifactStore(tmp_path)
    assert store.delete_owner(
        "alice", account_generation="generation-1"
    ) == {"artifacts": 0}

    with pytest.raises(RuntimeError, match="generation has been deleted"):
        store.put(
            owner_id="alice",
            account_generation="generation-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            tool_call_id="tool-1",
            tool_name="terminal",
            content="late output",
        )

    assert not list(store.data_root.rglob("*.aesgcm"))


def test_hosted_large_output_returns_artifact_reference_without_plaintext_file(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_TOOL_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_TOOL_ARTIFACT_OWNER", "alice")
    monkeypatch.setenv("HERMES_TOOL_ARTIFACT_CONVERSATION", "conversation-1")
    monkeypatch.setenv("HERMES_TOOL_ARTIFACT_TURN", "turn-1")
    monkeypatch.setenv("HERMES_ACCOUNT_GENERATION", "alice-gen-1")
    content = "结果数据\n" * 1000

    rendered = maybe_persist_tool_result(
        content,
        "terminal",
        "tool-1",
        env=None,
        config=BudgetConfig(default_result_size=10, preview_size=40, turn_budget=100),
    )

    assert "Account artifact: toolout_" in rendered
    assert content not in rendered
    store = EncryptedToolArtifactStore(tmp_path)
    artifact = store.list_owner("alice", account_generation="alice-gen-1")[0]
    assert store.read(
        "alice", artifact["id"], account_generation="alice-gen-1"
    ) == content.encode("utf-8")


def test_hosted_encrypted_artifact_never_duplicates_plaintext_into_sandbox(
    tmp_path,
    monkeypatch,
):
    class Sandbox:
        def __init__(self):
            self.calls = []

        def execute(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return {"returncode": 0}

    monkeypatch.setenv("HERMES_TOOL_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_TOOL_ARTIFACT_OWNER", "alice")
    monkeypatch.setenv("HERMES_ACCOUNT_GENERATION", "generation-1")
    sandbox = Sandbox()

    rendered = maybe_persist_tool_result(
        "private output\n" * 100,
        "terminal",
        "tool-private",
        env=sandbox,
        threshold=10,
    )

    assert "Account artifact: toolout_" in rendered
    assert "Full output saved to:" not in rendered
    assert sandbox.calls == []


def test_hosted_artifact_failure_never_falls_back_to_plaintext_sandbox(
    tmp_path,
    monkeypatch,
):
    class Sandbox:
        def __init__(self):
            self.calls = []

        def get_temp_dir(self):
            return "/tmp"

        def execute(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return {"returncode": 0}

    def fail_put(*_args, **_kwargs):
        raise OSError("simulated encrypted-store outage")

    monkeypatch.setenv("HERMES_TOOL_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_TOOL_ARTIFACT_OWNER", "alice")
    monkeypatch.setenv("HERMES_ACCOUNT_GENERATION", "generation-1")
    monkeypatch.setattr(EncryptedToolArtifactStore, "put", fail_put)
    sandbox = Sandbox()
    content = "private output\n" * 100

    rendered = maybe_persist_tool_result(
        content,
        "terminal",
        "tool-failure",
        env=sandbox,
        config=BudgetConfig(default_result_size=10, preview_size=40, turn_budget=100),
    )

    assert "full output was not retained" in rendered
    assert "Full output saved to:" not in rendered
    assert content not in rendered
    assert sandbox.calls == []


def test_owner_cleanup_removes_metadata_and_ciphertext(tmp_path):
    store = EncryptedToolArtifactStore(tmp_path)
    store.put(
        owner_id="alice",
        conversation_id="conversation-1",
        turn_id="turn-1",
        tool_call_id="tool-1",
        tool_name="terminal",
        content="secret",
    )

    result = store.delete_owner("alice", account_generation="legacy")

    assert result == {"artifacts": 1}
    assert store.list_owner("alice") == []
    assert not list((tmp_path / "tool-output-artifacts").rglob("*.aesgcm"))


def test_owner_cleanup_resumes_after_file_delete_process_exit(tmp_path, monkeypatch):
    store = EncryptedToolArtifactStore(tmp_path)
    artifact = store.put(
        owner_id="alice",
        account_generation="generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        tool_call_id="tool-1",
        tool_name="terminal",
        content="secret",
    )
    target = next((tmp_path / "tool-output-artifacts").rglob("*.aesgcm"))
    real_unlink = Path.unlink
    interrupted = {"raised": False}

    def unlink_then_exit(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if path == target and not interrupted["raised"]:
            interrupted["raised"] = True
            raise SystemExit("exit after ciphertext removal")
        return result

    monkeypatch.setattr(Path, "unlink", unlink_then_exit)
    with pytest.raises(SystemExit, match="exit after ciphertext removal"):
        store.delete_owner("alice", account_generation="generation-1")

    assert store.metadata(
        "alice", artifact["id"], account_generation="generation-1"
    ) is None
    with store._connect() as conn:
        row = conn.execute(
            "SELECT state FROM tool_output_artifacts WHERE id=?",
            (artifact["id"],),
        ).fetchone()
    assert row["state"] == "deleting"

    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert store.delete_owner(
        "alice", account_generation="generation-1"
    ) == {"artifacts": 1}
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tool_output_artifacts WHERE id=?",
            (artifact["id"],),
        ).fetchone()[0] == 0


def test_single_artifact_delete_is_generation_scoped(tmp_path):
    store = EncryptedToolArtifactStore(tmp_path)
    old = store.put(
        owner_id="alice",
        account_generation="generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        tool_call_id="tool-old",
        tool_name="terminal",
        content="old secret",
    )
    current = store.put(
        owner_id="alice",
        account_generation="generation-2",
        conversation_id="conversation-2",
        turn_id="turn-2",
        tool_call_id="tool-current",
        tool_name="terminal",
        content="current secret",
    )

    assert not store.delete(
        "alice", old["id"], account_generation="generation-2"
    )
    assert store.delete(
        "alice", current["id"], account_generation="generation-2"
    )
    assert store.list_owner("alice", account_generation="generation-2") == []
    assert store.read(
        "alice", old["id"], account_generation="generation-1"
    ) == b"old secret"


def test_same_username_new_generation_cannot_read_or_list_old_artifact(tmp_path):
    store = EncryptedToolArtifactStore(tmp_path)
    old = store.put(
        owner_id="alice",
        account_generation="generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        tool_call_id="tool-1",
        tool_name="terminal",
        content="old account secret",
    )
    new = store.put(
        owner_id="alice",
        account_generation="generation-2",
        conversation_id="conversation-1",
        turn_id="turn-1",
        tool_call_id="tool-1",
        tool_name="terminal",
        content="new account secret",
    )

    assert old["id"] != new["id"]
    with pytest.raises(FileNotFoundError):
        store.read("alice", old["id"], account_generation="generation-2")
    assert [item["id"] for item in store.list_owner(
        "alice", account_generation="generation-1"
    )] == [old["id"]]
    assert [item["id"] for item in store.list_owner(
        "alice", account_generation="generation-2"
    )] == [new["id"]]


def test_owner_cleanup_removes_all_generations(tmp_path):
    store = EncryptedToolArtifactStore(tmp_path)
    for generation in ("generation-1", "generation-2"):
        store.put(
            owner_id="alice",
            account_generation=generation,
            conversation_id="conversation-1",
            turn_id="turn-1",
            tool_call_id=f"tool-{generation}",
            tool_name="terminal",
            content=generation,
        )

    assert store.delete_owner(
        "alice",
        account_generation="generation-2",
        include_known_generations=True,
    ) == {"artifacts": 2}
    assert store.list_owner("alice", account_generation="generation-1") == []
    assert store.list_owner("alice", account_generation="generation-2") == []
    assert not list((tmp_path / "tool-output-artifacts").rglob("*.aesgcm"))


def test_empty_owner_cleanup_fences_paused_old_generation_and_allows_new_generation(
    tmp_path,
    monkeypatch,
):
    store = EncryptedToolArtifactStore(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    real_register = store._register_owner_directory
    outcome = {}

    def paused_register(owner_id, account_generation, directory_name):
        entered.set()
        assert release.wait(timeout=5)
        return real_register(owner_id, account_generation, directory_name)

    def late_writer():
        try:
            store.put(
                owner_id="alice",
                account_generation="generation-1",
                conversation_id="conversation-old",
                turn_id="turn-old",
                tool_call_id="tool-old",
                tool_name="terminal",
                content="old secret",
            )
        except BaseException as exc:
            outcome["error"] = exc

    monkeypatch.setattr(store, "_register_owner_directory", paused_register)
    writer = threading.Thread(target=late_writer)
    writer.start()
    assert entered.wait(timeout=5)

    assert store.delete_owner(
        "alice",
        account_generation="generation-1",
        include_known_generations=True,
    ) == {"artifacts": 0}
    release.set()
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert isinstance(outcome.get("error"), RuntimeError)
    assert "generation has been deleted" in str(outcome["error"])
    assert store.count_owner("alice", account_generation="generation-1") == 0

    monkeypatch.setattr(store, "_register_owner_directory", real_register)
    current = store.put(
        owner_id="alice",
        account_generation="generation-2",
        conversation_id="conversation-new",
        turn_id="turn-new",
        tool_call_id="tool-new",
        tool_name="terminal",
        content="new secret",
    )
    assert store.read(
        "alice", current["id"], account_generation="generation-2"
    ) == b"new secret"


def test_expired_artifact_is_hidden_and_physically_purged(tmp_path, monkeypatch):
    clock = {"now": 1_000}
    monkeypatch.setattr(
        "hermes_services.tool_output_artifacts.time.time",
        lambda: clock["now"],
    )
    store = EncryptedToolArtifactStore(tmp_path)
    artifact = store.put(
        owner_id="owner",
        account_generation="generation",
        conversation_id="conversation",
        turn_id="turn",
        tool_call_id="call",
        tool_name="terminal",
        content="complete output",
        retention_seconds=3_600,
    )
    assert list(store.data_root.rglob("*.aesgcm"))

    clock["now"] = 4_601

    with pytest.raises(FileNotFoundError):
        store.read("owner", artifact["id"], account_generation="generation")
    assert store.count_owner("owner", account_generation="generation") == 0
    assert store.list_owner("owner", account_generation="generation") == []
    assert store.metadata("owner", artifact["id"], account_generation="generation") is None
    assert not list(store.data_root.rglob("*.aesgcm"))
    assert store.purge_expired(now=clock["now"])["artifacts"] == 0


def test_schema_v1_rows_migrate_into_legacy_generation(tmp_path):
    db = sqlite3.connect(tmp_path / "tool-output-artifacts.db")
    db.executescript(
        """
        CREATE TABLE tool_output_artifacts (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL DEFAULT '',
            turn_id TEXT NOT NULL DEFAULT '',
            tool_call_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            stored_relpath TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            retained_until INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX idx_tool_output_owner_call
            ON tool_output_artifacts(owner_id, conversation_id, turn_id, tool_call_id);
        INSERT INTO tool_output_artifacts VALUES(
            'legacy-id','alice','conversation-1','turn-1','tool-1','terminal',
            'hash',1,'legacy/path','staging',1,2
        );
        PRAGMA user_version=1;
        """
    )
    db.commit()
    db.close()

    store = EncryptedToolArtifactStore(tmp_path)
    with store._connect() as migrated:
        row = migrated.execute(
            "SELECT account_generation FROM tool_output_artifacts WHERE id='legacy-id'"
        ).fetchone()
        indexes = {
            item[1]
            for item in migrated.execute("PRAGMA index_list(tool_output_artifacts)")
        }

    assert row["account_generation"] == "legacy"
    assert "idx_tool_output_owner_call" not in indexes
    assert "idx_tool_output_owner_generation_call" in indexes


def test_schema_v1_available_ciphertext_remains_readable_after_migration(tmp_path):
    store = EncryptedToolArtifactStore(tmp_path)
    owner = "alice"
    plaintext = b"legacy full output"
    digest = hashlib.sha256(plaintext).hexdigest()
    artifact_id = "legacy-available"
    relpath = "legacy/legacy-available.aesgcm"
    aad_record = {
        "id": artifact_id,
        "owner_id": owner,
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "tool_call_id": "tool-1",
        "tool_name": "terminal",
        "sha256": digest,
    }
    nonce = b"0" * 12
    ciphertext = AESGCM(store._owner_key(owner, "legacy")).encrypt(
        nonce,
        plaintext,
        json.dumps(aad_record, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
    target = tmp_path / "tool-output-artifacts" / relpath
    target.parent.mkdir(parents=True)
    target.write_bytes(nonce + ciphertext)
    with store._connect() as db:
        db.execute(
            "INSERT INTO tool_output_artifacts("
            "id,owner_id,account_generation,conversation_id,turn_id,tool_call_id,tool_name,"
            "sha256,size_bytes,stored_relpath,state,created_at,retained_until"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?, ?, ?)",
            (
                artifact_id, owner, "legacy", "conversation-1", "turn-1", "tool-1",
                "terminal", digest, len(plaintext), relpath, "available", 1, 4_102_444_800,
            ),
        )
        db.commit()

    assert store.read(owner, artifact_id, account_generation="legacy") == plaintext
