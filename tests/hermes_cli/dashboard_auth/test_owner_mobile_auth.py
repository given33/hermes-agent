"""Owner-account registration and native token authentication contracts."""
from __future__ import annotations

import base64
import secrets
import sqlite3
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hermes_cli.dashboard_auth import (
    clear_providers,
    get_provider,
)
from hermes_cli.dashboard_auth import owner_mobile, token_auth
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
    monkeypatch.setenv("HERMES_OWNER_EMAIL", "2821961676@qq.com")
    monkeypatch.setenv("HERMES_MOBILE_REGISTRATION_ENABLED", "1")
    monkeypatch.setenv("HERMES_QQ_SMTP_USERNAME", "2821961676@qq.com")
    monkeypatch.setenv("HERMES_QQ_SMTP_AUTH_CODE", "test-only-auth-code")
    owner_mobile._REGISTRATION_CODES.clear()
    owner_mobile._REGISTRATION_SENDS_BY_IP.clear()
    owner_mobile._REGISTRATION_SENDS_BY_EMAIL.clear()
    yield
    owner_mobile._REGISTRATION_CODES.clear()
    owner_mobile._REGISTRATION_SENDS_BY_IP.clear()
    owner_mobile._REGISTRATION_SENDS_BY_EMAIL.clear()
    clear_providers()
    token_auth.clear_optional_token_prefixes()
    _reset_password_rate_limit()


def _register(
    username: str = "owner",
    password: str = "correct-horse-42",
    email: str = "2821961676@qq.com",
    verification_code: str = "123456",
):
    _seed_registration_code(email, verification_code)
    return mobile_register(
        _Request(),
        MobileRegisterBody(
            email=email,
            verification_code=verification_code,
            username=username,
            password=password,
            device=MobileDeviceBody(
                id="ios-owner-device",
                name="Owner iPhone",
                model="iPhone17,1",
                os_version="18.6",
                app_version="2.0.0",
            ),
        ),
    )


def _seed_registration_code(
    email: str = "2821961676@qq.com",
    code: str = "123456",
) -> None:
    normalized_email = email.strip().lower()
    now = time.monotonic()
    owner_mobile._REGISTRATION_CODES[normalized_email] = owner_mobile._RegistrationCode(
        digest=owner_mobile._registration_code_digest(normalized_email, code),
        expires_at=now + 600,
        sent_at=now - 61,
    )


def _registration_payload(**overrides):
    payload = {
        "email": "2821961676@qq.com",
        "verification_code": "123456",
        "username": "owner",
        "password": "correct-horse-42",
    }
    payload.update(overrides)
    _seed_registration_code(payload["email"], payload["verification_code"])
    return payload


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
        "email_verification_required": True,
        "owner_email_configured": True,
    }
    assert "创建此 Hermes 服务器的所有者账号" in registration_page
    assert "owner-registration-form" in registration_page
    assert 'name="setup_token"' not in registration_page
    assert "注册并登录" in registration_page
    assert after == {
        "registration_open": False,
        "account_configured": True,
        "email_verification_required": True,
        "owner_email_configured": True,
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


def test_account_deletion_revokes_password_provider_and_persisted_credentials():
    _register()
    provider = get_provider("basic")
    store = owner_mobile._store()
    store.begin_account_deletion("owner", "https://hermes.example|owner")

    cleanup = owner_mobile.delete_owner_account_credentials("owner")

    from hermes_cli.config import load_config

    assert cleanup == {"disabled": True, "config_cleared": True}
    section = load_config()["dashboard"]["basic_auth"]
    assert section["disabled"] is True
    assert section["username"] == ""
    assert section["password_hash"] == ""
    assert get_provider("basic") is None
    assert owner_mobile.owner_account_configured() is False
    assert owner_mobile.owner_registration_open() is True
    with pytest.raises(owner_mobile.InvalidCredentialsError):
        provider.complete_password_login(
            username="owner",
            password="correct-horse-42",
        )
    with pytest.raises(HTTPException) as login_error:
        mobile_login(
            _Request(),
            MobileLoginBody(username="owner", password="correct-horse-42"),
        )
    assert login_error.value.status_code == 409


def test_terminally_deleted_username_is_permanently_retired():
    _register()
    store = owner_mobile._store()
    store.begin_account_deletion("owner", "https://hermes.example|owner")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE mobile_account_deletion_outbox SET state='delivered',completed_at=1 "
            "WHERE user_id='owner'"
        )
    owner_mobile.delete_owner_account_credentials("owner")
    from hermes_cli.config import load_config

    before_rejected_registration = load_config()
    _seed_registration_code()

    with pytest.raises(HTTPException) as error:
        mobile_register(
            _Request(),
            MobileRegisterBody(
                email="2821961676@qq.com",
                verification_code="123456",
                username="owner",
                password="new-correct-horse-42",
                device=MobileDeviceBody(id="new-device", name="New iPhone"),
            ),
        )

    assert error.value.status_code == 409
    assert "permanently retired" in str(error.value.detail)
    assert load_config() == before_rejected_registration


def test_plugin_loaded_basic_provider_is_reused_across_module_boundary():
    from hermes_cli.dashboard_auth import (
        DashboardAuthProvider,
        LoginStart,
        Session,
        register_provider,
    )

    class PluginLoadedBasicProvider(DashboardAuthProvider):
        name = "basic"
        display_name = "Plugin-loaded basic"
        supports_password = True

        def start_login(self, *, redirect_uri: str) -> LoginStart:
            raise NotImplementedError

        def complete_login(
            self,
            *,
            code: str,
            state: str,
            code_verifier: str,
            redirect_uri: str,
        ) -> Session:
            raise NotImplementedError

        def complete_password_login(self, *, username: str, password: str) -> Session:
            raise NotImplementedError

        def verify_session(self, *, access_token: str) -> Session | None:
            return None

        def refresh_session(self, *, refresh_token: str) -> Session:
            raise NotImplementedError

        def revoke_session(self, *, refresh_token: str) -> None:
            return None

    plugin_provider = PluginLoadedBasicProvider()
    register_provider(plugin_provider)

    provider = ensure_owner_provider()

    assert provider is plugin_provider
    assert token_auth.is_optional_token_path("/api/sessions") is True


def test_second_registration_is_rejected_without_overwriting_owner():
    _register()

    with pytest.raises(HTTPException) as exc:
        _register(username="attacker", password="another-password")

    from hermes_cli.config import load_config

    assert exc.value.status_code == 403
    assert load_config()["dashboard"]["basic_auth"]["username"] == "owner"


def test_owner_registration_switch_stays_closed_until_enabled(monkeypatch):
    from hermes_cli.dashboard_auth.login_page import render_login_html

    monkeypatch.setenv("HERMES_MOBILE_REGISTRATION_ENABLED", "0")

    status = mobile_registration_status()
    registration_page = render_login_html()
    assert status["registration_open"] is False
    assert status["account_configured"] is False
    assert "owner-registration-form" not in registration_page

    with pytest.raises(HTTPException) as send_error:
        owner_mobile.send_mobile_registration_code(
            _Request(),
            owner_mobile.MobileRegistrationCodeBody(email="2821961676@qq.com"),
        )
    with pytest.raises(HTTPException) as registration_error:
        _register()

    assert send_error.value.status_code == 403
    assert registration_error.value.status_code == 403


def test_qq_email_code_is_hashed_rate_limited_and_single_use(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(owner_mobile.secrets, "randbelow", lambda _: 123456)
    monkeypatch.setattr(
        owner_mobile,
        "_send_qq_verification_email",
        lambda email, code: sent.append((email, code)),
    )

    response = owner_mobile.send_mobile_registration_code(
        _Request(),
        owner_mobile.MobileRegistrationCodeBody(email="2821961676@qq.com"),
    )
    stored = owner_mobile._REGISTRATION_CODES["2821961676@qq.com"]

    assert response == {"ok": True, "expires_in": 600, "resend_after": 60}
    assert sent == [("2821961676@qq.com", "123456")]
    assert stored.digest != b"123456"
    assert not hasattr(stored, "code")

    with pytest.raises(HTTPException) as wrong:
        mobile_register(
            _Request(),
            MobileRegisterBody(
                email="2821961676@qq.com",
                verification_code="654321",
                username="owner",
                password="correct-horse-42",
            ),
        )
    assert wrong.value.status_code == 403

    registered = mobile_register(
        _Request(),
        MobileRegisterBody(
            email="2821961676@qq.com",
            verification_code="123456",
            username="owner",
            password="correct-horse-42",
        ),
    )
    assert registered["account"]["username"] == "owner"
    assert "2821961676@qq.com" not in owner_mobile._REGISTRATION_CODES


def test_registration_code_expires_and_locks_after_five_wrong_attempts():
    email = "2821961676@qq.com"
    owner_mobile._REGISTRATION_CODES[email] = owner_mobile._RegistrationCode(
        digest=owner_mobile._registration_code_digest(email, "123456"),
        expires_at=time.monotonic() - 1,
        sent_at=time.monotonic() - 601,
    )
    with pytest.raises(HTTPException) as expired:
        mobile_register(
            _Request(),
            MobileRegisterBody(
                email=email,
                verification_code="123456",
                username="owner",
                password="correct-horse-42",
            ),
        )
    assert expired.value.status_code == 403
    assert email not in owner_mobile._REGISTRATION_CODES

    _seed_registration_code(email, "123456")
    for _ in range(4):
        with pytest.raises(HTTPException) as wrong:
            mobile_register(
                _Request(),
                MobileRegisterBody(
                    email=email,
                    verification_code="654321",
                    username="owner",
                    password="correct-horse-42",
                ),
            )
        assert wrong.value.status_code == 403
    with pytest.raises(HTTPException) as locked:
        mobile_register(
            _Request(),
            MobileRegisterBody(
                email=email,
                verification_code="654321",
                username="owner",
                password="correct-horse-42",
            ),
        )
    assert locked.value.status_code == 429
    assert email not in owner_mobile._REGISTRATION_CODES


def test_valid_code_survives_transient_config_persistence_failure(monkeypatch):
    from hermes_cli import config as hermes_config

    email = "2821961676@qq.com"
    _seed_registration_code(email, "123456")

    def fail_save(_config):
        raise RuntimeError("temporary write failure")

    monkeypatch.setattr(hermes_config, "save_config", fail_save)

    with pytest.raises(RuntimeError, match="temporary write failure"):
        mobile_register(
            _Request(),
            MobileRegisterBody(
                email=email,
                verification_code="123456",
                username="owner",
                password="correct-horse-42",
            ),
        )

    assert email in owner_mobile._REGISTRATION_CODES
    assert mobile_registration_status()["registration_open"] is True


def test_web_owner_registration_uses_qq_email_verification():
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
        login = client.get("/login")
        payload = _registration_payload()
        registered = client.post(
            "/auth/mobile/register",
            json=payload,
        )
    finally:
        client.close()
        for name, value in previous.items():
            setattr(web_server.app.state, name, value)

    assert login.status_code == 200
    assert "QQ 邮箱" in login.text
    assert 'name="email"' in login.text
    assert 'name="verification_code"' in login.text
    assert "/auth/mobile/registration-code" in login.text
    assert "setup_token" not in login.text
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
            json=_registration_payload(
                device={
                    "id": "route-ios-device",
                    "name": "Route iPhone",
                    "model": "iPhone17,1",
                    "os_version": "18.6",
                    "app_version": "2.0.0",
                },
            ),
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
            json=_registration_payload(
                device={"id": "shared-iphone", "name": "Owner iPhone"},
            ),
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
            json=_registration_payload(
                device={"id": "refresh-ios-device", "name": "Owner iPhone"},
            ),
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
            json=_registration_payload(
                device={"id": "owner-phone-device", "name": "Owner iPhone"},
            ),
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
        active_pushes = MobileDeviceStore().list_active_apns_registrations(
            user_id="owner",
        )
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
