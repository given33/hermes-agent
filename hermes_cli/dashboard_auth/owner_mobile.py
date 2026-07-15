"""First-owner registration and token endpoints for native clients.

Hermes is a personal agent, so one server has one owner account. Every client
that signs in as that owner reads and writes the same server-side Hermes home;
clients never replicate the session or configuration database locally.
"""
from __future__ import annotations

import base64
import re
import secrets
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hermes_cli.dashboard_auth import (
    InvalidCredentialsError,
    RefreshExpiredError,
    get_provider,
    register_provider,
)
from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
from hermes_cli.dashboard_auth.routes import _client_ip, _password_rate_limited
from hermes_cli.dashboard_auth.token_auth import register_optional_token_prefix
from plugins.dashboard_auth.basic import (
    BasicAuthProvider,
    _DEFAULT_TTL_SECONDS,
    _load_config_basic_auth_section,
    _resolve,
    _resolve_secret,
    hash_password,
)


router = APIRouter()
_REGISTRATION_LOCK = threading.Lock()
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")


class MobileRegisterBody(BaseModel):
    username: str
    password: str


class MobileLoginBody(BaseModel):
    username: str
    password: str


class MobileRefreshBody(BaseModel):
    refresh_token: str


def _configured_credentials() -> tuple[str, str, str]:
    section = _load_config_basic_auth_section()
    return (
        _resolve("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", section, "username"),
        _resolve(
            "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH",
            section,
            "password_hash",
        ),
        _resolve("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", section, "password"),
    )


def owner_account_configured() -> bool:
    username, password_hash, plaintext = _configured_credentials()
    return bool(username and (password_hash or plaintext))


def owner_registration_open() -> bool:
    username, password_hash, plaintext = _configured_credentials()
    return not any((username, password_hash, plaintext))


def _validate_registration(username: str, password: str) -> tuple[str, str]:
    normalized_username = username.strip()
    if not _USERNAME_RE.fullmatch(normalized_username):
        raise HTTPException(
            status_code=422,
            detail="Username must be 3-64 letters, numbers, dots, dashes, or underscores",
        )
    if not 8 <= len(password) <= 256 or "\x00" in password:
        raise HTTPException(
            status_code=422,
            detail="Password must contain 8-256 characters",
        )
    return normalized_username, password


def _build_provider_from_config() -> BasicAuthProvider | None:
    username, password_hash, plaintext = _configured_credentials()
    if not username or not (password_hash or plaintext):
        return None

    section = _load_config_basic_auth_section()
    plaintext_from_env = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", {}, "password"
    )
    if plaintext_from_env:
        password_hash = hash_password(plaintext_from_env)
    elif not password_hash:
        password_hash = hash_password(plaintext)

    ttl_raw = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_TTL_SECONDS",
        section,
        "session_ttl_seconds",
    )
    try:
        ttl = int(ttl_raw) if ttl_raw else _DEFAULT_TTL_SECONDS
    except ValueError:
        ttl = _DEFAULT_TTL_SECONDS
    return BasicAuthProvider(
        username=username,
        password_hash=password_hash,
        secret=_resolve_secret(section),
        ttl_seconds=ttl,
    )


def ensure_owner_provider() -> BasicAuthProvider | None:
    existing = get_provider("basic")
    if existing is not None:
        if not isinstance(existing, BasicAuthProvider):
            return None
        register_optional_token_prefix("/api")
        return existing
    provider = _build_provider_from_config()
    if provider is None:
        return None
    try:
        register_provider(provider)
    except ValueError:
        existing = get_provider("basic")
        return existing if isinstance(existing, BasicAuthProvider) else None
    register_optional_token_prefix("/api")
    return provider


def _token_response(session) -> dict[str, Any]:
    return {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "token_type": "Bearer",
        "expires_at": session.expires_at,
        "account": {
            "username": session.user_id,
            "display_name": session.display_name,
        },
    }


@router.get("/auth/mobile/status")
def mobile_registration_status() -> dict[str, bool]:
    return {
        "registration_open": owner_registration_open(),
        "account_configured": owner_account_configured(),
    }


@router.post("/auth/mobile/register")
def mobile_register(request: Request, body: MobileRegisterBody):
    ip = _client_ip(request)
    if _password_rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many attempts")
    username, password = _validate_registration(body.username, body.password)

    with _REGISTRATION_LOCK:
        if not owner_registration_open():
            raise HTTPException(status_code=409, detail="Owner account already exists")

        from hermes_cli.config import load_config, save_config

        config = load_config()
        dashboard = config.setdefault("dashboard", {})
        basic = dashboard.setdefault("basic_auth", {})
        basic.update(
            {
                "username": username,
                "password_hash": hash_password(password),
                "password": "",
                "secret": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
                "session_ttl_seconds": _DEFAULT_TTL_SECONDS,
            }
        )
        save_config(config)
        provider = ensure_owner_provider()
        if provider is None:
            raise HTTPException(status_code=500, detail="Owner account unavailable")

    session = provider.complete_password_login(username=username, password=password)
    audit_log(
        AuditEvent.LOGIN_SUCCESS,
        provider=provider.name,
        user_id=session.user_id,
        ip=ip,
    )
    return _token_response(session)


@router.post("/auth/mobile/token")
def mobile_login(request: Request, body: MobileLoginBody):
    ip = _client_ip(request)
    if _password_rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many attempts")
    provider = ensure_owner_provider()
    if provider is None:
        raise HTTPException(status_code=409, detail="Owner account is not configured")
    try:
        session = provider.complete_password_login(
            username=body.username.strip(),
            password=body.password,
        )
    except InvalidCredentialsError:
        audit_log(
            AuditEvent.LOGIN_FAILURE,
            provider=provider.name,
            reason="invalid_credentials",
            ip=ip,
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")
    audit_log(
        AuditEvent.LOGIN_SUCCESS,
        provider=provider.name,
        user_id=session.user_id,
        ip=ip,
    )
    return _token_response(session)


@router.post("/auth/mobile/refresh")
def mobile_refresh(request: Request, body: MobileRefreshBody):
    ip = _client_ip(request)
    if _password_rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many attempts")
    provider = ensure_owner_provider()
    if provider is None:
        raise HTTPException(status_code=409, detail="Owner account is not configured")
    try:
        session = provider.refresh_session(refresh_token=body.refresh_token)
    except RefreshExpiredError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    return _token_response(session)


@router.post("/auth/mobile/logout")
def mobile_logout() -> dict[str, bool]:
    # BasicAuthProvider tokens are stateless. The native client removes both
    # tokens from Keychain; their bounded server-side expiry remains unchanged.
    return {"ok": True}
