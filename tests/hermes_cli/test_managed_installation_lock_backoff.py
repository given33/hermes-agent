from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import managed_installations


def _isolate_profile_root(monkeypatch, tmp_path) -> None:
    profiles_root = tmp_path / "profiles"
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: profiles_root / name,
    )


def test_account_runtime_lock_uses_bounded_exponential_backoff(
    monkeypatch, tmp_path
):
    _isolate_profile_root(monkeypatch, tmp_path)
    now = [0.0]
    sleeps = []
    attempts = [0]
    fence = SimpleNamespace(release=lambda: None)

    def try_fence(*_args, **_kwargs):
        attempts[0] += 1
        return fence if attempts[0] == 7 else None

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(managed_installations, "_try_execution_fence", try_fence)
    monkeypatch.setattr(managed_installations.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(managed_installations.time, "sleep", sleep)
    monkeypatch.setattr(managed_installations.random, "uniform", lambda *_args: 1.0)

    with managed_installations._account_runtime_lock(
        "owner", "generation", "profile", timeout=2.0
    ):
        pass

    assert sleeps == pytest.approx([0.01, 0.02, 0.04, 0.08, 0.16, 0.25])
    assert max(sleeps) <= managed_installations._ACCOUNT_RUNTIME_LOCK_MAX_DELAY_SECONDS


def test_account_runtime_lock_never_sleeps_past_deadline(monkeypatch, tmp_path):
    _isolate_profile_root(monkeypatch, tmp_path)
    now = [0.0]
    sleeps = []
    attempts = [0]

    def try_fence(*_args, **_kwargs):
        attempts[0] += 1
        return None

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(managed_installations, "_try_execution_fence", try_fence)
    monkeypatch.setattr(managed_installations.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(managed_installations.time, "sleep", sleep)
    monkeypatch.setattr(managed_installations.random, "uniform", lambda *_args: 1.0)

    with pytest.raises(TimeoutError, match="runtime is busy"):
        with managed_installations._account_runtime_lock(
            "owner", "generation", "profile", timeout=0.12
        ):
            pass

    assert sum(sleeps) == pytest.approx(0.12)
    assert attempts[0] == len(sleeps) + 1
