"""Tests for cross-profile auth fallback.

When ``HERMES_HOME`` points to a named profile, ``read_credential_pool()``
and ``get_provider_auth_state()`` fall back to the global-root
``auth.json`` per-provider when the profile has no entries for that
provider.  Writes still target the profile only.

See the #18594 follow-up report: profile workers couldn't see providers
authenticated only at the global root.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest


def _make_auth_store(pool: dict | None = None, providers: dict | None = None) -> dict:
    store: dict = {"version": 1}
    if pool is not None:
        store["credential_pool"] = pool
    if providers is not None:
        store["providers"] = providers
    return store


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Set up a global root + an active profile under Path.home()/.hermes/profiles/coder.

    * Path.home() -> tmp_path
    * Global root -> tmp_path/.hermes            (has its own auth.json fixture)
    * Profile     -> tmp_path/.hermes/profiles/coder   (active, HERMES_HOME points here)

    This mirrors the real "named profile mounted under the default root"
    layout that profile users actually have on disk.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    global_root = tmp_path / ".hermes"
    global_root.mkdir()
    profile_dir = global_root / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    return {"global": global_root, "profile": profile_dir}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# read_credential_pool — provider-slice reads
# ---------------------------------------------------------------------------








def test_missing_global_auth_file_is_safe(profile_env):
    """Profile processes that never had a global auth.json still work."""
    from hermes_cli.auth import read_credential_pool

    # No global auth.json written at all.
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "prof-1",
            "label": "profile",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-profile",
        }],
    }))

    assert read_credential_pool("openrouter")[0]["id"] == "prof-1"
    assert read_credential_pool("anthropic") == []


def test_malformed_global_auth_file_does_not_break_profile_read(profile_env):
    (profile_env["global"] / "auth.json").write_text("{not valid json")
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "prof-1",
            "label": "profile",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-profile",
        }],
    }))

    from hermes_cli.auth import read_credential_pool

    # Profile reads still work; malformed global is silently ignored.
    assert read_credential_pool("openrouter")[0]["id"] == "prof-1"
    # And no fallback for anthropic since global is unreadable.
    assert read_credential_pool("anthropic") == []


# ---------------------------------------------------------------------------
# read_credential_pool — whole-pool reads (provider_id=None)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_provider_auth_state — singleton fallback
# ---------------------------------------------------------------------------


def test_provider_auth_state_falls_back_to_global_when_profile_has_none(profile_env):
    from hermes_cli.auth import get_provider_auth_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "nous": {"access_token": "nous-global", "refresh_token": "rt-global"},
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    state = get_provider_auth_state("nous")
    assert state is not None
    assert state["access_token"] == "nous-global"


def test_provider_auth_state_returns_none_when_neither_has_it(profile_env):
    from hermes_cli.auth import get_provider_auth_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={}))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    assert get_provider_auth_state("nous") is None


# ---------------------------------------------------------------------------
# _load_provider_state — internal global fallback (issue #18594 follow-up)
#
# Several runtime helpers (notably ``resolve_nous_runtime_credentials`` and
# ``resolve_nous_access_token``) call ``_load_provider_state`` directly with
# a profile-loaded auth store rather than going through
# ``get_provider_auth_state``. Without the fallback wired into
# ``_load_provider_state`` itself, those helpers raise ``"Hermes is not
# logged into Nous Portal"`` even though the user has a valid global Nous
# login. These tests pin the per-provider shadowing into the helper.
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Classic mode — no fallback path should ever trigger
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Writes stay scoped to the profile
# ---------------------------------------------------------------------------


def test_write_credential_pool_targets_profile_not_global(profile_env):
    from hermes_cli.auth import read_credential_pool, write_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "glob-1",
            "label": "global",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-global",
        }],
    }))

    write_credential_pool("openrouter", [{
        "id": "prof-new",
        "label": "profile-new",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "sk-profile-new",
    }])

    # Global auth.json unchanged.
    global_data = json.loads((profile_env["global"] / "auth.json").read_text())
    assert global_data["credential_pool"]["openrouter"][0]["id"] == "glob-1"

    # Profile auth.json holds the new entry.
    profile_data = json.loads((profile_env["profile"] / "auth.json").read_text())
    assert profile_data["credential_pool"]["openrouter"][0]["id"] == "prof-new"

    # Subsequent read returns profile (shadows global).
    assert [e["id"] for e in read_credential_pool("openrouter")] == ["prof-new"]


def test_inherited_pool_runtime_mutations_remain_read_only(profile_env):
    from agent.credential_pool import load_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [_pool_entry(
            id="glob-1",
            access_token="sk-global",
            last_status="exhausted",
            last_status_at=time.time(),
            last_error_code=429,
        )],
    }))

    pool = load_pool("openrouter")
    assert [entry.id for entry in pool.entries()] == ["glob-1"]
    assert pool.remove_index(1) is None
    assert pool.reset_statuses() == 0
    assert pool.entries()[0].last_status == "exhausted"
    assert not (profile_env["profile"] / "auth.json").exists()

    reloaded = load_pool("openrouter")
    assert [entry.id for entry in reloaded.entries()] == ["glob-1"]
    assert reloaded.entries()[0].last_status == "exhausted"


def test_pool_slice_reports_global_fallback_provenance(profile_env):
    from hermes_cli.auth import read_credential_pool_with_source

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [_pool_entry(id="glob-1", access_token="sk-global")],
    }))

    result = read_credential_pool_with_source("openrouter")
    assert result.inherited is True
    assert result.source_path == profile_env["global"] / "auth.json"
    assert [entry["id"] for entry in result.entries] == ["glob-1"]

    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [_pool_entry(id="profile-1", access_token="sk-profile")],
    }))
    owned = read_credential_pool_with_source("openrouter")
    assert owned.inherited is False
    assert owned.source_path == profile_env["profile"] / "auth.json"
    assert [entry["id"] for entry in owned.entries] == ["profile-1"]


def test_same_source_seed_creates_owned_row_without_updating_fallback(
    profile_env, monkeypatch,
):
    import agent.credential_pool as credential_pool

    global_auth = profile_env["global"] / "auth.json"
    _write(global_auth, _make_auth_store(pool={
        "openrouter": [_pool_entry(
            id="glob-env",
            source="env:OPENROUTER_API_KEY",
            access_token="sk-global",
        )],
    }))
    monkeypatch.setattr(
        credential_pool,
        "get_env_prefer_dotenv",
        lambda key: "sk-profile" if key == "OPENROUTER_API_KEY" else "",
    )

    pool = credential_pool.load_pool("openrouter")
    assert len(pool.entries()) == 1
    assert pool.entries()[0].id != "glob-env"
    assert pool.entries()[0].access_token == "sk-profile"
    assert pool.is_inherited(pool.entries()[0]) is False

    profile_store = json.loads(
        (profile_env["profile"] / "auth.json").read_text(encoding="utf-8")
    )
    assert len(profile_store["credential_pool"]["openrouter"]) == 1
    assert json.loads(global_auth.read_text(encoding="utf-8"))[
        "credential_pool"
    ]["openrouter"][0]["access_token"] == "sk-global"


def test_global_singleton_seed_remains_inherited_and_unpersisted(profile_env):
    from agent.credential_pool import load_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "openai-codex": {
            "tokens": {
                "access_token": "codex-global",
                "refresh_token": "refresh-global",
            },
        },
    }))

    pool = load_pool("openai-codex")
    assert len(pool.entries()) == 1
    assert pool.entries()[0].access_token == "codex-global"
    assert pool.is_inherited(pool.entries()[0]) is True
    assert not (profile_env["profile"] / "auth.json").exists()


def test_inherited_pool_oauth_refresh_commits_to_global_owner(
    profile_env, monkeypatch,
):
    import agent.credential_pool as credential_pool
    import hermes_cli.auth as auth

    global_auth = profile_env["global"] / "auth.json"
    _write(global_auth, _make_auth_store(pool={
        "openai-codex": [_pool_entry(
            id="global-manual",
            source="manual:dashboard_oauth",
            auth_type="oauth",
            access_token="access-old",
            refresh_token="refresh-old",
        )],
    }))

    lock_was_held = False

    def _refresh(_access_token, _refresh_token):
        nonlocal lock_was_held
        lock_was_held = (
            getattr(auth._auth_lock_holder_for(global_auth), "depth", 0) > 0
        )
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-08-30T12:00:00Z",
        }

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _refresh)
    pool = credential_pool.load_pool("openai-codex")
    entry = pool.entries()[0]
    refreshed = pool._refresh_entry(entry, force=True)

    assert refreshed is not None
    assert lock_was_held is True
    global_row = json.loads(global_auth.read_text(encoding="utf-8"))[
        "credential_pool"
    ]["openai-codex"][0]
    assert global_row["refresh_token"] == "refresh-new"
    assert not (profile_env["profile"] / "auth.json").exists()


def test_inherited_singleton_refresh_updates_global_state_without_profile_shadow(
    profile_env, monkeypatch,
):
    import agent.credential_pool as credential_pool
    import hermes_cli.auth as auth

    global_auth = profile_env["global"] / "auth.json"
    _write(global_auth, _make_auth_store(providers={
        "xai-oauth": {
            "tokens": {
                "access_token": "xai-old",
                "refresh_token": "xai-refresh-old",
            },
        },
    }))
    monkeypatch.setattr(
        auth,
        "refresh_xai_oauth_pure",
        lambda _access, _refresh: {
            "access_token": "xai-new",
            "refresh_token": "xai-refresh-new",
            "last_refresh": "2026-08-30T12:00:00Z",
        },
    )

    pool = credential_pool.load_pool("xai-oauth")
    entry = pool.entries()[0]
    refreshed = pool._refresh_entry(entry, force=True)

    assert refreshed is not None
    global_store = json.loads(global_auth.read_text(encoding="utf-8"))
    assert global_store["providers"]["xai-oauth"]["tokens"][
        "refresh_token"
    ] == "xai-refresh-new"
    assert global_store["credential_pool"]["xai-oauth"][0][
        "refresh_token"
    ] == "xai-refresh-new"
    assert not (profile_env["profile"] / "auth.json").exists()


def test_inherited_hermes_pkce_refresh_updates_global_singleton(
    profile_env, monkeypatch,
):
    import agent.anthropic_credentials as anthropic_credentials
    import agent.credential_pool as credential_pool

    global_auth = profile_env["global"] / "auth.json"
    global_oauth = profile_env["global"] / ".anthropic_oauth.json"
    _write(global_auth, _make_auth_store(pool={
        "anthropic": [_pool_entry(
            id="global-pkce",
            source="hermes_pkce",
            auth_type="oauth",
            access_token="anthropic-old",
            refresh_token="anthropic-refresh-old",
            expires_at_ms=0,
        )],
    }))
    _write(global_oauth, {
        "accessToken": "anthropic-old",
        "refreshToken": "anthropic-refresh-old",
        "expiresAt": 0,
    })
    monkeypatch.setattr(
        anthropic_credentials,
        "refresh_anthropic_oauth_pure",
        lambda _refresh, use_json=False: {
            "access_token": "anthropic-new",
            "refresh_token": "anthropic-refresh-new",
            "expires_at_ms": int(time.time() * 1000) + 3_600_000,
        },
    )

    pool = credential_pool.load_pool("anthropic")
    entry = pool.entries()[0]
    refreshed = pool._refresh_entry(entry, force=True)

    assert refreshed is not None
    assert json.loads(global_oauth.read_text(encoding="utf-8"))[
        "refreshToken"
    ] == "anthropic-refresh-new"
    global_row = json.loads(global_auth.read_text(encoding="utf-8"))[
        "credential_pool"
    ]["anthropic"][0]
    assert global_row["refresh_token"] == "anthropic-refresh-new"
    assert not (profile_env["profile"] / ".anthropic_oauth.json").exists()
    assert not (profile_env["profile"] / "auth.json").exists()


def test_inherited_pkce_resolver_honors_global_spent_rotation_sidecar(
    profile_env,
):
    import agent.anthropic_credentials as anthropic_credentials

    global_oauth = profile_env["global"] / ".anthropic_oauth.json"
    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "anthropic": [_pool_entry(
            id="global-spent-pkce",
            source="hermes_pkce",
            auth_type="oauth",
            access_token="anthropic-spent",
            refresh_token="anthropic-refresh-spent",
        )],
    }))
    anthropic_credentials.mark_rotation_consumed_uncommitted(
        "anthropic-spent",
        "anthropic-refresh-spent",
        source_path=global_oauth,
    )
    # Simulate a fresh process: only the durable sidecar may carry the verdict.
    with anthropic_credentials._SPENT_ROTATION_LOCK:
        anthropic_credentials._SPENT_ROTATION_FINGERPRINTS.clear()

    assert anthropic_credentials._resolve_anthropic_pool_token() is None
    assert not (
        profile_env["profile"]
        / ".anthropic_oauth.json.hermes-spent-rotations.json"
    ).exists()


@pytest.mark.parametrize(
    ("provider", "refresh_helper", "error_code"),
    [
        ("openai-codex", "refresh_codex_oauth_pure", "invalid_grant"),
        ("xai-oauth", "refresh_xai_oauth_pure", "xai_refresh_failed"),
    ],
)
def test_terminal_inherited_refresh_invalidates_global_owner_only(
    profile_env, monkeypatch, provider, refresh_helper, error_code,
):
    import agent.credential_pool as credential_pool
    import hermes_cli.auth as auth

    global_auth = profile_env["global"] / "auth.json"
    _write(global_auth, _make_auth_store(
        pool={
            provider: [_pool_entry(
                id="global-oauth",
                source="device_code",
                auth_type="oauth",
                access_token="oauth-old",
                refresh_token="oauth-refresh-old",
            )],
        },
        providers={
            provider: {
                "tokens": {
                    "access_token": "oauth-old",
                    "refresh_token": "oauth-refresh-old",
                },
            },
        },
    ))

    def _terminal_refresh(_access_token, _refresh_token):
        raise auth.AuthError(
            "refresh token revoked",
            provider=provider,
            code=error_code,
            relogin_required=True,
        )

    monkeypatch.setattr(auth, refresh_helper, _terminal_refresh)
    pool = credential_pool.load_pool(provider)
    entry = pool.entries()[0]

    assert pool._refresh_entry(entry, force=True) is None
    global_store = json.loads(global_auth.read_text(encoding="utf-8"))
    assert global_store["credential_pool"][provider] == []
    tokens = global_store["providers"][provider]["tokens"]
    assert "access_token" not in tokens
    assert "refresh_token" not in tokens
    assert global_store["providers"][provider]["last_auth_error"][
        "relogin_required"
    ] is True
    assert not (profile_env["profile"] / "auth.json").exists()


def test_terminal_inherited_nous_refresh_invalidates_global_owner_only(
    profile_env, monkeypatch,
):
    import agent.credential_pool as credential_pool
    import hermes_cli.auth as auth

    global_auth = profile_env["global"] / "auth.json"
    _write(global_auth, _make_auth_store(
        pool={
            "nous": [_pool_entry(
                id="global-nous",
                source="device_code",
                auth_type="oauth",
                access_token="nous-old",
                refresh_token="nous-refresh-old",
            )],
        },
        providers={
            "nous": {
                "access_token": "nous-old",
                "refresh_token": "nous-refresh-old",
                "client_id": "client-id",
            },
        },
    ))

    def _terminal_refresh(**_kwargs):
        raise auth.AuthError(
            "refresh token revoked",
            provider="nous",
            code="invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(
        auth,
        "resolve_nous_runtime_credentials",
        _terminal_refresh,
    )
    pool = credential_pool.load_pool("nous")
    entry = pool.entries()[0]

    assert pool._refresh_entry(entry, force=True) is None
    global_store = json.loads(global_auth.read_text(encoding="utf-8"))
    assert global_store["credential_pool"]["nous"] == []
    state = global_store["providers"]["nous"]
    assert "access_token" not in state
    assert "refresh_token" not in state
    assert state["last_auth_error"]["relogin_required"] is True
    assert not (profile_env["profile"] / "auth.json").exists()


def test_inherited_rows_are_not_pruned_or_priority_normalized(profile_env):
    from agent.credential_pool import load_pool

    global_auth = profile_env["global"] / "auth.json"
    _write(global_auth, _make_auth_store(pool={
        "anthropic": [_pool_entry(
            id="global-pkce",
            source="hermes_pkce",
            auth_type="oauth",
            access_token="oauth-global",
            priority=7,
        )],
    }))

    pool = load_pool("anthropic")
    assert [entry.id for entry in pool.entries()] == ["global-pkce"]
    assert pool.entries()[0].priority == 7
    assert pool.is_inherited(pool.entries()[0]) is True
    assert not (profile_env["profile"] / "auth.json").exists()
    assert json.loads(global_auth.read_text(encoding="utf-8"))[
        "credential_pool"
    ]["anthropic"][0]["priority"] == 7


def test_runtime_aging_does_not_prune_inherited_dead_manual_row(profile_env):
    from agent.credential_pool import DEAD_MANUAL_PRUNE_TTL_SECONDS, load_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [_pool_entry(
            id="global-dead",
            last_status="dead",
            last_status_at=time.time() - DEAD_MANUAL_PRUNE_TTL_SECONDS - 60,
        )],
    }))

    pool = load_pool("openrouter")
    assert pool.has_available() is False
    assert [entry.id for entry in pool.entries()] == ["global-dead"]
    assert not (profile_env["profile"] / "auth.json").exists()


def test_cli_remove_rejects_inherited_row_before_source_cleanup(
    profile_env, monkeypatch,
):
    from hermes_cli.auth_commands import auth_remove_command

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [_pool_entry(id="global-1", access_token="sk-global")],
    }))

    cleanup_called = False

    def _unexpected_cleanup(*_args, **_kwargs):
        nonlocal cleanup_called
        cleanup_called = True
        raise AssertionError("inherited CLI removal reached source cleanup")

    monkeypatch.setattr(
        "agent.credential_sources.find_removal_step",
        _unexpected_cleanup,
    )

    class _Args:
        provider = "openrouter"
        target = "1"

    with pytest.raises(SystemExit, match="No credential matching"):
        auth_remove_command(_Args())
    assert cleanup_called is False
    assert not (profile_env["profile"] / "auth.json").exists()


def test_first_profile_pool_add_shadows_without_copying_global(profile_env):
    from agent.credential_pool import (
        AUTH_TYPE_API_KEY,
        SOURCE_MANUAL,
        PooledCredential,
        load_pool,
    )

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [_pool_entry(id="glob-1", access_token="sk-global")],
    }))

    pool = load_pool("openrouter")
    pool.add_entry(PooledCredential(
        provider="openrouter",
        id="profile-1",
        label="profile",
        auth_type=AUTH_TYPE_API_KEY,
        priority=0,
        source=SOURCE_MANUAL,
        access_token="sk-profile",
    ))

    profile_store = json.loads(
        (profile_env["profile"] / "auth.json").read_text(encoding="utf-8")
    )
    assert [
        entry["id"]
        for entry in profile_store["credential_pool"]["openrouter"]
    ] == ["profile-1"]
    assert [entry.id for entry in load_pool("openrouter").entries()] == ["profile-1"]


def test_mixed_pool_removing_last_owned_row_never_writes_inherited_row(profile_env):
    from agent.credential_pool import CredentialPool, PooledCredential

    global_auth = profile_env["global"] / "auth.json"
    profile_auth = profile_env["profile"] / "auth.json"
    global_row = _pool_entry(
        id="global-1",
        label="global",
        access_token="sk-global",
        priority=1,
    )
    owned_row = _pool_entry(
        id="owned-1",
        label="owned",
        access_token="sk-owned",
        priority=0,
    )
    _write(global_auth, _make_auth_store(pool={"openrouter": [global_row]}))
    _write(profile_auth, _make_auth_store(pool={"openrouter": [owned_row]}))

    pool = CredentialPool(
        "openrouter",
        [
            PooledCredential.from_dict("openrouter", owned_row),
            PooledCredential.from_dict("openrouter", global_row),
        ],
        read_only_entry_ids={"global-1"},
    )
    assert pool.remove_index(1).id == "owned-1"

    profile_store = json.loads(profile_auth.read_text(encoding="utf-8"))
    assert profile_store["credential_pool"]["openrouter"] == []
    assert json.loads(global_auth.read_text(encoding="utf-8"))[
        "credential_pool"
    ]["openrouter"][0]["id"] == "global-1"




def test_auth_lock_reentrancy_is_scoped_after_profile_context_switch(profile_env):
    """Changing profile context cannot inherit another store's lock depth."""
    import hermes_cli.auth as auth
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    profile_b = profile_env["global"] / "profiles" / "reviewer"
    profile_b.mkdir(parents=True)
    profile_b_lock = profile_b / "auth.lock"

    with auth._auth_store_lock():
        holder_a = auth._auth_lock_holder_for(profile_env["profile"] / "auth.json")
        assert getattr(holder_a, "depth", 0) == 1

        token = set_hermes_home_override(profile_b)
        try:
            holder_b = auth._auth_lock_holder_for(profile_b / "auth.json")
            assert holder_b is not holder_a
            assert getattr(holder_b, "depth", 0) == 0
            assert not profile_b_lock.exists()

            with auth._auth_store_lock():
                assert profile_b_lock.exists()
                assert getattr(holder_b, "depth", 0) == 1
        finally:
            reset_hermes_home_override(token)

    assert getattr(holder_a, "depth", 0) == 0


# ---------------------------------------------------------------------------
# write_credential_pool — stale-snapshot cooldown merge
# ---------------------------------------------------------------------------


@pytest.fixture()
def classic_env(tmp_path, monkeypatch):
    """Classic single-root layout (HERMES_HOME != ~/.hermes, no profiles)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    hermes_home = tmp_path / "classic"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def _pool_entry(**overrides) -> dict:
    entry = {
        "id": "cred-x",
        "label": "key-x",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "sk-x",
    }
    entry.update(overrides)
    return entry




def test_write_pool_never_merges_cooldown_onto_reauthed_entry(classic_env):
    """A token change means re-auth: the old cooldown must never carry over.

    A fresh login intentionally clears the entry's status; resurrecting the
    stale cooldown onto the new credentials would bench a just-authorized key.
    """
    from hermes_cli.auth import write_credential_pool

    _write(classic_env / "auth.json", _make_auth_store(pool={
        "openrouter": [_pool_entry(
            access_token="sk-old",
            last_status="exhausted",
            last_status_at=time.time() - 60,  # newer AND unexpired
            last_error_code=429,
        )],
    }))

    # Same entry id, freshly re-authed with a new token and cleared status.
    write_credential_pool("openrouter", [_pool_entry(access_token="sk-new")])

    data = json.loads((classic_env / "auth.json").read_text())
    persisted = data["credential_pool"]["openrouter"][0]
    assert persisted["access_token"] == "sk-new"
    assert persisted.get("last_status") != "exhausted"
    assert persisted.get("last_error_code") is None
