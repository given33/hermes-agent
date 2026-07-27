from __future__ import annotations

from pathlib import Path

import hermes_runtime.profile_identity as profile_identity


def test_profile_identity_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(profile_identity, "get_default_hermes_root", lambda: tmp_path)
    monkeypatch.setattr(profile_identity, "get_hermes_home", lambda: tmp_path)

    assert profile_identity.get_active_profile_name() == "default"


def test_profile_identity_named_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(profile_identity, "get_default_hermes_root", lambda: tmp_path)
    monkeypatch.setattr(
        profile_identity,
        "get_hermes_home",
        lambda: tmp_path / "profiles" / "ios-native",
    )

    assert profile_identity.get_active_profile_name() == "ios-native"


def test_profile_identity_rejects_nested_or_external_homes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(profile_identity, "get_default_hermes_root", lambda: tmp_path)
    monkeypatch.setattr(
        profile_identity,
        "get_hermes_home",
        lambda: tmp_path / "profiles" / "ios-native" / "nested",
    )
    assert profile_identity.get_active_profile_name() == "custom"

    monkeypatch.setattr(profile_identity, "get_hermes_home", lambda: tmp_path.parent / "other")
    assert profile_identity.get_active_profile_name() == "custom"
