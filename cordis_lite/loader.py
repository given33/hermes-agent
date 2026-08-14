"""Declarative component loader with reconciliation and transactional HMR
(paper Sections 5.2 and 6.2).

A configuration is a list of entries (id/url/config/disabled/isolate/
intercept/dependencies).  The loader keeps fibers in step with the entries
(reconciliation).  HMR reloads changed modules transactionally: backup caches,
dispose stale fibers, re-import, install fresh fibers, and roll back on any
failure so the system never enters a half-reloaded state.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .effects import Context
from .component import ComponentSpec, Fiber


@dataclass
class ConfigEntry:
    """One declared fiber (paper Def. 74)."""
    id: str
    url: str  # module path or factory key
    config: dict = field(default_factory=dict)
    disabled: bool = False
    isolate: Any = False
    intercept: Optional[Callable[[str, Any], Any]] = None
    inject: frozenset = frozenset()
    provide: frozenset = frozenset()


class ComponentLoader:
    """Reconciles a config tree against live fibers and runs HMR."""

    def __init__(
        self,
        context: Context,
        store,
        resolver: Callable[[ConfigEntry], ComponentSpec],
    ):
        self.context = context
        self.store = store
        self.resolver = resolver
        self.entries: dict[str, ConfigEntry] = {}
        self.fibers: dict[str, Fiber] = {}
        self.dependency_index: dict[str, set[str]] = {}  # provider_key -> fiber ids

    # -- config reconciliation ------------------------------------------

    def reconcile(self, spec: dict[str, dict]) -> list[str]:
        """Apply a declarative spec {id: {url, config, disabled, ...}}.

        Returns the list of changed fiber ids.
        """
        if _has_running_loop():
            raise RuntimeError("ComponentLoader.reconcile() cannot run inside an event loop; await reconcile_async()")
        return asyncio.run(self.reconcile_async(spec))

    async def reconcile_async(self, spec: dict[str, dict]) -> list[str]:
        """Apply config without silently dropping work in an active loop."""

        changed: list[str] = []
        next_ids = set(spec.keys())
        for fiber_id in list(self.fibers.keys()):
            if fiber_id not in next_ids:
                await self._remove_fiber(fiber_id)
                changed.append(fiber_id)
        for entry_id, raw in spec.items():
            entry = ConfigEntry(
                id=entry_id,
                url=str(raw.get("url") or entry_id),
                config=dict(raw.get("config") or {}),
                disabled=bool(raw.get("disabled", False)),
                isolate=raw.get("isolate", False),
                inject=frozenset(raw.get("inject") or ()),
                provide=frozenset(raw.get("provide") or ()),
            )
            if entry.disabled:
                if entry_id in self.fibers:
                    await self._remove_fiber(entry_id)
                    changed.append(entry_id)
                self.entries[entry_id] = entry
                continue
            previous_entry = self.entries.get(entry_id)
            if entry_id in self.fibers and previous_entry != entry:
                await self._remove_fiber(entry_id)
                changed.append(entry_id)
            self.entries[entry_id] = entry
            if entry_id not in self.fibers:
                await self._install_fiber(entry)
                changed.append(entry_id)
        return changed

    async def _install_fiber(self, entry: ConfigEntry) -> Fiber:
        spec = self.resolver(entry)
        child_ctx = Context(state=None, parent=self.context)
        fiber = Fiber(spec, entry.id, child_ctx, self.store)
        if entry.intercept is not None:
            store = self.store
            store._intercept = entry.intercept
        self.fibers[entry.id] = fiber

        def _provide(fiber: Fiber, key: str) -> Optional[Callable[[], None]]:
            def _register() -> None:
                return None
            return None

        await fiber.activate_async(provide_register=_provide)
        # register provides in the shared store through effects so they are
        # revertible and trigger notifications
        for key in spec.provide:
            marker = object()
            inverse = self.store.set(key, _ProviderBinding(entry.id, key, fiber))
            fiber.push_accumulator(inverse)
            self.dependency_index.setdefault(key, set()).add(entry.id)
        return fiber

    async def _remove_fiber(self, fiber_id: str) -> None:
        fiber = self.fibers.pop(fiber_id, None)
        if fiber is None:
            return
        await fiber.deactivate()
        await fiber.dispose_async()
        for key in list(self.dependency_index):
            self.dependency_index[key].discard(fiber_id)
            if not self.dependency_index[key]:
                del self.dependency_index[key]

    # -- transactional HMR -----------------------------------------------

    async def hmr_reload(
        self,
        changed_urls: set[str],
        externals: set[str] = frozenset(),
        module_loader: Optional[Callable[[str], Any]] = None,
    ) -> dict:
        """Reload changed modules transactionally (paper Algorithm 10).

        Returns {"reloaded": [...], "rolled_back": bool, "declined": [...]}.
        """
        accepted, declined = self._classify(changed_urls, externals)
        if declined:
            return {"reloaded": [], "rolled_back": False, "declined": sorted(declined)}

        stale_entries = [
            e for e in self.entries.values()
            if any(e.url.startswith(u) or u in e.url for u in accepted)
        ]
        if not stale_entries:
            return {"reloaded": [], "rolled_back": False, "declined": []}

        backup_fibers = {
            e.id: (self.fibers[e.id], e) for e in stale_entries if e.id in self.fibers
        }
        # 1) invalidate caches (backup by keeping old module refs)
        old_modules = {}
        for url in accepted:
            mod_name = _module_name(url)
            old_modules[url] = sys.modules.get(mod_name)
            if mod_name in sys.modules:
                del sys.modules[mod_name]
        importlib.invalidate_caches()

        try:
            for entry in stale_entries:
                if entry.id in self.fibers:
                    await self._remove_fiber(entry.id)
                if module_loader is not None:
                    module_loader(entry.url)
                await self._install_fiber(entry)
        except Exception:
            # rollback: restore module caches and rebuild from backup
            for url, old in old_modules.items():
                if old is not None:
                    sys.modules[_module_name(url)] = old
            for fiber_id, (_old_fiber, entry) in backup_fibers.items():
                if entry.id in self.fibers:
                    await self._remove_fiber(entry.id)
                await self._install_fiber(entry)
            return {
                "reloaded": [],
                "rolled_back": True,
                "declined": sorted(declined),
            }
        return {
            "reloaded": [e.id for e in stale_entries],
            "rolled_back": False,
            "declined": sorted(declined),
        }

    @staticmethod
    def _classify(changed_urls: set[str], externals: set[str]):
        accepted = set(changed_urls)
        declined = set(externals)
        return accepted - declined, declined & changed_urls


class _ProviderBinding:
    """Marker for a key bound by a fiber."""

    def __init__(self, fiber_id: str, key: str, fiber: Fiber):
        self.fiber_id = fiber_id
        self.key = key
        self.fiber = fiber


def _module_name(url: str) -> str:
    if url.endswith(".py"):
        return url[:-3].replace("/", ".").replace("\\", ".")
    return url


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False
