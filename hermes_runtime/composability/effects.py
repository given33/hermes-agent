"""Owned, idempotent effect scopes for resources inside Hermes' boundary.

An effect is registered only after acquisition has succeeded.  The disposer
therefore closes over the exact acquisition witness instead of looking up a
resource by name later.  This is the important part of Cordis' revertible
effect semantics; the scope is intentionally not an undo mechanism for
already-observed external emissions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import logging
from typing import Any, Awaitable, Callable, Optional
import uuid


logger = logging.getLogger(__name__)

Disposer = Callable[[], Any]


class EffectScopeError(RuntimeError):
    """Base error for scope lifecycle violations."""


class EffectScopeClosedError(EffectScopeError):
    """Raised when a new effect is registered after scope closure."""


@dataclass(frozen=True)
class EffectMetadata:
    effect_id: str
    owner_id: str
    scope_id: str
    description: str
    durability: str
    external_boundary: str
    idempotency_key: str


@dataclass
class _EffectEntry:
    metadata: EffectMetadata
    disposer: Disposer
    disposed: bool = False
    dispose_error: BaseException | None = None


class EffectHandle:
    """A precise handle for one acquired effect."""

    def __init__(self, scope: "EffectScope", entry: _EffectEntry) -> None:
        self._scope = scope
        self._entry = entry

    @property
    def effect_id(self) -> str:
        return self._entry.metadata.effect_id

    @property
    def metadata(self) -> EffectMetadata:
        return self._entry.metadata

    @property
    def disposed(self) -> bool:
        return self._entry.disposed

    async def dispose(self) -> None:
        await self._scope._dispose_entry(self._entry)


class EffectScope:
    """Async context for owned resources.

    Disposers execute in reverse acquisition order and are idempotent.  A
    child scope is registered as one parent effect, so parent close waits for
    the child's cleanup before continuing.  A failed disposer is recorded and
    cleanup continues; ``close`` raises an :class:`ExceptionGroup` after all
    entries have had a chance to run.
    """

    def __init__(
        self,
        *,
        owner_id: str,
        parent: Optional["EffectScope"] = None,
        scope_id: str = "",
        close_timeout: float | None = None,
    ) -> None:
        self.owner_id = str(owner_id or "").strip() or "anonymous"
        self.scope_id = str(scope_id or "").strip() or f"scope-{uuid.uuid4().hex}"
        self.close_timeout = close_timeout
        self.parent = parent
        self._entries: list[_EffectEntry] = []
        self._closed = False
        self._closing = False
        self._lock = asyncio.Lock()
        self._close_done: asyncio.Event | None = None
        self._close_owner: asyncio.Task[Any] | None = None
        self._close_errors: tuple[BaseException, ...] = ()
        self._child_handle: EffectHandle | None = None
        if parent is not None:
            self._child_handle = parent.add(
                self.close,
                description=f"child-scope:{self.scope_id}",
                durability="in_memory",
                external_boundary="internal",
                idempotency_key=f"child-scope:{self.scope_id}",
            )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def entries(self) -> tuple[EffectMetadata, ...]:
        return tuple(entry.metadata for entry in self._entries)

    def add(
        self,
        disposer: Disposer,
        *,
        description: str,
        durability: str = "in_memory",
        external_boundary: str = "internal",
        idempotency_key: str = "",
        effect_id: str = "",
    ) -> EffectHandle:
        """Register a disposer after its forward effect has succeeded."""

        if self._closed or self._closing:
            raise EffectScopeClosedError(
                f"effect scope {self.scope_id!r} is already closing"
            )
        if not callable(disposer):
            raise TypeError("disposer must be callable")
        metadata = EffectMetadata(
            effect_id=str(effect_id or "").strip() or f"effect-{uuid.uuid4().hex}",
            owner_id=self.owner_id,
            scope_id=self.scope_id,
            description=str(description or "").strip() or "unnamed-effect",
            durability=str(durability or "in_memory"),
            external_boundary=str(external_boundary or "internal"),
            idempotency_key=str(idempotency_key or "").strip(),
        )
        entry = _EffectEntry(metadata=metadata, disposer=disposer)
        self._entries.append(entry)
        return EffectHandle(self, entry)

    def child(self, *, owner_id: str = "", scope_id: str = "") -> "EffectScope":
        if self._closed or self._closing:
            raise EffectScopeClosedError(
                f"effect scope {self.scope_id!r} is already closing"
            )
        return EffectScope(
            owner_id=owner_id or self.owner_id,
            parent=self,
            scope_id=scope_id,
            close_timeout=self.close_timeout,
        )

    async def _dispose_entry(self, entry: _EffectEntry) -> None:
        async with self._lock:
            if entry.disposed:
                if entry.dispose_error is not None:
                    raise entry.dispose_error
                return
            try:
                result = entry.disposer()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                entry.dispose_error = exc
                raise
            finally:
                entry.disposed = True

    async def close(self) -> None:
        if self._closed:
            if self._close_errors:
                raise ExceptionGroup(
                    f"effect scope {self.scope_id!r} close failed",
                    list(self._close_errors),
                )
            return
        if self._closing:
            # A disposer can re-enter close from the owning task; waiting in
            # that case would deadlock. Independent callers, however, must not
            # observe a false completion while cleanup is still running.
            if self._close_owner is asyncio.current_task():
                return
            if self._close_done is not None:
                await self._close_done.wait()
            if self._close_errors:
                raise ExceptionGroup(
                    f"effect scope {self.scope_id!r} close failed",
                    list(self._close_errors),
                )
            return
        self._closing = True
        self._close_done = asyncio.Event()
        self._close_owner = asyncio.current_task()
        errors: list[BaseException] = []
        try:
            for entry in reversed(self._entries):
                try:
                    operation = self._dispose_entry(entry)
                    if self.close_timeout is not None:
                        await asyncio.wait_for(operation, self.close_timeout)
                    else:
                        await operation
                except BaseException as exc:
                    errors.append(exc)
                    logger.warning(
                        "Effect disposer failed: scope=%s effect=%s description=%s",
                        self.scope_id,
                        entry.metadata.effect_id,
                        entry.metadata.description,
                        exc_info=True,
                    )
        finally:
            self._close_errors = tuple(errors)
            self._closed = True
            self._closing = False
            self._close_owner = None
            self._close_done.set()
        if errors:
            raise ExceptionGroup(
                f"effect scope {self.scope_id!r} close failed",
                errors,
            )

    def close_sync(self) -> None:
        """Close a scope from a synchronous lifecycle boundary.

        Plugin discovery and reload are synchronous APIs.  They may therefore
        only close synchronous disposers in-process; an async disposer is
        rejected explicitly instead of being silently dropped or executed in
        an unrelated event loop.  Async-owned resources must be closed by the
        caller through :meth:`close` before entering this boundary.
        """

        if self._closed:
            if self._close_errors:
                raise ExceptionGroup(
                    f"effect scope {self.scope_id!r} close failed",
                    list(self._close_errors),
                )
            return
        if self._closing:
            raise RuntimeError(
                f"effect scope {self.scope_id!r} is already closing asynchronously"
            )
        self._closing = True
        errors: list[BaseException] = []
        try:
            for entry in reversed(self._entries):
                if entry.disposed:
                    continue
                try:
                    result = entry.disposer()
                    if inspect.isawaitable(result):
                        close = getattr(result, "close", None)
                        if callable(close):
                            close()
                        raise RuntimeError(
                            f"async disposer requires await: {entry.metadata.effect_id}"
                        )
                except BaseException as exc:
                    entry.dispose_error = exc
                    errors.append(exc)
                finally:
                    entry.disposed = True
        finally:
            self._close_errors = tuple(errors)
            self._closed = True
            self._closing = False
        if errors:
            raise ExceptionGroup(
                f"effect scope {self.scope_id!r} close failed",
                errors,
            )

    async def __aenter__(self) -> "EffectScope":
        if self._closed or self._closing:
            raise EffectScopeClosedError(self.scope_id)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


def sync_disposer(callback: Callable[[], Any]) -> Disposer:
    """Return a disposer that accepts sync or async callbacks unchanged."""

    if not callable(callback):
        raise TypeError("callback must be callable")
    return callback
