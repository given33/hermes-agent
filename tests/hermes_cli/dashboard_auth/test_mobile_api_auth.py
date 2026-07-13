"""End-to-end contract for native mobile dashboard authentication."""
from __future__ import annotations

import importlib
import os
import sys
import time
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from hermes_cli.dashboard_auth import (
    TokenPrincipal,
    clear_providers,
    get_provider,
    register_provider,
)
from hermes_cli.dashboard_auth.base import assert_protocol_compliance
from hermes_cli.dashboard_auth.cookies import SESSION_AT_COOKIE
from hermes_cli.dashboard_auth import token_auth
from hermes_cli.env_loader import load_hermes_dotenv
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider, _sign


MOBILE_KEY = "native-mobile-test-key-without-url-or-response-exposure"


@dataclass
class MobileAppHarness:
    client: TestClient
    key: str
    valid_cookie: dict[str, str]


@pytest.fixture
def mobile_app(tmp_path, monkeypatch):
    """Load the real dashboard app from an isolated server .env."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text(
        f"HERMES_MOBILE_API_KEY={MOBILE_KEY}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("HERMES_MOBILE_API_KEY", raising=False)
    load_hermes_dotenv(hermes_home=hermes_home)
    assert os.environ["HERMES_MOBILE_API_KEY"] == MOBILE_KEY

    clear_providers()
    token_auth.clear_token_routes()
    clear_optional = getattr(token_auth, "clear_optional_token_prefixes", None)
    if clear_optional is not None:
        clear_optional()

    web_server_was_loaded = "hermes_cli.web_server" in sys.modules
    web_server = importlib.import_module("hermes_cli.web_server")
    from hermes_cli.dashboard_auth.registry import (
        register_mobile_api_provider_if_configured,
    )

    if web_server_was_loaded:
        assert register_mobile_api_provider_if_configured() is True
        token_auth.register_optional_token_prefix("/api")
    assert get_provider("mobile-api") is not None
    assert token_auth.is_optional_token_path("/api/sessions") is True
    register_provider(StubAuthProvider())

    previous = {
        "bound_host": getattr(web_server.app.state, "bound_host", None),
        "bound_port": getattr(web_server.app.state, "bound_port", None),
        "auth_required": getattr(web_server.app.state, "auth_required", None),
    }
    web_server.app.state.bound_host = "mobile.test"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True

    valid_access_token = _sign({
        "sub": "stub-user-1",
        "email": "stub@example.test",
        "name": "Stub User",
        "org_id": "stub-org-1",
        "exp": int(time.time()) + 3600,
    })
    client = TestClient(web_server.app, base_url="https://mobile.test")
    try:
        yield MobileAppHarness(
            client=client,
            key=MOBILE_KEY,
            valid_cookie={SESSION_AT_COOKIE: valid_access_token},
        )
    finally:
        client.close()
        clear_providers()
        token_auth.clear_token_routes()
        if clear_optional is not None:
            clear_optional()
        web_server.app.state.bound_host = previous["bound_host"]
        web_server.app.state.bound_port = previous["bound_port"]
        web_server.app.state.auth_required = previous["auth_required"]
        os.environ.pop("HERMES_MOBILE_API_KEY", None)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _request_with_valid_cookie(
    mobile_app: MobileAppHarness,
    method: str,
    path: str,
    **kwargs,
):
    mobile_app.client.cookies.update(mobile_app.valid_cookie)
    try:
        return mobile_app.client.request(method, path, **kwargs)
    finally:
        mobile_app.client.cookies.clear()


def test_mobile_provider_protocol_and_admin_principal(monkeypatch):
    from hermes_cli.dashboard_auth.mobile_api_provider import MobileApiKeyProvider

    assert_protocol_compliance(MobileApiKeyProvider)
    monkeypatch.setenv("HERMES_MOBILE_API_KEY", MOBILE_KEY)
    provider = MobileApiKeyProvider()

    assert provider.verify_token(token="wrong") is None
    assert provider.verify_token(token=MOBILE_KEY) == TokenPrincipal(
        principal="ios-native",
        provider="mobile-api",
        scopes=("dashboard:admin",),
    )


def test_mobile_key_metadata_is_secret():
    from hermes_cli.config import OPTIONAL_ENV_VARS

    metadata = OPTIONAL_ENV_VARS["HERMES_MOBILE_API_KEY"]
    assert metadata["password"] is True


def test_mobile_provider_not_registered_without_key(monkeypatch):
    from hermes_cli.dashboard_auth.registry import (
        register_mobile_api_provider_if_configured,
    )

    clear_providers()
    monkeypatch.delenv("HERMES_MOBILE_API_KEY", raising=False)

    assert register_mobile_api_provider_if_configured() is False
    assert get_provider("mobile-api") is None


def test_optional_mobile_prefix_preserves_browser_cookie_auth(mobile_app):
    response = _request_with_valid_cookie(
        mobile_app,
        "GET",
        "/api/status",
    )

    assert response.status_code == 200


def test_optional_mobile_prefix_accepts_mobile_bearer(mobile_app):
    response = mobile_app.client.get(
        "/api/status",
        headers=_bearer(mobile_app.key),
    )

    assert response.status_code == 200


def test_invalid_mobile_bearer_fails_closed_even_with_cookie(mobile_app):
    response = _request_with_valid_cookie(
        mobile_app,
        "GET",
        "/api/status",
        headers=_bearer("wrong"),
    )

    assert response.status_code == 401


@pytest.mark.parametrize("authorization", ["Bearer", "Bearer   ", "Bearer\tbad"])
def test_malformed_mobile_bearer_fails_closed(mobile_app, authorization):
    response = _request_with_valid_cookie(
        mobile_app,
        "GET",
        "/api/status",
        headers={"Authorization": authorization},
    )

    assert response.status_code == 401


def test_protected_api_route_accepts_cookie_and_mobile_bearer(mobile_app):
    cookie_response = _request_with_valid_cookie(
        mobile_app,
        "GET",
        "/api/sessions",
    )
    bearer_response = mobile_app.client.get(
        "/api/sessions",
        headers=_bearer(mobile_app.key),
    )

    assert cookie_response.status_code == 200
    assert bearer_response.status_code == 200


def test_mobile_admin_passes_handler_level_token_check(mobile_app):
    response = mobile_app.client.post(
        "/api/providers/oauth/not-a-provider/start",
        headers=_bearer(mobile_app.key),
    )

    assert response.status_code == 400
    assert "Unknown provider" in response.json()["detail"]


def test_non_admin_token_provider_cannot_use_mobile_prefix(mobile_app):
    from plugins.dashboard_auth.drain import DrainSecretProvider

    drain_key = (
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_abcdef"
    )
    register_provider(DrainSecretProvider(secret=drain_key))

    response = mobile_app.client.get(
        "/api/sessions",
        headers=_bearer(drain_key),
    )

    assert response.status_code == 401


def test_mobile_key_is_not_returned_by_env_endpoints(mobile_app):
    listing = mobile_app.client.get(
        "/api/env",
        headers=_bearer(mobile_app.key),
    )
    reveal = mobile_app.client.post(
        "/api/env/reveal",
        headers=_bearer(mobile_app.key),
        json={"key": "HERMES_MOBILE_API_KEY"},
    )

    assert listing.status_code == 200
    assert listing.json()["HERMES_MOBILE_API_KEY"]["is_set"] is True
    assert listing.json()["HERMES_MOBILE_API_KEY"]["redacted_value"] is None
    assert mobile_app.key not in listing.text
    assert reveal.status_code == 403
    assert mobile_app.key not in reveal.text


def test_handshake_reports_versioned_capabilities_without_secret(mobile_app, caplog):
    response = mobile_app.client.get(
        "/api/mobile/v1/handshake",
        headers=_bearer(mobile_app.key),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == 1
    assert body["hermes_version"]
    assert isinstance(body["profiles"], list)
    assert "chat" in body["capabilities"]
    assert body["server_time"]
    assert mobile_app.key not in response.text
    assert mobile_app.key not in str(response.request.url)
    assert mobile_app.key not in caplog.text


def test_optional_prefix_does_not_match_apix(mobile_app):
    response = _request_with_valid_cookie(
        mobile_app,
        "GET",
        "/apix",
        headers=_bearer("wrong"),
        follow_redirects=False,
    )

    assert response.status_code != 401


def test_exact_token_route_keeps_token_only_behavior(mobile_app):
    token_auth.register_token_route("/api/gateway/drain")

    response = _request_with_valid_cookie(
        mobile_app,
        "POST",
        "/api/gateway/drain",
    )

    assert response.status_code == 401
