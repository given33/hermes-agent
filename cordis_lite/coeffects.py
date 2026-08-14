"""Reactive coeffects (paper Section 3.2).

The coeffect context is a dependency table Sigma = K => V.  Components
declare CoeffectSpec (the set of keys they require).  Every mutation to the
table is classified against a spec as activating / deactivating / neutral,
and registered callbacks are notified.  Isolation (realm) and interception
(metadata) are supported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
from typing import Any, Callable, Optional

ACTIVATING = "activating"
DEACTIVATING = "deactivating"
NEUTRAL = "neutral"


@dataclass(frozen=True)
class CoeffectSpec:
    """A set of dependency keys a component declares (paper Def. 25)."""
    keys: frozenset

    def __init__(self, keys):
        object.__setattr__(self, "keys", frozenset(keys))

    def satisfied(self, store: "CoeffectStore") -> bool:
        return all(store.has(key) for key in self.keys)


class CoeffectStore:
    """The dependency table with reactivity, isolation and interception.

    Realms let the same logical key resolve differently for different
    components (paper Def. 27 derived realization).  Interception wraps the
    resolved value on access (paper Def. 30).
    """

    def __init__(
        self,
        parent: Optional["CoeffectStore"] = None,
        realm: Optional[dict] = None,
        intercept: Optional[Callable[[str, Any], Any]] = None,
    ):
        self.parent = parent
        self._realm: dict[str, Any] = dict(realm or {})
        self._intercept = intercept
        self._bindings: dict[Any, dict[str, Any]] = {}  # realm_id -> {key: value}
        self._watchers: list[tuple[CoeffectSpec, Callable[[str, CoeffectSpec], None]]] = []

    # -- binding ---------------------------------------------------------

    def _realm_id(self, key: str) -> Any:
        return self._realm.get(key, key)

    def has(self, key: str) -> bool:
        rid = self._realm_id(key)
        if rid in self._bindings and key in self._bindings[rid]:
            return True
        if self.parent is not None:
            return self.parent.has(key)
        return False

    def get(self, key: str, default: Any = None) -> Any:
        rid = self._realm_id(key)
        value = None
        if rid in self._bindings and key in self._bindings[rid]:
            value = self._bindings[rid][key]
        elif self.parent is not None:
            value = self.parent.get(key, default)
        else:
            value = default
        if value is None:
            return default
        if self._intercept is not None:
            return self._intercept(key, value)
        return value

    def set(self, key: str, value: Any) -> Callable[[], None]:
        """Bind a key; returns an inverse that restores the previous binding."""
        rid = self._realm_id(key)
        before = copy.deepcopy(self._bindings)
        bindings = self._bindings.setdefault(rid, {})
        previous = copy.deepcopy(bindings.get(key))
        bindings[key] = value
        self._notify_all(before)

        def _inverse() -> None:
            if previous is None:
                self._bindings.get(rid, {}).pop(key, None)
            else:
                self._bindings.setdefault(rid, {})[key] = copy.deepcopy(previous)
            self._notify_all(dict(self._bindings))

        return _inverse

    def unset(self, key: str) -> Callable[[], None]:
        rid = self._realm_id(key)
        before = copy.deepcopy(self._bindings)
        bindings = self._bindings.setdefault(rid, {})
        previous = copy.deepcopy(bindings.get(key))
        bindings.pop(key, None)
        self._notify_all(before)

        def _inverse() -> None:
            if previous is not None:
                self._bindings.setdefault(rid, {})[key] = copy.deepcopy(previous)
            self._notify_all(dict(self._bindings))

        return _inverse

    # -- reactivity ------------------------------------------------------

    def classify(self, spec: CoeffectSpec, before: "CoeffectStore", after: "CoeffectStore") -> str:
        was = spec.satisfied(before)
        now = spec.satisfied(after)
        if was and now:
            return NEUTRAL
        if not was and now:
            return ACTIVATING
        if was and not now:
            return DEACTIVATING
        return NEUTRAL

    def watch(self, spec: CoeffectSpec, callback: Callable[[str, CoeffectSpec], None]) -> Callable[[], None]:
        self._watchers.append((spec, callback))

        def _unwatch() -> None:
            self._watchers.remove((spec, callback))

        return _unwatch

    def _notify_all(self, before: dict) -> None:
        for spec, callback in list(self._watchers):
            was = _satisfied_in(before, spec, self)
            now = spec.satisfied(self)
            if was and now:
                continue
            if not was and now:
                callback(ACTIVATING, spec)
            elif was and not now:
                callback(DEACTIVATING, spec)


def _satisfied_in(bindings: dict, spec: CoeffectSpec, store: CoeffectStore) -> bool:
    for key in spec.keys:
        rid = store._realm_id(key)
        if rid not in bindings or key not in bindings[rid]:
            if store.parent is not None and store.parent.has(key):
                continue
            return False
    return True


def attach_coeffects(context, store: Optional[CoeffectStore] = None) -> CoeffectStore:
    """Attach a coeffect store to a Context (the unified context)."""
    if store is None:
        store = CoeffectStore()
    context._coeffects = store
    return store
