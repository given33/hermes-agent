"""Regression tests for the removed Nous account-notice product surface."""
from __future__ import annotations

from unittest.mock import patch

from agent.credits_tracker import CreditsState
from run_agent import AIAgent


def _agent_with_state(*, paid_access: bool = False) -> AIAgent:
    """Bare agent with a depleted-shaped state that would normally emit."""
    agent = object.__new__(AIAgent)
    agent.notice_callback = None
    agent.notice_clear_callback = None
    agent._credits_state = CreditsState(paid_access=paid_access)
    agent.model = ""
    agent.base_url = ""
    return agent


def _cfg(enabled):
    return {"display": {"credits_notices": enabled}}


class TestCreditsNoticesToggle:
    def test_response_headers_are_not_captured(self):
        agent = _agent_with_state()
        agent._credits_state = None
        response = type(
            "Response",
            (),
            {"headers": {"x-nous-credits-remaining": "1000000"}},
        )()

        agent._capture_credits(response)

        assert agent._credits_state is None

    def test_disabled_emits_nothing(self):
        agent = _agent_with_state()
        received = []
        agent.notice_callback = received.append
        with patch("hermes_cli.config.load_config", return_value=_cfg(False)):
            agent._emit_credits_notices()
        assert received == []



    def test_config_error_emits_nothing(self):
        agent = _agent_with_state()
        received = []
        agent.notice_callback = received.append
        with patch("hermes_cli.config.load_config", side_effect=RuntimeError("boom")):
            agent._emit_credits_notices()
        assert received == []

    def test_removed_surface_does_not_load_toggle_config(self):
        agent = _agent_with_state()
        agent.notice_callback = lambda n: None
        with patch("hermes_cli.config.load_config", return_value=_cfg(True)) as mock_load:
            agent._emit_credits_notices()
            agent._emit_credits_notices()
        assert mock_load.call_count == 0

    def test_disabled_state_still_cached_for_usage(self):
        """The gate stops emission only — get_credits_state still returns data."""
        agent = _agent_with_state()
        agent.notice_callback = lambda n: None
        agent._credits_session_start_micros = None
        with patch("hermes_cli.config.load_config", return_value=_cfg(False)):
            agent._emit_credits_notices()
        assert agent.get_credits_state() is not None
