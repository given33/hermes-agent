"""Cross-process commit guard for account-generation lifecycle changes."""

from __future__ import annotations

import os
from pathlib import Path
import threading

from hermes_runtime.config import get_hermes_home


_PROCESS_LOCK = threading.RLock()
_LOCAL = threading.local()


def _lock_path() -> Path:
    return Path(get_hermes_home()) / "runtime" / "account-lifecycle.lock"


class _AccountLifecycleGuard:
    """Explicit RAII guard for account-owned state commits.

    A @contextmanager generator can be finalized by GC on another thread if a
    worker is torn down abnormally; releasing an RLock there raises and can
    leak the OS lock. An explicit object has deterministic __exit__ on the
    acquiring call stack. Keep the process lock across the critical section so
    Windows msvcrt byte-range locks have one owner per process.
    """

    def __enter__(self) -> None:
        depth = int(getattr(_LOCAL, "depth", 0))
        if depth:
            _LOCAL.depth = depth + 1
            return

        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
        try:
            with _PROCESS_LOCK:
                if os.name == "nt":
                    import portalocker

                    handle.seek(0)
                    portalocker.lock(
                        handle.fileno(),
                        portalocker.LockFlags.EXCLUSIVE,
                    )
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        self._depth = depth
        _LOCAL.guard = self
        _LOCAL.depth = depth + 1

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        depth = int(getattr(_LOCAL, "depth", 0))
        if depth > 1:
            _LOCAL.depth = depth - 1
            return

        handle = getattr(self, "_handle", None)
        if handle is not None:
            try:
                if os.name == "nt":
                    import portalocker

                    handle.seek(0)
                    portalocker.unlock(handle.fileno())
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        _LOCAL.depth = 0
        _LOCAL.guard = None


def account_lifecycle_commit_guard() -> _AccountLifecycleGuard:
    """Serialize deletion intent commits with account-owned state commits."""

    return _AccountLifecycleGuard()
