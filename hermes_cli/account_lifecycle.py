"""Cross-process commit guard for account-generation lifecycle changes."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
from typing import Iterator

from hermes_runtime.config import get_hermes_home


_PROCESS_LOCK = threading.RLock()
_LOCAL = threading.local()


def _lock_path() -> Path:
    return Path(get_hermes_home()) / "runtime" / "account-lifecycle.lock"


@contextmanager
def account_lifecycle_commit_guard() -> Iterator[None]:
    """Serialize deletion intent commits with account-owned state commits.

    The OS lock is process-wide and crash-released. The thread-local depth
    makes nested collaboration saves reentrant. The process RLock orders
    ACQUISITION only: it is held around the OS-lock take and released
    before the yield, so a caller that abandons the generator (test module
    reloads, torn-down workers) can be finalized by the GC on any thread
    without tripping RLock's owner-thread release rule — the msvcrt/fcntl
    unlock is handle-based and cross-thread safe.
    """

    depth = int(getattr(_LOCAL, "depth", 0))
    if depth:
        _LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _LOCAL.depth = depth
        return

    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    acquired = False
    try:
        with _PROCESS_LOCK:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired = True
        _LOCAL.depth = 1
        try:
            yield
        finally:
            _LOCAL.depth = 0
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
