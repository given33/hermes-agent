"""Update classification and transaction phases for safe runtime changes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
from typing import Any, Callable


class UpdateMode(str, Enum):
    EDGE_TRANSACTION = "edge_transaction"
    DRAIN_RESTART = "drain_restart"
    BLUE_GREEN = "blue_green"


@dataclass(frozen=True)
class UpdateClassification:
    path: str
    mode: UpdateMode
    owner: str
    reason: str


def classify_update(path: str, *, requested_mode: str = "") -> UpdateClassification:
    normalized = str(path or "").replace("\\", "/").lower()
    requested = str(requested_mode or "").strip().lower()
    if any(part in normalized for part in ("agent_loop", "run_agent.py", "core_loop", "hermes_loop")):
        mode = UpdateMode.BLUE_GREEN if requested == "blue_green" else UpdateMode.DRAIN_RESTART
        return UpdateClassification(normalized, mode, "core-agent-loop", "core loop state is not in-process replaceable")
    if any(part in normalized for part in ("connector", "plugin", "provider", "mcp", "adapter")):
        return UpdateClassification(normalized, UpdateMode.EDGE_TRANSACTION, "edge-provider", "edge component has an isolated lifecycle")
    return UpdateClassification(normalized, UpdateMode.DRAIN_RESTART, "unknown-runtime-component", "unknown component defaults to restart")


@dataclass(frozen=True)
class UpdateResult:
    mode: UpdateMode
    committed: bool
    rolled_back: bool
    phases: tuple[str, ...]
    reason: str = ""


class TransactionalUpdate:
    """Preflight, isolate, health-check, shift, drain, and commit one edge update."""

    def __init__(self, classification: UpdateClassification) -> None:
        self.classification = classification
        self.phases: list[str] = []

    def apply(
        self,
        *,
        snapshot: Callable[[], Any],
        isolated_load: Callable[[], Any],
        health_check: Callable[[Any], bool],
        traffic_shift: Callable[[Any], Any],
        drain: Callable[[Any], bool],
        commit: Callable[[Any], Any],
        rollback: Callable[[Any], Any],
    ) -> UpdateResult:
        if self.classification.mode is not UpdateMode.EDGE_TRANSACTION:
            return UpdateResult(
                mode=self.classification.mode,
                committed=False,
                rolled_back=False,
                phases=("rejected",),
                reason="core and unknown components require drain/restart or blue/green",
            )
        previous = None
        candidate = None
        try:
            self.phases.append("classify")
            self.phases.append("snapshot")
            previous = snapshot()
            self.phases.append("isolated_load")
            candidate = isolated_load()
            self.phases.append("health")
            if not health_check(candidate):
                raise RuntimeError("candidate health check failed")
            self.phases.append("traffic_shift")
            traffic_shift(candidate)
            self.phases.append("drain")
            if not drain(previous):
                raise RuntimeError("old component did not drain")
            self.phases.append("commit")
            commit(candidate)
            return UpdateResult(self.classification.mode, True, False, tuple(self.phases))
        except Exception as exc:
            self.phases.append("rollback")
            try:
                rollback(previous)
            except Exception as rollback_error:
                return UpdateResult(
                    self.classification.mode,
                    False,
                    False,
                    tuple(self.phases),
                    f"{exc}; rollback failed: {rollback_error}",
                )
            return UpdateResult(
                self.classification.mode,
                False,
                True,
                tuple(self.phases),
                str(exc),
            )
