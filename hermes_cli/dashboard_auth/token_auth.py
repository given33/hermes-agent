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
    :func:`register_token_route`. Only registered paths are token-authable;
    everything else is untouched, so this can never accidentally widen the
    auth surface of an existing route.

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
import hmac
import threading
from typing import Awaitable, Callable, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from hermes_cli.dashboard_auth import list_token_providers
from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
from hermes_cli.dashboard_auth.base import ProviderError, TokenPrincipal
from hermes_cli.dashboard_auth.client_ip import client_ip as _client_ip
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

_log = logging.getLogger(__name__)

# Exact paths that accept non-interactive bearer-token auth. A route registers
# itself here at import/startup; the seam only acts on registered paths.
_token_routes: set[str] = set()
_lock = threading.Lock()

# Path prefixes that accept a bearer token as an alternative to the session
# cookie. A plugin/module registers its API subtree here when token auth is
# optional (i.e. the auth seam should try the bearer token if present but
# fall back to cookie-gated access).
_optional_token_prefixes: set[str] = set()
_optional_token_scopes: dict[str, str | None] = {}
_optional_prefix_lock = threading.Lock()


def register_optional_token_prefix(
    prefix: str,
    *,
    required_scope: str | None = None,
) -> None:
    """Mark every path under ``prefix`` as accepting optional bearer auth.

    Idempotent. The prefix is matched with ``path.startswith(prefix)``
    — include the leading slash, no trailing slash (e.g. ``"/api"``).
    """
    with _optional_prefix_lock:
        normalized = prefix.rstrip("/") or "/"
        _optional_token_prefixes.add(normalized)
        if required_scope is not None:
            _optional_token_scopes[normalized] = required_scope


def is_optional_token_prefix(path: str) -> bool:
    """True if ``path`` falls under any registered optional-token prefix."""
    with _optional_prefix_lock:
        return any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in _optional_token_prefixes
        )


def is_optional_token_path(path: str) -> bool:
    """Compatibility alias for callers that use path-oriented naming."""
    return is_optional_token_prefix(path)


def optional_token_scope(path: str) -> str | None:
    """Return the most-specific scope required by an optional-token prefix."""
    with _optional_prefix_lock:
        matches = [
            (prefix, scope)
            for prefix, scope in _optional_token_scopes.items()
            if path == prefix or path.startswith(prefix + "/")
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: len(item[0]))[1]


def clear_optional_token_prefixes() -> None:
    """Clear optional token registrations (primarily for test isolation)."""
    with _optional_prefix_lock:
        _optional_token_prefixes.clear()
        _optional_token_scopes.clear()


def register_token_route(path: str) -> None:
    """Mark ``path`` (exact match) as token-authable.

    Idempotent. Call at module import / app setup so the seam knows which
    routes to guard. Registering a route does NOT make it public — it makes
    it authenticate by token instead of by session cookie.
    """
    with _lock:
        _token_routes.add(path)


def is_token_route(path: str) -> bool:
    """True if ``path`` was registered as token-authable (exact match)."""
    with _lock:
        return path in _token_routes


def clear_token_routes() -> None:
    """Test-only: drop all registered token routes."""
    with _lock:
        _token_routes.clear()


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


def _has_session_cookie(request: Request) -> bool:
    """Return whether a browser session cookie is present on this request."""
    from hermes_cli.dashboard_auth.cookies import read_session_cookies

    access_token, refresh_token = read_session_cookies(request)
    return bool(access_token or refresh_token)


def _has_valid_dashboard_session_header(request: Request) -> bool:
    """Return whether the dedicated dashboard session header is valid.

    Resolve the legacy web-server token lazily to avoid importing the web
    application while the auth package is being initialized.
    """
    try:
        from hermes_cli import web_server

        header = request.headers.get(web_server._SESSION_HEADER_NAME, "")
        return bool(header) and hmac.compare_digest(
            header.encode(), web_server._SESSION_TOKEN.encode()
        )
    except Exception:
        return False


def authenticate_token(
    request: Request,
) -> Tuple[Optional[TokenPrincipal], Optional[str]]:
    """Try every token provider against the request's bearer token.

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
        if principal is not None:
            return principal, None
    return None, unreachable


async def token_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Outermost auth seam for token-authable routes.

    No-op pass-through for any path not registered via
    :func:`register_token_route`. For a registered path, token auth is the
    only accepted scheme:

      * valid token  → attach principal + ``token_authenticated`` flag, pass through.
      * unreachable  → 503 (provider backing store down; not "bad credentials").
      * otherwise    → 401 unauthenticated.

    Runs before the cookie/session gates (installed last in ``web_server.py``).
    The cookie gates honour ``request.state.token_authenticated`` and skip
    enforcement, so a token-authed request is never redirected to ``/login``.
    """
    path = request.url.path
    if not is_token_route(path):
        if is_optional_token_path(path):
            principal = None
            raw_authorization = request.headers.get("authorization", "")
            # Reverse proxies commonly add ``Authorization: Basic ...`` to
            # every request.  That header is not a Hermes bearer credential;
            # when the dedicated dashboard session header is valid, let the
            # normal session-token gate handle the request.  Do not apply
            # this shortcut to an unknown Bearer value: combining a forged
            # bearer with a valid-looking header must remain fail-closed.
            if (
                raw_authorization
                and not extract_bearer_token(request)
                and _has_valid_dashboard_session_header(request)
            ):
                return await call_next(request)
            if raw_authorization:
                principal, unreachable = authenticate_token(request)
                if principal is None:
                    if unreachable:
                        return JSONResponse(
                            {"error": "unavailable", "detail": "Auth provider unavailable"},
                            status_code=503,
                        )
                    # The mobile/service token seam is optional on the whole
                    # ``/api`` tree. In gated mode a native desktop may send
                    # an interactive dashboard access token in the same
                    # Authorization header. Let that token use the normal
                    # session-provider stack instead of treating it as an
                    # invalid service credential and short-circuiting the
                    # cookie gate with a 401.
                    if getattr(request.app.state, "auth_required", False):
                        from hermes_cli.dashboard_auth.middleware import (
                            _verify_bearer,
                        )

                        try:
                            session = _verify_bearer(
                                request,
                                access_token=extract_bearer_token(request),
                            )
                        except ProviderError as exc:
                            return JSONResponse(
                                {
                                    "error": "unavailable",
                                    "detail": "Auth provider unavailable",
                                },
                                status_code=503,
                            )
                        if session is not None:
                            request.state.session = session
                            return await call_next(request)
                    # Public API endpoints may have their own verifier. The
                    # Chronos fire webhook is the important example: its NAS
                    # JWT is deliberately not a dashboard/mobile token and
                    # must reach the route handler. An unknown bearer on a
                    # request carrying browser cookies still fails closed so
                    # it cannot hide a credential mix-up behind cookie auth.
                    if path in PUBLIC_API_PATHS and not _has_session_cookie(request):
                        return await call_next(request)
                    # Preserve the loopback dashboard's legacy Bearer token
                    # while refusing an arbitrary bearer even when a stale
                    # session header is also present.  The downstream legacy
                    # middleware remains the authority for this token.
                    legacy_ok = False
                    if not getattr(request.app.state, "auth_required", False):
                        try:
                            from hermes_cli import web_server

                            legacy_ok = hmac.compare_digest(
                                raw_authorization.strip(),
                                f"Bearer {web_server._SESSION_TOKEN}",
                            )
                        except Exception:
                            legacy_ok = False
                    if not legacy_ok:
                        return JSONResponse(
                            {"error": "unauthenticated", "detail": "Unauthorized"},
                            status_code=401,
                        )
            required_scope = optional_token_scope(path)
            if principal is not None and required_scope and required_scope not in principal.scopes:
                return JSONResponse(
                    {"error": "unauthenticated", "detail": "Unauthorized"},
                    status_code=401,
                )
            if principal is not None:
                request.state.token_principal = principal
                request.state.token_authenticated = True
        return await call_next(request)

    principal, unreachable = authenticate_token(request)
    if principal is not None:
        request.state.token_principal = principal
        request.state.token_authenticated = True
        return await call_next(request)

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
