"""First-owner registration and token endpoints for native clients.

Hermes is a personal agent, so one server has one owner account. Every client
that signs in as that owner reads and writes the same server-side Hermes home;
clients never replicate the session or configuration database locally.
"""
from __future__ import annotations

import base64
import hmac
import os
import re
import secrets
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hermes_cli.dashboard_auth import (
    InvalidCredentialsError,
    get_provider,
    register_provider,
)
from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
from hermes_cli.dashboard_auth.mobile_device_store import (
    MobileDeviceInfo,
    MobileDeviceStore,
    MobileTokenPair,
    OwnerMobileTokenProvider,
)
from hermes_cli.dashboard_auth.routes import _client_ip, _password_rate_limited
from hermes_cli.dashboard_auth.token_auth import (
    extract_bearer_token,
    register_optional_token_prefix,
)
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


class MobileDeviceBody(BaseModel):
    id: str = ""
    name: str = ""
    model: str = ""
    os_version: str = ""
    app_version: str = ""


class MobileRegisterBody(BaseModel):
    username: str
    password: str
    setup_token: str = ""
    device: MobileDeviceBody | None = None


class MobileLoginBody(BaseModel):
    username: str
    password: str
    device: MobileDeviceBody | None = None


class MobileRefreshBody(BaseModel):
    refresh_token: str


class MobileLogoutBody(BaseModel):
    refresh_token: str = ""


class MobileApnsBody(BaseModel):
    token: str
    environment: str
    bundle_id: str


def _store() -> MobileDeviceStore:
    return MobileDeviceStore()


def _device_info(body: MobileDeviceBody | None) -> MobileDeviceInfo:
    if body is None:
        return MobileDeviceInfo()
    return MobileDeviceInfo(
        id=body.id,
        name=body.name,
        model=body.model,
        os_version=body.os_version,
        app_version=body.app_version,
    )


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


def owner_setup_token_required() -> bool:
    return bool(os.environ.get("HERMES_OWNER_SETUP_TOKEN", "").strip())


def _validate_owner_setup_token(value: str) -> None:
    expected = os.environ.get("HERMES_OWNER_SETUP_TOKEN", "").strip()
    if expected and not hmac.compare_digest(value.strip(), expected):
        raise HTTPException(status_code=403, detail="Invalid owner setup token")


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


def ensure_mobile_token_provider() -> OwnerMobileTokenProvider:
    existing = get_provider(OwnerMobileTokenProvider.name)
    if existing is not None:
        if not isinstance(existing, OwnerMobileTokenProvider):
            raise RuntimeError("owner-mobile auth provider name is already in use")
        register_optional_token_prefix("/api")
        return existing
    provider = OwnerMobileTokenProvider()
    try:
        register_provider(provider)
    except ValueError:
        existing = get_provider(OwnerMobileTokenProvider.name)
        if not isinstance(existing, OwnerMobileTokenProvider):
            raise RuntimeError("owner-mobile auth provider registration failed")
        provider = existing
    register_optional_token_prefix("/api")
    return provider


def ensure_owner_provider() -> BasicAuthProvider | None:
    ensure_mobile_token_provider()
    existing = get_provider("basic")
    if existing is not None:
        if not isinstance(existing, BasicAuthProvider):
            return None
        return existing
    provider = _build_provider_from_config()
    if provider is None:
        return None
    try:
        register_provider(provider)
    except ValueError:
        existing = get_provider("basic")
        return existing if isinstance(existing, BasicAuthProvider) else None
    return provider


def _token_response(tokens: MobileTokenPair) -> dict[str, Any]:
    return {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "token_type": "Bearer",
        "expires_at": tokens.session.access_expires_at,
        "refresh_expires_at": tokens.session.refresh_expires_at,
        "session_id": tokens.session.session_id,
        "device_id": tokens.session.device_id,
        "account": {
            "username": tokens.session.user_id,
            "display_name": tokens.session.user_id,
        },
    }


@router.get("/auth/mobile/status")
def mobile_registration_status() -> dict[str, bool]:
    return {
        "registration_open": owner_registration_open(),
        "account_configured": owner_account_configured(),
        "setup_token_required": owner_setup_token_required(),
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
        _validate_owner_setup_token(body.setup_token)

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

    provider.complete_password_login(username=username, password=password)
    tokens = _store().create_session(
        user_id=username,
        device=_device_info(body.device),
    )
    audit_log(
        AuditEvent.LOGIN_SUCCESS,
        provider=provider.name,
        user_id=username,
        ip=ip,
    )
    return _token_response(tokens)


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
    return _token_response(
        _store().create_session(
            user_id=session.user_id,
            device=_device_info(body.device),
        )
    )


@router.post("/auth/mobile/refresh")
def mobile_refresh(request: Request, body: MobileRefreshBody):
    ip = _client_ip(request)
    if _password_rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many attempts")
    if ensure_owner_provider() is None:
        raise HTTPException(status_code=409, detail="Owner account is not configured")
    tokens = _store().rotate_refresh(body.refresh_token)
    if tokens is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    return _token_response(tokens)


@router.post("/auth/mobile/logout")
def mobile_logout(request: Request, body: MobileLogoutBody) -> dict[str, bool]:
    revoked = _store().revoke_session(
        access_token=extract_bearer_token(request),
        refresh_token=body.refresh_token.strip(),
        reason="logout",
    )
    return {"ok": True, "revoked": revoked}


def _current_mobile_session(request: Request):
    token = extract_bearer_token(request)
    return _store().verify_access(token, touch=False) if token else None


@router.get("/api/mobile/v1/devices")
def mobile_devices(request: Request) -> dict[str, Any]:
    current = _current_mobile_session(request)
    return {
        "devices": _store().list_devices(
            current_device_id=current.device_id if current else "",
        )
    }


@router.delete("/api/mobile/v1/devices/{device_id}")
def revoke_mobile_device(device_id: str) -> dict[str, bool]:
    if not _store().revoke_device(device_id, reason="owner_revoked_device"):
        raise HTTPException(status_code=404, detail="Device not found")
    return {"ok": True}


@router.put("/api/mobile/v1/devices/{device_id}/apns")
def register_mobile_apns(
    request: Request,
    device_id: str,
    body: MobileApnsBody,
) -> dict[str, Any]:
    current = _current_mobile_session(request)
    if current is None or current.device_id != device_id:
        raise HTTPException(status_code=403, detail="APNs registration must use the current device")
    try:
        registration = _store().register_apns(
            device_id=device_id,
            token=body.token,
            environment=body.environment,
            bundle_id=body.bundle_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Device not found") from exc
    return {"ok": True, "registration": registration}


@router.delete("/api/mobile/v1/devices/{device_id}/apns")
def unregister_mobile_apns(
    request: Request,
    device_id: str,
    environment: str = "",
    bundle_id: str = "",
) -> dict[str, Any]:
    current = _current_mobile_session(request)
    if current is None or current.device_id != device_id:
        raise HTTPException(status_code=403, detail="APNs removal must use the current device")
    count = _store().unregister_apns(
        device_id=device_id,
        environment=environment,
        bundle_id=bundle_id,
    )
    return {"ok": True, "removed": count}
