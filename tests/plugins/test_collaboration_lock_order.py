from __future__ import annotations

from contextlib import contextmanager

from plugins.collaboration.dashboard import plugin_api


def test_account_state_lock_acquires_fence_before_state(monkeypatch):
    events = []

    class RecordingStateLock:
        def acquire(self):
            events.append("state-enter")

        def release(self):
            events.append("state-exit")

    class Backend:
        @staticmethod
        @contextmanager
        def account_lifecycle_commit_guard():
            events.append("account-enter")
            try:
                yield
            finally:
                events.append("account-exit")

    monkeypatch.setattr(plugin_api, "_backend_api", lambda: Backend)
    lock = plugin_api._AccountStateLock()
    lock._lock = RecordingStateLock()

    with lock:
        events.append("body")

    assert events == [
        "account-enter",
        "state-enter",
        "body",
        "state-exit",
        "account-exit",
    ]


def test_account_state_lock_is_reentrant():
    lock = plugin_api._AccountStateLock()
    with lock:
        with lock:
            pass
