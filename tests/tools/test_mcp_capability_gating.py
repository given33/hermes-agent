"""Tests for MCP 2026-07-28 capability-gated discovery and liveness."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools.mcp_tool import MCPServerTask


def _caps(tools=None, prompts=None, resources=None):
    """Build a minimal current-protocol DiscoverResult stand-in."""
    return SimpleNamespace(
        capabilities=SimpleNamespace(tools=tools, prompts=prompts, resources=resources)
    )


class TestAdvertisesTools:
    def test_true_when_tools_capability_present(self):
        task = MCPServerTask("test")
        task.discover_result = _caps(tools=SimpleNamespace(list_changed=True))
        assert task._advertises_tools() is True

    def test_false_for_prompt_only_server(self):
        task = MCPServerTask("test")
        task.discover_result = _caps(prompts=SimpleNamespace(list_changed=None))
        assert task._advertises_tools() is False

    def test_false_without_discovery(self):
        task = MCPServerTask("test")
        assert task.discover_result is None
        assert task._advertises_tools() is False

    def test_false_for_malformed_discovery(self):
        task = MCPServerTask("test")
        task.discover_result = SimpleNamespace()
        assert task._advertises_tools() is False


@pytest.mark.asyncio
class TestDiscoverToolsGating:
    async def test_skips_list_tools_for_prompt_only_server(self):
        task = MCPServerTask("test")
        task.discover_result = _caps(prompts=SimpleNamespace())
        task.session = SimpleNamespace(list_tools=AsyncMock())
        task._tools = ["stale"]

        await task._discover_tools()

        task.session.list_tools.assert_not_called()
        assert task._tools == []

    async def test_calls_list_tools_for_tool_capable_server(self):
        task = MCPServerTask("test")
        task.discover_result = _caps(tools=SimpleNamespace())
        fake_tool = SimpleNamespace(name="echo")
        task.session = SimpleNamespace(
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[fake_tool]))
        )

        await task._discover_tools()

        task.session.list_tools.assert_awaited_once()
        assert task._tools == [fake_tool]

    async def test_missing_discovery_does_not_probe_tools(self):
        task = MCPServerTask("test")
        task.session = SimpleNamespace(list_tools=AsyncMock())

        await task._discover_tools()

        task.session.list_tools.assert_not_called()


@pytest.mark.asyncio
class TestRefreshToolsGating:
    async def test_refresh_noop_for_prompt_only_server(self):
        task = MCPServerTask("test")
        task.discover_result = _caps(prompts=SimpleNamespace())
        task.session = SimpleNamespace(list_tools=AsyncMock())

        await task._refresh_tools()

        task.session.list_tools.assert_not_called()


@pytest.mark.asyncio
class TestKeepaliveProbe:
    async def test_uses_tools_list_when_tools_are_advertised(self):
        task = MCPServerTask("test")
        task.discover_result = _caps(tools=SimpleNamespace())
        task.session = SimpleNamespace(list_tools=AsyncMock())

        await task._keepalive_probe()

        task.session.list_tools.assert_awaited_once()

    async def test_uses_resources_list_for_resource_only_server(self):
        task = MCPServerTask("test")
        task.discover_result = _caps(resources=SimpleNamespace())
        task.session = SimpleNamespace(list_resources=AsyncMock())

        await task._keepalive_probe()

        task.session.list_resources.assert_awaited_once()

    async def test_uses_prompts_list_for_prompt_only_server(self):
        task = MCPServerTask("test")
        task.discover_result = _caps(prompts=SimpleNamespace())
        task.session = SimpleNamespace(list_prompts=AsyncMock())

        await task._keepalive_probe()

        task.session.list_prompts.assert_awaited_once()

    async def test_no_capability_leaves_session_idle(self):
        task = MCPServerTask("test")
        task.discover_result = _caps()
        task.session = SimpleNamespace()

        await task._keepalive_probe()


    async def test_list_failure_propagates_for_reconnect(self):
        task = MCPServerTask("test")
        task.discover_result = _caps(tools=SimpleNamespace())
        task.session = SimpleNamespace(list_tools=AsyncMock(side_effect=RuntimeError("closed")))

        with pytest.raises(RuntimeError, match="closed"):
            await task._keepalive_probe()


class TestKeepaliveInterval:
    async def _captured_interval(self, config):
        task = MCPServerTask("test")
        task._config = config
        task.discover_result = _caps()
        task.session = SimpleNamespace()
        captured = {}
        real_wait = asyncio.wait

        async def fake_wait(tasks, timeout=None, return_when=None):
            captured["timeout"] = timeout
            task._shutdown_event.set()
            return await real_wait(
                tasks, timeout=0.5, return_when=return_when or asyncio.FIRST_COMPLETED
            )

        import tools.mcp_tool as mcp_mod
        original_wait = mcp_mod.asyncio.wait
        mcp_mod.asyncio.wait = fake_wait
        try:
            await task._wait_for_lifecycle_event()
        finally:
            mcp_mod.asyncio.wait = original_wait
        return captured["timeout"]

    @pytest.mark.asyncio
    async def test_default_interval_when_unset(self):
        from tools.mcp_tool import _DEFAULT_KEEPALIVE_INTERVAL

        assert await self._captured_interval({}) == _DEFAULT_KEEPALIVE_INTERVAL

    @pytest.mark.asyncio
    async def test_configured_interval_honored(self):
        assert await self._captured_interval({"keepalive_interval": 10}) == 10

    @pytest.mark.asyncio
    async def test_interval_clamped_to_floor(self):
        from tools.mcp_tool import _MIN_KEEPALIVE_INTERVAL

        assert await self._captured_interval({"keepalive_interval": 0.1}) == _MIN_KEEPALIVE_INTERVAL
