import os
import subprocess
from pathlib import Path

import pytest

import hermes_cli.safe_delete as safe_delete_module
from hermes_cli.safe_delete import safe_rmtree


def _make_dir_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"Windows junctions unavailable: {result.stderr.strip()}")
    else:
        os.symlink(target, link)


def test_safe_rmtree_removes_owned_tree(tmp_path):
    root = tmp_path / "owned"
    nested = root / "profiles" / "worker"
    nested.mkdir(parents=True)
    (nested / "state.db").write_text("data", encoding="utf-8")

    safe_rmtree(nested, root)

    assert not nested.exists()
    assert (root / "profiles").exists()


def test_safe_rmtree_refuses_path_outside_allowed_root(tmp_path):
    owned = tmp_path / "owned"
    outside = tmp_path / "outside"
    owned.mkdir()
    outside.mkdir()
    victim = outside / "important.txt"
    victim.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="outside approved roots"):
        safe_rmtree(outside, owned)

    assert victim.read_text(encoding="utf-8") == "keep"


def test_safe_rmtree_does_not_follow_planted_directory_link(tmp_path):
    root = tmp_path / "hermes-home"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    outside = tmp_path / "user-documents"
    outside.mkdir()
    marker = outside / "taxes.txt"
    marker.write_text("keep", encoding="utf-8")
    planted = profile / "projects"
    _make_dir_link(planted, outside)

    safe_rmtree(profile, root)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not profile.exists()


def test_safe_rmtree_fails_closed_when_directory_is_replaced_after_scan(
    tmp_path, monkeypatch
):
    root = tmp_path / "hermes-home"
    target = root / "profiles" / "worker"
    target.mkdir(parents=True)
    (target / "state.db").write_text("keep", encoding="utf-8")
    outside = tmp_path / "user-documents"
    outside.mkdir()
    marker = outside / "taxes.txt"
    marker.write_text("keep", encoding="utf-8")

    original_identity = safe_delete_module._path_identity
    identity_calls = 0
    replaced_target = tmp_path / "worker-before-replacement"

    def replace_after_identity(path):
        nonlocal identity_calls
        if path == target:
            identity_calls += 1
            if identity_calls == 2:
                target.rename(replaced_target)
                _make_dir_link(target, outside)
        return original_identity(path)

    monkeypatch.setattr(
        safe_delete_module, "_path_identity", replace_after_identity
    )
    with pytest.raises(ValueError, match="changed during bounded deletion"):
        safe_rmtree(target, root)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert (replaced_target / "state.db").read_text(encoding="utf-8") == "keep"
    # Remove only the replacement link so pytest cleanup never traverses it.
    try:
        target.unlink()
    except OSError:
        target.rmdir()


def test_safe_rmtree_unlinks_target_link_without_deleting_internal_target(tmp_path):
    root = tmp_path / "hermes-home"
    root.mkdir()
    target = root / "real-profile"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    alias = root / "profile-alias"
    _make_dir_link(alias, target)

    safe_rmtree(alias, root)

    assert not alias.exists()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_safe_rmtree_refuses_intermediate_link_traversal(tmp_path):
    root = tmp_path / "hermes-home"
    root.mkdir()
    target = root / "real-profile"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    alias = root / "profile-alias"
    _make_dir_link(alias, target)

    with pytest.raises(ValueError, match="link-like path"):
        safe_rmtree(alias / "keep.txt", root)

    assert alias.exists()
    assert marker.read_text(encoding="utf-8") == "keep"
