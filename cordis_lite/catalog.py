"""Small registry used by the Cordis-lite experiment.

This module intentionally stays an experiment-only catalog.  The production
Hermes runtime uses ``hermes_runtime.composability.ProviderCatalog`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class CatalogEntry:
    component_id: str
    value: Any
    generation: int


class ComponentCatalog:
    """Idempotent component registry with exact unregister witnesses."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, CatalogEntry] = {}
        self._generation = 0

    def register(self, component_id: str, value: Any) -> Any:
        key = str(component_id or "").strip()
        if not key:
            raise ValueError("component_id is required")
        with self._lock:
            self._generation += 1
            entry = CatalogEntry(key, value, self._generation)
            previous = self._entries.get(key)
            self._entries[key] = entry

        armed = True

        def dispose() -> None:
            nonlocal armed
            if not armed:
                return
            armed = False
            with self._lock:
                if self._entries.get(key) == entry:
                    if previous is None:
                        self._entries.pop(key, None)
                    else:
                        self._entries[key] = previous

        return dispose

    def get(self, component_id: str) -> Any | None:
        with self._lock:
            entry = self._entries.get(str(component_id or "").strip())
            return entry.value if entry is not None else None

    def snapshot(self) -> dict[str, CatalogEntry]:
        with self._lock:
            return dict(self._entries)

    def remove(self, component_id: str) -> None:
        with self._lock:
            self._entries.pop(str(component_id or "").strip(), None)


def catalog_router(catalog: ComponentCatalog) -> dict[str, dict[str, Any]]:
    """Return a serializable diagnostic view for the prototype catalog."""

    return {
        key: {"generation": entry.generation, "value": entry.value}
        for key, entry in catalog.snapshot().items()
    }
