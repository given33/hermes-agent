"""Revertible effects (paper Section 3.1).

The effect context is dGamma = Gamma x (Gamma -> Gamma): the current state
plus an accumulator of inverses.  Every context mutation flows through
Context.effect(); inverses are folded LIFO; dispose() applies the folded
inverse exactly once (armed flag) and composes into the parent context so
that unloading a parent reverts children first.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

# A step yielded by an effect iterator: (value, inverse).
EffectStep = tuple[Any, Callable[[], Any]]
EffectIterator = AsyncIterator[Optional[EffectStep]]


class EffectNotArmedError(RuntimeError):
    """Raised when an effect is used after its disposer ran."""


class Context:
    """A unified context Gamma with effect tracking.

    state holds the current context value; parent links create the fiber
    context tree.  The accumulator is the list of disposers; Context.dispose()
    runs them in reverse (LIFO) and clears the accumulator.
    """

    def __init__(self, state: Any = None, parent: Optional["Context"] = None):
        self.state = state
        self.parent = parent
        self._accumulator: list[Callable[[], Any]] = []
        self._disposed = False
        self._coeffects: Any = None  # set by coeffects.attach_coeffects

    # -- effect tracking -------------------------------------------------

    async def effect(
        self,
        callback: Callable[[], EffectIterator],
        guard: Optional[Callable[[], bool]] = None,
    ) -> Callable[[], Any]:
        """Run an effect iterator and return an idempotent disposer.

        The iterator yields (value, inverse) steps; the inverse reverts one
        step.  The disposer stops iteration at the next boundary (guard), waits
        for it, then applies the accumulated inverses in reverse order.
        """
        if self._disposed:
            raise EffectNotArmedError("context already disposed")
        armed = True
        inverses: list[Callable[[], Any]] = []
        acquired = asyncio.Event()

        async def _run() -> None:
            iterable = callback()
            try:
                async for step in iterable:
                    if not armed or (guard is not None and not guard()):
                        break
                    if step is None:
                        continue
                    value, inverse = step
                    inverses.append(inverse)
                    if value is not None:
                        self.state = value
                    acquired.set()
            finally:
                acquired.set()
                if hasattr(iterable, "aclose"):
                    try:
                        await iterable.aclose()
                    except Exception:
                        pass

        task = asyncio.ensure_future(_run())
        # The disposer must carry the witness for at least the first completed
        # effect boundary.  Returning before this point creates a race where
        # immediate disposal cancels acquisition before its inverse is known.
        await acquired.wait()
        if task.done():
            task.result()

        async def dispose() -> None:
            nonlocal armed
            if not armed:
                return
            armed = False
            try:
                await task
            finally:
                for inverse in reversed(inverses):
                    result = inverse()
                    if inspect.isawaitable(result):
                        await result

        # Parent composition: the child's disposer is recorded on the parent's
        # accumulator so unloading the parent reverts children first (LIFO).
        if self.parent is not None:
            self.parent._accumulator.append(dispose)
        else:
            self._accumulator.append(dispose)
        return dispose

    def effect_sync(
        self,
        transform: Callable[[Any], Any],
        inverse: Callable[[Any], Any],
    ) -> Callable[[], Any]:
        """Plain (non-iterator) effect: transform the state, return a disposer."""
        if self._disposed:
            raise EffectNotArmedError("context already disposed")
        previous = self.state
        armed = True

        def _apply() -> None:
            self.state = transform(previous)

        def _revert() -> None:
            nonlocal armed
            if not armed:
                return
            armed = False
            # Do not overwrite a newer state acquired after this effect.
            if self.state is applied or self.state == applied:
                self.state = inverse(previous)

        _apply()
        applied = self.state
        if self.parent is not None:
            self.parent._accumulator.append(_revert)
        else:
            self._accumulator.append(_revert)
        return _revert

    async def dispose_async(self) -> None:
        """Apply sync and async inverses exactly once in LIFO order."""
        if self._disposed:
            return
        self._disposed = True
        for disposer in reversed(self._accumulator):
            try:
                result = disposer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                continue
        self._accumulator.clear()

    def dispose(self) -> None:
        """Synchronous compatibility wrapper that never drops async cleanup."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.dispose_async())
            return
        raise RuntimeError("Context.dispose() cannot run inside an event loop; await dispose_async()")

    # -- introspection ---------------------------------------------------

    def fingerprint(self) -> tuple:
        """A structural fingerprint of the state used by the confluence fuzzer."""
        return _fingerprint(self.state)

    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def accumulator_depth(self) -> int:
        return len(self._accumulator)


def _fingerprint(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _fingerprint(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(sorted(_fingerprint(v) for v in value))
    if hasattr(value, "fingerprint"):
        return value.fingerprint()
    return value
