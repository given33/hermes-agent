"""Framework-neutral HTTP boundary policy for Hermes service adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from .contracts import ServiceFailure


DEFAULT_MAX_REQUEST_BYTES = 10_000_000
LOCAL_DASHBOARD_CORS_ORIGIN_REGEX = (
    r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
)

_COMMON_SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "0",
    "Referrer-Policy": "no-referrer",
}

API_SECURITY_HEADERS: Mapping[str, str] = MappingProxyType(
    {
        **_COMMON_SECURITY_HEADERS,
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }
)

# The dashboard serves scripts, styles and optional microphone UX, so the API's
# deliberately sterile CSP/Permissions-Policy cannot be copied onto it.  These
# headers are safe for both its HTML shell and JSON routes.
DASHBOARD_SECURITY_HEADERS: Mapping[str, str] = MappingProxyType(
    dict(_COMMON_SECURITY_HEADERS)
)

_CORS_METHODS = "GET, POST, DELETE, OPTIONS"
_CORS_REQUEST_HEADERS = "Authorization, Content-Type, Idempotency-Key"


def security_headers(surface: str) -> Mapping[str, str]:
    """Return immutable response-header policy for a public service surface."""
    if surface == "api":
        return API_SECURITY_HEADERS
    if surface == "dashboard":
        return DASHBOARD_SECURITY_HEADERS
    raise ValueError(f"unknown Hermes HTTP surface: {surface}")


def origin_allowed(origin: str, allowed_origins: Iterable[str]) -> bool:
    """Decide whether a browser origin may call an allowlist-backed API."""
    if not origin:
        return True
    allowed = frozenset(allowed_origins)
    return "*" in allowed or origin in allowed


def cors_headers_for_origin(
    origin: str,
    allowed_origins: Iterable[str],
) -> dict[str, str] | None:
    """Build explicit CORS response headers without framework dependencies."""
    if not origin:
        return None
    allowed = frozenset(allowed_origins)
    if not allowed or ("*" not in allowed and origin not in allowed):
        return None

    headers = {
        "Access-Control-Allow-Methods": _CORS_METHODS,
        "Access-Control-Allow-Headers": _CORS_REQUEST_HEADERS,
        "Access-Control-Max-Age": "600",
    }
    if "*" in allowed:
        headers["Access-Control-Allow-Origin"] = "*"
    else:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    return headers


def validate_content_length(
    method: str,
    content_length: str | None,
    *,
    max_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
) -> ServiceFailure | None:
    """Validate a mutating request's declared body length.

    Chunked bodies still need the framework's streaming/client-size guard.  The
    shared decision here guarantees that declared lengths produce identical
    stable status/code/message values on every adapter.
    """
    if method.upper() not in {"POST", "PUT", "PATCH"} or content_length is None:
        return None
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        length = int(content_length)
    except (TypeError, ValueError):
        return ServiceFailure(
            status_code=400,
            code="invalid_content_length",
            message="Invalid Content-Length header.",
        )
    if length < 0:
        return ServiceFailure(
            status_code=400,
            code="invalid_content_length",
            message="Invalid Content-Length header.",
        )
    if length > max_bytes:
        return ServiceFailure(
            status_code=413,
            code="body_too_large",
            message="Request body too large.",
        )
    return None
