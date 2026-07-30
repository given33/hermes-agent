"""Contract tests for the MCP 2026-07-28-only client surface."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import mcp_tool


def test_sdk_and_client_use_latest_protocol_revision():
    from mcp.types.version import LATEST_PROTOCOL_VERSION as sdk_latest

    assert sdk_latest == "2026-07-28"
    assert mcp_tool.LATEST_PROTOCOL_VERSION == sdk_latest


@pytest.mark.asyncio
async def test_session_negotiation_uses_server_discover_only():
    expected = SimpleNamespace(capabilities=SimpleNamespace())
    session = SimpleNamespace(discover=AsyncMock(return_value=expected))

    result = await mcp_tool._discover_mcp_session(session)

    assert result is expected
    session.discover.assert_awaited_once_with()
    assert not hasattr(session, "initialize")
