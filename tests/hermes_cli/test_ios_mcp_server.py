from __future__ import annotations

import asyncio
import concurrent.futures
import socket
import time
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import hermes_cli.ios_mcp_server as ios_mcp_server_module
from hermes_cli.ios_intelligence import IOSIntelligenceStore
from hermes_cli.ios_mcp_server import (
    CAPABILITIES,
    create_mcp_server,
    ios_mcp_server_configs,
    install_ios_mcp_servers,
    merge_ios_mcp_servers,
    ios_mcp_manifests,
)


def _tools(server):
    return server._tool_manager.list_tools()


def _call(server, name, args):
    return asyncio.run(server._tool_manager.call_tool(name, args))


def test_every_declared_capability_builds_an_isolated_server(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    expected = set(CAPABILITIES)
    assert len(expected) == len(CAPABILITIES)
    for capability in CAPABILITIES:
        server = create_mcp_server(capability, store=store)
        tools = _tools(server)
        assert tools, capability
        assert server.name == capability
        assert all(tool.description for tool in tools)
        assert all("Use" in tool.description for tool in tools)


def test_independent_servers_do_not_leak_other_capability_tools(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    location_names = {tool.name for tool in _tools(create_mcp_server("ios-location", store=store))}
    reminder_names = {tool.name for tool in _tools(create_mcp_server("ios-reminders", store=store))}
    weather_names = {tool.name for tool in _tools(create_mcp_server("qweather", store=store))}

    assert location_names == {"current_location"}
    assert reminder_names == {"ios_reminders_list", "ios_reminder_create"}
    assert weather_names == {"weather_minutely", "weather_current", "weather_hourly", "weather_warnings"}
    assert location_names.isdisjoint(reminder_names | weather_names)

    watch_names = {tool.name for tool in _tools(create_mcp_server("ios-watch", store=store))}
    assert watch_names == {
        "ios_watch_get_latest",
        "ios_watch_send",
        "ios_watch_start_active_relay",
        "ios_watch_stop_active_relay",
    }


def test_screen_time_server_exposes_native_monitor_controls(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    names = {
        tool.name
        for tool in _tools(create_mcp_server("ios-screen-time", store=store))
    }
    assert {
        "ios_screen_time_authorize",
        "ios_screen_time_start",
        "ios_screen_time_stop",
    } <= names


def test_external_mcp_clients_honor_plugin_base_urls(monkeypatch, tmp_path):
    captured = {}

    class FakeWeatherClient:
        def __init__(self, store, *, base_url):
            captured["qweather"] = (store, base_url)

    class FakeAMapClient:
        def __init__(self, *, base_url):
            captured["amap"] = base_url

    monkeypatch.setattr(
        ios_mcp_server_module,
        "load_ios_intelligence_config",
        lambda: SimpleNamespace(weather=SimpleNamespace(
            qweather_base_url="https://weather.example",
            amap_base_url="https://amap.example",
        )),
    )
    monkeypatch.setattr(ios_mcp_server_module, "QWeatherClient", FakeWeatherClient)
    monkeypatch.setattr(ios_mcp_server_module, "AMapClient", FakeAMapClient)
    store = IOSIntelligenceStore(tmp_path)

    create_mcp_server("qweather", store=store)
    create_mcp_server("amap-route", store=store)

    assert captured == {
        "qweather": (store, "https://weather.example"),
        "amap": "https://amap.example",
    }


def test_location_tool_reads_snapshot_and_queues_refresh_when_missing(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    server = create_mcp_server("ios-location", store=store)
    missing = _call(server, "current_location", {"owner_id": "alice"})
    assert missing["location"] is None
    assert missing["refresh_queued"]["status"] == "pending"

    store.record_snapshot("alice", "location", {"latitude": 24.9, "longitude": 118.6})
    present = _call(server, "current_location", {"owner_id": "alice", "refresh_if_older_than_seconds": 300})
    assert present["location"]["data"]["latitude"] == 24.9


def test_ordinary_chat_tool_resolves_single_active_account_without_owner_argument(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    store.record_snapshot("alice", "power", {"level": 0.76, "charging": True})
    server = create_mcp_server("ios-power", store=store)
    result = _call(server, "ios_power_get_latest", {})
    assert result["snapshot"]["data"]["level"] == 0.76
    tool = _tools(server)[0]
    assert "owner_id" not in tool.parameters.get("required", [])


def test_calendar_create_queues_native_device_command(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    server = create_mcp_server("ios-calendar", store=store)
    result = _call(
        server,
        "ios_calendar_create",
        {"owner_id": "alice", "payload": {"title": "自习", "start": "2026-07-19T09:00:00+08:00"}},
    )
    commands = store.pull_device_commands("alice", "iphone")["commands"]
    assert result["status"] == "pending"
    assert commands[0]["capability"] == "ios-calendar"
    assert commands[0]["payload"]["title"] == "自习"


def test_unknown_capability_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown capability"):
        create_mcp_server("ios-everything", store=IOSIntelligenceStore(tmp_path))


def test_deployment_config_registers_every_independent_mcp_default_enabled():
    config, changed = merge_ios_mcp_servers(
        {"mcp_servers": {"custom": {"url": "https://example.test/mcp"}}},
        python_executable="/opt/hermes/bin/python",
    )
    servers = config["mcp_servers"]
    assert set(changed) == set(CAPABILITIES)
    assert servers["custom"]["url"] == "https://example.test/mcp"
    assert all(servers[name]["enabled"] is True for name in CAPABILITIES)
    assert servers["ios-location"]["command"] == "/opt/hermes/bin/python"
    assert servers["ios-location"]["args"][-1] == "ios-location"
    assert len({tuple(servers[name]["args"]) for name in CAPABILITIES}) == len(CAPABILITIES)


def test_supervised_http_config_uses_independent_local_endpoints():
    configs = ios_mcp_server_configs(
        transport="streamable-http",
        base_port=9000,
    )

    assert len({configs[name]["url"] for name in CAPABILITIES}) == len(CAPABILITIES)
    assert configs["ios-location"]["url"] == "http://127.0.0.1:9000/mcp"
    assert configs["ios-location"]["skip_preflight"] is True
    assert "command" not in configs["ios-location"]
    assert configs["ios-power"]["manifest"]["transport"] == "streamable-http"


def test_registration_is_idempotent_and_preserves_explicit_disable():
    first, _ = merge_ios_mcp_servers({}, python_executable="python")
    first["mcp_servers"]["ios-notes"]["enabled"] = False
    second, changed = merge_ios_mcp_servers(first, python_executable="python")
    assert second["mcp_servers"]["ios-notes"]["enabled"] is False
    assert changed == []


def test_install_command_persists_discovery_config(monkeypatch):
    from hermes_cli import config as config_module

    saved = {}
    monkeypatch.setattr(config_module, "read_raw_config", lambda: {"model": {"default": "demo"}})
    monkeypatch.setattr(config_module, "save_config", lambda value: saved.update(value))
    result = install_ios_mcp_servers(python_executable="/srv/hermes/.venv/bin/python")
    assert result["count"] == len(CAPABILITIES)
    assert len(result["installed"]) == len(CAPABILITIES)
    assert set(saved["mcp_servers"]) == set(CAPABILITIES)


def test_normal_chat_tool_resolution_includes_all_default_ios_mcps():
    from hermes_cli.tools_config import _get_platform_tools

    config = {"mcp_servers": ios_mcp_server_configs("python")}
    enabled = _get_platform_tools(config, "cli")
    assert set(CAPABILITIES) <= enabled


def test_discovered_schema_keeps_semantic_description_and_optional_owner(tmp_path):
    from tools.mcp_tool import _convert_mcp_schema

    store = IOSIntelligenceStore(tmp_path)
    server = create_mcp_server("ios-location", store=store)
    native_tool = _tools(server)[0]
    wire_tool = type(
        "WireTool",
        (),
        {
            "name": native_tool.name,
            "description": native_tool.description,
            "inputSchema": native_tool.parameters,
        },
    )()
    schema = _convert_mcp_schema("ios-location", wire_tool)
    assert schema["name"] == "mcp__ios_location__current_location"
    assert "where the user is now" in schema["description"]
    assert "owner_id" not in schema["parameters"].get("required", [])


def test_each_mcp_has_scoped_manifest_and_supervisor_registration(tmp_path):
    manifests = ios_mcp_manifests()
    assert set(manifests) == set(CAPABILITIES)
    assert all(item["version"] for item in manifests.values())
    assert all(item["scope"] for item in manifests.values())
    assert all(item["tool_scopes"] for item in manifests.values())
    assert all(item["deployment"]["blue_green"] for item in manifests.values())

    store = IOSIntelligenceStore(tmp_path / "scope-metadata")
    for capability in CAPABILITIES:
        server = create_mcp_server(capability, store=store)
        actual = {
            tool.name: tool.meta["required_scopes"]
            for tool in _tools(server)
        }
        assert actual == manifests[capability]["tool_scopes"]

    from hermes_cli.ios_mcp_supervisor import register_default_mcp_services

    result = register_default_mcp_services(tmp_path / "supervisor.db")
    assert result["count"] == len(CAPABILITIES)
    assert {item["name"] for item in result["services"]} == set(CAPABILITIES)


def test_mcp_runtime_enforces_read_and_write_scope_before_store_access(tmp_path):
    store = IOSIntelligenceStore(tmp_path)
    read_only = create_mcp_server(
        "ios-calendar",
        store=store,
        granted_scopes=("calendar:read",),
    )

    listed = _call(read_only, "ios_calendar_list", {"owner_id": "alice"})
    assert listed == {"items": []}
    with pytest.raises(ToolError, match=r"missing calendar:write"):
        _call(
            read_only,
            "ios_calendar_create",
            {"owner_id": "alice", "payload": {"title": "blocked"}},
        )
    assert store.pull_device_commands("alice", "iphone")["commands"] == []

    write_only = create_mcp_server(
        "ios-calendar",
        store=store,
        granted_scopes=("calendar:write",),
    )
    with pytest.raises(ToolError, match=r"missing calendar:read"):
        _call(write_only, "ios_calendar_list", {"owner_id": "alice"})


def test_mcp_scope_grants_reject_undeclared_permissions_and_survive_merge(tmp_path):
    with pytest.raises(ValueError, match="not declared"):
        create_mcp_server(
            "ios-calendar",
            store=IOSIntelligenceStore(tmp_path),
            granted_scopes=("calendar:admin",),
        )

    initial, _ = merge_ios_mcp_servers({}, python_executable="python")
    initial["mcp_servers"]["ios-calendar"]["granted_scopes"] = ["calendar:read"]
    merged, changed = merge_ios_mcp_servers(initial, python_executable="python")
    calendar = merged["mcp_servers"]["ios-calendar"]

    assert changed == ["ios-calendar"]
    assert calendar["granted_scopes"] == ["calendar:read"]
    assert calendar["args"].count("calendar:read") == 1
    assert "calendar:write" not in calendar["args"]
    assert calendar["args"][-1] == "ios-calendar"

    initial["mcp_servers"]["ios-calendar"]["granted_scopes"] = None
    no_grants, _ = merge_ios_mcp_servers(initial, python_executable="python")
    assert no_grants["mcp_servers"]["ios-calendar"]["granted_scopes"] == []
    assert "calendar:read" not in no_grants["mcp_servers"]["ios-calendar"]["args"]
    assert "calendar:write" not in no_grants["mcp_servers"]["ios-calendar"]["args"]


def test_runtime_supervisor_passes_configured_scope_grants_to_child_process(
    tmp_path,
    monkeypatch,
):
    from hermes_cli.ios_mcp_supervisor import IOSMCPRuntimeSupervisor

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "mcp_servers": {
                "ios-calendar": {"granted_scopes": ["calendar:read"]},
            }
        },
    )
    runtime = IOSMCPRuntimeSupervisor(
        tmp_path / "scope-runtime.db",
        capabilities=("ios-calendar",),
        log_directory=tmp_path / "logs",
    )

    command = runtime.command_for("ios-calendar")
    assert command.count("--grant-scope") == 1
    assert "calendar:read" in command
    assert "calendar:write" not in command
    assert command[-1] == "ios-calendar"


def test_supervised_http_process_enforces_restricted_scope_end_to_end(tmp_path):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from hermes_cli.ios_mcp_supervisor import IOSMCPRuntimeSupervisor

    calendar_port = 0
    for _ in range(100):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            candidate = listener.getsockname()[1]
        base_port = candidate - CAPABILITIES.index("ios-calendar")
        if base_port >= 1024:
            calendar_port = candidate
            break
    assert calendar_port

    data_dir = tmp_path / "restricted-intelligence"
    runtime = IOSMCPRuntimeSupervisor(
        tmp_path / "restricted-runtime.db",
        db_dir=data_dir,
        base_port=base_port,
        health_interval_seconds=60,
        log_directory=tmp_path / "restricted-logs",
        capabilities=("ios-calendar",),
        granted_scopes={"ios-calendar": ("calendar:read",)},
    )

    async def invoke_remote_tools():
        async with streamable_http_client(runtime.endpoint_for("ios-calendar")) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = await session.call_tool(
                    "ios_calendar_list",
                    {"owner_id": "alice"},
                )
                denied = await session.call_tool(
                    "ios_calendar_create",
                    {"owner_id": "alice", "payload": {"title": "blocked"}},
                )
                return listed, denied

    try:
        runtime.start()
        deadline = time.monotonic() + 15
        health = runtime.health_service("ios-calendar")
        while not health.get("ok") and time.monotonic() < deadline:
            time.sleep(0.2)
            health = runtime.health_service("ios-calendar")
        assert health["ok"] is True

        listed, denied = asyncio.run(invoke_remote_tools())
        assert listed.isError is False
        assert denied.isError is True
        assert "missing calendar:write" in denied.content[0].text
        assert IOSIntelligenceStore(data_dir).pull_device_commands(
            "alice",
            "iphone",
        )["commands"] == []
    finally:
        runtime.stop()


def test_supervisor_treats_a_missing_health_callback_as_unhealthy(tmp_path):
    from hermes_cli.ios_mcp_supervisor import IOSMCPSupervisor, MCPState

    supervisor = IOSMCPSupervisor(tmp_path / "missing-health.db", failure_threshold=2)
    supervisor.register("ios-power")

    result = supervisor.health_check("ios-power")

    assert result["healthy"] is False
    assert result["state"] == MCPState.DEGRADED.value


def test_runtime_start_reports_false_when_required_service_fails(tmp_path, monkeypatch):
    from hermes_cli.ios_mcp_supervisor import IOSMCPRuntimeSupervisor

    runtime = IOSMCPRuntimeSupervisor(
        tmp_path / "runtime-start-failure.db",
        capabilities=("ios-power",),
        log_directory=tmp_path / "logs",
    )
    monkeypatch.setattr(runtime, "start_service", lambda _name, verify=True: False)
    monkeypatch.setattr(runtime, "stop_service", lambda _name: True)

    try:
        runtime.start()
        health = runtime.health()
        assert runtime.running is False
        assert health["ok"] is False
        assert health["healthy_count"] == 0
        assert health["required_count"] == 1
        assert health["services"][0]["error"] == "process_missing"
    finally:
        runtime.stop()


def test_runtime_chat_client_reload_requires_a_live_replacement(tmp_path, monkeypatch):
    from hermes_cli import config as config_module
    from hermes_cli.ios_mcp_supervisor import IOSMCPRuntimeSupervisor
    from tools import mcp_tool

    runtime = IOSMCPRuntimeSupervisor(
        tmp_path / "runtime.db",
        capabilities=("ios-power",),
        log_directory=tmp_path / "logs",
    )
    old = SimpleNamespace(
        session=object(),
        _registered_tool_names=["mcp__ios_power__ios_power_get_latest"],
    )
    with mcp_tool._lock:
        mcp_tool._servers["ios-power"] = old
    monkeypatch.setattr(config_module, "read_raw_config", lambda: {
        "mcp_servers": {
            "ios-power": {
                "enabled": True,
                "url": "http://127.0.0.1:9000/mcp",
            },
        },
    })

    def failed_registration(_servers):
        with mcp_tool._lock:
            mcp_tool._servers.pop("ios-power", None)
        return []

    monkeypatch.setattr(mcp_tool, "register_mcp_servers", failed_registration)
    try:
        assert runtime._reload_chat_client("ios-power", required=True) is False
    finally:
        with mcp_tool._lock:
            mcp_tool._servers.pop("ios-power", None)


def test_runtime_supervisor_starts_probes_and_blue_green_upgrades_a_real_mcp_process(tmp_path, monkeypatch):
    from hermes_cli.ios_mcp_supervisor import IOSMCPRuntimeSupervisor

    offset = 25
    power_port = 0
    for _ in range(100):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            candidate = listener.getsockname()[1]
        if candidate + offset >= 65_535:
            continue
        try:
            with socket.socket() as alternate:
                alternate.bind(("127.0.0.1", candidate + offset))
        except OSError:
            continue
        power_port = candidate
        break
    assert power_port
    base_port = power_port - CAPABILITIES.index("ios-power")
    runtime = IOSMCPRuntimeSupervisor(
        tmp_path / "runtime.db",
        db_dir=tmp_path / "intelligence",
        base_port=base_port,
        health_interval_seconds=60,
        log_directory=tmp_path / "logs",
        capabilities=("ios-power",),
        blue_green_port_offset=offset,
        drain_timeout_seconds=0,
    )
    persisted: list[tuple[str, int, str | None]] = []
    monkeypatch.setattr(
        runtime,
        "_persist_discovery_endpoint",
        lambda name, port, version=None: persisted.append((name, port, version)),
    )
    try:
        runtime.start()
        deadline = time.monotonic() + 15
        health = runtime.health_service("ios-power")
        while not health.get("ok") and time.monotonic() < deadline:
            time.sleep(0.2)
            health = runtime.health_service("ios-power")
        assert health["ok"] is True
        assert health["tools"] == ["ios_power_get_latest"]
        old_pid = health["pid"]
        old_endpoint = runtime.endpoint_for("ios-power")

        upgraded = runtime.blue_green_upgrade("ios-power", "1.1.0")
        assert upgraded["upgraded"] is True
        assert upgraded["previous_endpoint"] == old_endpoint
        assert runtime.endpoint_for("ios-power").endswith(f":{power_port + offset}/mcp")
        assert persisted == [("ios-power", power_port + offset, "1.1.0")]
        assert runtime.health_service("ios-power")["pid"] != old_pid

        rolled_back = runtime.blue_green_upgrade(
            "ios-power",
            "1.2.0",
            green_python_executable=str(tmp_path / "missing-python"),
        )
        assert rolled_back["upgraded"] is False
        assert rolled_back["rolled_back"] is True
        assert runtime.endpoint_for("ios-power").endswith(f":{power_port + offset}/mcp")
        assert runtime.health_service("ios-power")["ok"] is True
    finally:
        runtime.stop()


@pytest.mark.integration
def test_all_supervised_http_mcps_are_discovered_by_hermes(tmp_path, monkeypatch):
    """Release gate: 21 real endpoints expose all 44 semantic tools."""
    from hermes_cli.ios_mcp_supervisor import IOSMCPRuntimeSupervisor
    from tools import mcp_tool

    count = len(CAPABILITIES)
    base_port = 0
    for candidate in range(20_000, 60_000, count + 3):
        listeners = []
        try:
            for port in range(candidate, candidate + count):
                listener = socket.socket()
                listener.bind(("127.0.0.1", port))
                listeners.append(listener)
        except OSError:
            pass
        else:
            base_port = candidate
            break
        finally:
            for listener in listeners:
                listener.close()
    assert base_port

    configs = ios_mcp_server_configs(
        transport="streamable-http",
        base_port=base_port,
    )
    runtime = IOSMCPRuntimeSupervisor(
        tmp_path / "all-runtime.db",
        db_dir=tmp_path / "all-intelligence",
        base_port=base_port,
        health_interval_seconds=120,
        log_directory=tmp_path / "all-logs",
    )
    mcp_tool.shutdown_mcp_servers()
    monkeypatch.setattr(mcp_tool, "_load_mcp_config", lambda: configs)
    try:
        runtime.start()
        healthy: dict[str, list[str]] = {}
        deadline = time.monotonic() + 75
        while len(healthy) < count and time.monotonic() < deadline:
            missing = [name for name in CAPABILITIES if name not in healthy]
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                outcomes = dict(zip(missing, executor.map(runtime.health_service, missing)))
            for name, result in outcomes.items():
                if result.get("ok"):
                    healthy[name] = result["tools"]
            if len(healthy) < count:
                time.sleep(0.5)

        assert len(healthy) == count
        assert sum(len(tools) for tools in healthy.values()) == 44

        names = mcp_tool.discover_mcp_tools()
        status = mcp_tool.get_mcp_status()
        assert len(names) == 44
        assert len(status) == count
        assert all(item["connected"] for item in status)
        assert mcp_tool._server_connect_errors == {}
    finally:
        mcp_tool.shutdown_mcp_servers()
        runtime.stop()


def test_runtime_supervisor_does_not_start_disabled_or_quarantined_services(tmp_path, monkeypatch):
    from hermes_cli.ios_mcp_supervisor import IOSMCPRuntimeSupervisor, MCPState

    runtime = IOSMCPRuntimeSupervisor(
        tmp_path / "runtime.db",
        db_dir=tmp_path / "intelligence",
        capabilities=("ios-power",),
        log_directory=tmp_path / "logs",
    )
    runtime.supervisor.register("ios-power")
    runtime.supervisor.disable("ios-power", "operator disabled")
    starts: list[str] = []
    monkeypatch.setattr(runtime, "start_service", lambda name, verify=False: starts.append(name) or True)
    try:
        runtime.start()
        assert starts == []
        assert runtime.supervisor.status("ios-power")["state"] == MCPState.DISABLED.value
    finally:
        runtime.stop()


def test_runtime_supervisor_honors_explicit_config_disable(tmp_path, monkeypatch):
    from hermes_cli.ios_mcp_supervisor import IOSMCPRuntimeSupervisor, MCPState

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"mcp_servers": {"ios-power": {"enabled": False}}},
    )
    runtime = IOSMCPRuntimeSupervisor(
        tmp_path / "runtime.db",
        db_dir=tmp_path / "intelligence.db",
        owner_id="alice@example.test",
        capabilities=("ios-power",),
        log_directory=tmp_path / "logs",
    )
    starts: list[str] = []
    monkeypatch.setattr(runtime, "start_service", lambda name, verify=False: starts.append(name) or True)
    try:
        runtime.start()
        assert starts == []
        assert runtime.owner_id == "alice@example.test"
        assert runtime.supervisor.status("ios-power")["state"] == MCPState.DISABLED.value
    finally:
        runtime.stop()
