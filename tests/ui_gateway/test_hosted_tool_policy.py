"""Hosted-chat tool policy tests.

The hosted mobile route temporarily removes the tool surface for a plain chat
turn, then must put the persistent gateway session back exactly as it was for
the next work turn.  Reasoning remains enabled for reasoning-capable models.
"""

from types import SimpleNamespace

import tui_gateway.server as server


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        tools=[{"type": "function", "function": {"name": "terminal"}}],
        valid_tool_names={"terminal"},
        enabled_toolsets=["core"],
        _last_content_with_tools="cached-tools",
        _last_content_tools_all_housekeeping=True,
        reasoning_config={"enabled": True, "effort": "xhigh"},
    )


def test_tool_free_turn_restores_the_persistent_agent_snapshot() -> None:
    agent = _agent()
    original = dict(vars(agent))

    snapshot = server._turn_tool_policy_snapshot(agent, False)

    assert agent.tools == []
    assert agent.valid_tool_names == set()
    assert agent.enabled_toolsets == []
    assert agent._last_content_with_tools is None
    assert agent._last_content_tools_all_housekeeping is False
    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}

    server._restore_turn_tool_policy(agent, snapshot)

    assert vars(agent) == original


def test_unspecified_turn_policy_does_not_mutate_the_agent() -> None:
    agent = _agent()
    original = dict(vars(agent))

    assert server._turn_tool_policy_snapshot(agent, None) is None
    assert vars(agent) == original


def test_custom_tool_free_turn_preserves_configured_reasoning_and_transport() -> None:
    agent = _agent()
    agent.api_mode = "codex_responses"
    agent.provider = "custom"
    agent.request_overrides = {"service_tier": "normal"}
    original = dict(vars(agent))

    snapshot = server._turn_tool_policy_snapshot(agent, False)

    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}
    assert agent.request_overrides == {"service_tier": "normal"}
    server._restore_turn_tool_policy(agent, snapshot)
    assert vars(agent) == original


def test_custom_fast_turn_keeps_resolved_transport_and_restores_mode() -> None:
    agent = _agent()
    agent.api_mode = "codex_responses"
    agent.provider = "custom"
    agent.base_url = "https://hubway.cc/v1"
    agent.request_overrides = {"service_tier": "normal"}
    original = dict(vars(agent))

    snapshot = server._turn_tool_policy_snapshot(agent, False)

    assert agent.api_mode == "codex_responses"
    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}
    assert agent.request_overrides == {"service_tier": "normal"}

    server._restore_turn_tool_policy(agent, snapshot)
    assert vars(agent) == original


def test_fast_session_hydrates_mcp_tools_only_at_an_explicit_tool_turn(monkeypatch) -> None:
    agent = _agent()
    session = {"create_allow_tools": False}
    calls: list[str] = []

    monkeypatch.setattr(
        "tui_gateway.entry.ensure_mcp_discovery_started",
        lambda: calls.append("start"),
    )
    monkeypatch.setattr(
        "tui_gateway.entry.wait_for_mcp_discovery",
        lambda: calls.append("wait"),
    )

    def refresh(current_agent, **_kwargs):
        assert current_agent is agent
        calls.append("refresh")
        return {"ios_intelligence"}

    monkeypatch.setattr("tools.mcp_tool.refresh_agent_mcp_tools", refresh)

    server._ensure_hosted_tools_for_turn("sid", session, agent, True)
    server._ensure_hosted_tools_for_turn("sid", session, agent, True)

    assert calls == ["start", "wait", "refresh"]
    assert session["_hosted_tools_hydrated"] is True
