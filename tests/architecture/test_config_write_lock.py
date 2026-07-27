"""Behavioral proof for the config write-lock claims the conversions rely on.

The mutate_config()/config_write_lock() conversions (gateway/slash_commands,
telegram adapter, yuanbao, doctor --fix) assume two properties:

1. **Reentrancy.** Several converted paths run ``mutate_config`` (or
   ``save_config``) while ALREADY inside ``config_write_lock`` — e.g. the
   /model picker wraps read→mutate→``save_config`` in an explicit lock, and
   ``save_config`` takes the lock again itself. The lock is depth-counted
   per process, so the nested acquire must be an instant no-op. Crucially,
   "did not hang" is NOT sufficient proof: the lock degrades to unlocked
   after a 10s timeout by design (it never deadlocks anything, ever), so a
   broken reentrancy counter would still "work" — 10 seconds late and
   without exclusion. The test therefore asserts the nested call completes
   fast (well under the degrade window), not merely that it completes.

2. **Serialized read-modify-write.** Two sequential ``mutate_config`` calls
   touching different keys must both survive in the file — the whole point
   of replacing the bare read→modify→``save_config`` pattern that could
   clobber a sibling's write.

Also pins the documented fail-closed stance: a non-mapping config.yaml must
refuse the read-modify-write rather than silently replacing the user's file
with just the mutation.
"""

from __future__ import annotations

import threading
import time

import yaml

# Degrade window is 10s (_CONFIG_FILE_LOCK_TIMEOUT_SECONDS); a reentrant
# acquire must be orders of magnitude faster. 5s keeps the assertion safe on
# a slow CI box while still cleanly separating "reentered" from "degraded".
_FAST_ENOUGH_SECONDS = 5.0


def test_mutate_config_inside_config_write_lock_reenters_fast(tmp_path):
    from hermes_cli.config import config_write_lock, mutate_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("existing: 1\n", encoding="utf-8")

    outcome: dict = {}

    def worker():
        # The exact shape of the converted /model persist path: explicit
        # outer lock, then a nested locker (mutate_config) on the same path.
        start = time.monotonic()
        with config_write_lock(config_path):
            def _set(cfg: dict):
                cfg["added"] = "yes"
                return cfg

            written = mutate_config(_set, config_path=config_path)
        outcome["elapsed"] = time.monotonic() - start
        outcome["written"] = written

    # Run in a worker thread so a true deadlock fails the test via join
    # timeout instead of hanging the whole pytest session.
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=30)

    assert not t.is_alive(), (
        "mutate_config() inside config_write_lock() never returned — "
        "the in-process lock is no longer reentrant"
    )
    assert outcome["elapsed"] < _FAST_ENOUGH_SECONDS, (
        f"nested acquire took {outcome['elapsed']:.1f}s — that is the 10s "
        "degrade-to-unlocked path, not reentrancy: the depth counter is "
        "broken and nested writers are running WITHOUT cross-process "
        "exclusion (plus a 10s stall per write)"
    )
    assert outcome["written"] == {"existing": 1, "added": "yes"}
    on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert on_disk == {"existing": 1, "added": "yes"}


def test_sequential_mutate_config_calls_preserve_each_other(tmp_path):
    from hermes_cli.config import mutate_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("model:\n  default: alpha\n", encoding="utf-8")

    def _writer_a(cfg: dict):
        cfg.setdefault("gateway", {})["footer"] = False
        return cfg

    def _writer_b(cfg: dict):
        cfg.setdefault("telegram", {})["dm_topic_thread_id"] = 42
        return cfg

    mutate_config(_writer_a, config_path=config_path)
    mutate_config(_writer_b, config_path=config_path)

    on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # Writer B re-read the CURRENT file under the lock, so writer A's key
    # survives — the lost-update the old read→modify→save_config pattern
    # allowed cannot happen through this API.
    assert on_disk == {
        "model": {"default": "alpha"},
        "gateway": {"footer": False},
        "telegram": {"dm_topic_thread_id": 42},
    }


def test_mutate_config_refuses_non_mapping_file(tmp_path):
    import pytest

    from hermes_cli.config import mutate_config

    config_path = tmp_path / "config.yaml"
    # A YAML list, not a mapping — e.g. a half-written or foreign file.
    config_path.write_text("- oops\n", encoding="utf-8")

    def _mutate(cfg: dict):
        cfg["k"] = "v"
        return cfg

    with pytest.raises(ValueError, match="not a YAML mapping"):
        mutate_config(_mutate, config_path=config_path)

    # Fail-closed means the bad file is left untouched for the user to fix,
    # not replaced by a config containing only the mutation.
    assert config_path.read_text(encoding="utf-8") == "- oops\n"


def test_mutate_fn_returning_none_skips_the_write(tmp_path):
    from hermes_cli.config import mutate_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("existing: 1\n", encoding="utf-8")
    before_stat = config_path.stat().st_mtime_ns

    result = mutate_config(lambda cfg: None, config_path=config_path)

    assert result is None
    # "Nothing changed" writes nothing — the telegram adapter and doctor
    # --fix conversions rely on this to avoid churning mtime (and the
    # (mtime_ns, size) read cache) on no-op passes.
    assert config_path.stat().st_mtime_ns == before_stat
    assert config_path.read_text(encoding="utf-8") == "existing: 1\n"


# ── Unsupported-filesystem vs contention classification ─────────────────────
# _try_lock_fd must distinguish "a peer holds the lock" (poll — it clears in
# milliseconds) from "this filesystem cannot lock at all" (some network
# mounts: polling can NEVER succeed). Before the classification, the latter
# burned the FULL 10s timeout on every write while holding _CONFIG_LOCK —
# and several converted call sites are sync functions invoked inside async
# handlers, so that was a 10s gateway event-loop stall per config write.


def _patch_os_lock_call(monkeypatch, fake):
    """Replace the platform lock primitive _try_lock_fd actually calls.

    _try_lock_fd imports msvcrt/fcntl lazily inside the function, so
    patching the module attribute is picked up on the very next attempt.
    ``fake`` must accept *args: msvcrt.locking(fd, mode, n) vs
    fcntl.flock(fd, op) — and on release _unlock_fd goes through the same
    patched callable (LK_UNLCK / LOCK_UN).
    """
    import os

    if os.name == "nt":
        monkeypatch.setattr("msvcrt.locking", fake)
    else:
        monkeypatch.setattr("fcntl.flock", fake)


def _reset_unsupported_memory(monkeypatch):
    """Give the test a pristine copy of the process-wide degrade memory.

    The unsupported-FS flag is deliberately permanent per process; without
    this reset one test's simulated ENOTSUP would leak the degrade (and eat
    the once-per-process warning) into every later test in the session.
    """
    import hermes_cli.config as config_module

    monkeypatch.setattr(config_module, "_CONFIG_FILE_LOCK_UNSUPPORTED_PATHS", set())
    monkeypatch.setattr(config_module, "_CONFIG_FILE_LOCK_UNSUPPORTED_WARNED", False)


def test_unsupported_filesystem_degrades_fast_and_warns_once(
    tmp_path, monkeypatch, caplog
):
    import errno
    import logging

    from hermes_cli.config import config_write_lock

    _reset_unsupported_memory(monkeypatch)

    calls = {"n": 0}

    def _always_unsupported(*args):
        calls["n"] += 1
        # What flock/locking raises on filesystems with no lock support
        # (NFS without lockd, some SMB/FUSE mounts): an errno that is NOT
        # in the contention family, so retrying is pointless.
        raise OSError(errno.ENOTSUP, "Operation not supported")

    _patch_os_lock_call(monkeypatch, _always_unsupported)

    config_path = tmp_path / "config.yaml"
    entered = []
    with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
        start = time.monotonic()
        with config_write_lock(config_path):
            entered.append(1)
        first_elapsed = time.monotonic() - start

        start = time.monotonic()
        with config_write_lock(config_path):
            entered.append(2)
        second_elapsed = time.monotonic() - start

    assert entered == [1, 2], "degraded acquire must still run the body"
    # The whole point of the fix: an unsupported filesystem is detected on
    # the FIRST attempt and degrades immediately — not after polling out the
    # 10s contention timeout while holding _CONFIG_LOCK. 1s is orders of
    # magnitude above the real cost (one syscall) but immune to CI jitter.
    assert first_elapsed < 1.0, (
        f"first acquire took {first_elapsed:.2f}s — unsupported-FS errno was "
        "treated as contention and polled out the timeout"
    )
    assert second_elapsed < 1.0, (
        f"second acquire took {second_elapsed:.2f}s — the per-path degrade "
        "memory is not being consulted"
    )
    # Degrade is remembered per process: the second acquire must skip the OS
    # layer entirely rather than re-probing the dead filesystem every write.
    assert calls["n"] == 1, (
        f"lock primitive called {calls['n']} times — the second acquire "
        "should not touch the OS lock layer at all"
    )
    unavailable_warnings = [
        rec
        for rec in caplog.records
        if "file locking is unavailable" in rec.getMessage()
    ]
    # Exactly ONE warning across both acquires: it would otherwise fire on
    # every single config write for the rest of the process's life.
    assert len(unavailable_warnings) == 1, (
        f"expected exactly one degrade warning, got {len(unavailable_warnings)}: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    # And no bogus "could not lock within timeout" noise — this is not
    # contention, and claiming a timeout would send users chasing a
    # nonexistent stuck peer process.
    assert not any(
        "could not lock" in rec.getMessage() for rec in caplog.records
    ), "unsupported FS must not be reported as a contention timeout"


def test_contention_still_polls_until_the_holder_releases(
    tmp_path, monkeypatch, caplog
):
    import errno
    import logging

    import hermes_cli.config as config_module
    from hermes_cli.config import config_write_lock

    _reset_unsupported_memory(monkeypatch)

    calls = {"n": 0}

    def _busy_then_free(*args):
        calls["n"] += 1
        if calls["n"] <= 3:
            # What flock/locking raises while a live peer holds the lock —
            # EAGAIN is in the contention family on both platforms, so the
            # loop must keep polling (the peer releases in milliseconds).
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
        return None  # acquired (and later the LK_UNLCK/LOCK_UN release)

    _patch_os_lock_call(monkeypatch, _busy_then_free)

    config_path = tmp_path / "config.yaml"
    entered = []
    with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
        start = time.monotonic()
        with config_write_lock(config_path):
            entered.append(1)
        elapsed = time.monotonic() - start

    assert entered == [1]
    # 3 contended attempts + the acquiring one; the release then reuses the
    # same patched primitive, so at least 4 calls prove real polling.
    assert calls["n"] >= 4, (
        f"lock primitive called only {calls['n']} times — contention was not "
        "polled through to acquisition"
    )
    # Three 0.05s poll sleeps ≈ 0.15s; well under the degrade window. The
    # cap mostly guards against contention being misrouted into a path that
    # waits out the whole timeout.
    assert elapsed < _FAST_ENOUGH_SECONDS
    # Contention must leave the permanent-degrade machinery untouched: no
    # unsupported-FS warning, and the path must NOT be blacklisted (that
    # would silently disable cross-process exclusion after any brief race).
    assert not any(
        "file locking is unavailable" in rec.getMessage()
        for rec in caplog.records
    ), "genuine contention was misclassified as an unsupported filesystem"
    assert not config_module._CONFIG_FILE_LOCK_UNSUPPORTED_PATHS, (
        "contention poisoned the unsupported-path memory — every later write "
        "would silently skip cross-process locking"
    )
