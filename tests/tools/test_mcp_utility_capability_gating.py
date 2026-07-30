"""Tests for current-protocol capability-gated MCP utility registration."""

from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_discover_result(*, resources: bool, prompts: bool):
    caps = {
        "tools": SimpleNamespace(list_changed=True),
        "resources": SimpleNamespace(list_changed=True) if resources else None,
        "prompts": SimpleNamespace(list_changed=True) if prompts else None,
    }
    return SimpleNamespace(capabilities=SimpleNamespace(**caps))


def _make_fake_server(*, discover_result):
    server = MagicMock()
    server.name = "test-server"
    server.session = MagicMock(
        spec=["list_resources", "read_resource", "list_prompts", "get_prompt"]
    )
    server.discover_result = discover_result
    return server


def _handler_keys(selected):
    return {entry["handler_key"] for entry in selected}


class TestCapabilityGatedRegistration:
    def test_tools_only_server_gets_no_utility_schemas(self):
        from tools.mcp_tool import _select_utility_schemas

        server = _make_fake_server(
            discover_result=_make_discover_result(resources=False, prompts=False)
        )
        assert _handler_keys(_select_utility_schemas("context7", server, {})) == set()

    def test_resources_only_server_gets_resource_stubs_only(self):
        from tools.mcp_tool import _select_utility_schemas

        server = _make_fake_server(
            discover_result=_make_discover_result(resources=True, prompts=False)
        )
        selected = _select_utility_schemas("resources", server, {})
        assert _handler_keys(selected) == {"list_resources", "read_resource"}

    def test_prompts_only_server_gets_prompt_stubs_only(self):
        from tools.mcp_tool import _select_utility_schemas

        server = _make_fake_server(
            discover_result=_make_discover_result(resources=False, prompts=True)
        )
        selected = _select_utility_schemas("prompts", server, {})
        assert _handler_keys(selected) == {"list_prompts", "get_prompt"}

    def test_fully_capable_server_gets_all_stubs(self):
        from tools.mcp_tool import _select_utility_schemas

        server = _make_fake_server(
            discover_result=_make_discover_result(resources=True, prompts=True)
        )
        selected = _select_utility_schemas("full", server, {})
        assert _handler_keys(selected) == {
            "list_resources",
            "read_resource",
            "list_prompts",
            "get_prompt",
        }


class TestConfigFilter:
    def test_config_disables_resources(self):
        from tools.mcp_tool import _select_utility_schemas

        server = _make_fake_server(
            discover_result=_make_discover_result(resources=True, prompts=True)
        )
        selected = _select_utility_schemas(
            "filtered", server, {"tools": {"resources": False}}
        )
        assert _handler_keys(selected) == {"list_prompts", "get_prompt"}

    def test_config_disables_prompts(self):
        from tools.mcp_tool import _select_utility_schemas

        server = _make_fake_server(
            discover_result=_make_discover_result(resources=True, prompts=True)
        )
        selected = _select_utility_schemas(
            "filtered", server, {"tools": {"prompts": False}}
        )
        assert _handler_keys(selected) == {"list_resources", "read_resource"}


class TestMissingDiscovery:
    def test_no_utility_schemas_without_discovery(self):
        from tools.mcp_tool import _select_utility_schemas

        server = _make_fake_server(discover_result=None)
        assert _handler_keys(_select_utility_schemas("undiscovered", server, {})) == set()
