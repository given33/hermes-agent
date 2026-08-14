"""Minimal hosted-turn fiber tree for the Cordis-lite experiment."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .component import Fiber


@dataclass
class HostedTurnFiberTree:
    """Owns fibers for one experimental hosted turn.

    It provides explicit ownership and teardown ordering, but it is not the
    production Hosted Collaboration scheduler.  Production turns use the
    adapter in ``hermes_runtime.composability.hosted_plan``.
    """

    turn_id: str
    fibers: dict[str, Fiber] = field(default_factory=dict)
    terminal_witness: dict[str, Any] | None = None

    def add(self, fiber: Fiber) -> Fiber:
        if fiber.uid in self.fibers:
            raise ValueError(f"fiber already exists: {fiber.uid}")
        self.fibers[fiber.uid] = fiber
        return fiber

    def get(self, fiber_id: str) -> Fiber | None:
        return self.fibers.get(str(fiber_id or "").strip())

    def mark_terminal(self, witness: dict[str, Any]) -> None:
        if not isinstance(witness, dict) or not witness.get("status"):
            raise ValueError("terminal witness requires a status")
        self.terminal_witness = dict(witness)

    async def dispose(self) -> None:
        for fiber in reversed(tuple(self.fibers.values())):
            await fiber.deactivate()
            await fiber.dispose_async()
        self.fibers.clear()
