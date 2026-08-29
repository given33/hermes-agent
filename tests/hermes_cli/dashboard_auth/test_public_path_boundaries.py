"""Regression tests for unauthenticated dashboard route boundaries."""

from hermes_cli.dashboard_auth.middleware import _path_is_public


def test_public_route_prefixes_require_a_path_boundary():
    assert _path_is_public("/auth/login") is True
    assert _path_is_public("/auth/login/callback") is True
    assert _path_is_public("/auth/login-evil") is False
    assert _path_is_public("/api/auth/providers") is True
    assert _path_is_public("/api/auth/providers-evil") is False
    assert _path_is_public("/manifest.webmanifest") is True
    assert _path_is_public("/manifest.webmanifest.bak") is False


def test_slash_terminated_mounts_keep_nested_assets_public():
    assert _path_is_public("/assets/app.js") is True
    assert _path_is_public("/fonts-terminal/regular.woff2") is True
    assert _path_is_public("/assetsleak/app.js") is False
