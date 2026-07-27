"""Chronos fire authentication and request contract.

The dashboard FastAPI listener and the aiohttp API listener expose the same
managed-cron callback.  Authentication and payload validation live here so a
security change cannot land on only one public surface.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Generic, Mapping, Optional, TypeVar

from hermes_runtime.config import cfg_get, load_config
from hermes_secret_compare import extract_bearer_token

from .contracts import ServiceFailure

logger = logging.getLogger("cron.chronos.verify")

_FIRE_PURPOSE = "cron_fire"
_JWK_CLIENTS: Dict[str, Any] = {}
_JWK_CLIENTS_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class CronFireAuthorization:
    claims: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CronFireCommand:
    job_id: str
    claims: Mapping[str, Any]


CronFireTarget = TypeVar("CronFireTarget")


@dataclass(frozen=True, slots=True)
class CronFireAcceptance(Generic[CronFireTarget]):
    """Framework-neutral result returned to an HTTP transport adapter."""

    status_code: int
    body: Mapping[str, Any]
    background_task: asyncio.Task[Any] | None = None
    target: CronFireTarget | None = None
    failure: ServiceFailure | None = None


def _get_jwk_client(jwks_url: str) -> Any:
    """Return the process-cached PyJWKClient for a JWKS URL."""
    client = _JWK_CLIENTS.get(jwks_url)
    if client is not None:
        return client
    with _JWK_CLIENTS_LOCK:
        client = _JWK_CLIENTS.get(jwks_url)
        if client is None:
            from jwt import PyJWKClient

            client = PyJWKClient(jwks_url)
            _JWK_CLIENTS[jwks_url] = client
        return client


def verify_nas_fire_token(
    *,
    token: str,
    expected_audience: str,
    jwks_or_key: Optional[str] = None,
    issuer: Optional[str] = None,
    leeway_seconds: int = 30,
) -> Optional[Dict[str, Any]]:
    """Verify a purpose-scoped NAS JWT and return its claims.

    Signature, audience, expiry, optional issuer, and the ``cron_fire`` purpose
    are all mandatory.  Failures are deliberately collapsed to ``None`` so the
    public adapter does not reveal which credential check failed.
    """
    if not token or not expected_audience or not jwks_or_key:
        if token and expected_audience and not jwks_or_key:
            logger.warning("cron fire: no JWKS/key configured; refusing token")
        return None

    try:
        import jwt

        if jwks_or_key.startswith(("http://", "https://")):
            signing_key = _get_jwk_client(jwks_or_key).get_signing_key_from_jwt(token).key
        else:
            signing_key = jwks_or_key

        decode_kwargs: Dict[str, Any] = {
            "algorithms": ["RS256", "RS384", "RS512", "ES256", "ES384"],
            "audience": expected_audience,
            "leeway": leeway_seconds,
            "options": {"require": ["exp", "aud"]},
        }
        if issuer:
            decode_kwargs["issuer"] = issuer
        claims = jwt.decode(token, signing_key, **decode_kwargs)
    except Exception as exc:
        logger.warning("cron fire: token verification failed: %s", exc)
        return None

    if claims.get("purpose") != _FIRE_PURPOSE:
        logger.warning("cron fire: token missing/!=%s purpose claim", _FIRE_PURPOSE)
        return None
    return claims


def get_fire_verifier() -> Callable[..., Optional[Dict[str, Any]]]:
    """Return the active inbound fire verifier."""
    return verify_nas_fire_token


def authorize_cron_fire(
    authorization_header: str | None,
    *,
    config: Mapping[str, Any] | None = None,
    verifier: Callable[..., Optional[Dict[str, Any]]] | None = None,
) -> tuple[CronFireAuthorization | None, ServiceFailure | None]:
    """Authenticate a Chronos callback independently of an HTTP framework."""
    cfg = config if config is not None else load_config()
    active_verifier = verifier or get_fire_verifier()
    claims = active_verifier(
        token=extract_bearer_token(authorization_header),
        expected_audience=cfg_get(
            cfg, "cron", "chronos", "expected_audience", default=""
        ),
        jwks_or_key=cfg_get(
            cfg, "cron", "chronos", "nas_jwks_url", default=""
        ) or None,
        issuer=cfg_get(cfg, "cron", "chronos", "portal_url", default="") or None,
    )
    if claims is None:
        return None, ServiceFailure(401, "invalid_fire_token", "invalid fire token")
    return CronFireAuthorization(claims=claims), None


def parse_cron_fire_payload(
    payload: Any,
    authorization: CronFireAuthorization,
) -> tuple[CronFireCommand | None, ServiceFailure | None]:
    """Validate the callback body after authentication has succeeded."""
    job_id = payload.get("job_id") if isinstance(payload, Mapping) else None
    if not isinstance(job_id, str) or not job_id.strip():
        return None, ServiceFailure(400, "missing_job_id", "missing job_id")
    normalized = job_id.strip()
    if len(normalized) > 512:
        return None, ServiceFailure(400, "invalid_job_id", "invalid job_id")
    return CronFireCommand(job_id=normalized, claims=authorization.claims), None


async def accept_cron_fire_request(
    authorization_header: str | None,
    payload: Any,
    *,
    execute: Callable[[CronFireCommand, CronFireTarget | None], Awaitable[Any]],
    resolve_target: Callable[[CronFireCommand], Awaitable[CronFireTarget | None]]
    | None = None,
    config: Mapping[str, Any] | None = None,
    verifier: Callable[..., Optional[Dict[str, Any]]] | None = None,
) -> CronFireAcceptance[CronFireTarget]:
    """Authorize, validate, resolve and accept one Chronos fire request.

    The FastAPI and aiohttp listeners share this application flow. Target
    lookup remains injected because the dashboard resolves an owning profile,
    while the gateway executes inside its already-selected profile. The
    returned task lets each adapter attach its own drain/lifecycle accounting.
    """
    authorization, failure = await asyncio.to_thread(
        authorize_cron_fire,
        authorization_header,
        config=config,
        verifier=verifier,
    )
    if failure is not None:
        return CronFireAcceptance(
            status_code=failure.status_code,
            body=failure.simple_body(),
            failure=failure,
        )
    assert authorization is not None

    command, failure = parse_cron_fire_payload(payload, authorization)
    if failure is not None:
        return CronFireAcceptance(
            status_code=failure.status_code,
            body=failure.simple_body(),
            failure=failure,
        )
    assert command is not None

    target: CronFireTarget | None = None
    if resolve_target is not None:
        target = await resolve_target(command)
        if target is None:
            return CronFireAcceptance(
                status_code=200,
                body={"status": "gone", "job_id": command.job_id},
            )

    task = asyncio.create_task(
        execute(command, target),
        name=f"cron-fire:{command.job_id}",
    )
    return CronFireAcceptance(
        status_code=202,
        body={"status": "accepted", "job_id": command.job_id},
        background_task=task,
        target=target,
    )


__all__ = [
    "CronFireAcceptance",
    "CronFireAuthorization",
    "CronFireCommand",
    "accept_cron_fire_request",
    "authorize_cron_fire",
    "get_fire_verifier",
    "parse_cron_fire_payload",
    "verify_nas_fire_token",
]
