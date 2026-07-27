"""Framework-neutral in-memory session registry.

Runtime adapters keep different session payloads, but they must not each
invent another unlocked module-level dictionary.  This registry provides the
shared concurrency and snapshot semantics for those live handles.  Durable
conversation history remains owned by ``SessionDB``; this object is only the
process-local index of sessions currently attached to an adapter.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from threading import RLock
from typing import Generic, TypeVar


SessionValue = TypeVar("SessionValue")


class LiveSessionRegistry(MutableMapping[str, SessionValue], Generic[SessionValue]):
    """A lock-owning mapping with stable iteration snapshots.

    ``MutableMapping`` compatibility lets legacy adapters migrate without a
    flag day.  Every mapping operation is synchronized, and iteration methods
    return immutable snapshots so another RPC thread cannot resize the backing
    dictionary halfway through a loop.  Values are intentionally returned by
    reference because adapter-specific session records contain live agents,
    transports and cancellation primitives.
    """

    def __init__(self) -> None:
        self._items: dict[str, SessionValue] = {}
        self._lock = RLock()

    @property
    def lock(self) -> RLock:
        """The registry's re-entrant lock for atomic multi-step operations."""

        return self._lock

    def __getitem__(self, key: str) -> SessionValue:
        with self._lock:
            return self._items[key]

    def __setitem__(self, key: str, value: SessionValue) -> None:
        with self._lock:
            self._items[key] = value

    def __delitem__(self, key: str) -> None:
        with self._lock:
            del self._items[key]

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(tuple(self._items))

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def get(self, key: str, default=None):
        with self._lock:
            return self._items.get(key, default)

    def pop(self, key: str, default=...):
        with self._lock:
            if default is ...:
                return self._items.pop(key)
            return self._items.pop(key, default)

    def setdefault(self, key: str, default: SessionValue | None = None):
        with self._lock:
            return self._items.setdefault(key, default)  # type: ignore[arg-type]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def keys(self):
        with self._lock:
            return tuple(self._items.keys())

    def values(self):
        with self._lock:
            return tuple(self._items.values())

    def items(self):
        with self._lock:
            return tuple(self._items.items())

    def snapshot(self) -> dict[str, SessionValue]:
        """Return a shallow point-in-time copy for diagnostics or shutdown."""

        with self._lock:
            return dict(self._items)
