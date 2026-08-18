"""Module-level registry for DashboardAuthProvider instances.

Plugins call ``register_provider`` via the plugin context hook at startup.
The auth gate middleware iterates ``list_providers()`` and uses
``get_provider`` to dispatch on the session's ``provider`` field.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional

from hermes_constants import hermes_home_key
from hermes_cli.dashboard_auth.base import (
    DashboardAuthProvider,
    assert_protocol_compliance,
)

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_providers: dict[str, DashboardAuthProvider] = {}
_scoped_providers: dict[str, dict[str, DashboardAuthProvider]] = {}


def _merged(scope: Optional[str] = None) -> dict[str, DashboardAuthProvider]:
    providers = dict(_providers)
    providers.update(_scoped_providers.get(scope or hermes_home_key(), {}))
    return providers


def register_provider(
    provider: DashboardAuthProvider,
    *,
    scope: Optional[str] = None,
) -> None:
    """Register a provider.

    Raises:
        TypeError: on protocol violation.
        ValueError: if a provider with the same name is already registered.
    """
    assert_protocol_compliance(type(provider))
    with _lock:
        target = _providers if scope is None else _scoped_providers.setdefault(scope, {})
        effective = target if scope is None else _merged(scope)
        if provider.name in effective:
            raise ValueError(
                f"dashboard-auth provider already registered: {provider.name!r}"
            )
        target[provider.name] = provider
    _log.info(
        "dashboard-auth: registered provider %r (%s)",
        provider.name, provider.display_name,
    )


def get_provider(
    name: str,
    *,
    scope: Optional[str] = None,
) -> Optional[DashboardAuthProvider]:
    """Return the registered provider for ``name``, or None if unknown."""
    with _lock:
        return _merged(scope).get(name)


def register_mobile_api_provider_if_configured() -> bool:
    """Register the built-in mobile provider when its secret is configured.

    Registration is idempotent so app assembly and startup checks may safely
    call this from different threads. The provider reads the current secret at
    verification time; the registry never stores or logs the value.
    """
    from hermes_cli.dashboard_auth.mobile_api_provider import (
        MOBILE_API_KEY_ENV,
        MobileApiKeyProvider,
    )

    if not os.environ.get(MOBILE_API_KEY_ENV, "").strip():
        return False

    # Prefer the owner-mobile short-lived token flow.  If the operator has
    # configured the official owner account (password + per-device
    # access/refresh tokens), the shared static key is no longer needed and
    # keeping it registered would leave an unrevocable backdoor.
    try:
        from hermes_cli.dashboard_auth.owner_mobile import owner_account_configured

        if owner_account_configured():
            _log.warning(
                "dashboard-auth: HERMES_MOBILE_API_KEY is ignored because "
                "owner-mobile short-lived token auth is configured; remove the "
                "static key from the environment"
            )
            return False
    except Exception:
        pass

    provider = MobileApiKeyProvider()
    assert_protocol_compliance(type(provider))
    registered = False
    with _lock:
        existing = _providers.get(provider.name)
        if existing is None:
            _providers[provider.name] = provider
            registered = True
        elif not isinstance(existing, MobileApiKeyProvider):
            raise ValueError(
                f"dashboard-auth provider already registered: {provider.name!r}"
            )

    if registered:
        _log.info(
            "dashboard-auth: registered provider %r (%s)",
            provider.name,
            provider.display_name,
        )
    return True



def unregister_provider(
    name: str,
    *,
    expected: DashboardAuthProvider | None = None,
) -> bool:
    """Remove one provider without disturbing a concurrent replacement."""

    with _lock:
        current = _providers.get(name)
        if current is None or (expected is not None and current is not expected):
            return False
        del _providers[name]
    _log.info("dashboard-auth: unregistered provider %r", name)
    return True


def snapshot_registration(
    name: str,
    *,
    scope: Optional[str] = None,
) -> Optional[DashboardAuthProvider]:
    with _lock:
        target = _providers if scope is None else _scoped_providers.get(scope, {})
        return target.get(name)


def restore_registration(
    name: str,
    current: DashboardAuthProvider,
    previous: Optional[DashboardAuthProvider],
    *,
    scope: Optional[str] = None,
) -> bool:
    """Restore a host-owned provider registration if it is still current."""
    with _lock:
        target = _providers if scope is None else _scoped_providers.setdefault(scope, {})
        if target.get(name) is not current:
            return False
        if previous is None:
            target.pop(name, None)
        else:
            target[name] = previous
        if scope is not None and not target:
            _scoped_providers.pop(scope, None)
    return True


def list_providers(*, scope: Optional[str] = None) -> List[DashboardAuthProvider]:
    """All registered providers, in registration order."""
    with _lock:
        return list(_merged(scope).values())


def list_token_providers() -> List[DashboardAuthProvider]:
    """Registered providers that support non-interactive token auth.

    The subset of ``list_providers()`` whose ``supports_token`` flag is True,
    in registration order. The ``token_auth`` middleware seam consults these
    (and only these) when a token-authable route is hit, so OAuth/password-only
    providers are never asked to ``verify_token``. Returns an empty list when
    no token provider is registered — a token-authable route then fails
    closed (401), never open.
    """
    return [p for p in list_providers() if getattr(p, "supports_token", False)]


def list_session_providers() -> List[DashboardAuthProvider]:
    """Registered providers with supports_session True (interactive cookie
    sessions). The login page, /auth/login, and the gate's verify/refresh loops
    consult only these. Mirror of list_token_providers.
    """
    return [p for p in list_providers() if getattr(p, "supports_session", True)]


def clear_providers() -> None:
    """Test-only: drop all registrations."""
    with _lock:
        _providers.clear()
        _scoped_providers.clear()
