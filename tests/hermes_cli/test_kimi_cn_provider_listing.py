"""Test that kimi-coding and kimi-coding-cn both appear in the /model picker.

Both providers share the same models.dev ID (kimi-for-coding) but are distinct
profiles with different API keys, base URLs, and endpoints.  The /model picker
must show both so users can pick the right endpoint for their key type.

Regression: the original ``seen_mdev_ids`` dedup by mdev_id alone would skip
kimi-coding-cn after kimi-coding was emitted because both map to
``kimi-for-coding`` (#10526).  The fix deduplicates by
``(mdev_id, canonical_profile_name)`` instead, allowing distinct profiles
through.
"""

import os
from unittest.mock import patch

import pytest

from hermes_cli.model_switch import (
    list_authenticated_providers,
    parse_model_flags,
    switch_model,
)
from hermes_cli.providers import resolve_provider_full


@pytest.fixture(autouse=True)
def _isolated_provider_catalog(monkeypatch, tmp_path):
    import agent.model_catalog as model_catalog
    import agent.models_dev as models_dev

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_CN_API_KEY", raising=False)
    catalog = {
        "kimi-for-coding": {
            "name": "Kimi For Coding",
            "env": ["KIMI_API_KEY", "KIMI_CN_API_KEY"],
            "models": {"kimi-k2.6": {}},
        }
    }
    monkeypatch.setattr(models_dev, "fetch_models_dev", lambda *args, **kwargs: catalog)
    monkeypatch.setattr(
        model_catalog,
        "cached_provider_model_ids",
        lambda provider, *args, **kwargs: list(
            model_catalog._PROVIDER_MODELS.get(provider, [])
        ),
    )
    monkeypatch.setattr(
        model_catalog,
        "get_curated_nous_model_ids",
        lambda: list(model_catalog._PROVIDER_MODELS.get("nous", [])),
    )
    monkeypatch.setattr(model_catalog, "fetch_ollama_cloud_models", lambda *args, **kwargs: [])


# -- Only KIMI_CN_API_KEY set ------------------------------------------------


@patch.dict(os.environ, {"KIMI_CN_API_KEY": "sk-cn-fake"}, clear=False)
def test_kimi_cn_appears_when_only_cn_key_set():
    """kimi-coding-cn should appear when only KIMI_CN_API_KEY is set."""
    providers = list_authenticated_providers(current_provider="kimi-coding-cn")

    # kimi-coding-cn must be listed (it has credentials)
    cn = next((p for p in providers if p["slug"] == "kimi-coding-cn"), None)
    assert cn is not None, (
        "kimi-coding-cn should appear when KIMI_CN_API_KEY is set"
    )
    assert cn["is_current"] is True
    assert cn["total_models"] > 0

    # kimi-coding must NOT appear (no KIMI_API_KEY)
    intl = next((p for p in providers if p["slug"] == "kimi-coding"), None)
    assert intl is None, (
        "kimi-coding should NOT appear when only KIMI_CN_API_KEY is set"
    )


# -- Only KIMI_API_KEY set ---------------------------------------------------


# -- Both keys set -----------------------------------------------------------



# -- Both aliases deduped correctly ------------------------------------------



@patch.dict(os.environ, {
    "KIMI_API_KEY": "sk-intl-fake",
    "KIMI_CN_API_KEY": "sk-cn-fake",
}, clear=False)
def test_resolve_provider_full_preserves_kimi_cn_provider_identity():
    """Explicit kimi-coding-cn must not collapse to shared models.dev alias.

    Regression: resolve_provider_full('kimi-coding-cn') used normalize_provider(),
    which mapped both kimi-coding and kimi-coding-cn to the models.dev alias
    'kimi-for-coding'. That silently rewired CN users to the international
    endpoint and KIMI_API_KEY.
    """
    pdef = resolve_provider_full("kimi-coding-cn", None, None)
    assert pdef is not None
    assert pdef.id == "kimi-coding-cn"
    assert pdef.base_url == "https://api.moonshot.cn/v1"
    assert pdef.api_key_env_vars == ("KIMI_CN_API_KEY",)


