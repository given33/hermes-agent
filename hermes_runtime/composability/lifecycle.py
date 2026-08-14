"""Strict lifecycle transition rules shared by hosted runtime producers."""

from __future__ import annotations

from typing import Final


LIFECYCLE_STATES: Final[frozenset[str]] = frozenset(
    {
        "unknown", "declared", "waiting", "activating", "active",
        "quiescing", "leaving", "unloading", "recovering", "failed",
        "completed",
    }
)

LIFECYCLE_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "unknown": frozenset(LIFECYCLE_STATES),
    "declared": frozenset({"declared", "waiting", "activating", "failed", "leaving"}),
    "waiting": frozenset({"waiting", "activating", "failed", "leaving"}),
    "activating": frozenset({"activating", "active", "waiting", "failed", "leaving"}),
    "active": frozenset({"active", "quiescing", "leaving", "recovering", "completed", "failed"}),
    "quiescing": frozenset({"quiescing", "active", "leaving", "unloading", "failed"}),
    "leaving": frozenset({"leaving", "unloading", "completed", "failed"}),
    "unloading": frozenset({"unloading", "completed", "failed"}),
    "recovering": frozenset({"recovering", "activating", "active", "failed", "completed"}),
    "failed": frozenset({"failed"}),
    "completed": frozenset({"completed"}),
}


def normalize_lifecycle_state(value: object) -> str:
    state = str(value or "").strip().lower()
    if state not in LIFECYCLE_STATES:
        raise ValueError(f"unsupported lifecycle state: {state!r}")
    return state


def lifecycle_transition_allowed(previous: object, current: object) -> bool:
    before = normalize_lifecycle_state(previous)
    after = normalize_lifecycle_state(current)
    return after in LIFECYCLE_TRANSITIONS[before]


def assert_lifecycle_transition(previous: object, current: object) -> None:
    before = normalize_lifecycle_state(previous)
    after = normalize_lifecycle_state(current)
    if not lifecycle_transition_allowed(before, after):
        raise ValueError(f"illegal lifecycle transition: {before} -> {after}")
