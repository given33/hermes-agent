"""Validation and canonicalization for versioned workflow definitions."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from copy import deepcopy
from typing import Any

from plugins.workflows.models import WorkflowValidationError

MAX_NODES = 500
MAX_EDGES = 5_000
MAX_LOOP_ITERATIONS = 100
NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
JOIN_POLICIES = {"all", "any", "first"}
CONDITION_OPERATORS = {"eq", "ne", "in", "not_in", "truthy", "falsy", "exists"}
WORKSPACE_KINDS = {"scratch", "worktree", "dir"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _condition_errors(condition: Any, prefix: str) -> list[str]:
    if condition is None:
        return []
    if not isinstance(condition, dict):
        return [f"{prefix}.condition must be a structured object"]
    errors: list[str] = []
    unknown = sorted(set(condition) - {"path", "op", "value"})
    if unknown:
        errors.append(f"{prefix}.condition has unknown fields: {', '.join(unknown)}")
    path = condition.get("path", "")
    if not isinstance(path, str) or len(path) > 256 or ".." in path:
        errors.append(f"{prefix}.condition.path must be a safe path up to 256 characters")
    op = condition.get("op")
    if op not in CONDITION_OPERATORS:
        errors.append(f"{prefix}.condition.op must be one of {sorted(CONDITION_OPERATORS)}")
    if op in {"eq", "ne", "in", "not_in"} and "value" not in condition:
        errors.append(f"{prefix}.condition.value is required for {op}")
    if op in {"in", "not_in"} and "value" in condition and not isinstance(condition["value"], list):
        errors.append(f"{prefix}.condition.value must be a list for {op}")
    return errors


def validate_definition(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a stable snapshot, rejecting ambiguous or unbounded graphs."""

    if not isinstance(spec, dict):
        raise WorkflowValidationError(["definition must be an object"])
    errors: list[str] = []
    raw_nodes = spec.get("nodes")
    if isinstance(raw_nodes, dict):
        nodes = []
        for node_id, value in raw_nodes.items():
            node = deepcopy(value) if isinstance(value, dict) else {"config": value}
            node.setdefault("id", node_id)
            nodes.append(node)
    elif isinstance(raw_nodes, list):
        nodes = deepcopy(raw_nodes)
    else:
        nodes = []
        errors.append("nodes must be a list or object")
    if not nodes:
        errors.append("at least one node is required")
    if len(nodes) > MAX_NODES:
        errors.append(f"node count exceeds {MAX_NODES}")

    normalized_nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for index, raw in enumerate(nodes):
        if not isinstance(raw, dict):
            errors.append(f"node[{index}] must be an object")
            continue
        node = deepcopy(raw)
        node_id = node.get("id")
        if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
            errors.append(f"node[{index}].id is invalid")
            continue
        if node_id in node_ids:
            errors.append(f"duplicate node id: {node_id}")
            continue
        node_ids.add(node_id)
        join_policy = node.get("join_policy", "all")
        if join_policy not in JOIN_POLICIES:
            errors.append(f"node {node_id} join_policy must be one of {sorted(JOIN_POLICIES)}")
        node["join_policy"] = join_policy
        node.setdefault("type", "agent")
        if not isinstance(node["type"], str) or not node["type"].strip():
            errors.append(f"node {node_id} type is required")
        config = node.get("config")
        if isinstance(config, dict):
            workspace_kind = config.get("workspace_kind")
            if workspace_kind is not None and workspace_kind not in WORKSPACE_KINDS:
                errors.append(
                    f"node {node_id} config.workspace_kind must be one of "
                    f"{sorted(WORKSPACE_KINDS)}"
                )
            workspace_path = config.get("workspace_path")
            if workspace_path is not None and (
                not isinstance(workspace_path, str)
                or not workspace_path.strip()
                or len(workspace_path) > 4096
            ):
                errors.append(
                    f"node {node_id} config.workspace_path must be a non-empty string "
                    "up to 4096 characters"
                )
        normalized_nodes.append(node)

    raw_edges = spec.get("edges", [])
    if not isinstance(raw_edges, list):
        raw_edges = []
        errors.append("edges must be a list")
    if len(raw_edges) > MAX_EDGES:
        errors.append(f"edge count exceeds {MAX_EDGES}")
    normalized_edges: list[dict[str, Any]] = []
    adjacency: dict[str, list[str]] = defaultdict(list)
    indegree = {node_id: 0 for node_id in node_ids}
    loop_edges: list[tuple[str, str]] = []
    seen_edges: set[tuple[str, str, bool, str]] = set()
    for index, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            errors.append(f"edge[{index}] must be an object")
            continue
        edge = deepcopy(raw)
        source = edge.pop("from", edge.pop("source", None))
        target = edge.pop("to", edge.pop("target", None))
        edge["source"], edge["target"] = source, target
        edge.setdefault("loop", False)
        prefix = f"edge[{index}]"
        if source not in node_ids:
            errors.append(f"{prefix}.source references a missing node: {source}")
        if target not in node_ids:
            errors.append(f"{prefix}.target references a missing node: {target}")
        is_loop = edge.get("loop")
        if not isinstance(is_loop, bool):
            errors.append(f"{prefix}.loop must be boolean")
            is_loop = bool(is_loop)
            edge["loop"] = is_loop
        errors.extend(_condition_errors(edge.get("condition"), prefix))
        if is_loop:
            limit = edge.get("max_iterations")
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LOOP_ITERATIONS:
                errors.append(f"{prefix}.max_iterations must be between 1 and {MAX_LOOP_ITERATIONS}")
            loop_edges.append((str(source), str(target)))
        elif "max_iterations" in edge:
            errors.append(f"{prefix}.max_iterations is only valid on loop edges")
        key = (str(source), str(target), is_loop, canonical_json(edge.get("condition")))
        if key in seen_edges:
            errors.append(f"duplicate edge: {source} -> {target}")
        seen_edges.add(key)
        if source in node_ids and target in node_ids and not is_loop:
            adjacency[str(source)].append(str(target))
            indegree[str(target)] += 1
        normalized_edges.append(edge)

    queue = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    visited = 0
    while queue:
        source = queue.popleft()
        visited += 1
        for target in adjacency[source]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if node_ids and visited != len(node_ids):
        errors.append("ordinary edges contain a cycle; mark bounded back-edges as loop=true")

    def reaches(start: str, goal: str) -> bool:
        pending, seen = [start], set()
        while pending:
            current = pending.pop()
            if current == goal:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(adjacency[current])
        return False

    for source, target in loop_edges:
        if source in node_ids and target in node_ids and not reaches(target, source):
            errors.append(f"loop edge {source} -> {target} must point back to an ordinary ancestor")
    try:
        canonical_json(spec)
    except (TypeError, ValueError) as exc:
        errors.append(f"definition is not JSON serializable: {exc}")
    if errors:
        raise WorkflowValidationError(errors)
    result = deepcopy(spec)
    result["schema_version"] = int(spec.get("schema_version", 1))
    result["nodes"], result["edges"] = normalized_nodes, normalized_edges
    return result


def condition_matches(condition: dict[str, Any] | None, output: Any) -> bool:
    if condition is None:
        return True
    value = output
    for segment in condition.get("path", "").split(".") if condition.get("path") else []:
        value = value.get(segment) if isinstance(value, dict) else None
    op, expected = condition["op"], condition.get("value")
    operations = {
        "eq": lambda: value == expected,
        "ne": lambda: value != expected,
        "in": lambda: value in expected,
        "not_in": lambda: value not in expected,
        "truthy": lambda: bool(value),
        "falsy": lambda: not bool(value),
        "exists": lambda: value is not None,
    }
    return bool(operations[op]())
