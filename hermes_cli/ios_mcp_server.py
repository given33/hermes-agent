"""Independent MCP processes for native iOS capabilities.

Run one capability per process, for example::

    python -m hermes_cli.ios_mcp_server ios-location

Keeping the tool descriptions specific is intentional: ordinary Hermes chat
can autonomously select a relevant MCP tool from the user's request without a
special command or prompt rewrite.
"""

from __future__ import annotations

import argparse
import copy
from functools import wraps
import os
from pathlib import Path
import sys
import time
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from hermes_cli.ios_intelligence import (
    AMapClient,
    IOSIntelligenceStore,
    ios_native_action_metadata,
    QWeatherClient,
    load_ios_feature_weights,
)
from hermes_cli.ios_intelligence_config import load_ios_intelligence_config

if TYPE_CHECKING:
    from mcp.server import MCPServer


CAPABILITIES = (
    "ios-location", "ios-trajectory", "ios-places", "ios-motion", "ios-behavior",
    "qweather", "amap-route", "ios-map", "ios-power", "ios-health-sleep",
    "ios-health-heart", "ios-health-oxygen", "ios-health-activity", "ios-health-write", "ios-calendar",
    "ios-reminders", "ios-clipboard", "ios-contacts", "ios-photos", "ios-media",
    "ios-vision", "ios-bluetooth", "ios-nfc", "ios-homekit", "ios-notes", "ios-screen-time", "ios-watch", "ios-notification",
    "ios-live-activity", "ios-browser", "ios-device", "ios-alarm", "ios-nlp",
)

MCP_VERSION = "1.0.0"
_SCOPE_BY_CAPABILITY = {
    "qweather": ("weather:read",),
    "amap-route": ("route:read",),
    "ios-location": ("location:read", "location:refresh"),
    "ios-trajectory": ("trajectory:read",),
    "ios-places": ("places:read",),
    "ios-motion": ("motion:read", "motion:control"),
    "ios-behavior": ("behavior:read",),
    "ios-map": ("map:read",),
    "ios-power": ("power:read",),
    "ios-health-sleep": ("health:sleep:read",),
    "ios-health-heart": ("health:heart:read",),
    "ios-health-oxygen": ("health:oxygen:read",),
    "ios-health-activity": ("health:activity:read",),
    "ios-calendar": ("calendar:read", "calendar:write"),
    "ios-reminders": ("reminders:read", "reminders:write"),
    "ios-alarm": ("reminders:read", "reminders:write", "notification:send"),
    "ios-nlp": ("nlp:read",),
    "ios-clipboard": ("clipboard:read", "clipboard:write"),
    "ios-contacts": ("contacts:read", "contacts:write"),
    "ios-photos": ("photos:read", "photos:write", "camera:write"),
    "ios-vision": ("photos:read",),
    "ios-media": ("media:read", "media:control"),
    "ios-bluetooth": ("bluetooth:read", "bluetooth:control"),
    "ios-nfc": ("nfc:read", "nfc:write"),
    "ios-homekit": ("homekit:read", "homekit:control"),
    "ios-notes": ("notes:share",),
    "ios-screen-time": ("screen-time:read", "screen-time:control"),
    "ios-watch": ("watch:read", "watch:control"),
    "ios-notification": ("notification:send",),
    "ios-live-activity": ("live-activity:write",),
    "ios-browser": ("browser:read", "browser:control"),
    "ios-device": ("device:read", "device:control"),
    "ios-health-write": ("health:write",),
}

_TOOL_SCOPE_BY_CAPABILITY = {
    "ios-location": {"current_location": ("location:read", "location:refresh")},
    "ios-trajectory": {"trajectory_today": ("trajectory:read",)},
    "ios-places": {"places_today": ("places:read",)},
    "ios-motion": {
        "ios_motion_get_latest": ("motion:read",),
        "ios_motion_start": ("motion:control",),
        "ios_motion_stop": ("motion:control",),
    },
    "ios-behavior": {"behavior_predict": ("behavior:read",)},
    "qweather": {
        "weather_minutely": ("weather:read",),
        "weather_current": ("weather:read",),
        "weather_hourly": ("weather:read",),
        "weather_warnings": ("weather:read",),
    },
    "amap-route": {
        "amap_reverse_geocode": ("route:read",),
        "amap_plan_route": ("route:read",),
        "amap_search_poi": ("route:read",),
    },
    "ios-map": {"ios_map_get_today": ("map:read",)},
    "ios-power": {"ios_power_get_latest": ("power:read",)},
    "ios-health-sleep": {
        "ios_health_sleep_get_latest": ("health:sleep:read",),
        "ios_health_sleep_get_history": ("health:sleep:read",),
    },
    "ios-health-heart": {
        "ios_health_heart_get_latest": ("health:heart:read",),
        "ios_health_heart_get_history": ("health:heart:read",),
    },
    "ios-health-oxygen": {
        "ios_health_oxygen_get_latest": ("health:oxygen:read",),
        "ios_health_oxygen_get_history": ("health:oxygen:read",),
    },
    "ios-health-activity": {
        "ios_health_activity_get_latest": ("health:activity:read",),
        "ios_health_activity_get_history": ("health:activity:read",),
    },
    "ios-calendar": {
        "ios_calendar_list": ("calendar:read",),
        "ios_calendar_calendars": ("calendar:read",),
        "ios_calendar_freebusy": ("calendar:read",),
        "ios_calendar_create": ("calendar:write",),
        "ios_calendar_update": ("calendar:write",),
        "ios_calendar_delete": ("calendar:write",),
    },
    "ios-reminders": {
        "ios_reminders_list": ("reminders:read",),
        "ios_reminder_create": ("reminders:write",),
        "ios_reminder_update": ("reminders:write",),
        "ios_reminder_complete": ("reminders:write",),
        "ios_reminder_delete": ("reminders:write",),
    },
    "ios-alarm": {
        "ios_alarm_schedule": ("reminders:write", "notification:send"),
        "ios_alarm_list": ("reminders:read",),
        "ios_alarm_cancel": ("reminders:write", "notification:send"),
    },
    "ios-nlp": {"ios_nlp_analyze": ("nlp:read",)},
    "ios-clipboard": {
        "ios_clipboard_read": ("clipboard:read",),
        "ios_clipboard_write": ("clipboard:write",),
    },
    "ios-contacts": {
        "ios_contacts_list": ("contacts:read",),
        "ios_contacts_search": ("contacts:read",),
        "ios_contacts_create": ("contacts:write",),
    },
    "ios-photos": {
        "ios_photos_list": ("photos:read",),
        "ios_photos_search": ("photos:read",),
        "ios_photos_capture": ("camera:write",),
        "ios_photos_scan": ("camera:write",),
        "ios_photos_ocr": ("photos:read",),
        "ios_photos_albums": ("photos:read",),
        "ios_photos_near": ("photos:read",),
        "ios_photos_export": ("photos:read",),
        "ios_photos_favorite": ("photos:write",),
        "ios_photos_delete": ("photos:write",),
        "ios_photos_album_create": ("photos:write",),
        "ios_photos_album_add": ("photos:write",),
        "ios_photos_import": ("photos:write",),
    },
    "ios-vision": {
        "ios_vision_analyze": ("photos:read",),
        "ios_vision_classify": ("photos:read",),
        "ios_vision_detect": ("photos:read",),
        "ios_vision_faces": ("photos:read",),
    },
    "ios-media": {
        "ios_media_get": ("media:read",),
        "ios_media_control": ("media:control",),
        "ios_media_search": ("media:read",),
        "ios_media_play_search": ("media:control",),
        "ios_media_volume": ("media:control",),
    },
    "ios-bluetooth": {
        "ios_bluetooth_state": ("bluetooth:read",),
        "ios_bluetooth_scan": ("bluetooth:read",),
        "ios_bluetooth_connect": ("bluetooth:control",),
        "ios_bluetooth_disconnect": ("bluetooth:control",),
        "ios_bluetooth_services": ("bluetooth:read",),
        "ios_bluetooth_read": ("bluetooth:read",),
        "ios_bluetooth_write": ("bluetooth:control",),
        "ios_bluetooth_notify": ("bluetooth:read",),
    },
    "ios-nfc": {"ios_nfc_scan": ("nfc:read",), "ios_nfc_write": ("nfc:write",)},
    "ios-homekit": {
        "ios_homekit_list": ("homekit:read",),
        "ios_homekit_set": ("homekit:control",),
        "ios_homekit_search": ("homekit:read",),
        "ios_homekit_scenes": ("homekit:read",),
        "ios_homekit_trigger": ("homekit:control",),
    },
    "ios-notes": {"ios_notes_share_text": ("notes:share",)},
    "ios-screen-time": {
        "ios_screen_time_get_latest": ("screen-time:read",),
        "ios_screen_time_authorize": ("screen-time:control",),
        "ios_screen_time_start": ("screen-time:control",),
        "ios_screen_time_stop": ("screen-time:control",),
    },
    "ios-watch": {
        "ios_watch_get_latest": ("watch:read",),
        "ios_watch_send": ("watch:control",),
        "ios_watch_start_active_relay": ("watch:control",),
        "ios_watch_stop_active_relay": ("watch:control",),
    },
    "ios-notification": {
        "ios_notification_send": ("notification:send",),
        "ios_notification_schedule": ("notification:send",),
        "ios_notification_cancel": ("notification:send",),
    },
    "ios-live-activity": {
        "ios_live_activity_update": ("live-activity:write",),
        "ios_live_activity_start": ("live-activity:write",),
        "ios_live_activity_end": ("live-activity:write",),
    },
    "ios-device": {
        "ios_device_get_latest": ("device:read",),
        "ios_device_open_url": ("device:control",),
    },
    "ios-browser": {
        "ios_browser_navigate": ("browser:read",),
        "ios_browser_screenshot": ("browser:read",),
        "ios_browser_get_text": ("browser:read",),
        "ios_browser_get_page_info": ("browser:read",),
        "ios_browser_find_elements": ("browser:read",),
        "ios_browser_get_readable": ("browser:read",),
        "ios_browser_get_backbone": ("browser:read",),
        "ios_browser_list_tabs": ("browser:read",),
        "ios_browser_get_cookies": ("browser:read",),
        "ios_browser_fetch": ("browser:read",),
        "ios_browser_wait_for_dom_stable": ("browser:read",),
        "ios_browser_click": ("browser:control",),
        "ios_browser_type": ("browser:control",),
        "ios_browser_scroll": ("browser:control",),
        "ios_browser_hover": ("browser:control",),
        "ios_browser_execute_js": ("browser:control",),
        "ios_browser_set_user_agent": ("browser:control",),
        "ios_browser_set_viewport": ("browser:control",),
        "ios_browser_new_tab": ("browser:control",),
        "ios_browser_close_tab": ("browser:control",),
        "ios_browser_set_cookies": ("browser:control",),
        "ios_browser_scroll_and_collect": ("browser:control",),
    },
    "ios-health-write": {
        "ios_health_write_authorize": ("health:write",),
        "ios_health_write": ("health:write",),
        "ios_health_write_batch": ("health:write",),
        "ios_health_delete": ("health:write",),
    },
}


class IOSMCPScopeError(PermissionError):
    """Raised before a tool crosses a capability's configured scope boundary."""


def normalize_mcp_scope_grants(
    capability: str,
    scopes: Iterable[str] | str | None,
) -> tuple[str, ...]:
    """Validate and canonicalize grants against a capability manifest."""

    normalized_capability = str(capability or "").strip().lower()
    if normalized_capability not in CAPABILITIES:
        raise ValueError(f"Unknown capability {normalized_capability!r}")
    declared = _SCOPE_BY_CAPABILITY[normalized_capability]
    if scopes is None:
        return declared
    raw_scopes = scopes.split(",") if isinstance(scopes, str) else scopes
    requested = {str(scope or "").strip() for scope in raw_scopes}
    requested.discard("")
    unknown = requested - set(declared)
    if unknown:
        raise ValueError(
            f"Scopes not declared by {normalized_capability}: {', '.join(sorted(unknown))}"
        )
    return tuple(scope for scope in declared if scope in requested)


class _ScopeEnforcer:
    def __init__(self, capability: str, granted_scopes: Iterable[str]) -> None:
        self.capability = capability
        self.granted_scopes = frozenset(granted_scopes)

    def require_tool(self, tool_name: str) -> None:
        required = _TOOL_SCOPE_BY_CAPABILITY[self.capability][tool_name]
        missing = set(required) - self.granted_scopes
        if missing:
            raise IOSMCPScopeError(
                f"scope denied for {self.capability}/{tool_name}; "
                f"missing {', '.join(sorted(missing))}"
            )


def _register_scoped_tool(
    mcp: MCPServer,
    enforcer: _ScopeEnforcer,
    capability: str,
    fn: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
) -> None:
    tool_name = name or str(getattr(fn, "__name__", ""))
    if not tool_name:
        raise ValueError("scoped MCP tools require a name")
    required = _TOOL_SCOPE_BY_CAPABILITY[capability][tool_name]

    @wraps(fn)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        enforcer.require_tool(tool_name)
        return fn(*args, **kwargs)

    mcp.tool(
        name=name,
        description=description,
        meta={"required_scopes": list(required)},
    )(guarded)


def ios_mcp_manifests(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    base_port: int = 8760,
) -> dict[str, dict[str, Any]]:
    """Return the deployable contract for every isolated MCP process."""

    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("MCP transport must be 'stdio' or 'streamable-http'")

    return {
        capability: {
            "name": capability,
            "version": MCP_VERSION,
            "transport": transport,
            "endpoint": (
                f"http://{host}:{base_port + index}/mcp"
                if transport == "streamable-http"
                else f"stdio://hermes/{capability}"
            ),
            "scope": list(_SCOPE_BY_CAPABILITY.get(capability, ())),
            "tool_scopes": {
                tool_name: list(scopes)
                for tool_name, scopes in _TOOL_SCOPE_BY_CAPABILITY[capability].items()
            },
            "health": {"command": "tools/list", "timeout_seconds": 10},
            # The dashboard can advertise the complete MCP contract without
            # starting the isolated service.  The client uses these schemas
            # for tool registration and the runtime proxy starts the service
            # only when a tool call is actually made.
            "tool_definitions": ios_mcp_tool_definitions(capability),
            "log_namespace": f"hermes.mcp.{capability}",
            "deployment": {"supervised": True, "blue_green": True, "rollback": True},
        }
        for index, capability in enumerate(CAPABILITIES)
    }


def ios_mcp_tool_definitions(capability: str) -> list[dict[str, Any]]:
    """Return serializable tool schemas without starting an MCP server.

    ``create_mcp_server`` only registers Python functions, so constructing it
    here is side-effect free and gives the dashboard the same input schemas
    that a live ``tools/list`` response would expose.
    """

    server = create_mcp_server(capability)
    manager = getattr(server, "_tool_manager", None)
    tools = getattr(manager, "_tools", {}) if manager is not None else {}
    definitions: list[dict[str, Any]] = []
    for tool in (tools.values() if isinstance(tools, Mapping) else ()):
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        parameters = getattr(tool, "parameters", None)
        if not isinstance(parameters, Mapping):
            parameters = {"type": "object", "properties": {}}
        definitions.append(
            {
                "name": name,
                "description": str(getattr(tool, "description", "") or ""),
                "input_schema": dict(parameters),
            }
        )
    return definitions


def ios_mcp_server_configs(
    python_executable: str | None = None,
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    base_port: int = 8760,
) -> dict[str, dict[str, Any]]:
    """Return the default-enabled stdio entries consumed by Hermes discovery."""

    executable = str(python_executable or sys.executable)
    manifests = ios_mcp_manifests(transport=transport, host=host, base_port=base_port)
    configs: dict[str, dict[str, Any]] = {}
    for capability in CAPABILITIES:
        granted_scopes = normalize_mcp_scope_grants(capability, None)
        common = {
            "enabled": True,
            "connect_timeout": 20,
            "timeout": 60,
            "tools": {"resources": False, "prompts": False},
            "manifest": manifests[capability],
            "granted_scopes": list(granted_scopes),
            "lazy_start": transport == "streamable-http",
        }
        if transport == "streamable-http":
            configs[capability] = {
                **common,
                "url": manifests[capability]["endpoint"],
                # These endpoints are created and health-checked by the local
                # runtime supervisor. Avoid an extra HEAD/GET preflight for all
                # 21 processes during every Hermes cold start.
                "skip_preflight": True,
            }
        else:
            args = ["-m", "hermes_cli.ios_mcp_server"]
            for scope in granted_scopes:
                args.extend(["--grant-scope", scope])
            args.append(capability)
            configs[capability] = {
                **common,
                "command": executable,
                "args": args,
            }
    if transport != "streamable-http":
        configs["qweather"]["env"] = {
            "HERMES_QWEATHER_API_KEY": "${HERMES_QWEATHER_API_KEY}",
        }
        configs["amap-route"]["env"] = {
            "HERMES_AMAP_WEB_API_KEY": "${HERMES_AMAP_WEB_API_KEY}",
        }
    return configs


def merge_ios_mcp_servers(
    config: Mapping[str, Any] | None,
    *,
    python_executable: str | None = None,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    base_port: int = 8760,
) -> tuple[dict[str, Any], list[str]]:
    """Idempotently install managed entries while preserving explicit disables."""

    merged = copy.deepcopy(dict(config or {}))
    current = merged.get("mcp_servers")
    servers = copy.deepcopy(current) if isinstance(current, dict) else {}
    changed: list[str] = []
    for capability, desired in ios_mcp_server_configs(
        python_executable,
        transport=transport,
        host=host,
        base_port=base_port,
    ).items():
        prior = servers.get(capability)
        if isinstance(prior, dict):
            desired["enabled"] = prior.get("enabled", True)
            if "granted_scopes" in prior:
                raw_grants = prior.get("granted_scopes")
                granted_scopes = normalize_mcp_scope_grants(
                    capability,
                    () if raw_grants is None else raw_grants,
                )
                desired["granted_scopes"] = list(granted_scopes)
                if "command" in desired:
                    args = ["-m", "hermes_cli.ios_mcp_server"]
                    for scope in granted_scopes:
                        args.extend(["--grant-scope", scope])
                    args.append(capability)
                    desired["args"] = args
        if prior != desired:
            servers[capability] = desired
            changed.append(capability)
    merged["mcp_servers"] = servers
    return merged, changed


def install_ios_mcp_servers(
    *,
    python_executable: str | None = None,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    base_port: int = 8760,
) -> dict[str, Any]:
    """Persist all independent MCPs so ordinary Hermes chats discover them."""

    from hermes_cli.config import read_raw_config, save_config

    config, changed = merge_ios_mcp_servers(
        read_raw_config(),
        python_executable=python_executable,
        transport=transport,
        host=host,
        base_port=base_port,
    )
    if changed:
        save_config(config)
    return {"installed": changed, "count": len(CAPABILITIES)}

_LATEST_KINDS = {
    "ios-motion": "motion",
    "ios-power": "power",
    "ios-health-sleep": "health-sleep",
    "ios-health-heart": "health-heart",
    "ios-health-oxygen": "health-oxygen",
    "ios-health-activity": "health-activity",
    "ios-screen-time": "screen-time",
    "ios-watch": "watch",
    "ios-device": "device",
}

_LATEST_DESCRIPTIONS = {
    "ios-motion": "Use automatically when the user asks whether they are moving, walking, cycling, driving, or stationary, or when movement affects an answer.",
    "ios-power": "Use automatically when the user asks about iPhone battery level, charging state, or Low Power Mode, or when power state affects a plan.",
    "ios-health-sleep": "Use automatically for questions or planning that depend on the user's recent sleep duration or sleep stages.",
    "ios-health-heart": "Use automatically for questions that need the user's latest heart-rate, resting-heart-rate, or heart trend data.",
    "ios-health-oxygen": "Use automatically for questions that need the user's latest HealthKit blood-oxygen data.",
    "ios-health-activity": "Use automatically for questions that need steps, workouts, exercise, or activity data from HealthKit.",
    "ios-screen-time": "Use automatically when the user asks about their app/device usage or when recent screen activity is relevant to behavior context.",
    "ios-watch": "Use automatically when an answer needs the latest Apple Watch connectivity, workout, location, or sensor summary.",
    "ios-device": "Use automatically when the user asks about this device, iOS version, capabilities, permissions, connectivity, or native feature availability.",
}

_DEVICE_PERMISSION_BY_CAPABILITY = {
    "ios-location": "location",
    "ios-trajectory": "location",
    "ios-places": "location",
    "ios-map": "location",
    "ios-motion": "motion",
    "ios-health-sleep": "health",
    "ios-health-heart": "health",
    "ios-health-oxygen": "health",
    "ios-health-activity": "health",
    "ios-health-write": "health",
    "ios-calendar": "calendar",
    "ios-reminders": "reminders",
    "ios-alarm": "reminders",
    "ios-screen-time": "screenTime",
}


def _require_device_permission(
    store: IOSIntelligenceStore,
    owner_id: str,
    capability: str,
) -> None:
    """Fail closed when the latest iPhone snapshot does not prove OS access."""

    permission = _DEVICE_PERMISSION_BY_CAPABILITY.get(capability)
    if permission is None:
        return
    device = store.latest_snapshot(owner_id, "device")
    data = (device or {}).get("data")
    permissions = data.get("permissions") if isinstance(data, Mapping) else None
    if not isinstance(permissions, Mapping):
        raise PermissionError(
            f"No synchronized iOS permission state is available for {capability}"
        )
    state = str(permissions.get(permission) or "").strip()
    allowed = state == "authorized" or (
        permission in {"health", "location"} and state == "limited"
    )
    if permission == "location":
        allowed = allowed and permissions.get("locationAlways") is True
    if not allowed:
        raise PermissionError(
            f"The current iOS {permission} permission does not allow {capability}"
        )


def _resolve_location(store: IOSIntelligenceStore, owner_id: str, latitude: float | None, longitude: float | None) -> tuple[float, float]:
    if latitude is not None and longitude is not None:
        return float(latitude), float(longitude)
    _require_device_permission(store, owner_id, "ios-location")
    snapshot = store.latest_snapshot(owner_id, "location")
    data = (snapshot or {}).get("data", {})
    lat = data.get("latitude", data.get("lat"))
    lon = data.get("longitude", data.get("lon", data.get("lng")))
    if lat is None or lon is None:
        raise ValueError("No current iOS location is available for this account")
    return float(lat), float(lon)


def _resolve_owner(store: IOSIntelligenceStore, owner_id: str = "") -> str:
    """Resolve the account for an MCP tool call.

    Multi-user hosts must bind each MCP process with HERMES_IOS_OWNER_ID (or
    HERMES_OWNER_EMAIL). Auto-picking the only active account is fail-closed:
    private calendar/health/location must never leak across chat owners when a
    host later gains a second account or serves multiple users from one DB.
    """

    explicit = str(owner_id or "").strip()
    configured = str(os.getenv("HERMES_IOS_OWNER_ID") or os.getenv("HERMES_OWNER_EMAIL") or "").strip()
    if configured:
        if explicit and explicit != configured:
            raise PermissionError("This MCP process is bound to a different account")
        return configured
    if explicit:
        # Unbound process: require the tool argument and never invent an owner
        # from active_accounts(). Verify the account has synchronized data so
        # typos fail closed rather than creating ghost queues.
        accounts = store.active_accounts()
        if accounts and explicit not in accounts:
            raise PermissionError("This MCP process is bound to a different account")
        return explicit
    raise RuntimeError(
        "iOS MCP owner is unbound; set HERMES_IOS_OWNER_ID on the MCP process "
        "or pass owner_id for a single-account operator binding"
    )


def _register_latest(
    mcp: MCPServer,
    enforcer: _ScopeEnforcer,
    capability: str,
    store: IOSIntelligenceStore,
) -> None:
    kind = _LATEST_KINDS[capability]
    tool_name = capability.replace("-", "_") + "_get_latest"

    def get_latest(owner_id: str = "") -> dict[str, Any]:
        resolved_owner = _resolve_owner(store, owner_id)
        _require_device_permission(store, resolved_owner, capability)
        return {"snapshot": store.latest_snapshot(resolved_owner, kind)}

    _register_scoped_tool(
        mcp,
        enforcer,
        capability,
        get_latest,
        name=tool_name,
        description=_LATEST_DESCRIPTIONS[capability],
    )


def _register_history(
    mcp: MCPServer,
    enforcer: _ScopeEnforcer,
    *,
    capability: str,
    tool_name: str,
    kind: str,
    description: str,
    store: IOSIntelligenceStore,
) -> None:
    def get_history(owner_id: str = "", limit: int = 100) -> dict[str, Any]:
        resolved_owner = _resolve_owner(store, owner_id)
        _require_device_permission(store, resolved_owner, capability)
        return {"items": store.list_snapshots(resolved_owner, kind, limit)}

    _register_scoped_tool(
        mcp,
        enforcer,
        capability,
        get_history,
        name=tool_name,
        description=description,
    )


def _queue_tool(
    mcp: MCPServer,
    enforcer: _ScopeEnforcer,
    store: IOSIntelligenceStore,
    capability: str,
    name: str,
    action: str,
    description: str,
) -> None:
    action_metadata = ios_native_action_metadata(capability, action)

    def queue(payload: dict[str, Any] | None = None, owner_id: str = "", device_id: str = "", idempotency_key: str = "") -> dict[str, Any]:
        owner_id = _resolve_owner(store, owner_id)
        if action_metadata["risk"] in {"write", "destructive"} and not str(idempotency_key or "").strip():
            raise ValueError(f"idempotency_key is required for {capability}:{action}")
        return store.queue_device_command(
            owner_id, capability, action, payload or {}, device_id=device_id,
            idempotency_key=idempotency_key,
        )

    _register_scoped_tool(
        mcp,
        enforcer,
        capability,
        queue,
        name=name,
        description=description,
    )


def create_mcp_server(
    capability: str,
    *,
    store: IOSIntelligenceStore | None = None,
    qweather: QWeatherClient | None = None,
    amap: AMapClient | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    granted_scopes: Iterable[str] | str | None = None,
) -> MCPServer:
    """Build a server exposing tools for exactly one capability."""

    from mcp.server import MCPServer as FastMCP

    capability = str(capability).strip().lower()
    if capability not in CAPABILITIES:
        raise ValueError(f"Unknown capability {capability!r}; choose one of: {', '.join(CAPABILITIES)}")
    effective_scopes = normalize_mcp_scope_grants(capability, granted_scopes)
    enforcer = _ScopeEnforcer(capability, effective_scopes)
    store = store or IOSIntelligenceStore(os.getenv("HERMES_IOS_INTELLIGENCE_DIR") or None)
    mcp = FastMCP(
        capability,
        instructions=(
            f"Native iOS capability: {capability}. These tools contain the account's live, "
            "device-synchronized context. Hermes should call a relevant tool automatically "
            "when the user's normal chat request depends on this information; the user does "
            "not need to ask for an MCP call explicitly. Never invent missing device data."
        ),
    )
    mcp._http_runtime_options = {
        "host": host,
        "port": port,
        "stateless_http": True,
    }  # type: ignore[attr-defined]
    mcp.granted_scopes = effective_scopes  # type: ignore[attr-defined]

    if capability in _LATEST_KINDS:
        _register_latest(mcp, enforcer, capability, store)
        health_history = {
            "ios-health-sleep": ("ios_health_sleep_get_history", "health-sleep"),
            "ios-health-heart": ("ios_health_heart_get_history", "health-heart"),
            "ios-health-oxygen": ("ios_health_oxygen_get_history", "health-oxygen"),
            "ios-health-activity": ("ios_health_activity_get_history", "health-activity"),
        }.get(capability)
        if health_history:
            _register_history(
                mcp,
                enforcer,
                capability=capability,
                tool_name=health_history[0],
                kind=health_history[1],
                store=store,
                description=f"Use when the user asks for a recent trend or history, not only the latest {health_history[1]} reading.",
            )
        if capability == "ios-motion":
            _queue_tool(
                mcp, enforcer, store, capability, "ios_motion_start", "start",
                "Use when current movement context is needed and native motion collection should start on the iPhone.",
            )
            _queue_tool(
                mcp, enforcer, store, capability, "ios_motion_stop", "stop",
                "Use when the user explicitly asks Hermes to stop native iPhone motion collection.",
            )
        if capability == "ios-watch":
            _queue_tool(
                mcp, enforcer, store, capability, "ios_watch_start_active_relay", "start-active-relay",
                "Use when the user starts a walk, run, ride, workout, or navigation and wants Apple Watch location and motion relayed in the background. Payload may contain activity.",
            )
            _queue_tool(
                mcp, enforcer, store, capability, "ios_watch_stop_active_relay", "stop-active-relay",
                "Use when the user asks to stop the active Apple Watch workout or navigation relay.",
            )
            _queue_tool(
                mcp, enforcer, store, capability, "ios_watch_send", "send",
                "Use when a normal chat request needs a structured command delivered to the paired Apple Watch.",
            )
        if capability == "ios-screen-time":
            _queue_tool(
                mcp, enforcer, store, capability, "ios_screen_time_authorize", "authorize",
                "Use when the user asks Hermes to enable native Screen Time context collection.",
            )
            _queue_tool(
                mcp, enforcer, store, capability, "ios_screen_time_start", "start",
                "Use when native Device Activity monitoring should start; payload may include identifier, startHour, and endHour.",
            )
            _queue_tool(
                mcp, enforcer, store, capability, "ios_screen_time_stop", "stop",
                "Use when the user asks Hermes to stop a named native Device Activity monitor.",
            )
        if capability == "ios-device":
            def ios_device_open_url(
                url: str,
                owner_id: str = "",
                device_id: str = "",
                idempotency_key: str = "",
                confirmed: bool = False,
                confirmation_token: str = "",
            ) -> dict[str, Any]:
                normalized = str(url or "").strip()
                parsed = urlparse(normalized)
                allowed = {"http", "https", "mailto", "tel", "facetime", "maps", "shortcuts", "app-settings"}
                if len(normalized) > 2048 or parsed.scheme.lower() not in allowed:
                    raise ValueError("url must use an allowed iOS URL scheme")
                if parsed.scheme.lower() in {"http", "https", "facetime", "maps", "shortcuts"} and not parsed.netloc:
                    raise ValueError("url host is required")
                if not str(idempotency_key or "").strip():
                    raise ValueError("idempotency_key is required for ios_device_open_url")
                payload: dict[str, Any] = {"url": normalized}
                if confirmed:
                    payload["confirmed"] = True
                token = str(confirmation_token or "").strip()
                if token:
                    payload["confirmation_token"] = token[:512]
                owner = _resolve_owner(store, owner_id)
                return store.queue_device_command(
                    owner, capability, "open-url", payload,
                    device_id=device_id, idempotency_key=idempotency_key,
                )

            _register_scoped_tool(
                mcp, enforcer, capability, ios_device_open_url,
                name="ios_device_open_url",
                description="Use when the user asks Hermes to open a safe URL, app deep link, or the iOS Settings screen on the user's iPhone.",
            )
        return mcp

    if capability == "ios-location":
        def current_location(owner_id: str = "", refresh_if_older_than_seconds: int = 300) -> dict[str, Any]:
            """Use automatically whenever a normal chat request depends on where the user is now, nearby conditions, or location-aware help."""
            owner_id = _resolve_owner(store, owner_id)
            _require_device_permission(store, owner_id, capability)
            snapshot = store.latest_snapshot(owner_id, "location")
            stale = snapshot is None or int(snapshot["observed_at"]) < time.time() - max(0, refresh_if_older_than_seconds)
            command = None
            if stale:
                command = store.queue_device_command(
                    owner_id, "ios-location", "refresh", {"accuracy": "best"},
                    idempotency_key=f"location-refresh:{owner_id}:{int(time.time() // 60)}",
                )
            return {"location": snapshot, "refresh_queued": command}

        _register_scoped_tool(mcp, enforcer, capability, current_location)
        return mcp

    if capability == "ios-trajectory":
        def trajectory_today(owner_id: str = "", timezone: str = "") -> dict[str, Any]:
            """Use automatically when the user asks where they traveled today, what path they took, or when route history is relevant."""
            owner_id = _resolve_owner(store, owner_id)
            _require_device_permission(store, owner_id, capability)
            tz = timezone or load_ios_intelligence_config().timezone
            today = store.today_snapshot(owner_id, tz)
            return {"date": today["date"], "timezone": today["timezone"], "trajectory": today["trajectory"]}

        _register_scoped_tool(mcp, enforcer, capability, trajectory_today)
        return mcp

    if capability == "ios-places":
        def places_today(owner_id: str = "", timezone: str = "") -> dict[str, Any]:
            """Use automatically when a chat request depends on places the user visited today or their arrival, departure, and dwell times."""
            owner_id = _resolve_owner(store, owner_id)
            _require_device_permission(store, owner_id, capability)
            tz = timezone or load_ios_intelligence_config().timezone
            today = store.today_snapshot(owner_id, tz)
            return {"date": today["date"], "timezone": today["timezone"], "places": today["places"]}

        _register_scoped_tool(mcp, enforcer, capability, places_today)
        return mcp

    if capability == "ios-behavior":
        def behavior_predict(owner_id: str = "") -> dict[str, Any]:
            """Use automatically for proactive weather decisions and normal chat that needs the user's learned routine, likely departure, or behavior context."""
            owner_id = _resolve_owner(store, owner_id)
            _require_device_permission(store, owner_id, capability)
            config = load_ios_intelligence_config()
            # Align with the scheduler path: apply MCP health weights and fail
            # closed when the supervisor is unavailable so stale sensors are
            # not treated as full trust.
            feature_weights = load_ios_feature_weights()
            behavior = store.evaluate_behavior(
                owner_id,
                feature_weights=feature_weights,
                timezone=config.timezone,
            )
            behavior["feature_weights"] = feature_weights
            return behavior

        _register_scoped_tool(mcp, enforcer, capability, behavior_predict)
        return mcp

    if capability == "qweather":
        if qweather is None:
            config = load_ios_intelligence_config()
            policy = config.weather
            qweather = QWeatherClient(
                store,
                base_url=policy.qweather_base_url,
                timezone=config.timezone,
            )

        def weather_minutely(owner_id: str = "", latitude: float | None = None, longitude: float | None = None) -> dict[str, Any]:
            """Use automatically when the user asks whether rain will start or stop soon, or a location-aware plan needs minute-level precipitation."""
            owner_id = _resolve_owner(store, owner_id)
            lat, lon = _resolve_location(store, owner_id, latitude, longitude)
            return qweather.minutely(lat, lon)

        def weather_current(owner_id: str = "", latitude: float | None = None, longitude: float | None = None) -> dict[str, Any]:
            """Use automatically for current weather questions, using the synchronized iOS location when coordinates are omitted."""
            owner_id = _resolve_owner(store, owner_id)
            lat, lon = _resolve_location(store, owner_id, latitude, longitude)
            return qweather.current(lat, lon)

        def weather_hourly(owner_id: str = "", hours: int = 24, latitude: float | None = None, longitude: float | None = None) -> dict[str, Any]:
            """Use automatically when a plan or question needs the next 24 to 72 hours of weather at the user's current or supplied location."""
            owner_id = _resolve_owner(store, owner_id)
            lat, lon = _resolve_location(store, owner_id, latitude, longitude)
            return qweather.hourly(lat, lon, hours)

        def weather_warnings(owner_id: str = "", latitude: float | None = None, longitude: float | None = None) -> dict[str, Any]:
            """Use automatically when severe weather, typhoon, storm, heat, or official weather warnings could affect the user."""
            owner_id = _resolve_owner(store, owner_id)
            lat, lon = _resolve_location(store, owner_id, latitude, longitude)
            return qweather.warnings(lat, lon)

        _register_scoped_tool(mcp, enforcer, capability, weather_minutely)
        _register_scoped_tool(mcp, enforcer, capability, weather_current)
        _register_scoped_tool(mcp, enforcer, capability, weather_hourly)
        _register_scoped_tool(mcp, enforcer, capability, weather_warnings)
        return mcp

    if capability == "amap-route":
        if amap is None:
            policy = load_ios_intelligence_config().weather
            amap = AMapClient(base_url=policy.amap_base_url)

        def amap_reverse_geocode(latitude: float, longitude: float) -> dict[str, Any]:
            """Use automatically to turn synchronized coordinates into a readable place or nearby POI for a user's location-aware request."""
            return amap.reverse_geocode(latitude, longitude)

        def amap_plan_route(origin: str, destination: str, mode: str = "walking", city: str = "") -> dict[str, Any]:
            """Use automatically when a request needs route duration, distance, or outdoor exposure between two places; supports walking, cycling, driving, and transit."""
            return amap.route(origin, destination, mode, city)

        def amap_search_poi(keywords: str, city: str = "", location: str = "", types: str = "") -> dict[str, Any]:
            """Use automatically when a request needs a place, venue, or nearby POI resolved before location or route planning."""
            return amap.search_poi(keywords, city=city, location=location, types=types)

        _register_scoped_tool(mcp, enforcer, capability, amap_reverse_geocode)
        _register_scoped_tool(mcp, enforcer, capability, amap_plan_route)
        _register_scoped_tool(mcp, enforcer, capability, amap_search_poi)
        return mcp

    if capability == "ios-map":
        def ios_map_get_today(owner_id: str = "", timezone: str = "") -> dict[str, Any]:
            """Use when the user asks to show or reason over today's standard-map location, visited-place markers, and movement polyline."""
            owner_id = _resolve_owner(store, owner_id)
            _require_device_permission(store, owner_id, capability)
            tz = timezone or load_ios_intelligence_config().timezone
            return store.today_snapshot(owner_id, tz)

        _register_scoped_tool(mcp, enforcer, capability, ios_map_get_today)
        return mcp

    if capability == "ios-calendar":
        _register_history(
            mcp,
            enforcer,
            capability=capability,
            tool_name="ios_calendar_list",
            kind="calendar",
            store=store,
            description="Use automatically when a normal chat request depends on the user's synced calendar events or schedule.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_calendar_create", "create",
            "Use automatically when the user asks Hermes to create a calendar event. Queue the native EventKit operation with title and dates in payload.",
        )
        _queue_tool(mcp, enforcer, store, capability, "ios_calendar_calendars", "calendars", "Use when Hermes needs the user's available iOS calendars.")
        _queue_tool(mcp, enforcer, store, capability, "ios_calendar_freebusy", "freebusy", "Use when Hermes needs to check iOS calendar busy windows.")
        _queue_tool(mcp, enforcer, store, capability, "ios_calendar_update", "update", "Use when the user asks Hermes to update a calendar event after confirmation.")
        _queue_tool(mcp, enforcer, store, capability, "ios_calendar_delete", "delete", "Use when the user asks Hermes to delete a calendar event after confirmation.")
        return mcp

    if capability == "ios-reminders":
        _register_history(
            mcp,
            enforcer,
            capability=capability,
            tool_name="ios_reminders_list",
            kind="reminder",
            store=store,
            description="Use automatically when a chat request depends on the user's synced reminders or requested tasks.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_reminder_create", "create",
            "Use automatically when the user asks Hermes to create an iOS reminder. Queue the native EventKit operation with title and due date in payload.",
        )
        _queue_tool(mcp, enforcer, store, capability, "ios_reminder_update", "update", "Use when the user asks Hermes to update a reminder after confirmation.")
        _queue_tool(mcp, enforcer, store, capability, "ios_reminder_complete", "complete", "Use when the user asks Hermes to complete a reminder after confirmation.")
        _queue_tool(mcp, enforcer, store, capability, "ios_reminder_delete", "delete", "Use when the user asks Hermes to delete a reminder after confirmation.")
        return mcp

    if capability == "ios-alarm":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_alarm_schedule", "schedule",
            "Use when the user asks Hermes to schedule an iPhone alarm-style reminder and local notification. Payload requires title and fireAt.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_alarm_list", "list",
            "Use when Hermes needs the user's current iPhone alarm-style reminders.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_alarm_cancel", "cancel",
            "Use when the user asks Hermes to cancel an alarm-style reminder or notification after confirmation.",
        )
        return mcp

    if capability == "ios-nlp":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_nlp_analyze", "analyze",
            "Use when Hermes needs on-device language identification, sentiment, or token counts for supplied text.",
        )
        return mcp

    if capability == "ios-clipboard":
        def ios_clipboard_read(owner_id: str = "", device_id: str = "", idempotency_key: str = "") -> dict[str, Any]:
            """Queue a read of the current iPhone clipboard for the requesting account."""
            owner_id = _resolve_owner(store, owner_id)
            return store.queue_device_command(
                owner_id,
                capability,
                "read",
                {},
                device_id=device_id,
                idempotency_key=idempotency_key,
            )

        def ios_clipboard_write(
            text: str,
            owner_id: str = "",
            device_id: str = "",
            idempotency_key: str = "",
            confirmed: bool = False,
            confirmation_token: str = "",
        ) -> dict[str, Any]:
            """Queue a clipboard write; explicit user confirmation is required on the phone."""
            owner_id = _resolve_owner(store, owner_id)
            normalized_text = str(text or "")
            if not normalized_text:
                raise ValueError("text is required")
            payload: dict[str, Any] = {"text": normalized_text[:100_000]}
            if confirmed:
                payload["confirmed"] = True
            normalized_token = str(confirmation_token or "").strip()
            if normalized_token:
                payload["confirmation_token"] = normalized_token[:512]
            return store.queue_device_command(
                owner_id,
                capability,
                "write",
                payload,
                device_id=device_id,
                idempotency_key=idempotency_key,
            )

        _register_scoped_tool(
            mcp,
            enforcer,
            capability,
            ios_clipboard_read,
            name="ios_clipboard_read",
            description="Use when Hermes needs to read the current iPhone clipboard into a task.",
        )
        _register_scoped_tool(
            mcp,
            enforcer,
            capability,
            ios_clipboard_write,
            name="ios_clipboard_write",
            description="Use when Hermes should write task output to the iPhone clipboard after the user explicitly approves it.",
        )
        return mcp

    if capability == "ios-contacts":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_contacts_list", "list",
            "Use when Hermes needs a bounded list of the user's authorized iPhone contacts.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_contacts_search", "search",
            "Use when Hermes needs to search the user's authorized iPhone contacts. Payload may include query and limit.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_contacts_create", "create",
            "Use when the user explicitly asks Hermes to create a contact on the iPhone. The native bridge requires confirmation.",
        )
        return mcp

    if capability == "ios-photos":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_photos_list", "list",
            "Use when Hermes needs a bounded list of the user's authorized iPhone photo and video assets.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_photos_search", "search",
            "Use when Hermes needs to search the user's authorized iPhone photo library by filename or date window.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_photos_capture", "capture",
            "Use when the user asks Hermes to take a new photo on the iPhone; the app must be foregrounded and the native camera requires confirmation.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_photos_scan", "scan",
            "Use when the user asks Hermes to scan a visual code with the iPhone camera; the app must be foregrounded.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_photos_ocr", "ocr",
            "Use when Hermes needs to extract text from a local iPhone image. Payload must include imageURL or uri.",
        )
        for name, action, description in (
            ("ios_photos_albums", "albums", "Use when Hermes needs to list iPhone photo albums."),
            ("ios_photos_near", "near", "Use when Hermes needs to search iPhone photos near a GPS coordinate."),
            ("ios_photos_export", "export", "Use when Hermes needs to export an iPhone photo into the owner-scoped workspace."),
            ("ios_photos_favorite", "favorite", "Use when the user asks Hermes to favorite or unfavorite selected iPhone photos after confirmation."),
            ("ios_photos_delete", "delete", "Use when the user asks Hermes to delete selected iPhone photos after confirmation."),
            ("ios_photos_album_create", "album-create", "Use when the user asks Hermes to create an iPhone photo album after confirmation."),
            ("ios_photos_album_add", "album-add", "Use when the user asks Hermes to add selected photos to an iPhone album after confirmation."),
            ("ios_photos_import", "import", "Use when the user asks Hermes to import an owner-scoped image into Photos after confirmation."),
        ):
            _queue_tool(mcp, enforcer, store, capability, name, action, description)
        return mcp

    if capability == "ios-vision":
        for tool_name, action, description in (
            ("ios_vision_analyze", "analyze", "Use when Hermes needs complete on-device Vision analysis for an owner-scoped image."),
            ("ios_vision_classify", "classify", "Use when Hermes needs image classifications from an owner-scoped iPhone image."),
            ("ios_vision_detect", "detect", "Use when Hermes needs rectangle detections from an owner-scoped iPhone image."),
            ("ios_vision_faces", "faces", "Use when Hermes needs face detections from an owner-scoped iPhone image."),
        ):
            _queue_tool(mcp, enforcer, store, capability, tool_name, action, description)
        return mcp

    if capability == "ios-health-write":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_health_write_authorize", "authorize",
            "Use to request the native HealthKit write permission for a specific quantity identifier; iOS shows the system permission prompt.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_health_write", "write",
            "Use only when the user explicitly asks Hermes to write a HealthKit quantity sample. Payload must include identifier, value, unit, start, and end.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_health_write_batch", "batch",
            "Use only when the user explicitly asks Hermes to write a bounded batch of HealthKit quantity samples; every sample must include identifier, value, unit, start, and end.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_health_delete", "delete",
            "Use only when the user explicitly asks Hermes to delete HealthKit samples created by a prior Hermes command; the native bridge matches the command id and type.",
        )
        return mcp

    if capability == "ios-media":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_media_get", "get",
            "Use when Hermes needs the current iPhone media playback state.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_media_control", "control",
            "Use when the user asks Hermes to play, pause, stop, skip, or resume media. Payload must contain action.",
        )
        _queue_tool(mcp, enforcer, store, capability, "ios_media_search", "search", "Use when Hermes needs to search the iPhone Media Library by title.")
        _queue_tool(mcp, enforcer, store, capability, "ios_media_play_search", "play-search", "Use when the user asks Hermes to search and play matching iPhone media after confirmation.")
        _queue_tool(mcp, enforcer, store, capability, "ios_media_volume", "volume", "Use when the user asks Hermes to set iPhone media volume after confirmation.")
        return mcp

    if capability == "ios-bluetooth":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_bluetooth_state", "state",
            "Use when Hermes needs the iPhone Bluetooth adapter state.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_bluetooth_scan", "scan",
            "Use when the user asks Hermes to scan nearby Bluetooth devices. Payload may contain seconds.",
        )
        for name, action, description in (
            ("ios_bluetooth_connect", "connect", "Use when Hermes needs to connect to a Bluetooth peripheral for this owner."),
            ("ios_bluetooth_disconnect", "disconnect", "Use when the user asks Hermes to disconnect the current Bluetooth session."),
            ("ios_bluetooth_services", "services", "Use when Hermes needs to discover Bluetooth services and characteristics."),
            ("ios_bluetooth_read", "read", "Use when Hermes needs to read a Bluetooth characteristic."),
            ("ios_bluetooth_write", "write", "Use when the user asks Hermes to write a Bluetooth characteristic after confirmation."),
            ("ios_bluetooth_notify", "notify", "Use when Hermes needs to collect Bluetooth notification samples for a bounded period."),
        ):
            _queue_tool(mcp, enforcer, store, capability, name, action, description)
        return mcp

    if capability == "ios-nfc":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_nfc_scan", "scan",
            "Use when the user asks Hermes to read an NFC tag; the iPhone presents the native reader session.",
        )
        _queue_tool(mcp, enforcer, store, capability, "ios_nfc_write", "write", "Use when the user asks Hermes to write a text NDEF record after explicit confirmation.")
        return mcp

    if capability == "ios-homekit":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_homekit_list", "list",
            "Use when Hermes needs the user's HomeKit homes and reachable accessory state.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_homekit_set", "set",
            "Use when the user explicitly asks Hermes to change a HomeKit characteristic. Payload must include accessoryId, characteristicId, and value; the native bridge requires confirmation.",
        )
        _queue_tool(mcp, enforcer, store, capability, "ios_homekit_search", "search", "Use when Hermes needs to search HomeKit accessories by name, room, or service.")
        _queue_tool(mcp, enforcer, store, capability, "ios_homekit_scenes", "scenes", "Use when Hermes needs to list HomeKit scenes." )
        _queue_tool(mcp, enforcer, store, capability, "ios_homekit_trigger", "trigger", "Use when the user asks Hermes to trigger a HomeKit scene after confirmation.")
        return mcp

    if capability == "ios-notes":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_notes_share_text", "share-text",
            "Use automatically when the user asks to put generated text into Apple Notes. The iPhone opens the native Notes share sheet for user confirmation.",
        )
        return mcp

    if capability == "ios-notification":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_notification_send", "send",
            "Use automatically when Hermes should proactively notify the user about a timely result or weather risk. Payload supplies title, body, deep link, and expiry.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_notification_schedule", "schedule",
            "Use when the user asks for a local iOS notification at a specific fireAt timestamp.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_notification_cancel", "cancel",
            "Use when the user asks to cancel a previously scheduled local iOS notification by id.",
        )
        return mcp

    if capability == "ios-live-activity":
        _queue_tool(
            mcp, enforcer, store, capability, "ios_live_activity_update", "update",
            "Use automatically when a current task or imminent weather event is best shown as an iOS Lock Screen Live Activity. Payload may start, update, or end it.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_live_activity_start", "start",
            "Use when the user asks to start a new iOS Lock Screen Live Activity.",
        )
        _queue_tool(
            mcp, enforcer, store, capability, "ios_live_activity_end", "end",
            "Use when the user asks to end an existing iOS Lock Screen Live Activity by id.",
        )
        return mcp

    if capability == "ios-browser":
        for tool_name, action, description in (
            ("ios_browser_navigate", "navigate", "Use when Hermes needs the iPhone's native web runtime to open a bounded HTTP(S) URL."),
            ("ios_browser_screenshot", "screenshot", "Use when Hermes needs a screenshot of the current iPhone browser tab; the result is stored in the owner-scoped app support directory."),
            ("ios_browser_get_text", "get_text", "Use when Hermes needs the visible text from the current iPhone browser tab."),
            ("ios_browser_get_page_info", "get_page_info", "Use when Hermes needs the title, URL, ready state, or viewport of the current iPhone browser tab."),
            ("ios_browser_find_elements", "find_elements", "Use when Hermes needs bounded DOM element summaries matching a CSS selector."),
            ("ios_browser_get_readable", "get_readable", "Use when Hermes needs readable page text with script/style noise removed."),
            ("ios_browser_get_backbone", "get_backbone", "Use when Hermes needs a bounded structural outline of the current page."),
            ("ios_browser_list_tabs", "list_tabs", "Use when Hermes needs to inspect the open native iPhone browser tabs."),
            ("ios_browser_get_cookies", "get_cookies", "Use when Hermes needs cookies from the current native iPhone browser tab."),
            ("ios_browser_fetch", "fetch", "Use when Hermes needs to fetch a bounded HTTP(S) response into the owner-scoped iPhone browser workspace."),
            ("ios_browser_wait_for_dom_stable", "wait_for_dom_stable", "Use when Hermes needs to wait for a page's DOM text to stop changing."),
            ("ios_browser_click", "click", "Use when the user explicitly asks Hermes to click a page element on the iPhone after confirmation."),
            ("ios_browser_type", "type", "Use when the user explicitly asks Hermes to enter text into a page field on the iPhone after confirmation."),
            ("ios_browser_scroll", "scroll", "Use when Hermes needs to scroll the current iPhone browser page after confirmation."),
            ("ios_browser_hover", "hover", "Use when Hermes needs to dispatch hover events to a page element after confirmation."),
            ("ios_browser_execute_js", "execute_js", "Use only for an explicitly confirmed browser automation request; arbitrary page JavaScript is treated as destructive."),
            ("ios_browser_set_user_agent", "set_user_agent", "Use when the user explicitly asks Hermes to change the browser user agent after confirmation."),
            ("ios_browser_set_viewport", "set_viewport", "Use when the user explicitly asks Hermes to change the browser viewport after confirmation."),
            ("ios_browser_new_tab", "new_tab", "Use when the user explicitly asks Hermes to open another native iPhone browser tab after confirmation."),
            ("ios_browser_close_tab", "close_tab", "Use when the user explicitly asks Hermes to close a native iPhone browser tab after confirmation."),
            ("ios_browser_set_cookies", "set_cookies", "Use when the user explicitly asks Hermes to write browser cookies after confirmation."),
            ("ios_browser_scroll_and_collect", "scroll_and_collect", "Use when the user explicitly asks Hermes to scroll and collect bounded page items after confirmation."),
        ):
            _queue_tool(mcp, enforcer, store, capability, tool_name, action, description)
        return mcp

    raise AssertionError(f"Capability registration missing: {capability}")


def _run_supervised_mcp_child(
    argv: Sequence[str],
    environment: Mapping[str, str],
    log_path: str | os.PathLike[str],
) -> int:
    """Enter one forkserver child with the same boundary as a fresh exec."""

    os.environ.clear()
    os.environ.update({str(key): str(value) for key, value in environment.items()})
    if hasattr(time, "tzset"):
        time.tzset()

    descriptor = os.open(
        os.fspath(log_path),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
    finally:
        if descriptor not in {1, 2}:
            os.close(descriptor)
    return main(list(argv))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one independent Hermes iOS MCP server")
    parser.add_argument("capability", nargs="?", choices=CAPABILITIES)
    parser.add_argument(
        "--install", action="store_true",
        help="register every independent iOS MCP in Hermes config, enabled by default",
    )
    parser.add_argument("--db-dir", type=Path, help="Directory containing ios-intelligence.db")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--base-port", type=int, default=8760)
    parser.add_argument(
        "--grant-scope",
        action="append",
        default=None,
        help="grant one manifest-declared scope to this MCP process (repeatable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.install:
        result = install_ios_mcp_servers(
            transport=args.transport,
            host=args.host,
            base_port=args.base_port,
        )
        print(f"Registered {result['count']} iOS MCP servers ({len(result['installed'])} updated).")
        return 0
    if not args.capability:
        build_parser().error("capability is required unless --install is used")
    server = create_mcp_server(
        args.capability,
        store=IOSIntelligenceStore(args.db_dir) if args.db_dir else None,
        host=args.host,
        port=args.port,
        granted_scopes=args.grant_scope,
    )
    server.run(
        transport=args.transport,
        **getattr(server, "_http_runtime_options", {}),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CAPABILITIES", "IOSMCPScopeError", "MCP_VERSION", "create_mcp_server", "install_ios_mcp_servers",
    "ios_mcp_manifests",
    "ios_mcp_server_configs", "main", "merge_ios_mcp_servers", "normalize_mcp_scope_grants",
]
