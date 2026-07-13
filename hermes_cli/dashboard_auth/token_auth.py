"""Route-agnostic non-interactive (bearer-token) auth seam for the dashboard.

This is the generic API-token capability (decisions.md Q-C): a reusable seam
that ANY service-to-service / machine-credential provider plugs into, NOT a
drain-specific hook. The drain bearer-secret plugin is merely the first
consumer.

How it fits the existing auth framework:

  * The interactive gate (``gated_auth_middleware``) authenticates a human
    via a session cookie on every non-public route. A service caller has no
    cookie — it presents a bearer token in the ``Authorization`` header on a
    single request. That is what this seam verifies.

  * A route opts in by registering its exact path via
    :func:`register_token_route`. An API family may additionally accept an
    admin bearer while preserving cookie auth via
    :func:`register_optional_token_prefix`.

  * :func:`token_auth_middleware` runs OUTERMOST (installed last in
    ``web_server.py``). For a token route it fully owns the auth decision:
    authenticate via the stacked token providers, attach the verified
    :class:`~hermes_cli.dashboard_auth.base.TokenPrincipal` to
    ``request.state.token_principal`` + set ``request.state.token_authenticated``,
    and pass through; otherwise reject (401 unauthenticated, or 503 when a
    provider's backing store was unreachable). The downstream cookie/session
    gates honour ``token_authenticated`` and skip enforcement, so a
    token-authed service request is never bounced to ``/login``.

  * Fails closed: a token route with no registered token provider, no token,
    or an unrecognised token gets 401 — never an open pass-through.

Provider stacking mirrors ``verify_session``: each ``supports_token`` provider
is consulted in registration order until one returns a principal. A provider
that doesn't recognise the token returns ``None`` and the seam moves on; a
provider whose backing store is unreachable raises ``ProviderError``, which the
seam remembers and surfaces as 503 only if NO provider accepts the token.
"""
from __future__ import annotations

import logging
import threading
from typing import Awaitable, Callable, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from hermes_cli.dashboard_auth import list_token_providers
from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
from hermes_cli.dashboard_auth.base import ProviderError, TokenPrincipal

_log = logging.getLogger(__name__)

# Exact paths that require non-interactive bearer-token auth, plus prefixes
# that optionally accept an admin bearer and otherwise preserve cookie auth.
_token_routes: set[str] = set()
_optional_token_prefixes: set[str] = set()
_lock = threading.Lock()
_OPTIONAL_PREFIX_SCOPE = "dashboard:admin"


def register_token_route(path: str) -> None:
    """Mark ``path`` (exact match) as token-authable.

    Idempotent. Call at module import / app setup so the seam knows which
    routes to guard. Registering a route does NOT make it public — it makes
    it authenticate by token instead of by session cookie.
    """
    with _lock:
        _token_routes.add(path)


def register_optional_token_prefix(prefix: str) -> None:
    """Allow an admin bearer under ``prefix`` while preserving cookie auth."""
    normalized = "/" + prefix.strip("/")
    with _lock:
        _optional_token_prefixes.add(normalized)


def is_token_route(path: str) -> bool:
    """True if ``path`` was registered as token-authable (exact match)."""
    with _lock:
        return path in _token_routes


def is_optional_token_path(path: str) -> bool:
    """True when ``path`` is at or below a registered optional prefix."""
    with _lock:
        prefixes = tuple(_optional_token_prefixes)
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in prefixes
    )


def clear_token_routes() -> None:
    """Test-only: drop all registered token routes."""
    with _lock:
        _token_routes.clear()


def clear_optional_token_prefixes() -> None:
    """Test-only: drop all optional token prefixes."""
    with _lock:
        _optional_token_prefixes.clear()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def extract_bearer_token(request: Request) -> str:
    """Return the bearer token from the ``Authorization`` header, or "".

    Accepts ``<scheme> <token>`` where scheme is "bearer" (case-insensitive).
    Returns an empty string for a missing/malformed header or a non-bearer
    scheme — the caller treats "" as "no token presented".
    """
    auth = request.headers.get("authorization", "")
    parts = auth.split(" ", 1)
    if len(parts) == 2 and parts[0].strip().lower() == "bearer":
        return parts[1].strip()
    return ""


def _has_explicit_bearer_scheme(request: Request) -> bool:
    """Detect a Bearer attempt even when the header has no usable token."""
    auth = request.headers.get("authorization", "").lstrip()
    if not auth:
        return False
    return auth.split(None, 1)[0].lower() == "bearer"


def authenticate_token(
    request: Request,
    *,
    required_scope: Optional[str] = None,
) -> Tuple[Optional[TokenPrincipal], Optional[str]]:
    """Try every token provider against the request's bearer token.

    When ``required_scope`` is set, a recognised principal without that scope
    is skipped and cannot authorise the request.

    Returns ``(principal, unreachable_provider_name)``:
      * ``(TokenPrincipal, None)`` — a provider recognised and accepted the token.
      * ``(None, None)`` — no token, or no provider recognised it (reject 401).
      * ``(None, name)`` — no provider accepted it AND at least one provider's
        backing store was unreachable (the caller surfaces 503, not 401, so a
        transient outage doesn't read as "bad credentials").

    Never raises: a provider ``ProviderError`` is caught and remembered.
    """
    token = extract_bearer_token(request)
    if not token:
        return None, None
    unreachable: Optional[str] = None
    recognised_without_scope = False
    for provider in list_token_providers():
        try:
            principal = provider.verify_token(token=token)
        except ProviderError as e:
            _log.warning(
                "dashboard-auth: token provider %r unreachable during verify: %s",
                provider.name, e,
            )
            if unreachable is None:
                unreachable = provider.name
            continue
        except Exception as e:  # noqa: BLE001 — a buggy provider must not 500 the gate
            _log.warning(
                "dashboard-auth: token provider %r raised during verify: %s",
                provider.name, e,
            )
            continue
        if principal is not None and (
            required_scope is None or required_scope in principal.scopes
        ):
            return principal, None
        if principal is not None:
            recognised_without_scope = True
    return None, None if recognised_without_scope else unreachable


def token_failure_response(request: Request, unreachable: Optional[str]) -> Response:
    """Build the shared fail-closed response without exposing credentials."""
    path = request.url.path
    if unreachable:
        audit_log(
            AuditEvent.TOKEN_AUTH_FAILURE,
            provider=unreachable,
            reason="provider_unreachable",
            path=path,
            ip=_client_ip(request),
        )
        return JSONResponse(
            {"detail": f"Auth provider {unreachable!r} unreachable"},
            status_code=503,
        )

    audit_log(
        AuditEvent.TOKEN_AUTH_FAILURE,
        reason="no_provider_recognises_token",
        path=path,
        ip=_client_ip(request),
    )
    return JSONResponse(
        {"error": "unauthenticated", "detail": "Unauthorized"},
        status_code=401,
    )


async def token_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Outermost auth seam for token-authable routes.

    No-op pass-through for paths outside the exact-route and optional-prefix
    registries. Exact routes remain token-only. Optional prefixes accept an
    admin bearer, but delegate to the existing cookie gate when no Bearer was
    attempted:

      * valid token  → attach principal + ``token_authenticated`` flag, pass through.
      * unreachable  → 503 (provider backing store down; not "bad credentials").
      * otherwise    → 401 unauthenticated.

    Runs before the cookie/session gates (installed last in ``web_server.py``).
    The cookie gates honour ``request.state.token_authenticated`` and skip
    enforcement, so a token-authed request is never redirected to ``/login``.
    """
    path = request.url.path
    exact_route = is_token_route(path)
    optional_prefix = is_optional_token_path(path)
    if not exact_route and not optional_prefix:
        return await call_next(request)

    if optional_prefix and not exact_route and not extract_bearer_token(request):
        if _has_explicit_bearer_scheme(request):
            return token_failure_response(request, None)
        return await call_next(request)

    principal, unreachable = authenticate_token(
        request,
        required_scope=None if exact_route else _OPTIONAL_PREFIX_SCOPE,
    )
    if principal is not None:
        request.state.token_principal = principal
        request.state.token_authenticated = True
        return await call_next(request)

    return token_failure_response(request, unreachable)
