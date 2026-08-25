"""Tests for tui_gateway.server._cfg_max_turns.

The TUI gateway parses agent.max_turns / max_turns on every prompt submit.
Documented spellings (see cron/scheduler.py, which handles them correctly)
must not crash int() and an explicit 0 must not collapse into the default.
"""

from tui_gateway import server


class TestUnlimitedSpellings:
    def test_none_and_unlimited_are_unbounded(self):
        for spelling in ("none", "unlimited", "None", "UNLIMITED", " unlimited ", "\tnone\n"):
            assert server._cfg_max_turns({"agent": {"max_turns": spelling}}, 500) == 999_999

    def test_unlimited_at_top_level(self):
        assert server._cfg_max_turns({"max_turns": "none"}, 500) == 999_999


class TestExplicitZero:
    def test_zero_is_preserved_not_defaulted(self):
        # The old or-chain collapsed 0 into the default; the cron scheduler
        # preserves it and so does the TUI path now.
        assert server._cfg_max_turns({"agent": {"max_turns": 0}}, 500) == 0

    def test_top_level_zero_preserved_when_agent_key_absent(self):
        assert server._cfg_max_turns({"agent": {}, "max_turns": 0}, 500) == 0


class TestNumericAndFallback:
    def test_numeric_values_pass_through(self):
        assert server._cfg_max_turns({"agent": {"max_turns": 25}}, 500) == 25
        assert server._cfg_max_turns({}, 500) == 500
        assert server._cfg_max_turns({"max_turns": 42}, 500) == 42

    def test_non_numeric_garbage_falls_back_to_default(self):
        # Previously raised ValueError out of every prompt submit.
        assert server._cfg_max_turns({"agent": {"max_turns": "lots"}}, 500) == 500
        assert server._cfg_max_turns({"agent": {"max_turns": ""}}, 500) == 500

    def test_none_value_falls_back_to_default(self):
        assert server._cfg_max_turns({"agent": {"max_turns": None}}, 500) == 500


class TestPrecedence:
    def test_agent_level_wins_over_top_level(self):
        cfg = {"agent": {"max_turns": 10}, "max_turns": 20}
        assert server._cfg_max_turns(cfg, 500) == 10

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("HERMES_TUI_MAX_TURNS", "7")
        assert server._cfg_max_turns({"agent": {"max_turns": "none"}}, 500) == 7

    def test_bad_env_value_ignored(self, monkeypatch):
        monkeypatch.setenv("HERMES_TUI_MAX_TURNS", "junk")
        assert server._cfg_max_turns({"agent": {"max_turns": 3}}, 500) == 3
