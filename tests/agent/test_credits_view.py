"""Legacy account-view compatibility and removed product-surface contracts."""

from __future__ import annotations

import pytest

import agent.account_usage as account_usage
from agent.account_usage import CreditsView, build_credits_view
from hermes_cli.nous_account import NousPortalAccountInfo, NousPaidServiceAccessInfo


def _account(**kwargs) -> NousPortalAccountInfo:
    kwargs.setdefault("logged_in", True)
    kwargs.setdefault("source", "account_api")
    kwargs.setdefault("fresh", True)
    kwargs.setdefault("portal_base_url", "https://portal.example.test")
    return NousPortalAccountInfo(**kwargs)


@pytest.fixture
def _logged_in_account(monkeypatch):
    """Stub the auth token + account fetch so build_credits_view runs offline."""
    monkeypatch.setattr(
        "hermes_cli.auth.get_provider_auth_state",
        lambda provider: {"access_token": "tok", "portal_base_url": "https://portal.example.test"},
    )

    def _install(account):
        monkeypatch.setattr(
            "hermes_cli.nous_account.get_nous_portal_account_info",
            lambda *a, **kw: account,
        )

    return _install


# ── build_credits_view core ─────────────────────────────────────────────────




def test_view_built_with_org_pinned_url_and_identity(_logged_in_account):
    _logged_in_account(
        _account(
            org_slug="acme",
            org_name="Acme Inc",
            email="alice@example.test",
            paid_service_access=True,
            paid_service_access_info=NousPaidServiceAccessInfo(
                purchased_credits_remaining=30.0,
                total_usable_credits=30.0,
            ),
            subscription=None,
        )
    )

    view = build_credits_view()

    assert view.logged_in is True
    assert view.topup_url == "https://portal.example.test/orgs/acme/billing?topup=open"
    assert view.identity_line == "Topping up as alice@example.test / org Acme Inc"
    assert view.depleted is False
    # Balance lines carry the magnitudes but NOT the /usage affordance lines.
    blob = "\n".join(view.balance_lines)
    assert "Top-up credits: $30.00" in blob
    assert "Top up:" not in blob  # the trailing /usage affordance is stripped
    assert "(or run" not in blob








# ── gateway _handle_topup_command (the messaging billing surface) ────────────




# ── command registry ────────────────────────────────────────────────────────


def test_credits_command_fully_removed():
    """Consumer account balance and billing commands are absent everywhere."""
    from hermes_cli.commands import resolve_command, COMMAND_REGISTRY
    from gateway.slash_commands import GatewaySlashCommandsMixin

    removed = {"credits", "billing", "topup", "subscription", "upgrade"}
    for name in removed:
        assert resolve_command(name) is None
    assert not any(c.name in removed for c in COMMAND_REGISTRY)
    for c in COMMAND_REGISTRY:
        assert removed.isdisjoint(c.aliases or ())
    assert not hasattr(GatewaySlashCommandsMixin, "_handle_topup_command")
