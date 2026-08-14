"""Components and fibers (paper Section 4).

A component declares inject (coeffect spec), provide (keys it binds) and an
apply callback (an effect iterator over its child context).  A fiber is one
instantiation with a lifecycle state machine:
  LOADING -> ACTIVE | INACTIVE -> UNLOADING -> (removed)
  FAILED -> INACTIVE
Deactivation waits for dependents (inertia: asynchronous teardown completes
before further change).  Cancellation of a fiber applies its accumulator.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from .effects import Context
from .coeffects import CoeffectSpec


class FiberState(Enum):
    LOADING = "loading"
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNLOADING = "unloading"
    FAILED = "failed"


@dataclass
class ComponentSpec:
    """The static description of a component (paper Section 4.1)."""
    name: str
    inject: frozenset = frozenset()
    provide: frozenset = frozenset()
    apply: Optional[Callable[[Context], Any]] = None  # effect iterator factory
    on_deactivate: Optional[Callable[[], Any]] = None


class Fiber:
    """One live instantiation of a component."""

    def __init__(
        self,
        spec: ComponentSpec,
        uid: str,
        ctx: Context,
        store,
    ):
        self.spec = spec
        self.uid = uid
        self.ctx = ctx
        self.store = store
        self.state = FiberState.LOADING
        self.spec_coeffect = CoeffectSpec(spec.inject)
        self.provided: list[Callable[[], None]] = []
        self.dependents: set["Fiber"] = set()
        self._accumulator: list[Callable[[], None]] = []
        self._inertia: Optional[asyncio.Task] = None
        self._disposers: list[Callable[[], Any]] = []
        self._error: Optional[Exception] = None

    # -- activation ------------------------------------------------------

    def satisfied(self) -> bool:
        return self.spec_coeffect.satisfied(self.store)

    async def activate_async(self, provide_register=None) -> bool:
        """Activate if inject satisfied; bind provided keys; run apply."""
        if self.state == FiberState.ACTIVE:
            return True
        if not self.satisfied():
            self.state = FiberState.INACTIVE
            return False
        self.state = FiberState.LOADING
        try:
            if self.spec.provide and provide_register is not None:
                for key in self.spec.provide:
                    inverse = provide_register(self, key)
                    if inverse is not None:
                        self.provided.append(inverse)
            if self.spec.apply is not None:
                disposer = self.spec.apply(self.ctx)
                if inspect.isawaitable(disposer):
                    disposer = await disposer
                if disposer is not None:
                    self._disposers.append(disposer)
        except Exception as error:
            self.state = FiberState.FAILED
            for disposer in reversed(self._disposers):
                try:
                    result = disposer()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass
            for inverse in reversed(self.provided):
                try:
                    inverse()
                except Exception:
                    pass
            self._disposers.clear()
            self.provided.clear()
            self._error = error
            raise
        self.state = FiberState.ACTIVE
        return True

    def activate(self, provide_register=None) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.activate_async(provide_register))
        raise RuntimeError("Fiber.activate() cannot run inside an event loop; await activate_async()")

    # -- deactivation (with dependency ordering + inertia) ---------------

    async def deactivate(self, wait_for_dependents: bool = True) -> None:
        """UNLOADING -> run teardown -> INACTIVE.  Dependents go first."""
        if self.state in (FiberState.INACTIVE, FiberState.UNLOADING):
            return
        if self.state == FiberState.LOADING and self._inertia is not None:
            # inherit the in-flight transition (paper inertia)
            await self._inertia
        self.state = FiberState.UNLOADING
        if wait_for_dependents and self.dependents:
            await asyncio.gather(
                *[d.deactivate(wait_for_dependents=False) for d in self.dependents],
                return_exceptions=True,
            )
        if self.spec.on_deactivate is not None:
            try:
                result = self.spec.on_deactivate()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        for disposer in reversed(self._disposers):
            try:
                result = disposer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                continue
        for inverse in reversed(self.provided):
            try:
                inverse()
            except Exception:
                continue
        self._disposers.clear()
        self.provided.clear()
        self.state = FiberState.INACTIVE

    async def dispose_async(self) -> None:
        """Apply the fiber's whole accumulator (cancel = apply accumulator)."""
        for disposer in reversed(self._accumulator):
            try:
                result = disposer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                continue
        self._accumulator.clear()

    def dispose(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.dispose_async())
            return
        raise RuntimeError("Fiber.dispose() cannot run inside an event loop; await dispose_async()")

    def push_accumulator(self, disposer: Callable[[], Any]) -> None:
        self._accumulator.append(disposer)

    def fail(self, error: Exception) -> None:
        self._error = error
        self.state = FiberState.FAILED

    def to_inactive_after_failure(self) -> None:
        if self.state == FiberState.FAILED:
            self.state = FiberState.INACTIVE

    # -- change notification ---------------------------------------------

    def handle_change(self, classification: str, spec: CoeffectSpec) -> None:
        if classification == "activating" and self.state == FiberState.INACTIVE:
            self.activate()
        elif classification == "deactivating" and self.state == FiberState.ACTIVE:
            # deactivation is async; spawn an inertia task so teardown runs to
            # completion and later changes see the settled state.
            self._inertia = asyncio.ensure_future(self.deactivate())
            self._inertia.add_done_callback(lambda _t: setattr(self, "_inertia", None))

    # -- registration bookkeeping ----------------------------------------

    def register_dependency(self, other: "Fiber") -> None:
        self.dependents.add(other)

    def unregister_dependency(self, other: "Fiber") -> None:
        self.dependents.discard(other)

    def __hash__(self) -> int:
        return id(self)
