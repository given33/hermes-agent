"""Tests for hermes_cli.status model/provider display."""

from types import SimpleNamespace

def _patch_common_status_deps(monkeypatch, status_mod, tmp_path, *, openai_base_url=""):
    import hermes_cli.auth as auth_mod

    monkeypatch.setattr(status_mod, "get_env_path", lambda: tmp_path / ".env", raising=False)
    monkeypatch.setattr(status_mod, "get_hermes_home", lambda: tmp_path, raising=False)

    def _get_env_value(name: str):
        if name == "OPENAI_BASE_URL":
            return openai_base_url
        return ""

    monkeypatch.setattr(status_mod, "get_env_value", _get_env_value, raising=False)
    monkeypatch.setattr(auth_mod, "get_nous_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_codex_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(
        status_mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="inactive\n", returncode=3),
    )


def test_show_status_displays_configured_dict_model_and_provider_label(monkeypatch, capsys, tmp_path):
    from hermes_cli import status as status_mod

    _patch_common_status_deps(monkeypatch, status_mod, tmp_path)
    monkeypatch.setattr(
        status_mod,
        "load_config",
        lambda: {"model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"}},
        raising=False,
    )
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "anthropic", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "anthropic", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "Anthropic", raising=False)

    status_mod.show_status(SimpleNamespace(all=False, deep=False))

    out = capsys.readouterr().out
    assert "Model:        anthropic/claude-sonnet-4" in out
    assert "Provider:     Anthropic" in out


def test_show_status_displays_legacy_string_model_and_custom_endpoint(monkeypatch, capsys, tmp_path):
    from hermes_cli import status as status_mod

    _patch_common_status_deps(monkeypatch, status_mod, tmp_path, openai_base_url="http://localhost:8080/v1")
    monkeypatch.setattr(status_mod, "load_config", lambda: {"model": "qwen3:latest"}, raising=False)
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "auto", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "openrouter", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "Custom endpoint" if provider == "custom" else provider, raising=False)

    status_mod.show_status(SimpleNamespace(all=False, deep=False))

    out = capsys.readouterr().out
    assert "Model:        qwen3:latest" in out
    assert "Provider:     Custom endpoint" in out


def test_show_status_exposes_nous_as_api_key_provider_without_account_surface(
    monkeypatch, capsys, tmp_path
):
    from hermes_cli import status as status_mod
    import hermes_cli.auth as auth_mod

    _patch_common_status_deps(monkeypatch, status_mod, tmp_path)
    monkeypatch.setattr(
        status_mod,
        "get_env_value",
        lambda name: "sk-nous-test" if name == "NOUS_API_KEY" else "",
        raising=False,
    )
    monkeypatch.setattr(
        auth_mod,
        "get_nous_auth_status",
        lambda: (_ for _ in ()).throw(
            AssertionError("Nous account state must not be queried")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        status_mod,
        "load_config",
        lambda: {"model": {"default": "hermes-4", "provider": "nous"}},
        raising=False,
    )
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "nous", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "nous", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "Nous Research", raising=False)

    status_mod.show_status(SimpleNamespace(all=False, deep=False))

    out = capsys.readouterr().out
    assert "Nous Inference" in out
    assert "Nous Research" in out
    assert "Nous Portal" not in out
    assert "Nous Tool Gateway" not in out


def test_show_status_reports_empty_lmstudio_listing_as_reachable(monkeypatch, capsys, tmp_path):
    from hermes_cli import status as status_mod

    _patch_common_status_deps(monkeypatch, status_mod, tmp_path)
    monkeypatch.setattr(
        status_mod,
        "load_config",
        lambda: {
            "model": {
                "default": "qwen/qwen3-coder-30b",
                "provider": "lmstudio",
                "base_url": "http://127.0.0.1:1234/v1",
            }
        },
        raising=False,
    )
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "lmstudio", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "lmstudio", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "LM Studio", raising=False)
    monkeypatch.setattr(
        "hermes_cli.models.probe_lmstudio_models",
        lambda api_key=None, base_url=None, timeout=5.0: [],
    )

    status_mod.show_status(SimpleNamespace(all=False, deep=False))

    out = capsys.readouterr().out
    assert "LM Studio" in out
    assert "reachable (0 model(s)) at http://127.0.0.1:1234/v1" in out
