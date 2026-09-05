from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "public" / "profile-runtime-io.py"
COMMANDS = {
    "backup-file",
    "snapshot-sqlite",
    "snapshot-tree",
    "restore-file",
    "restore-sqlite",
    "restore-tree",
    "copy-profile",
    "ensure-dir",
    "ensure-owned-dir",
    "copy-if-absent",
    "remove-tree",
    "prepare-sqlite",
    "publish-file",
    "publish-stdin",
}


def _run(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *(str(value) for value in arguments)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _run_with_stdin(
    content: bytes,
    *arguments: str | Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *(str(value) for value in arguments)],
        input=content,
        capture_output=True,
        text=False,
        timeout=30,
        check=False,
    )


def _succeeds(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    result = _run(*arguments)
    assert result.returncode == 0, result.stderr
    return result


def _fails(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    result = _run(*arguments)
    assert result.returncode != 0, result.stdout
    assert "profile-runtime-io:" in result.stderr
    return result


def _create_database(path: Path, values: tuple[str, ...] = ("one",)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    try:
        database.execute("PRAGMA user_version=17")
        database.execute("PRAGMA application_id=1212501075")
        database.execute("CREATE TABLE messages(value TEXT NOT NULL)")
        database.executemany(
            "INSERT INTO messages(value) VALUES (?)",
            ((value,) for value in values),
        )
        database.commit()
    finally:
        database.close()


def _database_values(path: Path) -> list[str]:
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as database:
        return [row[0] for row in database.execute("SELECT value FROM messages")]


def _make_symlink(target: str | Path, link: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symbolic links are unavailable to this test user: {error}")


def _permission_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def test_cli_is_isolated_stdlib_only_and_has_no_chown() -> None:
    result = _succeeds("--help")
    for command in COMMANDS:
        assert command in result.stdout

    source = SCRIPT.read_text(encoding="utf-8")
    assert "os.chown" not in source
    assert "shutil.chown" not in source
    assert "subprocess" not in source


def test_backup_and_restore_file_support_missing_marker(tmp_path: Path) -> None:
    source = tmp_path / "live" / "settings.json"
    source.parent.mkdir()
    source.write_bytes(b'{"enabled":true}\n')
    source.chmod(0o640)
    snapshot = tmp_path / "snapshots" / "settings.json"

    _succeeds("backup-file", source, snapshot)
    assert snapshot.read_bytes() == source.read_bytes()
    assert not snapshot.with_name(f"{snapshot.name}.missing").exists()
    if os.name == "posix":
        assert _permission_bits(snapshot) == 0o640

    restored = tmp_path / "restored" / "settings.json"
    _succeeds("restore-file", snapshot, restored, "0600")
    assert restored.read_bytes() == source.read_bytes()
    if os.name == "posix":
        assert _permission_bits(restored) == 0o600

    source.unlink()
    _succeeds("backup-file", source, snapshot, "0600")
    missing = snapshot.with_name(f"{snapshot.name}.missing")
    assert not snapshot.exists()
    assert missing.is_file()
    assert missing.read_bytes() == b""

    restored.write_bytes(b"stale")
    _succeeds("restore-file", snapshot, restored, "0600")
    assert not restored.exists()
    assert not list(snapshot.parent.glob(f".{snapshot.name}.new-*"))


def test_backup_rejects_source_destination_and_marker_symlinks(
    tmp_path: Path,
) -> None:
    real_source = tmp_path / "real-source"
    real_source.write_text("source", encoding="utf-8")
    linked_source = tmp_path / "linked-source"
    _make_symlink(real_source.name, linked_source)
    destination = tmp_path / "destination"

    _fails("backup-file", linked_source, destination)
    assert not destination.exists()

    referent = tmp_path / "referent"
    referent.write_text("keep", encoding="utf-8")
    _make_symlink(referent.name, destination)
    _fails("backup-file", real_source, destination)
    assert referent.read_text(encoding="utf-8") == "keep"

    destination.unlink()
    missing_marker = destination.with_name(f"{destination.name}.missing")
    _make_symlink(referent.name, missing_marker)
    absent = tmp_path / "absent"
    _fails("backup-file", absent, destination)
    assert referent.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "posix", reason="FIFOs are POSIX-only")
def test_file_commands_reject_special_files(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    destination = tmp_path / "destination"
    _fails("backup-file", fifo, destination)

    source = tmp_path / "source"
    source.write_text("payload", encoding="utf-8")
    os.mkfifo(destination)
    _fails("publish-file", source, destination, "0600")


def test_publish_file_atomically_replaces_regular_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    payload = os.urandom(2 * 1024 * 1024 + 37)
    source.write_bytes(payload)
    destination = tmp_path / "published.bin"
    destination.write_bytes(b"old")

    _succeeds("publish-file", source, destination, "0640")
    assert destination.read_bytes() == payload
    if os.name == "posix":
        assert _permission_bits(destination) == 0o640
    assert not list(tmp_path.glob(f".{destination.name}.new-*"))

    before = hashlib.sha256(destination.read_bytes()).hexdigest()
    _fails("publish-file", destination, destination, "0600")
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == before


def test_publish_stdin_is_binary_atomic_and_clears_missing_marker(tmp_path: Path) -> None:
    destination = tmp_path / "published.bin"
    destination.write_bytes(b"old")
    destination.with_name(f"{destination.name}.missing").write_bytes(b"")
    payload = b"\x00\xffline\r\n\x80"
    result = _run_with_stdin(payload, "publish-stdin", destination, "0600")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert destination.read_bytes() == payload
    assert not destination.with_name(f"{destination.name}.missing").exists()
    assert not list(tmp_path.glob(f".{destination.name}.new-*"))


def test_publish_stdin_rejects_final_symlink_and_oversized_input(tmp_path: Path) -> None:
    referent = tmp_path / "referent"
    referent.write_bytes(b"keep")
    destination = tmp_path / "published"
    _make_symlink(referent.name, destination)
    result = _run_with_stdin(b"replacement", "publish-stdin", destination, "0600")
    assert result.returncode != 0
    assert referent.read_bytes() == b"keep"

    destination.unlink()
    oversized = b"x" * (4 * 1024 * 1024 * 16)
    result = _run_with_stdin(oversized, "publish-stdin", destination, "0600")
    assert result.returncode != 0
    assert b"maximum size" in result.stderr
    assert not destination.exists()


def test_paths_must_be_absolute_and_lexically_normalized(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("payload", encoding="utf-8")
    destination = tmp_path / "destination"

    relative = _fails("publish-file", "source", destination, "0600")
    assert "absolute path" in relative.stderr

    unnormalized = str(tmp_path / "child" / ".." / "source")
    result = _fails("publish-file", unnormalized, destination, "0600")
    assert "lexically normalized" in result.stderr


def test_sqlite_wal_snapshot_and_metadata_restore(tmp_path: Path) -> None:
    source = tmp_path / "live" / "history.db"
    source.parent.mkdir()
    database = sqlite3.connect(source)
    try:
        journal_mode = database.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            pytest.skip("SQLite WAL is unavailable")
        database.execute("PRAGMA wal_autocheckpoint=0")
        database.execute("PRAGMA user_version=29")
        database.execute("PRAGMA application_id=1212501075")
        database.execute("CREATE TABLE messages(value TEXT NOT NULL)")
        database.execute("INSERT INTO messages(value) VALUES ('base')")
        database.commit()
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        database.execute("INSERT INTO messages(value) VALUES ('wal-only')")
        database.commit()
        assert source.with_name(f"{source.name}-wal").exists()

        snapshot = tmp_path / "snapshot" / "history.db"
        _succeeds("snapshot-sqlite", source, snapshot)
    finally:
        database.close()

    assert _database_values(snapshot) == ["base", "wal-only"]
    metadata_path = snapshot.with_name(f"{snapshot.name}.metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert set(metadata) == {
        "schema",
        "source",
        "user_version",
        "application_id",
        "integrity_check",
        "schema_sha256",
        "snapshot_sha256",
    }
    assert metadata["schema"] == "hermes.sqlite-snapshot.v1"
    assert metadata["source"] == str(source)
    assert metadata["user_version"] == 29
    assert metadata["integrity_check"] == "ok"
    assert metadata["snapshot_sha256"] == hashlib.sha256(
        snapshot.read_bytes()
    ).hexdigest()

    destination = tmp_path / "restore" / "history.db"
    _create_database(destination, ("stale",))
    for suffix in ("-wal", "-shm", "-journal"):
        destination.with_name(f"{destination.name}{suffix}").write_bytes(b"stale")
    _succeeds("restore-sqlite", snapshot, destination)
    for suffix in ("-wal", "-shm", "-journal"):
        assert not destination.with_name(f"{destination.name}{suffix}").exists()
    assert _database_values(destination) == ["base", "wal-only"]
    if os.name == "posix":
        assert _permission_bits(destination) == 0o600


def test_sqlite_restore_validates_metadata_before_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _create_database(source)
    snapshot = tmp_path / "snapshot.db"
    _succeeds("snapshot-sqlite", source, snapshot)
    metadata_path = snapshot.with_name(f"{snapshot.name}.metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["snapshot_sha256"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    destination = tmp_path / "destination.db"
    destination.write_bytes(b"do-not-mutate")
    result = _fails("restore-sqlite", snapshot, destination)
    assert "does not match metadata" in result.stderr
    assert destination.read_bytes() == b"do-not-mutate"


def test_missing_sqlite_snapshot_removes_database_and_sidecars(tmp_path: Path) -> None:
    absent = tmp_path / "absent.db"
    snapshot = tmp_path / "snapshot" / "absent.db"
    _succeeds("snapshot-sqlite", absent, snapshot)
    assert snapshot.with_name(f"{snapshot.name}.missing").is_file()
    assert not snapshot.with_name(f"{snapshot.name}.metadata.json").exists()

    destination = tmp_path / "restore" / "absent.db"
    destination.parent.mkdir()
    for suffix in ("", "-wal", "-shm", "-journal"):
        destination.with_name(f"{destination.name}{suffix}").write_bytes(b"stale")
    _succeeds("restore-sqlite", snapshot, destination)
    for suffix in ("", "-wal", "-shm", "-journal"):
        assert not destination.with_name(f"{destination.name}{suffix}").exists()


def test_snapshot_and_restore_tree_v1_manifest_without_owner_ids(
    tmp_path: Path,
) -> None:
    source = tmp_path / "profile"
    first = source / "state.db"
    second = source / "nested" / "cache.sqlite3"
    _create_database(first, ("first",))
    _create_database(second, ("second", "third"))
    (source / "nested" / "ignored.txt").write_text("ignore", encoding="utf-8")
    first.chmod(0o666)
    second.chmod(0o640)

    snapshot = tmp_path / "tree-snapshot"
    _succeeds("snapshot-tree", source, snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "hermes.sqlite-tree-snapshot.v1"
    assert manifest["root"] == str(source)
    assert manifest["database_count"] == 2
    assert [record["relative_path"] for record in manifest["databases"]] == [
        "nested/cache.sqlite3",
        "state.db",
    ]
    for record in manifest["databases"]:
        assert set(record) == {
            "relative_path",
            "snapshot_path",
            "snapshot_sha256",
            "user_version",
            "application_id",
            "integrity_check",
            "schema_sha256",
            "mode",
        }
        assert "uid" not in record
        assert "gid" not in record

    destination = tmp_path / "restored-profile"
    existing = destination / "state.db"
    extra = destination / "obsolete.sqlite"
    _create_database(existing, ("stale",))
    _create_database(extra, ("remove",))
    for suffix in ("-wal", "-shm", "-journal"):
        existing.with_name(f"{existing.name}{suffix}").write_bytes(b"stale")

    _succeeds("restore-tree", snapshot, destination)
    assert _database_values(destination / "state.db") == ["first"]
    assert _database_values(destination / "nested" / "cache.sqlite3") == [
        "second",
        "third",
    ]
    assert not extra.exists()
    if os.name == "posix":
        assert _permission_bits(destination / "state.db") <= 0o600
        assert _permission_bits(destination / "nested" / "cache.sqlite3") <= 0o600


def test_snapshot_tree_skips_directory_links_and_rejects_database_links(
    tmp_path: Path,
) -> None:
    source = tmp_path / "profile"
    source.mkdir()
    outside = tmp_path / "outside"
    outside_database = outside / "outside.db"
    _create_database(outside_database)
    _make_symlink(outside, source / "linked-directory", directory=True)

    snapshot = tmp_path / "snapshot"
    _succeeds("snapshot-tree", source, snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["database_count"] == 0

    snapshot_two = tmp_path / "snapshot-two"
    _make_symlink(outside_database, source / "linked.db")
    result = _fails("snapshot-tree", source, snapshot_two)
    assert "unsafe SQLite path" in result.stderr
    assert not snapshot_two.exists()


def test_restore_tree_rejects_manifest_escape_before_mutating_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "profile"
    _create_database(source / "nested" / "state.db", ("snapshot",))
    snapshot = tmp_path / "snapshot"
    _succeeds("snapshot-tree", source, snapshot)
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["databases"][0]["relative_path"] = "../escaped.db"
    manifest["databases"][0]["snapshot_path"] = "databases/../escaped.db"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    destination = tmp_path / "destination"
    sentinel = destination / "keep.db"
    _create_database(sentinel, ("keep",))
    result = _fails("restore-tree", snapshot, destination)
    assert "safe relative path" in result.stderr
    assert _database_values(sentinel) == ["keep"]
    assert not (tmp_path / "escaped.db").exists()


def test_restore_tree_rejects_destination_directory_symlink_before_deletes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "profile"
    _create_database(source / "nested" / "state.db", ("snapshot",))
    snapshot = tmp_path / "snapshot"
    _succeeds("snapshot-tree", source, snapshot)

    destination = tmp_path / "destination"
    destination.mkdir()
    sentinel = destination / "keep.db"
    _create_database(sentinel, ("keep",))
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_symlink(outside, destination / "nested", directory=True)

    _fails("restore-tree", snapshot, destination)
    assert _database_values(sentinel) == ["keep"]
    assert not (outside / "state.db").exists()


def test_copy_profile_preserves_only_internal_relative_symlinks(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-profile"
    data = source / "data"
    data.mkdir(parents=True)
    (source / "root.txt").write_text("root", encoding="utf-8")
    (data / "value.txt").write_text("value", encoding="utf-8")
    _make_symlink("data/value.txt", source / "value-link")
    _make_symlink("../root.txt", data / "root-link")
    _make_symlink("data", source / "data-link", directory=True)

    destination = tmp_path / "destination-profile"
    _succeeds("copy-profile", source, destination)
    assert (destination / "root.txt").read_text(encoding="utf-8") == "root"
    assert (destination / "data" / "value.txt").read_text(encoding="utf-8") == "value"
    assert os.readlink(destination / "value-link") == "data/value.txt"
    assert os.readlink(destination / "data" / "root-link") == "../root.txt"
    assert os.readlink(destination / "data-link") == "data"
    assert (destination / "value-link").read_text(encoding="utf-8") == "value"


def test_ensure_dir_and_copy_if_absent_preserve_existing_credentials(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "state" / "shared"
    _succeeds("ensure-dir", shared, "0770")
    assert shared.is_dir()
    if os.name == "posix":
        assert _permission_bits(shared) == 0o770

    source = tmp_path / "legacy" / "auth.json"
    source.parent.mkdir()
    source.write_bytes(b'{"provider":"nous"}\n')
    destination = shared / "nous_auth.json"
    _succeeds("copy-if-absent", source, destination, "0600")
    assert destination.read_bytes() == source.read_bytes()
    if os.name == "posix":
        assert _permission_bits(destination) == 0o600

    source.write_bytes(b'{"provider":"newer"}\n')
    _succeeds("copy-if-absent", source, destination, "0600")
    assert destination.read_bytes() == b'{"provider":"nous"}\n'

    # A prior interrupted absence publication must not survive once a regular
    # credential is present; both states together would make restore/discovery
    # reject the artifact as inconsistent.
    missing_marker = destination.with_name(f"{destination.name}.missing")
    missing_marker.write_bytes(b"")
    _succeeds("copy-if-absent", source, destination, "0600")
    assert not missing_marker.exists()

    missing = tmp_path / "legacy" / "missing.json"
    _succeeds("copy-if-absent", missing, tmp_path / "state" / "missing.json", "0600")
    assert not (tmp_path / "state" / "missing.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX ownership")
def test_ensure_owned_dir_normalizes_a_root_owned_legacy_parent(
    tmp_path: Path,
) -> None:
    if os.geteuid() != 0:
        pytest.skip("ownership normalization requires root in this fixture")
    import pwd

    service = next(account for account in pwd.getpwall() if account.pw_uid > 0 and account.pw_gid > 0)
    nested = tmp_path / "legacy" / "nested"
    nested.mkdir(parents=True)
    result = _run("ensure-owned-dir", nested, str(service.pw_uid), str(service.pw_gid), "0770")
    assert result.returncode == 0, result.stderr
    assert nested.stat().st_uid == service.pw_uid
    assert nested.stat().st_gid == service.pw_gid
    assert nested.parent.stat().st_uid == 0
    assert _permission_bits(nested) == 0o770


def test_copy_if_absent_rejects_destination_symlink_without_overwrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"source")
    referent = tmp_path / "referent"
    referent.write_bytes(b"keep")
    destination = tmp_path / "destination"
    _make_symlink(referent.name, destination)
    _fails("copy-if-absent", source, destination, "0600")
    assert referent.read_bytes() == b"keep"


@pytest.mark.parametrize("target", ["../outside.txt", None])
def test_copy_profile_rejects_escaping_and_absolute_symlinks(
    tmp_path: Path, target: str | None
) -> None:
    source = tmp_path / "source-profile"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link_target = target if target is not None else str(outside)
    _make_symlink(link_target, source / "escape")

    destination = tmp_path / "destination-profile"
    result = _fails("copy-profile", source, destination)
    assert "symlink" in result.stderr
    assert not destination.exists()


def test_remove_tree_recursively_removes_only_a_real_tree(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "state.db").write_bytes(b"state")
    (root / "config.json").write_bytes(b"config")
    _succeeds("remove-tree", root)
    assert not root.exists()
    # Rollback cleanup is intentionally idempotent when the staging tree is gone.
    _succeeds("remove-tree", root)


def test_remove_tree_rejects_links_and_special_entries_before_deleting(
    tmp_path: Path,
) -> None:
    root = tmp_path / "staging"
    root.mkdir()
    sentinel = root / "sentinel"
    sentinel.write_bytes(b"keep")
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_symlink(outside, root / "linked-directory", directory=True)
    _fails("remove-tree", root)
    assert root.exists()
    assert sentinel.read_bytes() == b"keep"

    root2 = tmp_path / "staging-special"
    root2.mkdir()
    sentinel2 = root2 / "sentinel"
    sentinel2.write_bytes(b"keep")
    if os.name == "posix":
        os.mkfifo(root2 / "fifo")
        _fails("remove-tree", root2)
        assert root2.exists()
        assert sentinel2.read_bytes() == b"keep"


def test_remove_tree_rejects_root_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_bytes(b"keep")
    linked = tmp_path / "linked"
    _make_symlink(target, linked, directory=True)
    _fails("remove-tree", linked)
    assert target.exists()
    assert (target / "keep").read_bytes() == b"keep"


def test_prepare_sqlite_creates_parents_and_secures_owned_runtime_files(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "new" / "nested" / "state.db"
    _succeeds("prepare-sqlite", absent)
    assert absent.parent.is_dir()
    assert not absent.exists()

    target = tmp_path / "runtime" / "state.db"
    target.parent.mkdir()
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = target.with_name(f"{target.name}{suffix}")
        path.write_bytes(b"owned")
        path.chmod(0o666)
    _succeeds("prepare-sqlite", target)
    if os.name == "posix":
        for suffix in ("", "-wal", "-shm", "-journal"):
            assert _permission_bits(target.with_name(f"{target.name}{suffix}")) == 0o600


def test_prepare_sqlite_rejects_final_and_parent_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real.db"
    real.write_bytes(b"keep")
    linked = tmp_path / "linked.db"
    _make_symlink(real.name, linked)
    result = _fails("prepare-sqlite", linked)
    assert "symlink" in result.stderr
    assert real.read_bytes() == b"keep"

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    _make_symlink(outside, linked_parent, directory=True)
    _fails("prepare-sqlite", linked_parent / "state.db")
    assert not (outside / "state.db").exists()


@pytest.mark.skipif(
    os.name != "posix" or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="requires a non-root POSIX service user",
)
def test_prepare_sqlite_reports_non_writable_owned_file(tmp_path: Path) -> None:
    target = tmp_path / "state.db"
    target.write_bytes(b"state")
    target.chmod(0o400)
    result = _fails("prepare-sqlite", target)
    assert "not writable by the current service user" in result.stderr


def test_tree_operations_reject_overlapping_roots(tmp_path: Path) -> None:
    source = tmp_path / "profile"
    source.mkdir()
    nested_snapshot = source / "snapshot"
    result = _fails("snapshot-tree", source, nested_snapshot)
    assert "must not overlap" in result.stderr

    destination = tmp_path / "copy"
    destination.mkdir()
    nested_source = destination / "source"
    nested_source.mkdir()
    result = _fails("copy-profile", nested_source, destination)
    assert "must not overlap" in result.stderr
