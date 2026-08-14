"""Bounded long-task controls for hosted and connector-backed execution."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any


@dataclass(frozen=True)
class LongTaskBudget:
    deadline_at: float
    token_budget: int | None = None
    checkpoint_interval_seconds: float = 30.0

    @classmethod
    def from_now(
        cls,
        seconds: float,
        *,
        token_budget: int | None = None,
        checkpoint_interval_seconds: float = 30.0,
    ) -> "LongTaskBudget":
        if seconds <= 0:
            raise ValueError("long task deadline must be positive")
        return cls(time.monotonic() + seconds, token_budget, checkpoint_interval_seconds)


class LongTaskController:
    def __init__(self, budget: LongTaskBudget) -> None:
        self.budget = budget
        self.cancel_requested = False
        self.terminal_status = "running"
        self.last_checkpoint_at = float("-inf")
        self.checkpoint_count = 0
        self.tokens_used = 0

    def request_cancel(self, reason: str = "user") -> None:
        if self.terminal_status in {"completed", "failed", "cancelled"}:
            return
        self.cancel_requested = True
        self.cancel_reason = str(reason or "user")[:256]

    def should_stop(self, *, tokens: int = 0, now: float | None = None) -> bool:
        self.tokens_used += max(0, int(tokens))
        current = time.monotonic() if now is None else now
        return bool(
            self.cancel_requested
            or current >= self.budget.deadline_at
            or (
                self.budget.token_budget is not None
                and self.tokens_used >= self.budget.token_budget
            )
        )

    def checkpoint(self, state: Any, *, now: float | None = None) -> dict[str, Any]:
        current = time.monotonic() if now is None else now
        if self.terminal_status in {"completed", "failed", "cancelled"}:
            return {"accepted": False, "reason": "already_terminal"}
        if current - self.last_checkpoint_at < self.budget.checkpoint_interval_seconds:
            return {"accepted": False, "reason": "interval", "checkpoint_count": self.checkpoint_count}
        self.last_checkpoint_at = current
        self.checkpoint_count += 1
        return {
            "accepted": True,
            "checkpoint_count": self.checkpoint_count,
            "state": state,
            "tokens_used": self.tokens_used,
            "cancel_requested": self.cancel_requested,
        }

    def settle(self, status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized not in {"completed", "failed", "cancelled"}:
            raise ValueError("long task terminal status is invalid")
        self.terminal_status = normalized
        return normalized


class BoundedEventBuffer:
    """Bounded producer/consumer buffer with explicit drop accounting."""

    def __init__(self, capacity: int = 128) -> None:
        if capacity <= 0:
            raise ValueError("event buffer capacity must be positive")
        self.capacity = int(capacity)
        self._items: list[Any] = []
        self.dropped = 0

    def put(self, item: Any) -> bool:
        if len(self._items) >= self.capacity:
            self.dropped += 1
            return False
        self._items.append(item)
        return True

    def get(self) -> Any | None:
        if not self._items:
            return None
        return self._items.pop(0)

    def __len__(self) -> int:
        return len(self._items)


def recover_after_process_exit(
    checkpoint: dict[str, Any] | None,
    *,
    exit_code: int,
) -> dict[str, Any]:
    """Convert a killed worker checkpoint into a resumable terminal boundary."""

    if not isinstance(checkpoint, dict):
        return {"status": "failed", "recovery": "missing_checkpoint", "exit_code": int(exit_code)}
    if int(exit_code) == 0:
        return {**checkpoint, "status": "recovering", "recovery": "clean_exit"}
    return {
        **checkpoint,
        "status": "recovering",
        "recovery": "process_exit",
        "exit_code": int(exit_code),
        "resume_required": True,
    }
