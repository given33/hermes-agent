"""Validated turn plans for deterministic, conflict-aware orchestration."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


class TurnPlanError(ValueError):
    """Raised when a plan violates dependency or execution invariants."""


@dataclass(frozen=True)
class TurnPlanNode:
    node_id: str
    role: str
    depends_on: tuple[str, ...] = ()
    input_artifact_refs: tuple[str, ...] = ()
    output_contract: str = ""
    acceptance_contract: str = ""
    conflict_keys: tuple[str, ...] = ()
    risk_class: str = "read_only"
    parallel_group: str = ""
    token_budget: int | None = None
    time_budget_seconds: float | None = None

    def __post_init__(self) -> None:
        if not str(self.node_id).strip():
            raise ValueError("node_id is required")
        if not str(self.role).strip():
            raise ValueError("role is required")
        if self.token_budget is not None and self.token_budget <= 0:
            raise ValueError("token_budget must be positive")
        if self.time_budget_seconds is not None and self.time_budget_seconds <= 0:
            raise ValueError("time_budget_seconds must be positive")


@dataclass(frozen=True)
class TurnPlan:
    plan_id: str
    revision: int
    nodes: tuple[TurnPlanNode, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.plan_id).strip():
            raise ValueError("plan_id is required")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        self.validate()

    @property
    def node_map(self) -> dict[str, TurnPlanNode]:
        return {node.node_id: node for node in self.nodes}

    def validate(self) -> None:
        node_map: dict[str, TurnPlanNode] = {}
        for node in self.nodes:
            if node.node_id in node_map:
                raise TurnPlanError(f"duplicate node_id: {node.node_id}")
            node_map[node.node_id] = node
        for node in self.nodes:
            missing = [dep for dep in node.depends_on if dep not in node_map]
            if missing:
                raise TurnPlanError(
                    f"node {node.node_id!r} depends on missing node(s): {', '.join(missing)}"
                )
            if node.node_id in node.depends_on:
                raise TurnPlanError(f"node {node.node_id!r} depends on itself")
        self._topological_order(node_map)

    def ready_nodes(self, completed: Iterable[str], running: Iterable[str] = ()) -> tuple[TurnPlanNode, ...]:
        completed_set = set(completed)
        running_set = set(running)
        ready = [
            node
            for node in self.nodes
            if node.node_id not in completed_set
            and node.node_id not in running_set
            and all(dependency in completed_set for dependency in node.depends_on)
        ]
        return tuple(sorted(ready, key=lambda node: node.node_id))

    def can_run_together(self, nodes: Iterable[TurnPlanNode]) -> bool:
        selected = list(nodes)
        if len({node.node_id for node in selected}) != len(selected):
            return False
        for index, left in enumerate(selected):
            left_keys = set(left.conflict_keys)
            for right in selected[index + 1:]:
                if left_keys.intersection(right.conflict_keys):
                    return False
        return True

    def critical_path(self) -> tuple[str, ...]:
        node_map = self.node_map
        order = self._topological_order(node_map)
        best: dict[str, tuple[str, ...]] = {}
        for node_id in order:
            node = node_map[node_id]
            parents = [best[dep] for dep in node.depends_on]
            prefix = max(parents, key=len) if parents else ()
            best[node_id] = prefix + (node_id,)
        return max(best.values(), key=len) if best else ()

    def public_dict(self) -> dict[str, object]:
        return {
            "schema_version": "hermes.turn-plan.v1",
            "plan_id": self.plan_id,
            "revision": self.revision,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "role": node.role,
                    "depends_on": list(node.depends_on),
                    "input_artifact_refs": list(node.input_artifact_refs),
                    "output_contract": node.output_contract,
                    "acceptance_contract": node.acceptance_contract,
                    "conflict_keys": list(node.conflict_keys),
                    "risk_class": node.risk_class,
                    "parallel_group": node.parallel_group,
                    "token_budget": node.token_budget,
                    "time_budget_seconds": node.time_budget_seconds,
                }
                for node in self.nodes
            ],
        }

    @staticmethod
    def _topological_order(node_map: dict[str, TurnPlanNode]) -> tuple[str, ...]:
        remaining = {node_id: set(node.depends_on) for node_id, node in node_map.items()}
        order: list[str] = []
        while remaining:
            available = sorted(node_id for node_id, deps in remaining.items() if not deps)
            if not available:
                cycle = ", ".join(sorted(remaining))
                raise TurnPlanError(f"dependency cycle detected: {cycle}")
            order.extend(available)
            for node_id in available:
                remaining.pop(node_id)
            for deps in remaining.values():
                deps.difference_update(available)
        return tuple(order)
