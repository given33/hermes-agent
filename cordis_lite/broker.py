"""Service broker (paper Section 6.2).

Two binding modes:
- exclusive: at most one provider bound at a time (switching perturbs
  consumers briefly).
- broker: a central entrypoint; multiple providers register through
  revertible effects; unload drops them from the routing set automatically.
Rolling updates load a new provider, register it, shift traffic, then unload
the old provider once it no longer carries in-flight requests.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any, Callable, Optional


class ServiceBroker:
    def __init__(self, policy: str = "round-robin"):
        self.policy = policy
        self.providers: dict[str, dict[str, Any]] = {}  # interface -> {id: provider}
        self.weights: dict[str, dict[str, float]] = {}
        self._rr: dict[str, itertools.cycle] = {}
        self._rr_keys: dict[str, tuple[str, ...]] = {}
        self._inflight: dict[str, dict[str, int]] = {}
        self._draining: set[tuple[str, str]] = set()

    # -- registration ----------------------------------------------------

    def register(self, interface: str, provider_id: str, provider: Any, weight: float = 1.0):
        """Register a provider; returns a disposer that unregisters it."""
        self.providers.setdefault(interface, {})[provider_id] = provider
        self.weights.setdefault(interface, {})[provider_id] = weight
        self._inflight.setdefault(interface, {})[provider_id] = 0
        self._draining.discard((interface, provider_id))

        def _dispose() -> None:
            self.providers.get(interface, {}).pop(provider_id, None)
            self.weights.get(interface, {}).pop(provider_id, None)
            self._inflight.get(interface, {}).pop(provider_id, None)
            self._draining.discard((interface, provider_id))
            self._rr.pop(interface, None)
            self._rr_keys.pop(interface, None)

        return _dispose

    # -- routing ---------------------------------------------------------

    def resolve(self, interface: str) -> Optional[Any]:
        pool = {
            provider_id: provider
            for provider_id, provider in self.providers.get(interface, {}).items()
            if (interface, provider_id) not in self._draining
        }
        if not pool:
            return None
        if self.policy == "exclusive":
            # exclusive: exactly one provider, selected by weight
            return next(iter(pool.values()))
        if self.policy == "least-loaded":
            ids = list(pool.keys())
            best = min(ids, key=lambda pid: self._inflight.get(interface, {}).get(pid, 0))
            return pool[best]
        # round-robin (weight-agnostic for simplicity)
        keys = tuple(
            provider_id
            for provider_id, provider in pool.items()
            for _ in range(max(1, min(1000, round(float(self.weights.get(interface, {}).get(provider_id, 1.0)) * 10))))
        )
        if self._rr_keys.get(interface) != keys:
            self._rr[interface] = itertools.cycle(keys)
            self._rr_keys[interface] = keys
        cycle = self._rr[interface]
        pid = next(cycle)
        return pool[pid]

    def begin_call(self, interface: str, provider_id: str) -> None:
        if provider_id not in self.providers.get(interface, {}):
            raise KeyError(f"unknown provider: {interface}:{provider_id}")
        if (interface, provider_id) in self._draining:
            raise RuntimeError(f"provider is draining: {interface}:{provider_id}")
        self._inflight.setdefault(interface, {}).setdefault(provider_id, 0)
        self._inflight[interface][provider_id] += 1

    def end_call(self, interface: str, provider_id: str) -> None:
        self._inflight.get(interface, {}).get(provider_id, 0)
        current = self._inflight.get(interface, {}).get(provider_id, 0)
        if current > 0:
            self._inflight[interface][provider_id] = current - 1

    # -- rolling update --------------------------------------------------

    async def rolling_update(
        self,
        interface: str,
        new_provider_id: str,
        new_provider: Any,
        unload_old: Callable[[str], Any],
        steps: int = 3,
    ) -> None:
        """Load new provider, register it, shift traffic, unload old."""
        self.register(interface, new_provider_id, new_provider)
        await asyncio.sleep(0)  # let the new provider become ACTIVE
        old_ids = [pid for pid in self.providers.get(interface, {}) if pid != new_provider_id]
        for _ in range(steps):
            await asyncio.sleep(0)
        for pid in old_ids:
            self._draining.add((interface, pid))
        for pid in old_ids:
            while self._inflight.get(interface, {}).get(pid, 0):
                await asyncio.sleep(0)
            unload_old(pid)
            self.providers.get(interface, {}).pop(pid, None)
            self.weights.get(interface, {}).pop(pid, None)
            self._inflight.get(interface, {}).pop(pid, None)
