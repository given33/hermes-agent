"""Shared authentication decisions for HTTP and RPC adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    # Official interface: the canonical AuthError contract lives in
    # hermes_cli.auth (upstream home since 0.20). Use it first so we never
    # drift from the official implementation.
    from hermes_cli.auth import (
        AuthError,
        CODEX_RATE_LIMITED_CODE,
        is_rate_limited_auth_error,
    )
except ImportError:
    try:
        # Older hop: the pre-0.20 top-level auth contract.
        from hermes_auth_errors import (
            AuthError,
            CODEX_RATE_LIMITED_CODE,
            is_rate_limited_auth_error,
        )
    except ImportError as exc:
        # Fabric nodes running the pre-0.20 updater copy hermes_services as a
        # package but do not know about the newer auth contracts yet.
        # Keep that one upgrade hop bootable so it can install the current updater.
        if getattr(exc, "name", None) not in {"hermes_auth_errors", "hermes_cli"}:
            raise

        CODEX_RATE_LIMITED_CODE = "codex_rate_limited"

        class AuthError(RuntimeError):
            def __init__(
                self,
                message: str,
                *,
                provider: str = "",
                code: str | None = None,
                relogin_required: bool = False,
            ) -> None:
                super().__init__(message)
                self.provider = provider
                self.code = code
                self.relogin_required = relogin_required

        def is_rate_limited_auth_error(error: Exception) -> bool:
            return (
                isinstance(error, AuthError)
                and not error.relogin_required
                and error.code == CODEX_RATE_LIMITED_CODE
            )
from hermes_secret_compare import bearer_matches


_PLACEHOLDER_SECRET_VALUES = frozenset(
    {
        "*",
        "**",
        "***",
        "changeme",
        "your_api_key",
        "your_api_key_here",
        "your-api-key",
        "placeholder",
        "example",
        "dummy",
        "null",
        "none",
    }
)


@dataclass(frozen=True, slots=True)
class BearerAuthorization:
    authenticated: bool
    configured: bool
    error_code: str | None = None


def has_usable_secret(value: Any, *, min_length: int = 4) -> bool:
    """Return whether a configured credential is non-empty and non-placeholder."""
    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    return len(cleaned) >= min_length and cleaned.lower() not in _PLACEHOLDER_SECRET_VALUES


def authorize_bearer(
    authorization_header: str | None,
    expected_secret: str | None,
    *,
    allow_unconfigured: bool = False,
) -> BearerAuthorization:
    """Evaluate an exact bearer credential without transport dependencies.

    Production listeners should fail closed when no secret is configured.
    ``allow_unconfigured`` exists only for adapters whose startup validation is
    authoritative and for their isolated unit tests.
    """
    configured = bool(expected_secret)
    if not configured:
        return BearerAuthorization(
            authenticated=allow_unconfigured,
            configured=False,
            error_code=None if allow_unconfigured else "credential_unconfigured",
        )
    if bearer_matches(authorization_header, expected_secret):
        return BearerAuthorization(authenticated=True, configured=True)
    return BearerAuthorization(
        authenticated=False,
        configured=True,
        error_code="invalid_api_key",
    )
