from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - Windows has no POSIX identity database
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "deploy" / "public" / "runtime-home-guard.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("runtime_home_guard", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _identity(path: Path) -> str:
    metadata = path.stat(follow_symlinks=False)
    return f"{metadata.st_dev}:{metadata.st_ino}"


def _run(*arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(GUARD), *(str(value) for value in arguments)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _service_identity() -> tuple[int, int]:
    assert pwd is not None and grp is not None
    for account in pwd.getpwall():
        if account.pw_uid <= 0:
            continue
        try:
            grp.getgrgid(account.pw_gid)
            memberships = os.getgrouplist(account.pw_name, account.pw_gid)
        except (KeyError, OSError):
            continue
        if account.pw_gid in memberships:
            return account.pw_uid, account.pw_gid
    current = pwd.getpwuid(os.getuid())
    if current.pw_uid == 0 or current.pw_gid == 0:
        pytest.skip("no non-root service identity is available")
    return current.pw_uid, current.pw_gid


@pytest.fixture
def managed_paths(tmp_path: Path):
    if os.name != "posix" or os.geteuid() != 0:
        pytest.skip("root-owned runtime-home topology requires POSIX root")
    uid, gid = _service_identity()
    state_root = tmp_path / "state"
    profiles_parent = state_root / "profiles"
    leaf = profiles_parent / "dispatcher"
    journal_parent = tmp_path / "release-state"
    journal_parent.mkdir(mode=0o700)
    os.chown(journal_parent, 0, 0)
    os.chmod(journal_parent, 0o700)
    result = _run(
        "ensure-managed",
        state_root,
        profiles_parent,
        leaf,
        uid,
        gid,
    )
    assert result.returncode == 0, result.stderr
    leaf_identity = result.stdout.strip()
    return {
        "uid": uid,
        "gid": gid,
        "state_root": state_root,
        "profiles_parent": profiles_parent,
        "leaf": leaf,
        "leaf_identity": leaf_identity,
        "journal_parent": journal_parent,
        "journal": journal_parent / "runtime-home-seal.json",
        "journal_parent_identity": _identity(journal_parent),
    }


def test_cli_exposes_the_required_actions() -> None:
    result = _run("--help")
    assert result.returncode == 0
    for action in (
        "ensure-managed",
        "ensure-leaf",
        "adopt-staging",
        "replace-empty-leaf",
        "normalize-files",
        "seal",
        "unseal",
        "remove-empty",
        "journal-inspect",
        "journal-field",
        "journal-write",
        "journal-advance",
        "journal-remove",
    ):
        assert action in result.stdout


@pytest.mark.parametrize(
    "raw",
    ["relative/path", "//double/root", "/tmp/../tmp/value", "/tmp/value/", "/"],
)
def test_strict_path_normalization_rejects_ambiguous_paths(raw: str) -> None:
    guard = _load_guard()
    with pytest.raises(guard.GuardError):
        guard._absolute_path(raw, "test")


def test_ensure_managed_creates_exact_modes_and_is_inode_idempotent(managed_paths) -> None:
    paths = managed_paths
    state_metadata = paths["state_root"].stat()
    parent_metadata = paths["profiles_parent"].stat()
    leaf_metadata = paths["leaf"].stat()
    assert (state_metadata.st_uid, state_metadata.st_gid) == (0, paths["gid"])
    assert stat.S_IMODE(state_metadata.st_mode) == 0o1770
    assert (parent_metadata.st_uid, parent_metadata.st_gid) == (0, paths["gid"])
    assert stat.S_IMODE(parent_metadata.st_mode) == 0o1770
    assert (leaf_metadata.st_uid, leaf_metadata.st_gid) == (0, paths["gid"])
    assert stat.S_IMODE(leaf_metadata.st_mode) == 0o770

    before = paths["leaf_identity"]
    result = _run(
        "ensure-managed",
        paths["state_root"],
        paths["profiles_parent"],
        paths["leaf"],
        paths["uid"],
        paths["gid"],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == before
    assert _identity(paths["leaf"]) == before


def test_ensure_leaf_supports_root_controlled_custom_parent(managed_paths, tmp_path: Path) -> None:
    paths = managed_paths
    custom_parent = tmp_path / "custom-parent"
    custom_parent.mkdir(mode=0o755)
    os.chown(custom_parent, 0, 0)
    os.chmod(custom_parent, 0o755)
    custom_leaf = custom_parent / "dispatcher"

    result = _run(
        "ensure-leaf",
        custom_parent,
        custom_leaf,
        paths["uid"],
        paths["gid"],
        _identity(custom_parent),
    )
    assert result.returncode == 0, result.stderr
    metadata = custom_leaf.stat(follow_symlinks=False)
    assert result.stdout.strip() == _identity(custom_leaf)
    assert (metadata.st_uid, metadata.st_gid) == (0, paths["gid"])
    assert stat.S_IMODE(metadata.st_mode) == 0o770

    os.chmod(custom_leaf, 0o700)
    before = _identity(custom_leaf)
    repeated = _run(
        "ensure-leaf",
        custom_parent,
        custom_leaf,
        paths["uid"],
        paths["gid"],
        _identity(custom_parent),
    )
    assert repeated.returncode == 0, repeated.stderr
    assert repeated.stdout.strip() == before
    assert stat.S_IMODE(custom_leaf.stat().st_mode) == 0o770


def test_ensure_leaf_rejects_parent_identity_and_symlink_leaf(managed_paths, tmp_path: Path) -> None:
    paths = managed_paths
    custom_parent = tmp_path / "custom-parent"
    custom_parent.mkdir(mode=0o755)
    os.chown(custom_parent, 0, 0)
    os.chmod(custom_parent, 0o755)
    custom_leaf = custom_parent / "dispatcher"
    victim = tmp_path / "custom-victim"
    victim.mkdir(mode=0o711)
    os.chown(victim, 0, 0)
    before = victim.stat(follow_symlinks=False)
    custom_leaf.symlink_to(victim, target_is_directory=True)

    wrong_identity = _run(
        "ensure-leaf",
        custom_parent,
        custom_leaf,
        paths["uid"],
        paths["gid"],
        "1:1",
    )
    assert wrong_identity.returncode != 0
    symlink = _run(
        "ensure-leaf",
        custom_parent,
        custom_leaf,
        paths["uid"],
        paths["gid"],
        _identity(custom_parent),
    )
    assert symlink.returncode != 0
    after = victim.stat(follow_symlinks=False)
    assert (after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode)) == (
        before.st_dev,
        before.st_ino,
        stat.S_IMODE(before.st_mode),
    )


def test_adopt_staging_changes_only_expected_service_inode(managed_paths) -> None:
    paths = managed_paths
    staging = paths["profiles_parent"] / ".dispatcher-staging"
    staging.mkdir(mode=0o700)
    os.chown(staging, paths["uid"], paths["gid"])
    os.chmod(staging, 0o770)
    staging_identity = _identity(staging)

    result = _run(
        "adopt-staging",
        paths["profiles_parent"],
        staging,
        paths["uid"],
        paths["gid"],
        _identity(paths["profiles_parent"]),
        staging_identity,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == staging_identity
    metadata = staging.stat(follow_symlinks=False)
    assert (metadata.st_uid, metadata.st_gid) == (0, paths["gid"])
    assert stat.S_IMODE(metadata.st_mode) == 0o770


def test_adopt_staging_rejects_wrong_identity_without_touching_staging(
    managed_paths,
) -> None:
    paths = managed_paths
    staging = paths["profiles_parent"] / ".dispatcher-staging"
    staging.mkdir(mode=0o700)
    os.chown(staging, paths["uid"], paths["gid"])
    os.chmod(staging, 0o770)
    before = staging.stat(follow_symlinks=False)
    result = _run(
        "adopt-staging",
        paths["profiles_parent"],
        staging,
        paths["uid"],
        paths["gid"],
        _identity(paths["profiles_parent"]),
        "1:1",
    )
    assert result.returncode != 0
    after = staging.stat(follow_symlinks=False)
    assert (after.st_dev, after.st_ino, after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
        before.st_dev,
        before.st_ino,
        before.st_uid,
        before.st_gid,
        stat.S_IMODE(before.st_mode),
    )


def test_replace_empty_leaf_atomically_swaps_expected_directories(managed_paths) -> None:
    paths = managed_paths
    staging = paths["profiles_parent"] / ".dispatcher-staging"
    staging.mkdir(mode=0o700)
    os.chown(staging, paths["uid"], paths["gid"])
    os.chmod(staging, 0o770)
    # The staging candidate is adopted before the atomic switch, as the
    # installer does in production.
    adopted = _run(
        "adopt-staging",
        paths["profiles_parent"],
        staging,
        paths["uid"],
        paths["gid"],
        _identity(paths["profiles_parent"]),
        _identity(staging),
    )
    assert adopted.returncode == 0, adopted.stderr
    staging_identity = _identity(staging)
    result = _run(
        "replace-empty-leaf",
        paths["profiles_parent"],
        paths["leaf"],
        paths["leaf_identity"],
        staging,
        staging_identity,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == staging_identity
    assert _identity(paths["leaf"]) == staging_identity
    assert not staging.exists()
    parent_metadata = paths["profiles_parent"].stat(follow_symlinks=False)
    assert (parent_metadata.st_uid, parent_metadata.st_gid) == (0, paths["gid"])
    assert stat.S_IMODE(parent_metadata.st_mode) == 0o1770


def test_replace_empty_leaf_refuses_nonempty_or_wrong_inode_without_deleting(
    managed_paths,
) -> None:
    paths = managed_paths
    staging = paths["profiles_parent"] / ".dispatcher-staging"
    staging.mkdir(mode=0o700)
    os.chown(staging, 0, paths["gid"])
    os.chmod(staging, 0o770)
    (paths["leaf"] / "must-stay").write_text("keep\n", encoding="utf-8")
    result = _run(
        "replace-empty-leaf",
        paths["profiles_parent"],
        paths["leaf"],
        "1:1",
        staging,
        _identity(staging),
    )
    assert result.returncode != 0
    assert _identity(paths["leaf"]) == paths["leaf_identity"]
    assert (paths["leaf"] / "must-stay").read_text(encoding="utf-8") == "keep\n"
    assert _identity(staging) != paths["leaf_identity"]


def test_replace_empty_leaf_restores_modes_when_empty_precondition_fails(
    managed_paths,
) -> None:
    paths = managed_paths
    staging = paths["profiles_parent"] / ".dispatcher-staging"
    staging.mkdir(mode=0o700)
    os.chown(staging, 0, paths["gid"])
    os.chmod(staging, 0o770)
    (paths["leaf"] / "must-stay").write_text("keep\n", encoding="utf-8")
    result = _run(
        "replace-empty-leaf",
        paths["profiles_parent"],
        paths["leaf"],
        paths["leaf_identity"],
        staging,
        _identity(staging),
    )
    assert result.returncode != 0
    assert stat.S_IMODE(paths["leaf"].stat(follow_symlinks=False).st_mode) == 0o770
    assert stat.S_IMODE(
        paths["profiles_parent"].stat(follow_symlinks=False).st_mode
    ) == 0o1770


def test_replace_empty_leaf_rejects_symlink_staging_without_touching_target(
    managed_paths,
    tmp_path: Path,
) -> None:
    paths = managed_paths
    staging = paths["profiles_parent"] / ".dispatcher-staging"
    victim = tmp_path / "staging-victim"
    victim.mkdir(mode=0o711)
    os.chown(victim, 0, 0)
    before = victim.stat(follow_symlinks=False)
    staging.symlink_to(victim, target_is_directory=True)
    result = _run(
        "replace-empty-leaf",
        paths["profiles_parent"],
        paths["leaf"],
        paths["leaf_identity"],
        staging,
        "1:1",
    )
    assert result.returncode != 0
    after = victim.stat(follow_symlinks=False)
    assert (after.st_dev, after.st_ino, after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
        before.st_dev,
        before.st_ino,
        before.st_uid,
        before.st_gid,
        stat.S_IMODE(before.st_mode),
    )
    assert _identity(paths["leaf"]) == paths["leaf_identity"]


def test_ensure_managed_rejects_symlink_leaf_without_touching_target(managed_paths, tmp_path: Path) -> None:
    paths = managed_paths
    paths["leaf"].rmdir()
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o711)
    os.chown(victim, 0, 0)
    before = victim.stat(follow_symlinks=False)
    paths["leaf"].symlink_to(victim, target_is_directory=True)

    result = _run(
        "ensure-managed",
        paths["state_root"],
        paths["profiles_parent"],
        paths["leaf"],
        paths["uid"],
        paths["gid"],
    )
    assert result.returncode != 0
    after = victim.stat(follow_symlinks=False)
    assert (after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode)) == (
        before.st_dev,
        before.st_ino,
        stat.S_IMODE(before.st_mode),
    )


def test_ensure_managed_rejects_symlink_parent_without_touching_target(managed_paths, tmp_path: Path) -> None:
    paths = managed_paths
    paths["leaf"].rmdir()
    paths["profiles_parent"].rmdir()
    victim = tmp_path / "victim-parent"
    victim.mkdir(mode=0o711)
    os.chown(victim, 0, 0)
    before = victim.stat(follow_symlinks=False)
    paths["profiles_parent"].symlink_to(victim, target_is_directory=True)

    result = _run(
        "ensure-managed",
        paths["state_root"],
        paths["profiles_parent"],
        paths["leaf"],
        paths["uid"],
        paths["gid"],
    )
    assert result.returncode != 0
    after = victim.stat(follow_symlinks=False)
    assert (after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode)) == (
        before.st_dev,
        before.st_ino,
        stat.S_IMODE(before.st_mode),
    )


def test_ensure_managed_rejects_root_service_uid_without_creating_topology(tmp_path: Path) -> None:
    if os.name != "posix" or os.geteuid() != 0:
        pytest.skip("root-owned runtime-home topology requires POSIX root")
    root = tmp_path / "state"
    result = _run(
        "ensure-managed",
        root,
        root / "profiles",
        root / "profiles" / "dispatcher",
        0,
        os.getgid(),
    )
    assert result.returncode != 0
    assert not root.exists()


def test_ensure_managed_rejects_root_service_gid_without_creating_topology(
    tmp_path: Path,
) -> None:
    if os.name != "posix" or os.geteuid() != 0:
        pytest.skip("root-owned runtime-home topology requires POSIX root")
    root = tmp_path / "state"
    result = _run(
        "ensure-managed",
        root,
        root / "profiles",
        root / "profiles" / "dispatcher",
        _service_identity()[0],
        0,
    )
    assert result.returncode != 0
    assert not root.exists()


def test_symlink_swap_race_never_changes_external_target(managed_paths, tmp_path: Path) -> None:
    """A same-host race may make the guard fail, but must never touch victim."""

    paths = managed_paths
    paths["leaf"].rmdir()
    victim = tmp_path / "race-victim"
    victim.mkdir(mode=0o711)
    os.chown(victim, 0, 0)
    before = victim.stat(follow_symlinks=False)
    stop = threading.Event()

    def swap() -> None:
        while not stop.is_set():
            try:
                paths["leaf"].symlink_to(victim, target_is_directory=True)
            except FileExistsError:
                pass
            try:
                if paths["leaf"].is_symlink():
                    paths["leaf"].unlink()
            except FileNotFoundError:
                pass

    attacker = threading.Thread(target=swap, daemon=True)
    attacker.start()
    try:
        for _ in range(20):
            _run(
                "ensure-managed",
                paths["state_root"],
                paths["profiles_parent"],
                paths["leaf"],
                paths["uid"],
                paths["gid"],
            )
    finally:
        stop.set()
        attacker.join(timeout=5)
    after = victim.stat(follow_symlinks=False)
    assert (after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode)) == (
        before.st_dev,
        before.st_ino,
        stat.S_IMODE(before.st_mode),
    )


def test_seal_and_unseal_are_durable_idempotent_inode_transitions(managed_paths) -> None:
    paths = managed_paths
    seal = _run(
        "seal",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert seal.returncode == 0, seal.stderr
    assert _identity(paths["leaf"]) == paths["leaf_identity"]
    assert stat.S_IMODE(paths["leaf"].stat().st_mode) == 0o700
    journal_metadata = paths["journal"].stat(follow_symlinks=False)
    assert (journal_metadata.st_uid, journal_metadata.st_gid) == (0, 0)
    assert stat.S_IMODE(journal_metadata.st_mode) == 0o600
    assert json.loads(paths["journal"].read_text(encoding="utf-8"))["phase"] == "sealed"

    repeated = _run(
        "seal",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["journal_identity"] == json.loads(seal.stdout)[
        "journal_identity"
    ]

    unseal = _run(
        "unseal",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert unseal.returncode == 0, unseal.stderr
    assert _identity(paths["leaf"]) == paths["leaf_identity"]
    assert stat.S_IMODE(paths["leaf"].stat().st_mode) == 0o770
    assert not paths["journal"].exists()


def test_normalize_files_is_fd_anchored_and_handles_sqlite_sidecars(managed_paths) -> None:
    paths = managed_paths
    database_dir = paths["leaf"] / "collaboration" / "account-files"
    database_dir.mkdir(parents=True)
    database = database_dir / "library.sqlite3"
    sidecar = Path(f"{database}-wal")
    database.write_bytes(b"db")
    sidecar.write_bytes(b"wal")
    os.chown(database, 0, 0)
    os.chown(sidecar, 0, 0)
    os.chmod(database, 0o640)
    os.chmod(sidecar, 0o640)
    seal = _run(
        "seal",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert seal.returncode == 0, seal.stderr
    normalized = _run(
        "normalize-files",
        paths["leaf"],
        paths["leaf_identity"],
        paths["uid"],
        paths["gid"],
        "collaboration/account-files/library.sqlite3",
    )
    assert normalized.returncode == 0, normalized.stderr
    for target in (database, sidecar):
        metadata = target.stat(follow_symlinks=False)
        assert (metadata.st_uid, metadata.st_gid) == (paths["uid"], paths["gid"])
        assert stat.S_IMODE(metadata.st_mode) == 0o600


def test_normalize_files_rejects_nested_or_final_symlink_without_touching_victim(managed_paths, tmp_path: Path) -> None:
    paths = managed_paths
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.sqlite3"
    victim.write_bytes(b"secret")
    os.chown(victim, 0, 0)
    os.chmod(victim, 0o640)
    nested = paths["leaf"] / "nested"
    nested.mkdir()
    (nested / "link.sqlite3").symlink_to(victim)
    seal = _run(
        "seal",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert seal.returncode == 0, seal.stderr
    result = _run(
        "normalize-files",
        paths["leaf"],
        paths["leaf_identity"],
        paths["uid"],
        paths["gid"],
        "nested/link.sqlite3",
    )
    assert result.returncode != 0
    metadata = victim.stat(follow_symlinks=False)
    assert (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) == (
        0,
        0,
        0o640,
    )


def test_migration_journal_lifecycle_and_field_allowlist(managed_paths, tmp_path: Path) -> None:
    paths = managed_paths
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    journal = paths["journal_parent"] / "migration.json"
    parent_identity = paths["journal_parent_identity"]
    txid = "a" * 32
    commit = "b" * 40
    source_id = _identity(source)
    destination_id = _identity(destination)
    write = _run(
        "journal-write",
        journal,
        parent_identity,
        txid,
        source,
        source_id,
        destination,
        destination_id,
        "1.2.3",
        commit,
    )
    assert write.returncode == 0, write.stderr
    assert _run("journal-inspect", journal, parent_identity).stdout.strip() == "prepared"
    assert _run("journal-field", journal, parent_identity, "txid").stdout.strip() == txid
    assert _run("journal-field", journal, parent_identity, "source").stdout.strip() == str(source)
    assert _run("journal-field", journal, parent_identity, "commit").returncode != 0
    advanced = _run("journal-advance", journal, parent_identity, txid)
    assert advanced.returncode == 0, advanced.stderr
    assert _run("journal-inspect", journal, parent_identity).stdout.strip() == "copied"
    removed = _run("journal-remove", journal, parent_identity, txid)
    assert removed.returncode == 0, removed.stderr
    assert _run("journal-inspect", journal, parent_identity).stdout.strip() == "absent"
    repeated_remove = _run("journal-remove", journal, parent_identity, txid)
    assert repeated_remove.returncode == 0, repeated_remove.stderr
    assert json.loads(repeated_remove.stdout) == {"phase": "absent"}


def test_migration_journal_detects_directory_inode_change(managed_paths, tmp_path: Path) -> None:
    paths = managed_paths
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    journal = paths["journal_parent"] / "migration.json"
    txid = "c" * 32
    write = _run(
        "journal-write",
        journal,
        paths["journal_parent_identity"],
        txid,
        source,
        _identity(source),
        destination,
        _identity(destination),
        "1.2.3",
        "d" * 40,
    )
    assert write.returncode == 0, write.stderr
    replacement = tmp_path / "source-replacement"
    replacement.mkdir()
    source.rename(tmp_path / "source-old")
    replacement.rename(source)
    result = _run("journal-inspect", journal, paths["journal_parent_identity"])
    assert result.returncode != 0


def test_seal_rejects_wrong_inode_without_mode_or_journal_change(managed_paths) -> None:
    paths = managed_paths
    result = _run(
        "seal",
        paths["leaf"],
        "1:1",
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert result.returncode != 0
    assert stat.S_IMODE(paths["leaf"].stat().st_mode) == 0o770
    assert not paths["journal"].exists()


def test_seal_rejects_wrong_leaf_mode_and_owner(managed_paths) -> None:
    paths = managed_paths
    os.chmod(paths["leaf"], 0o750)
    result = _run(
        "seal",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert result.returncode != 0
    assert not paths["journal"].exists()

    os.chmod(paths["leaf"], 0o770)
    if paths["uid"] != 0:
        os.chown(paths["leaf"], paths["uid"], paths["gid"])
        result = _run(
            "seal",
            paths["leaf"],
            paths["leaf_identity"],
            paths["journal"],
            paths["journal_parent_identity"],
        )
        assert result.returncode != 0
        assert not paths["journal"].exists()


def test_seal_rejects_symlink_journal_without_touching_target(managed_paths, tmp_path: Path) -> None:
    paths = managed_paths
    victim = tmp_path / "journal-victim"
    victim.write_text("unchanged\n", encoding="utf-8")
    os.chown(victim, 0, 0)
    os.chmod(victim, 0o600)
    paths["journal"].symlink_to(victim)

    result = _run(
        "seal",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert result.returncode != 0
    assert victim.read_text(encoding="utf-8") == "unchanged\n"
    assert stat.S_IMODE(paths["leaf"].stat().st_mode) == 0o770


def test_transition_rejects_changed_journal_parent_identity(managed_paths) -> None:
    paths = managed_paths
    result = _run(
        "seal",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        "1:1",
    )
    assert result.returncode != 0
    assert stat.S_IMODE(paths["leaf"].stat().st_mode) == 0o770
    assert not paths["journal"].exists()


def test_remove_empty_requires_a_sealed_empty_exact_inode(managed_paths) -> None:
    paths = managed_paths
    seal = _run(
        "seal",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert seal.returncode == 0, seal.stderr
    removed = _run(
        "remove-empty",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert removed.returncode == 0, removed.stderr
    assert not paths["leaf"].exists()
    assert not paths["journal"].exists()


def test_remove_empty_refuses_nonempty_leaf_without_deleting_it(managed_paths) -> None:
    paths = managed_paths
    (paths["leaf"] / "owned-state").write_text("keep\n", encoding="utf-8")
    seal = _run(
        "seal",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert seal.returncode == 0, seal.stderr
    result = _run(
        "remove-empty",
        paths["leaf"],
        paths["leaf_identity"],
        paths["journal"],
        paths["journal_parent_identity"],
    )
    assert result.returncode != 0
    assert _identity(paths["leaf"]) == paths["leaf_identity"]
    assert (paths["leaf"] / "owned-state").read_text(encoding="utf-8") == "keep\n"
    assert paths["journal"].exists()


def test_non_root_invocation_fails_closed(tmp_path: Path) -> None:
    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("requires a non-root POSIX test runner")
    result = _run(
        "ensure-managed",
        tmp_path / "state",
        tmp_path / "state" / "profiles",
        tmp_path / "state" / "profiles" / "dispatcher",
        os.getuid(),
        os.getgid(),
    )
    assert result.returncode != 0
    assert "must run as root" in result.stderr
