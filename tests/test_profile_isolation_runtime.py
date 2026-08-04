"""Profile-isolation regression tests for single-process multi-profile runtimes.

In runtimes that serve every profile from one OS process (the desktop
``tui_gateway``), the profile boundary is the context-local
``_HERMES_HOME_OVERRIDE`` ContextVar, not the process environment.  State that
escapes the request call stack — import-time-frozen path constants, direct
``os.environ`` reads, or worker threads that don't inherit the request context —
silently reverts to the launch/default profile and leaks one profile's data
into another.

These tests drive each previously-leaking site under override A then override B
with real temp HERMES_HOME directories (no mocks) and assert the *active*
profile's path is used.  They are the productionized form of the manual smoke
probes used to confirm the bug class.
"""

import json
import threading
from pathlib import Path

import pytest

from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


@pytest.fixture
def two_profiles(tmp_path):
    """Two distinct profile HERMES_HOME dirs with the dir skeleton created."""
    prof_a = tmp_path / "profA"
    prof_b = tmp_path / "profB"
    for p in (prof_a, prof_b):
        (p / "skills").mkdir(parents=True, exist_ok=True)
        (p / "state").mkdir(parents=True, exist_ok=True)
        (p / "cache").mkdir(parents=True, exist_ok=True)
    return prof_a, prof_b


def _under_override(home: Path, fn):
    """Run ``fn`` with the profile override set to ``home`` and reset after."""
    token = set_hermes_home_override(str(home))
    try:
        return fn()
    finally:
        reset_hermes_home_override(token)


# ---------------------------------------------------------------------------
# M1 — import-time path globals / direct os.environ reads
# ---------------------------------------------------------------------------

class TestSkillsHubPathResolution:
    """tools/skills_hub.py path constants must reflect the active profile."""

    def test_skills_dir_follows_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import tools.skills_hub as sh

        # Importing/touching under A must NOT pin the path for B.
        a_seen = _under_override(prof_a, lambda: Path(sh.SKILLS_DIR))
        b_seen = _under_override(prof_b, lambda: Path(sh.SKILLS_DIR))

        assert a_seen == prof_a / "skills"
        assert b_seen == prof_b / "skills"
        assert a_seen != b_seen

    def test_hub_derived_paths_follow_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import tools.skills_hub as sh

        b_lock = _under_override(prof_b, lambda: Path(sh.LOCK_FILE))
        b_audit = _under_override(prof_b, lambda: Path(sh.AUDIT_LOG))
        b_index = _under_override(prof_b, lambda: Path(sh.INDEX_CACHE_DIR))

        assert b_lock == prof_b / "skills" / ".hub" / "lock.json"
        assert b_audit == prof_b / "skills" / ".hub" / "audit.log"
        assert b_index == prof_b / "skills" / ".hub" / "index-cache"



class TestGatewayCacheDirResolution:
    """gateway/platforms/base.py cache getters must follow the active profile."""

    def test_image_cache_dir_follows_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import gateway.platforms.base as gb

        a_seen = _under_override(prof_a, lambda: gb.get_image_cache_dir())
        b_seen = _under_override(prof_b, lambda: gb.get_image_cache_dir())

        assert str(a_seen).startswith(str(prof_a))
        assert str(b_seen).startswith(str(prof_b))
        assert a_seen != b_seen




class TestRichSentStorePathResolution:
    """gateway/rich_sent_store.py must honor the override, not read os.environ."""

    def test_store_path_follows_override(self, two_profiles, monkeypatch):
        prof_a, prof_b = two_profiles
        # Ensure no ambient HERMES_HOME env masks the test.
        monkeypatch.delenv("HERMES_HOME", raising=False)
        import gateway.rich_sent_store as rss

        b_seen = _under_override(prof_b, lambda: rss._store_path())
        assert b_seen.startswith(str(prof_b))
        assert Path(b_seen).parts[-2:] == ("state", "rich_sent_index.json")


class TestGatewayProfileLocalState:
    def test_channel_directory_and_aliases_follow_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import gateway.channel_directory as directory

        (prof_a / "channel_directory.json").write_text(
            json.dumps({"platforms": {"telegram": [{"id": "a", "name": "A"}]}}),
            encoding="utf-8",
        )
        (prof_b / "channel_directory.json").write_text(
            json.dumps({"platforms": {"telegram": [{"id": "b", "name": "B"}]}}),
            encoding="utf-8",
        )
        (prof_b / "channel_aliases.json").write_text(
            json.dumps({"telegram": {"b": "Profile B"}}), encoding="utf-8"
        )

        seen_a = _under_override(prof_a, directory.load_directory)
        seen_b = _under_override(prof_b, directory.load_directory)

        assert seen_a["platforms"]["telegram"][0]["id"] == "a"
        assert seen_b["platforms"]["telegram"][0]["name"] == "Profile B"

    def test_sticker_cache_follows_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import gateway.sticker_cache as stickers

        _under_override(
            prof_a,
            lambda: stickers.cache_sticker_description("same", "profile A"),
        )
        _under_override(
            prof_b,
            lambda: stickers.cache_sticker_description("same", "profile B"),
        )

        assert _under_override(
            prof_a, lambda: stickers.get_cached_description("same")["description"]
        ) == "profile A"
        assert _under_override(
            prof_b, lambda: stickers.get_cached_description("same")["description"]
        ) == "profile B"

    def test_legacy_mirror_lookup_follows_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import gateway.mirror as mirror

        sessions = prof_b / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "sessions.json").write_text(
            json.dumps(
                {
                    "profile-b-session": {
                        "session_id": "profile-b-session",
                        "origin": {"platform": "telegram", "chat_id": "b-chat"},
                    }
                }
            ),
            encoding="utf-8",
        )

        assert _under_override(
            prof_b, lambda: mirror._find_session_id("telegram", "b-chat")
        ) == "profile-b-session"
        assert _under_override(
            prof_a, lambda: mirror._find_session_id("telegram", "b-chat")
        ) is None


class TestCronProfileLocalState:
    def test_suggestions_follow_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import cron.suggestions as suggestions

        def add(home, title):
            return _under_override(
                home,
                lambda: suggestions.add_suggestion(
                    title=title,
                    description="test",
                    source="usage",
                    job_spec={"name": title, "prompt": "test", "schedule": "1d"},
                    dedup_key=title,
                ),
            )

        add(prof_a, "profile-a")
        add(prof_b, "profile-b")

        assert [item["title"] for item in _under_override(prof_a, suggestions.list_pending)] == ["profile-a"]
        assert [item["title"] for item in _under_override(prof_b, suggestions.list_pending)] == ["profile-b"]

    def test_execution_ledger_follows_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import cron.executions as executions

        _under_override(
            prof_b, lambda: executions.create_execution("profile-b-job", source="test")
        )

        assert _under_override(
            prof_b, lambda: len(executions.list_executions(job_id="profile-b-job"))
        ) == 1
        assert _under_override(
            prof_a, lambda: executions.list_executions(job_id="profile-b-job")
        ) == []


def test_media_delivery_blocks_active_profile_credentials(two_profiles):
    _prof_a, prof_b = two_profiles
    from gateway.platforms.base import validate_media_delivery_path

    token_file = prof_b / "mcp-tokens" / "server.json"
    token_file.parent.mkdir(parents=True)
    token_file.write_text('{"access_token":"secret"}', encoding="utf-8")

    assert _under_override(
        prof_b, lambda: validate_media_delivery_path(str(token_file.resolve()))
    ) is None


# ---------------------------------------------------------------------------
# M2 — thread / executor context propagation
# ---------------------------------------------------------------------------

class TestThreadContextPropagation:
    """Worker threads must inherit the spawning turn's profile override."""

    def test_raw_thread_loses_override(self, two_profiles):
        """Document the underlying hazard: a bare thread does NOT inherit it."""
        _prof_a, prof_b = two_profiles
        seen = {}

        def worker():
            seen["home"] = str(get_hermes_home())

        def run():
            t = threading.Thread(target=worker)
            t.start()
            t.join()

        _under_override(prof_b, run)
        # A bare thread falls back to the process default — this is WHY the fix
        # primitive is needed.  (Asserted as the hazard, not the desired state.)
        assert seen["home"] != str(prof_b)


    def test_run_async_worker_preserves_override(self, two_profiles):
        """model_tools._run_async's worker-thread branch must keep the override.

        This is the generic sync->async bridge for every async tool; if it
        leaks, every async tool that resolves get_hermes_home() leaks.
        """
        import asyncio

        _prof_a, prof_b = two_profiles
        import model_tools

        async def reads_home():
            return str(get_hermes_home())

        async def driver():
            # Inside a running loop, _run_async spawns a worker thread + loop.
            return model_tools._run_async(reads_home())

        seen = _under_override(prof_b, lambda: asyncio.run(driver()))
        assert seen == str(prof_b)
