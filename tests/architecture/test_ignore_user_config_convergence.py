"""HERMES_IGNORE_USER_CONFIG must mean the same thing through every loader.

Historically only the legacy ``cli.load_cli_config`` path honored the flag,
which forced ``tools/delegate_tool._load_config`` to carry compensating
loader-choice logic (prefer the legacy loader whenever the flag was set —
even though the legacy loader is otherwise the WORSE choice because it can
serve a stale module-level snapshot). The flag is now honored inside the
shared ``hermes_cli.config._load_config_impl`` itself (user file treated as
absent, managed overlay still applied, cache/LKG bypassed), and the
compensation in delegate_tool was removed.

These tests pin the convergence through both public entry points:

- shared loader:   ``hermes_cli.config.load_config()``
- delegate path:   ``tools.delegate_tool._load_config()`` (shared loader
  first, ``cli.CLI_CONFIG`` only as an import-failure fallback)

using the exact key whose disappearance motivated the old compensation:
``delegation.max_concurrent_children``.

Flag semantics under test (all verified against the shared implementation):
the gate is the literal string ``"1"``; user config becomes invisible while
set; and the effect is NOT sticky — clearing the flag restores the user
value within the same process (the cache is bypassed, not poisoned).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_USER_VALUE = 7  # deliberately != any plausible default


@pytest.fixture
def user_home(monkeypatch, tmp_path):
    """A HERMES_HOME whose config.yaml sets the sentinel delegation key.

    The suite-wide conftest already isolates HERMES_HOME per test; this
    fixture narrows it further to a home we fully control and guarantees
    the flag starts unset.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "config.yaml").write_text(
        f"delegation:\n  max_concurrent_children: {_USER_VALUE}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    return home


def _shared_loader_value():
    from hermes_cli.config import load_config

    cfg = load_config()
    delegation = cfg.get("delegation") or {}
    return delegation.get("max_concurrent_children")


def _delegate_tool_value():
    from tools.delegate_tool import _load_config

    return _load_config().get("max_concurrent_children")


def test_flag_off_user_value_visible_through_both(user_home: Path):
    assert _shared_loader_value() == _USER_VALUE
    assert _delegate_tool_value() == _USER_VALUE


def test_flag_on_hides_user_value_identically_through_both(
    user_home: Path, monkeypatch
):
    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "1")

    shared = _shared_loader_value()
    delegate = _delegate_tool_value()

    # The user's 7 must be invisible...
    assert shared != _USER_VALUE, (
        "shared loader served the user config despite "
        "HERMES_IGNORE_USER_CONFIG=1"
    )
    # ...and BOTH entry points must agree on what replaces it (the merged
    # default), which is the whole convergence claim: delegate_tool no
    # longer needs — and no longer has — loader-choice compensation.
    assert delegate == shared, (
        f"divergence under the flag: shared loader -> {shared!r}, "
        f"tools.delegate_tool._load_config -> {delegate!r}. The flag is "
        "being honored by one path and not the other again."
    )


def test_flag_is_not_sticky_and_gate_is_literal_one(user_home: Path, monkeypatch):
    # Non-"1" values do NOT engage the gate (documented literal match).
    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "true")
    assert _shared_loader_value() == _USER_VALUE
    assert _delegate_tool_value() == _USER_VALUE

    # Engage, observe, clear, observe: toggling within one process works —
    # proving the flag path bypasses the (mtime_ns, size) cache instead of
    # poisoning it with the defaults-only view.
    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "1")
    assert _shared_loader_value() != _USER_VALUE

    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG")
    assert os.environ.get("HERMES_IGNORE_USER_CONFIG") is None
    assert _shared_loader_value() == _USER_VALUE
    assert _delegate_tool_value() == _USER_VALUE
