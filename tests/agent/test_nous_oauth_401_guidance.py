"""Tests for the Nous OAuth 401 actionable-guidance branch in
``agent.conversation_loop.run_conversation``.

Source-inspection style (matches ``test_gemini_fast_fallback.py``): we assert
that the guidance strings exist in the function body so that the user-facing
hint cannot be silently removed by a future refactor.

Regression context: ashh hit a Nous 401 (OAuth token expired / portal said
account out of credits) plus a model slug ``deepseek/deepseek-v4-flash:free``
that's OpenRouter syntax, not a Nous catalog name. The previous guidance
branch only covered ``openai-codex`` and ``xai-oauth``; ``nous`` fell through
to a generic "Your API key was rejected... run hermes setup" message, which is
the wrong advice for a pure-OAuth provider.
"""
from __future__ import annotations

import inspect

from agent import conversation_loop


def test_nous_provider_is_not_in_oauth_401_set():
    source = inspect.getsource(conversation_loop.run_conversation)

    assert '_provider in {"openai-codex", "xai-oauth"}' in source
    assert '_provider in {"openai-codex", "xai-oauth", "nous"}' not in source


def test_nous_401_guidance_uses_direct_api_key_copy():
    source = inspect.getsource(conversation_loop._nous_entitlement_message)
    assert "NOUS_API_KEY" in source
    assert "Portal" not in source


def test_nous_account_refresh_is_disabled():
    source = inspect.getsource(
        conversation_loop._try_refresh_nous_paid_entitlement_credentials
    )
    assert "return False" in source
    assert "get_nous_portal_account_info" not in source


def test_nous_direct_key_quota_errors_do_not_link_to_a_portal_account():
    from agent.provider_auth import AuthError, format_auth_error

    rendered = format_auth_error(
        AuthError("Nous API rejected the configured key", provider="nous", code="insufficient_credits")
    )

    assert rendered == "Nous API rejected the configured key"
    assert "Portal" not in rendered
    assert "billing" not in rendered.lower()
