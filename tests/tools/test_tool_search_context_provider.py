"""Regression coverage for provider-aware context sizing in the tool-search gate.

``model_tools._resolve_active_context_length()`` feeds ``should_activate``'s
window-fraction check. Providers like Codex OAuth enforce a lower context
window than the direct API for the same slug (e.g. gpt-5.5 is 1.05M on the
API but 272K on the Codex route), and ``get_model_context_length()`` only
applies those provider-aware resolutions when it receives the provider,
base_url, and credential. Before this coverage existed the gate called the
resolver with the model id alone, so Codex sessions sized activation against
generic direct-API metadata.
"""

from unittest.mock import patch


def test_agent_override_does_not_probe_profile_default_for_tool_search():
    """The compressor resolves the real route; disclosure must reuse it."""
    from run_agent import AIAgent
    from tools.mcp_tool import refresh_agent_mcp_tools

    with patch("model_tools._resolve_active_context_length", return_value=999_000) as default_probe, \
         patch("agent.context_compressor.get_model_context_length", return_value=64_000):
        agent = AIAgent(
            model="gpt-4o", provider="openai", api_key="test-key",
            base_url="https://api.openai.com/v1", enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
        # The first conversation prologue and late MCP discovery both rebuild
        # the snapshot. This was a separate default-model network probe even
        # after initialization correctly reused the compressor's window.
        refresh_agent_mcp_tools(agent, content_aware=True)
    default_probe.assert_not_called()
    assert agent.valid_tool_names == {tool["function"]["name"] for tool in agent.tools}


def test_resolved_window_controls_disclosure_without_another_probe():
    import json
    import model_tools
    from tools.registry import registry
    from tools.tool_search import ToolSearchConfig

    tools = [{"type": "function", "function": {
        "name": "mcp_demo_search", "description": "search " * 2000,
        "parameters": {"type": "object", "properties": {}},
    }}]
    registry.register(name="mcp_demo_search", toolset="mcp-demo",
                      schema=tools[0], handler=lambda args, **kwargs: "{}")
    with patch("model_tools._resolve_active_context_length") as default_probe, \
         patch("tools.tool_search.load_config", return_value=ToolSearchConfig.from_raw(None)):
        small = model_tools.assemble_tool_search(tools, quiet_mode=True, context_length=64)
        large = model_tools.assemble_tool_search(tools, quiet_mode=True, context_length=1_000_000)
    default_probe.assert_not_called()
    assert "tool_call" in {t["function"]["name"] for t in small}
    assert "tool_call" in {t["function"]["name"] for t in large}
    assert len(json.dumps(small)) < len(json.dumps(large))


def test_tool_definition_cache_keeps_active_windows_separate():
    import json
    import model_tools
    from tools.registry import registry
    from tools.tool_search import ToolSearchConfig

    schema = {"type": "function", "function": {
        "name": "mcp_window_search", "description": "search " * 2000,
        "parameters": {"type": "object", "properties": {}},
    }}
    registry.register(name="mcp_window_search", toolset="mcp-window",
                      schema=schema, handler=lambda args, **kwargs: "{}")
    model_tools._clear_tool_defs_cache()
    with patch("model_tools._resolve_active_context_length") as default_probe, \
         patch("tools.tool_search.load_config", return_value=ToolSearchConfig.from_raw(None)):
        small = model_tools.get_tool_definitions(
            enabled_toolsets=["mcp-window"], quiet_mode=True, context_length=64)
        large = model_tools.get_tool_definitions(
            enabled_toolsets=["mcp-window"], quiet_mode=True, context_length=1_000_000)
        again = model_tools.get_tool_definitions(
            enabled_toolsets=["mcp-window"], quiet_mode=True, context_length=64)
    default_probe.assert_not_called()
    assert len(json.dumps(small)) < len(json.dumps(large))
    assert again == small


def _model_cfg(**overrides):
    cfg = {
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "base_url": "",
    }
    cfg.update(overrides)
    return {"model": cfg}


class TestResolveActiveContextLengthProviderAware:
    def test_passes_provider_base_url_and_key_from_runtime(self):
        """Resolved runtime credentials must reach get_model_context_length."""
        import model_tools

        captured = {}

        def fake_get_ctx(model_id, base_url="", api_key="", config_context_length=None, provider=""):
            captured.update(
                model=model_id, base_url=base_url, api_key=api_key,
                config_ctx=config_context_length, provider=provider,
            )
            return 272_000

        with patch("hermes_cli.config.load_config", return_value=_model_cfg()), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   return_value={"base_url": "https://chatgpt.com/backend-api/codex",
                                 "api_key": "tok-live"}) as mock_rt, \
             patch("agent.model_metadata.get_model_context_length", side_effect=fake_get_ctx):
            ctx = model_tools._resolve_active_context_length()

        assert ctx == 272_000
        assert captured["provider"] == "openai-codex"
        assert captured["base_url"] == "https://chatgpt.com/backend-api/codex"
        assert captured["api_key"] == "tok-live"
        mock_rt.assert_called_once_with(
            requested="openai-codex", target_model="gpt-5.6-sol"
        )

    def test_offline_credential_failure_degrades_to_config_values(self):
        """Runtime resolution raising must not zero the gate — the resolver is
        still called with the configured provider/base_url and an empty key so
        static provider-aware fallbacks apply."""
        import model_tools

        captured = {}

        def fake_get_ctx(model_id, base_url="", api_key="", config_context_length=None, provider=""):
            captured.update(base_url=base_url, api_key=api_key, provider=provider)
            return 272_000

        with patch("hermes_cli.config.load_config",
                   return_value=_model_cfg(base_url="https://chatgpt.com/backend-api/codex")), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   side_effect=RuntimeError("no credentials")), \
             patch("agent.model_metadata.get_model_context_length", side_effect=fake_get_ctx):
            ctx = model_tools._resolve_active_context_length()

        assert ctx == 272_000
        assert captured["provider"] == "openai-codex"
        assert captured["base_url"] == "https://chatgpt.com/backend-api/codex"
        assert captured["api_key"] == ""

    def test_no_provider_configured_skips_runtime_resolution(self):
        """Without a provider in config, behavior matches the legacy path: no
        runtime resolution attempt, resolver called with empty routing."""
        import model_tools

        captured = {}

        def fake_get_ctx(model_id, base_url="", api_key="", config_context_length=None, provider=""):
            captured.update(base_url=base_url, provider=provider)
            return 200_000

        with patch("hermes_cli.config.load_config",
                   return_value={"model": {"model": "some-model"}}), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider") as mock_rt, \
             patch("agent.model_metadata.get_model_context_length", side_effect=fake_get_ctx):
            ctx = model_tools._resolve_active_context_length()

        assert ctx == 200_000
        assert captured["provider"] == ""
        mock_rt.assert_not_called()

    def test_config_context_length_still_short_circuits(self):
        """Explicit model.context_length must keep winning (issue #46620)."""
        import model_tools

        captured = {}

        def fake_get_ctx(model_id, base_url="", api_key="", config_context_length=None, provider=""):
            captured["config_ctx"] = config_context_length
            return config_context_length or 0

        with patch("hermes_cli.config.load_config",
                   return_value=_model_cfg(context_length=150_000)), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   return_value={"base_url": "https://chatgpt.com/backend-api/codex",
                                 "api_key": "tok"}), \
             patch("agent.model_metadata.get_model_context_length", side_effect=fake_get_ctx):
            ctx = model_tools._resolve_active_context_length()

        assert ctx == 150_000
        assert captured["config_ctx"] == 150_000
