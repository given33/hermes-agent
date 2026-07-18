"""Device identity isolation for the native iOS relay."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from hermes_cli.dashboard_auth.mobile_device_store import MobileDeviceInfo, MobileDeviceStore
from hermes_cli.ios_intelligence_config import load_ios_intelligence_config


def _load_plugin(monkeypatch):
    plugin_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "ios-intelligence"
        / "dashboard"
        / "plugin_api.py"
    )
    name = f"test_ios_relay_device_binding_{id(monkeypatch)}"
    spec = importlib.util.spec_from_file_location(name, plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _device(device_id: str) -> MobileDeviceInfo:
    return MobileDeviceInfo(
        id=device_id,
        name=device_id,
        model="iPhone17,1",
        os_version="18.6",
        app_version="2.0.0",
    )


class _RelayStore:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def ingest_events(self, owner_id, device_id, *_args, **_kwargs):
        self.calls.append(("events", owner_id, device_id))
        return {"accepted": 1}

    def pull_device_commands(self, owner_id, device_id, **_kwargs):
        self.calls.append(("pull", owner_id, device_id))
        return {"commands": []}

    def ack_device_command(self, owner_id, device_id, *_args, **_kwargs):
        self.calls.append(("ack", owner_id, device_id))
        return True


def _request(token: str, owner_id: str = "owner"):
    authorization = f"Bearer {token}" if token else ""
    return SimpleNamespace(
        headers={"authorization": authorization},
        state=SimpleNamespace(token_principal=SimpleNamespace(principal=owner_id)),
    )


def _invoke_device_routes(module, request, device_id: str) -> None:
    module.ingest_event_batch(
        request,
        module.ContextEventBatch(
            device_id=device_id,
            cursor="event-1",
            events=[module.ContextEvent(id="event-1", kind="power", timestamp=1)],
        ),
    )
    module.record_capabilities(
        request,
        module.CapabilityBody(device_id=device_id, capabilities={}, observed_at=1),
    )
    module.pull_commands(request, module.DeviceCommandPull(device_id=device_id))
    module.acknowledge_command(
        "command-1",
        request,
        module.DeviceCommandAck(device_id=device_id),
    )


def test_relay_routes_bind_body_device_to_verified_mobile_session(monkeypatch, tmp_path):
    module = _load_plugin(monkeypatch)
    mobile_store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    phone = mobile_store.create_session(user_id="owner", device=_device("device-phone"))
    tablet = mobile_store.create_session(user_id="owner", device=_device("device-tablet"))
    relay_store = _RelayStore()
    policy = load_ios_intelligence_config(
        {"ios_intelligence": {"relay": {"require_device_token": True}}}
    )
    monkeypatch.setattr(module, "MobileDeviceStore", lambda: mobile_store)
    monkeypatch.setattr(module, "intelligence_store", lambda: relay_store)
    monkeypatch.setattr(module, "load_ios_intelligence_config", lambda: policy)

    route_calls = (
        lambda: module.ingest_event_batch(
            _request(phone.access_token),
            module.ContextEventBatch(device_id="device-tablet", events=[]),
        ),
        lambda: module.record_capabilities(
            _request(phone.access_token),
            module.CapabilityBody(device_id="device-tablet", capabilities={}, observed_at=1),
        ),
        lambda: module.pull_commands(
            _request(phone.access_token),
            module.DeviceCommandPull(device_id="device-tablet"),
        ),
        lambda: module.acknowledge_command(
            "command-1",
            _request(phone.access_token),
            module.DeviceCommandAck(device_id="device-tablet"),
        ),
    )
    for call in route_calls:
        with pytest.raises(module.HTTPException) as exc_info:
            call()
        assert exc_info.value.status_code == 403
    assert relay_store.calls == []

    _invoke_device_routes(module, _request(tablet.access_token), "device-tablet")
    assert relay_store.calls == [
        ("events", "owner", "device-tablet"),
        ("events", "owner", "device-tablet"),
        ("pull", "owner", "device-tablet"),
        ("ack", "owner", "device-tablet"),
    ]


def test_relay_device_policy_rejects_missing_or_cross_account_token(monkeypatch, tmp_path):
    module = _load_plugin(monkeypatch)
    mobile_store = MobileDeviceStore(tmp_path / "mobile-auth.db")
    token = mobile_store.create_session(user_id="owner-a", device=_device("device-owner-a"))
    relay_store = _RelayStore()
    policy = load_ios_intelligence_config(
        {"ios_intelligence": {"relay": {"require_device_token": True}}}
    )
    monkeypatch.setattr(module, "MobileDeviceStore", lambda: mobile_store)
    monkeypatch.setattr(module, "intelligence_store", lambda: relay_store)
    monkeypatch.setattr(module, "load_ios_intelligence_config", lambda: policy)

    with pytest.raises(module.HTTPException) as missing:
        module.pull_commands(_request(""), module.DeviceCommandPull(device_id="device-owner-a"))
    assert missing.value.status_code == 401

    with pytest.raises(module.HTTPException) as cross_account:
        module.pull_commands(
            _request(token.access_token, owner_id="owner-b"),
            module.DeviceCommandPull(device_id="device-owner-a"),
        )
    assert cross_account.value.status_code == 403
    assert relay_store.calls == []
