"""Shared workflow domain types.

Workflow state is authoritative here. Kanban remains an execution adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class WorkflowError(RuntimeError):
    pass


class WorkflowNotFound(WorkflowError):
    pass


class WorkflowConflict(WorkflowError):
    pass


class WorkflowValidationError(WorkflowError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class WorkflowSecurityError(WorkflowError):
    pass


class RunState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATES = {"succeeded", "failed", "cancelled"}
TERMINAL_NODE_STATES = {"succeeded", "failed", "skipped", "cancelled"}


@dataclass(frozen=True)
class WorkflowScope:
    account_id: str
    account_generation: int
    profile_id: str

    def validate(self) -> "WorkflowScope":
        account_id = str(self.account_id or "").strip()
        profile_id = str(self.profile_id or "").strip()
        if not account_id or len(account_id) > 512:
            raise ValueError("account_id is required and must be at most 512 characters")
        if int(self.account_generation) < 1:
            raise ValueError("account_generation must be at least 1")
        if not profile_id or len(profile_id) > 128:
            raise ValueError("profile_id is required and must be at most 128 characters")
        return WorkflowScope(account_id, int(self.account_generation), profile_id)


@dataclass(frozen=True)
class SecurityVerdict:
    denied: bool = False
    hardline_blocked: bool = False
    tirith_blocked: bool = False
    write_gate_required: bool = False
    plugin_blocked: bool = False

    @property
    def allows_one_shot(self) -> bool:
        return not any((self.denied, self.hardline_blocked, self.tirith_blocked,
                        self.write_gate_required, self.plugin_blocked))


class DispatchAdapter(Protocol):
    def dispatch(self, intent: dict[str, Any]) -> str:
        """Return the stable task reference, idempotently keyed by ``intent['id']``."""

    def probe(self, external_ref: str) -> tuple[str, Any, str]:
        """Return the external task's state, output, and error."""

    def cancel(self, external_ref: str) -> None:
        """Idempotently cancel the external task."""
