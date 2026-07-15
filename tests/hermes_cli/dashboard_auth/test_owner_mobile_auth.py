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
    yield
    clear_providers()
    token_auth.clear_optional_token_prefixes()
    _reset_password_rate_limit()


def _register(username: str = "owner", password: str = "correct-horse-42"):
    return mobile_register(
        _Request(),
        MobileRegisterBody(username=username, password=password),
    )


def test_first_registration_persists_only_scrypt_hash_and_stable_secret():
    from hermes_cli.dashboard_auth.login_page import render_login_html

    before = mobile_registration_status()
    registration_page = render_login_html()
    result = _register()
    after = mobile_registration_status()

    from hermes_cli.config import load_config

    section = load_config()["dashboard"]["basic_auth"]
    assert before == {"registration_open": True, "account_configured": False}
    assert "创建此 Hermes 服务器的所有者账号" in registration_page
    assert "owner-registration-form" in registration_page
    assert "注册并登录" in registration_page
    assert after == {"registration_open": False, "account_configured": True}
    assert section["username"] == "owner"
    assert section["password_hash"].startswith("scrypt$")
    assert "correct-horse-42" not in section["password_hash"]
    assert section["password"] == ""
    assert len(base64.b64decode(section["secret"])) == 32
    assert result["token_type"] == "Bearer"
    assert result["account"]["username"] == "owner"
    assert result["access_token"]
    assert result["refresh_token"]


def test_registration_registers_basic_provider_for_api_bearer_requests():
    result = _register()
    provider = get_provider("basic")

    assert provider is not None
    principal = provider.verify_token(token=result["access_token"])
    assert principal is not None
    assert principal.principal == "owner"
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
        MobileLoginBody(username="owner", password="correct-horse-42"),
    )
    refreshed = mobile_refresh(
        _Request(),
        MobileRefreshBody(refresh_token=logged_in["refresh_token"]),
    )

    assert logged_in["account"]["username"] == "owner"
    assert refreshed["account"]["username"] == "owner"
    assert refreshed["access_token"]
    assert refreshed["refresh_token"]
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
            json={"username": "owner", "password": "correct-horse-42"},
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
