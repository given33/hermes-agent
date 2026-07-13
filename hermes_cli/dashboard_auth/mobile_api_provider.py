"""Token-only dashboard auth provider for native mobile clients."""
from __future__ import annotations

import hmac
import os
from typing import Optional

from hermes_cli.dashboard_auth.base import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
)


MOBILE_API_KEY_ENV = "HERMES_MOBILE_API_KEY"


class MobileApiKeyProvider(DashboardAuthProvider):
    """Verify the server's mobile API key without creating a session."""

    name = "mobile-api"
    display_name = "Hermes Native Mobile"
    supports_session = False
    supports_token = True

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        expected = os.environ.get(MOBILE_API_KEY_ENV, "").strip()
        if not expected or not token or not hmac.compare_digest(token, expected):
            return None
        return TokenPrincipal(
            principal="ios-native",
            provider=self.name,
            scopes=("dashboard:admin",),
        )

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError(
            "MobileApiKeyProvider is a non-interactive service credential."
        )

    def complete_login(
        self,
        *,
        code: str,
        state: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> Session:
        raise NotImplementedError(
            "MobileApiKeyProvider is a non-interactive service credential."
        )

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError(
            "MobileApiKeyProvider is a non-interactive service credential."
        )

    def revoke_session(self, *, refresh_token: str) -> None:
        return None
