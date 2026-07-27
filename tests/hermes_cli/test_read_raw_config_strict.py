"""read_raw_config_strict: the fail-closed read half of read→mutate→save_config.

Regression tests for the /model global-persist wipe: ``read_raw_config``
degrades a corrupt-but-recoverable config.yaml to ``{}``, and feeding that
into ``save_config(..., merge_existing=False)`` replaces the whole file with
just the mutation. The strict variant must raise instead, so the caller
aborts the write and the user's file survives.
"""

import pytest

from hermes_cli.config import read_raw_config, read_raw_config_strict


class TestReadRawConfigStrict:
    def test_valid_mapping_round_trips(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("model:\n  default: gpt-4\nplatforms:\n  telegram:\n    token: t\n", encoding="utf-8")
        cfg = read_raw_config_strict(p)
        assert cfg["model"]["default"] == "gpt-4"
        assert cfg["platforms"]["telegram"]["token"] == "t"

    def test_missing_file_starts_empty(self, tmp_path):
        # First write creates the file; a missing file is not corruption.
        assert read_raw_config_strict(tmp_path / "config.yaml") == {}

    def test_empty_file_is_empty_mapping(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("", encoding="utf-8")
        assert read_raw_config_strict(p) == {}

    def test_parse_error_raises_instead_of_wiping(self, tmp_path):
        # A corrupt-but-recoverable file: one bad line, everything else intact.
        # read_raw_config fail-opens to {}; the strict variant must refuse.
        p = tmp_path / "config.yaml"
        p.write_text(
            "model:\n  default: gpt-4\nbroken: [unclosed\nplatforms:\n  slack: {}\n",
            encoding="utf-8",
        )
        assert read_raw_config(p) == {}  # documents the fail-open contrast
        with pytest.raises(Exception) as exc_info:
            read_raw_config_strict(p)
        # Any parse-layer exception is acceptable; silently returning a dict
        # is the only failure mode.
        assert not isinstance(exc_info.value, (KeyboardInterrupt, SystemExit))

    def test_non_mapping_document_refused(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="not a YAML mapping"):
            read_raw_config_strict(p)


class TestModelPersistSitesUseStrictRead:
    def test_slash_commands_no_longer_pair_fail_open_read_with_save(self):
        # Structural guard: the two /model global-persist sites must not
        # regress to read_raw_config + save_config. Grep-level check keeps
        # this independent of gateway import weight on CI.
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[2]
            / "gateway"
            / "slash_commands.py"
        ).read_text(encoding="utf-8")
        assert "read_raw_config_strict" in src
        # Every remaining bare read_raw_config use must not feed save_config
        # in the same handler; the simplest enforceable form: no import of
        # bare read_raw_config alongside save_config in one import block.
        for block in src.split("from hermes_cli.config import"):
            names = block.split(")")[0] if "(" in block[:20] else block.split("\n")[0]
            if "save_config" in names and "read_raw_config" in names:
                assert "read_raw_config_strict" in names, (
                    "a slash_commands import block pairs fail-open "
                    "read_raw_config with save_config again"
                )
