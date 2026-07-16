"""Owner-account registration and native token authentication contracts."""
from __future__ import annotations

import base64
import secrets

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hermes_cli.dashboard_auth import (
    clear_providers,
    get_provider,
)
from hermes_cli.dashboard_auth import token_auth
from hermes_cli.dashboard_auth.owner_mobile import (
    MobileDeviceBody,
    MobileLoginBody,
    MobileRefreshBody,
    MobileRegisterBody,
    ensure_owner_provider,
    mobile_login,
    mobile_refresh,
    mobile_register,
    mobile_registration_status,
)
from hermes_cli.dashboard_auth.routes import _reset_password_rate_limit


class _Client:
    host = "203.0.113.20"


class _Request:
    client = _Client()
    headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _isolated_owner_account(tmp_path, monkeypatch):
    clear_providers()
    token_auth.clear_optional_token_prefixes()
    _reset_password_rate_limit()
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_BASIC_AUTH_SECRET", raising=False)
    monkeypatch.delenv("HERMES_OWNER_SETUP_TOKEN", raising=False)
    yield
    clear_providers()
    token_auth.clear_optional_token_prefixes()
    _reset_password_rate_limit()


def _register(
    username: str = "owner",
    password: str = "correct-horse-42",
    setup_token: str = "",
):
    return mobile_register(
        _Request(),
        MobileRegisterBody(
            username=username,
            password=password,
            setup_token=setup_token,
            device=MobileDeviceBody(
                id="ios-owner-device",
                name="Owner iPhone",
                model="iPhone17,1",
                os_version="18.6",
                app_version="2.0.0",
            ),
        ),
    )


def test_first_registration_persists_only_scrypt_hash_and_stable_secret():
    from hermes_cli.dashboard_auth.login_page import render_login_html

    before = mobile_registration_status()
    registration_page = render_login_html()
    result = _register()
    after = mobile_registration_status()

    from hermes_cli.config import load_config

    section = load_config()["dashboard"]["basic_auth"]
    assert before == {
        "registration_open": True,
        "account_configured": False,
        "setup_token_required": False,
    }
    assert "创建此 Hermes 服务器的所有者账号" in registration_page
    assert "owner-registration-form" in registration_page
    assert 'name="setup_token"' not in registration_page
    assert "注册并登录" in registration_page
    assert after == {
        "registration_open": False,
        "account_configured": True,
        "setup_token_required": False,
    }
    assert section["username"] == "owner"
    assert section["password_hash"].startswith("scrypt$")
    assert "correct-horse-42" not in section["password_hash"]
    assert section["password"] == ""
    assert len(base64.b64decode(section["secret"])) == 32
    assert result["token_type"] == "Bearer"
    assert result["account"]["username"] == "owner"
    assert result["access_token"]
    assert result["refresh_token"]


def test_registration_registers_device_provider_for_api_bearer_requests():
    result = _register()
    provider = get_provider("owner-mobile")

    assert provider is not None
    principal = provider.verify_token(token=result["access_token"])
    assert principal is not None
    assert principal.principal == "owner"
    assert principal.provider == "owner-mobile"
    assert principal.scopes == ("dashboard:admin",)
    assert token_auth.is_optional_token_path("/api/sessions") is True


def test_existing_basic_provider_enables_mobile_prefix_after_restart():
    _register()
    token_auth.clear_optional_token_prefixes()

    provider = ensure_owner_provider()

    assert provider is get_provider("basic")
    assert token_auth.is_optional_token_path("/api/sessions") is True


def test_second_registration_is_rejected_without_overwriting_owner():
    _register()

    with pytest.raises(HTTPException) as exc:
        _register(username="attacker", password="another-password")

    from hermes_cli.config import load_config

    assert exc.value.status_code == 409
    assert load_config()["dashboard"]["basic_auth"]["username"] == "owner"


def test_public_first_owner_registration_requires_configured_setup_token(monkeypatch):
    from hermes_cli.dashboard_auth.login_page import render_login_html

    monkeypatch.setenv("HERMES_OWNER_SETUP_TOKEN", "server-only-bootstrap-code")

    status = mobile_registration_status()
    registration_page = render_login_html()
    assert status["registration_open"] is True
    assert status["setup_token_required"] is True
    assert "服务器初始化码" in registration_page
    assert 'type="password" name="setup_token"' in registration_page
    assert "setup_token: setupToken" in registration_page
    assert "初始化码错误。" in registration_page
    assert "server-only-bootstrap-code" not in registration_page

    with pytest.raises(HTTPException) as missing:
        _register()
    with pytest.raises(HTTPException) as wrong:
        _register(setup_token="wrong-code")

    assert missing.value.status_code == 403
    assert wrong.value.status_code == 403
    registered = _register(setup_token="server-only-bootstrap-code")
    assert registered["account"]["username"] == "owner"


def test_web_owner_registration_submits_required_setup_token(monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setenv("HERMES_OWNER_SETUP_TOKEN", "web-bootstrap-code")
    previous = {
        name: getattr(web_server.app.state, name, None)
        for name in ("bound_host", "bound_port", "auth_required")
    }
    web_server.app.state.bound_host = "owner.test"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://owner.test")
    try:
        login = client.get("/login")
        missing = client.post(
            "/auth/mobile/register",
            json={"username": "owner", "password": "correct-horse-42"},
        )
        wrong = client.post(
            "/auth/mobile/register",
            json={
                "username": "owner",
                "password": "correct-horse-42",
                "setup_token": "wrong-code",
            },
        )
        registered = client.post(
            "/auth/mobile/register",
            json={
                "username": "owner",
                "password": "correct-horse-42",
                "setup_token": "web-bootstrap-code",
            },
        )
    finally:
        client.close()
        for name, value in previous.items():
            setattr(web_server.app.state, name, value)

    assert login.status_code == 200
    assert "服务器初始化码" in login.text
    assert 'name="setup_token"' in login.text
    assert "初始化码错误。" in login.text
    assert "web-bootstrap-code" not in login.text
    assert missing.status_code == 403
    assert missing.json()["detail"] == "Invalid owner setup token"
    assert wrong.status_code == 403
    assert wrong.json()["detail"] == "Invalid owner setup token"
    assert registered.status_code == 200
    assert registered.json()["account"]["username"] == "owner"


@pytest.mark.parametrize(
    "username,password",
    [
        ("ab", "correct-horse-42"),
        ("has space", "correct-horse-42"),
        ("owner", "short"),
        ("owner", "x" * 257),
    ],
)
def test_registration_validates_credentials(username, password):
    with pytest.raises(HTTPException) as exc:
        _register(username=username, password=password)

    assert exc.value.status_code == 422
    assert mobile_registration_status()["registration_open"] is True


def test_login_and_refresh_work_after_provider_registry_is_rebuilt():
    registered = _register()
    clear_providers()

    logged_in = mobile_login(
        _Request(),
        MobileLoginBody(
            username="owner",
            password="correct-horse-42",
            device=MobileDeviceBody(id="second-ios-device", name="Owner iPad"),
        ),
    )
    refreshed = mobile_refresh(
        _Request(),
        MobileRefreshBody(refresh_token=logged_in["refresh_token"]),
    )

    assert logged_in["account"]["username"] == "owner"
    assert refreshed["account"]["username"] == "owner"
    assert refreshed["access_token"]
    assert refreshed["refresh_token"]
    assert refreshed["access_token"] != logged_in["access_token"]
    assert refreshed["refresh_token"] != logged_in["refresh_token"]
    assert registered["access_token"]


def test_wrong_password_and_foreign_refresh_token_fail_generically():
    _register()

    with pytest.raises(HTTPException) as login_exc:
        mobile_login(
            _Request(),
            MobileLoginBody(username="owner", password="wrong-password"),
        )
    with pytest.raises(HTTPException) as refresh_exc:
        mobile_refresh(
            _Request(),
            MobileRefreshBody(refresh_token=secrets.token_urlsafe(48)),
        )

    assert login_exc.value.status_code == 401
    assert login_exc.value.detail == "Invalid credentials"
    assert refresh_exc.value.status_code == 401
    assert refresh_exc.value.detail == "Invalid refresh token"


def test_real_dashboard_routes_register_login_refresh_and_authorize_api():
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth.middleware import _path_is_public

    assert _path_is_public("/auth/mobile/register") is True
    assert any(
        getattr(route, "path", None) == "/auth/mobile/register"
        for route in web_server.app.routes
    )

    previous = {
        name: getattr(web_server.app.state, name, None)
        for name in ("bound_host", "bound_port", "auth_required")
    }
    web_server.app.state.bound_host = "owner.test"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://owner.test")
    try:
        status = client.get("/auth/mobile/status")
        registered = client.post(
            "/auth/mobile/register",
            json={
                "username": "owner",
                "password": "correct-horse-42",
                "device": {
                    "id": "route-ios-device",
                    "name": "Route iPhone",
                    "model": "iPhone17,1",
                    "os_version": "18.6",
                    "app_version": "2.0.0",
                },
            },
            follow_redirects=False,
        )
        assert registered.status_code == 200, (
            registered.status_code,
            registered.headers.get("location"),
            registered.text[:240],
        )
        token = registered.json()["access_token"]
        protected = client.get(
            "/api/sessions",
            headers={"Authorization": f"Bearer {token}"},
        )
        rejected = client.get(
            "/api/sessions",
            headers={"Authorization": "Bearer invalid"},
        )
        login = client.post(
            "/auth/mobile/token",
            json={"username": "owner", "password": "correct-horse-42"},
        )
        refresh = client.post(
            "/auth/mobile/refresh",
            json={"refresh_token": login.json()["refresh_token"]},
        )
        ticket = client.post(
            "/api/auth/ws-ticket",
            headers={"Authorization": f"Bearer {token}"},
        )
        apns = client.put(
            "/api/mobile/v1/devices/route-ios-device/apns",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "token": "a1" * 32,
                "environment": "sandbox",
                "bundle_id": "com.given33.hermesagent.nativebeta",
            },
        )
        devices = client.get(
            "/api/mobile/v1/devices",
            headers={"Authorization": f"Bearer {token}"},
        )
        logout = client.post(
            "/auth/mobile/logout",
            headers={"Authorization": f"Bearer {token}"},
            json={"refresh_token": registered.json()["refresh_token"]},
        )
        revoked = client.get(
            "/api/sessions",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        client.close()
        for name, value in previous.items():
            setattr(web_server.app.state, name, value)

    assert status.json()["registration_open"] is True
    assert registered.status_code == 200
    assert protected.status_code == 200
    assert rejected.status_code == 401
    assert login.status_code == 200
    assert refresh.status_code == 200
    assert ticket.status_code == 200
    assert ticket.json()["ticket"]
    assert apns.status_code == 200
    assert apns.json()["registration"]["token_suffix"] == ("a1" * 32)[-8:]
    assert devices.status_code == 200
    route_device = next(
        item for item in devices.json()["devices"] if item["id"] == "route-ios-device"
    )
    assert route_device["current"] is True
    assert route_device["apns"][0]["bundle_id"] == "com.given33.hermesagent.nativebeta"
    assert logout.json() == {"ok": True, "revoked": True}
    assert revoked.status_code == 401


def test_same_owner_devices_read_and_write_one_cloud_workspace():
    """Device sessions authenticate clients; they never partition Hermes data."""
    from hermes_cli import web_server

    previous = {
        name: getattr(web_server.app.state, name, None)
        for name in ("bound_host", "bound_port", "auth_required")
    }
    web_server.app.state.bound_host = "owner.test"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://owner.test")
    try:
        first = client.post(
            "/auth/mobile/register",
            json={
                "username": "owner",
                "password": "correct-horse-42",
                "device": {"id": "shared-iphone", "name": "Owner iPhone"},
            },
        )
        second = client.post(
            "/auth/mobile/token",
            json={
                "username": "owner",
                "password": "correct-horse-42",
                "device": {"id": "shared-ipad", "name": "Owner iPad"},
            },
        )
        assert first.status_code == 200
        assert second.status_code == 200
        first_token = first.json()["access_token"]
        second_token = second.json()["access_token"]
        first_headers = {"Authorization": f"Bearer {first_token}"}
        second_headers = {"Authorization": f"Bearer {second_token}"}

        first_config = client.get("/api/config", headers=first_headers)
        assert first_config.status_code == 200
        config = first_config.json()
        config["cloud_workspace_contract"] = {
            "writer": "shared-iphone",
            "revision": 1,
        }
        written_by_first = client.put(
            "/api/config",
            headers=first_headers,
            json={"config": config},
        )
        visible_to_second = client.get("/api/config", headers=second_headers)

        assert written_by_first.status_code == 200
        assert visible_to_second.status_code == 200
        assert visible_to_second.json()["cloud_workspace_contract"] == {
            "writer": "shared-iphone",
            "revision": 1,
        }

        second_config = visible_to_second.json()
        second_config["cloud_workspace_contract"] = {
            "writer": "shared-ipad",
            "revision": 2,
        }
        written_by_second = client.put(
            "/api/config",
            headers=second_headers,
            json={"config": second_config},
        )
        visible_to_first = client.get("/api/config", headers=first_headers)
        devices = client.get("/api/mobile/v1/devices", headers=first_headers)
    finally:
        client.close()
        for name, value in previous.items():
            setattr(web_server.app.state, name, value)

    assert first_token != second_token
    assert first.json()["device_id"] != second.json()["device_id"]
    assert written_by_second.status_code == 200
    assert visible_to_first.json()["cloud_workspace_contract"] == {
        "writer": "shared-ipad",
        "revision": 2,
    }
    assert {item["id"] for item in devices.json()["devices"]} >= {
        "shared-iphone",
        "shared-ipad",
    }


def test_http_refresh_replay_revokes_the_rotated_session():
    from hermes_cli import web_server

    previous = {
        name: getattr(web_server.app.state, name, None)
        for name in ("bound_host", "bound_port", "auth_required")
    }
    web_server.app.state.bound_host = "owner.test"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://owner.test")
    try:
        registered = client.post(
            "/auth/mobile/register",
            json={
                "username": "owner",
                "password": "correct-horse-42",
                "device": {"id": "refresh-ios-device", "name": "Owner iPhone"},
            },
        )
        assert registered.status_code == 200
        original = registered.json()

        refreshed = client.post(
            "/auth/mobile/refresh",
            json={"refresh_token": original["refresh_token"]},
        )
        replayed = client.post(
            "/auth/mobile/refresh",
            json={"refresh_token": original["refresh_token"]},
        )
        old_access = client.get(
            "/api/sessions",
            headers={"Authorization": f"Bearer {original['access_token']}"},
        )
        new_access = client.get(
            "/api/sessions",
            headers={
                "Authorization": f"Bearer {refreshed.json()['access_token']}"
            },
        )

        basic_session = get_provider("basic").complete_password_login(
            username="owner",
            password="correct-horse-42",
        )
        basic_as_bearer = client.get(
            "/api/sessions",
            headers={"Authorization": f"Bearer {basic_session.access_token}"},
        )
    finally:
        client.close()
        for name, value in previous.items():
            setattr(web_server.app.state, name, value)

    assert refreshed.status_code == 200
    assert replayed.status_code == 401
    assert old_access.status_code == 401
    assert new_access.status_code == 401
    assert basic_as_bearer.status_code == 401


def test_http_device_revoke_is_isolated_and_apns_is_current_device_only():
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth.mobile_device_store import MobileDeviceStore

    previous = {
        name: getattr(web_server.app.state, name, None)
        for name in ("bound_host", "bound_port", "auth_required")
    }
    web_server.app.state.bound_host = "owner.test"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://owner.test")
    try:
        phone = client.post(
            "/auth/mobile/register",
            json={
                "username": "owner",
                "password": "correct-horse-42",
                "device": {"id": "owner-phone-device", "name": "Owner iPhone"},
            },
        ).json()
        tablet = client.post(
            "/auth/mobile/token",
            json={
                "username": "owner",
                "password": "correct-horse-42",
                "device": {"id": "owner-tablet-device", "name": "Owner iPad"},
            },
        ).json()
        phone_headers = {"Authorization": f"Bearer {phone['access_token']}"}
        tablet_headers = {"Authorization": f"Bearer {tablet['access_token']}"}

        phone_apns = client.put(
            "/api/mobile/v1/devices/owner-phone-device/apns",
            headers=phone_headers,
            json={
                "token": "c3" * 32,
                "environment": "production",
                "bundle_id": "com.given33.hermesagent.nativebeta",
            },
        )
        cross_device_apns = client.put(
            "/api/mobile/v1/devices/owner-phone-device/apns",
            headers=tablet_headers,
            json={
                "token": "d4" * 32,
                "environment": "production",
                "bundle_id": "com.given33.hermesagent.nativebeta",
            },
        )
        revoked = client.delete(
            "/api/mobile/v1/devices/owner-phone-device",
            headers=tablet_headers,
        )
        phone_access = client.get("/api/sessions", headers=phone_headers)
        tablet_access = client.get("/api/sessions", headers=tablet_headers)
        devices = client.get(
            "/api/mobile/v1/devices",
            headers=tablet_headers,
        )
        active_pushes = MobileDeviceStore().list_active_apns_registrations()
    finally:
        client.close()
        for name, value in previous.items():
            setattr(web_server.app.state, name, value)

    assert phone_apns.status_code == 200
    assert cross_device_apns.status_code == 403
    assert revoked.json() == {"ok": True}
    assert phone_access.status_code == 401
    assert tablet_access.status_code == 200
    by_id = {item["id"]: item for item in devices.json()["devices"]}
    assert by_id["owner-phone-device"]["active"] is False
    assert by_id["owner-phone-device"]["apns"] == []
    assert by_id["owner-tablet-device"]["active"] is True
    assert active_pushes == []
