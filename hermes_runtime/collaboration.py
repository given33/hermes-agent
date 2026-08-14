"""Small durable-shape-independent primitives for AgentTeams collaboration."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


DEPENDENCY_STATES = frozenset({"pending", "ready", "running", "succeeded", "failed", "cancelled"})


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class CollaborationDependency:
    node_id: str
    requires: tuple[str, ...] = ()
    acceptance_contract: str = ""
    conflict_key: str = ""
    budget: Mapping[str, int] = field(default_factory=dict)
    state: str = "pending"

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("dependency node_id is required")
        if self.state not in DEPENDENCY_STATES:
            raise ValueError(f"invalid dependency state: {self.state}")
        if self.node_id in self.requires:
            raise ValueError("dependency cannot require itself")


class DependencyGraph:
    def __init__(self, nodes: Iterable[CollaborationDependency] = ()) -> None:
        node_list = list(nodes)
        node_ids = [node.node_id for node in node_list]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("dependency graph contains duplicate node ids")
        self._nodes = {node.node_id: node for node in node_list}
        self._validate()

    def _validate(self) -> None:
        for node in self._nodes.values():
            missing = set(node.requires) - set(self._nodes)
            if missing:
                raise ValueError(f"dependency references missing node(s): {', '.join(sorted(missing))}")
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("dependency graph contains a cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for required in self._nodes[node_id].requires:
                visit(required)
            visiting.remove(node_id)
            visited.add(node_id)
        for node_id in self._nodes:
            visit(node_id)

    def ready(self, completed: Iterable[str] = ()) -> list[str]:
        completed_set = set(completed)
        completed_set.update(
            node.node_id
            for node in self._nodes.values()
            if node.state == "succeeded"
        )
        return sorted(
            node.node_id for node in self._nodes.values()
            if node.node_id not in completed_set
            and node.state in {"pending", "ready"}
            and set(node.requires) <= completed_set
        )

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "node_id": node.node_id,
                "requires": list(node.requires),
                "acceptance_contract": node.acceptance_contract,
                "conflict_key": node.conflict_key,
                "budget": dict(node.budget),
                "state": node.state,
            }
            for node in sorted(self._nodes.values(), key=lambda item: item.node_id)
        ]


@dataclass(frozen=True)
class MailboxMessage:
    sender_id: str
    recipient_id: str
    body: Mapping[str, Any]
    message_id: str = ""
    idempotency_key: str = ""
    account_generation: str = ""
    created_at_ms: int = 0

    def __post_init__(self) -> None:
        if not self.sender_id or not self.recipient_id:
            raise ValueError("sender_id and recipient_id are required")
        if not self.account_generation:
            raise ValueError("account_generation is required")

    def as_dict(self) -> dict[str, Any]:
        message_id = self.message_id or _id("msg")
        if self.idempotency_key:
            key = self.idempotency_key
        else:
            body_json = json.dumps(
                self.body, ensure_ascii=False, sort_keys=True, default=str
            )
            key = hashlib.sha256(
                f"{self.sender_id}:{self.recipient_id}:{self.account_generation}:{body_json}".encode(
                    "utf-8", errors="replace"
                )
            ).hexdigest()
            # Make a caller-created message stable across repeated append calls
            # even when it did not provide an explicit idempotency key.
            message_id = f"msg_{key[:32]}"
        return {
            "message_id": message_id,
            "idempotency_key": key,
            "sender_id": self.sender_id,
            "recipient_id": self.recipient_id,
            "body": dict(self.body),
            "account_generation": self.account_generation,
            "created_at_ms": self.created_at_ms or int(time.time() * 1000),
            "status": "unread",
        }


def append_mailbox(messages: list[dict[str, Any]], message: MailboxMessage) -> dict[str, Any]:
    record = message.as_dict()
    existing = next((item for item in messages if item.get("idempotency_key") == record["idempotency_key"]), None)
    if isinstance(existing, dict):
        return existing
    messages.append(record)
    return record


def read_mailbox(messages: Iterable[Mapping[str, Any]], recipient_id: str, account_generation: str, *, after_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    passed = not after_id
    for item in messages:
        if not isinstance(item, Mapping) or str(item.get("recipient_id") or "") != recipient_id:
            continue
        if str(item.get("account_generation") or "") != account_generation:
            continue
        if not passed:
            passed = str(item.get("message_id") or "") == after_id
            continue
        result.append(dict(item))
        if len(result) >= max(1, min(int(limit), 500)):
            break
    return result
