"""Tests for the TOCTOU-safe secret-write primitives in utils.py.

``write_secret_file`` must never expose secret bytes with permissions
looser than the requested mode: the temp file is created 0o600 by
``mkstemp`` and pinned via ``fchmod`` *before* the payload is written,
then atomically swapped into place.  ``atomic_yaml_write(mode=...)``
gets the same treatment; ``mode=None`` keeps the historical
"preserve existing permissions" behavior.
"""
from __future__ import annotations

import os
import stat
import sys

import pytest
import yaml

from utils import atomic_yaml_write, write_secret_file

POSIX = sys.platform != "win32"


def _mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _no_tmp_leftovers(directory) -> bool:
    return not [p for p in os.listdir(directory) if p.endswith(".tmp")]


class TestWriteSecretFile:
    def test_creates_file_with_content(self, tmp_path):
        target = tmp_path / ".env"
        write_secret_file(target, "API_KEY=hunter2\n")
        assert target.read_text(encoding="utf-8") == "API_KEY=hunter2\n"
        assert _no_tmp_leftovers(tmp_path)

    @pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
    def test_mode_is_owner_only_by_default(self, tmp_path):
        target = tmp_path / ".env"
        write_secret_file(target, "API_KEY=hunter2\n")
        assert _mode_of(target) == 0o600

    @pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
    def test_overwrite_tightens_loose_permissions(self, tmp_path):
        # A pre-existing world-readable file must come out 0o600 — the
        # whole point is that the secret is never left readable.
        target = tmp_path / ".env"
        target.write_text("OLD=1\n", encoding="utf-8")
        os.chmod(target, 0o644)
        write_secret_file(target, "API_KEY=hunter2\n")
        assert target.read_text(encoding="utf-8") == "API_KEY=hunter2\n"
        assert _mode_of(target) == 0o600

    def test_creates_parent_directories(self, tmp_path):
        target = tmp_path / "nested" / "dir" / "secrets.env"
        write_secret_file(target, "TOKEN=x\n")
        assert target.read_text(encoding="utf-8") == "TOKEN=x\n"

    def test_custom_mode(self, tmp_path):
        target = tmp_path / "shared.env"
        write_secret_file(target, "TOKEN=x\n", mode=0o640)
        assert target.read_text(encoding="utf-8") == "TOKEN=x\n"
        if POSIX:
            assert _mode_of(target) == 0o640

    def test_failed_write_leaves_previous_content(self, tmp_path, monkeypatch):
        target = tmp_path / ".env"
        write_secret_file(target, "FIRST=1\n")

        import utils as utils_mod

        def boom(tmp, dest):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(utils_mod, "atomic_replace", boom)
        with pytest.raises(OSError, match="simulated replace failure"):
            write_secret_file(target, "SECOND=2\n")
        # Previous version intact, temp file cleaned up.
        assert target.read_text(encoding="utf-8") == "FIRST=1\n"
        assert _no_tmp_leftovers(tmp_path)


class TestAtomicYamlWriteMode:
    def test_mode_none_preserves_existing_permissions(self, tmp_path):
        target = tmp_path / "config.yaml"
        target.write_text("a: 1\n", encoding="utf-8")
        if POSIX:
            os.chmod(target, 0o644)
        atomic_yaml_write(target, {"a": 2})
        assert yaml.safe_load(target.read_text(encoding="utf-8")) == {"a": 2}
        if POSIX:
            assert _mode_of(target) == 0o644

    @pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
    def test_explicit_mode_applies_to_new_file(self, tmp_path):
        target = tmp_path / "secrets.yaml"
        atomic_yaml_write(target, {"api_key": "hunter2"}, mode=0o600)
        assert yaml.safe_load(target.read_text(encoding="utf-8")) == {
            "api_key": "hunter2"
        }
        assert _mode_of(target) == 0o600

    @pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
    def test_explicit_mode_overrides_loose_existing(self, tmp_path):
        target = tmp_path / "secrets.yaml"
        target.write_text("api_key: old\n", encoding="utf-8")
        os.chmod(target, 0o666)
        atomic_yaml_write(target, {"api_key": "new"}, mode=0o600)
        assert _mode_of(target) == 0o600
