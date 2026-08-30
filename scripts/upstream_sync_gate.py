"""Fail-closed checks for the official-upstream merge workflow.

This runs after an upstream merge and before the automation is allowed to
push ``main``.  It intentionally checks product-owned seams that an upstream
change could silently overwrite (the three worker topology, mobile commands,
and retired reviewer lane) while leaving official Hermes implementation free
to evolve.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]

# When this file is executed directly (the way the GitHub workflow invokes
# it), Python puts ``scripts/`` on ``sys.path`` rather than the repository
# root.  The gate imports the product packages below, so make the execution
# mode deterministic instead of relying on callers to set PYTHONPATH.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_RETIRED_HOSTED_EXECUTION_SYMBOLS = frozenset(
    {
        "_append_supervisor_companion_intervention",
        "_hosted_companion_loop",
        "_hosted_companion_notes_text",
        "_hosted_companion_parse",
        "_hosted_companion_protocol_prompt",
        "_hosted_companion_role_snapshot",
        "_hosted_reviewer_control",
        "_hosted_reviewer_protocol_prompt",
        "_hosted_reviewer_verdict",
        "_hosted_supervisor_control",
        "_hosted_supervisor_protocol_prompt",
        "_hosted_supervisor_verdict",
        "_persist_hosted_reviewer_display",
        "_persist_hosted_supervisor_check",
        "_require_supervisor_pass",
        "_review_requests_rework",
        "_run_hosted_supervisor_check",
        "_start_hosted_companion",
        "_stop_hosted_companion",
    }
)
_RETIRED_HOSTED_STAGES = frozenset({"reviewer", "supervisor", "reporter"})
_LEGACY_RESULT_FIELDS = frozenset({"reporter_result", "reporter_status"})


def _literal_string(node: ast.AST | None) -> str:
    return str(node.value) if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _assert_retired_hosted_runtime_absent(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _RETIRED_HOSTED_EXECUTION_SYMBOLS:
                violations.append(f"retired execution symbol {node.name}")
            argument_names = {
                argument.arg
                for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            }
            if "remote_supervisor" in argument_names:
                violations.append(f"remote_supervisor argument at line {node.lineno}")
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "remote_supervisor":
                    violations.append(f"remote_supervisor keyword at line {node.lineno}")
                if keyword.arg == "role_stage":
                    stage = _literal_string(keyword.value).split(":", 1)[0].split(".", 1)[0]
                    if stage in _RETIRED_HOSTED_STAGES:
                        violations.append(f"retired role_stage {stage} at line {node.lineno}")
        if isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values):
                key = _literal_string(key_node)
                if key in _LEGACY_RESULT_FIELDS:
                    violations.append(f"legacy result write {key} at line {node.lineno}")
                if key == "role_stage":
                    stage = _literal_string(value_node).split(":", 1)[0].split(".", 1)[0]
                    if stage in _RETIRED_HOSTED_STAGES:
                        violations.append(f"retired role_stage {stage} at line {node.lineno}")
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript):
                    key = _literal_string(target.slice)
                    if key in _LEGACY_RESULT_FIELDS:
                        violations.append(f"legacy result write {key} at line {node.lineno}")
    if violations:
        raise SystemExit("upstream sync gate: " + "; ".join(sorted(set(violations))))


def main() -> int:
    required = (
        ROOT / ".github/workflows/upstream-sync.yml",
        ROOT / ".github/workflows/deploy-three-endpoints.yml",
        ROOT / "deploy/hk/install-hk-worker.sh",
        ROOT / "deploy/hk/hk-cloud-connector.service",
        ROOT / "deploy/hk/profile/config.yaml.example",
        ROOT / "plugins/collaboration/dashboard/hosted_tui_runtime.py",
    )
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("upstream sync gate: missing product assets: " + ", ".join(missing))

    _assert_retired_hosted_runtime_absent(
        ROOT / "plugins/collaboration/dashboard/plugin_api.py"
    )

    from hermes_cli.commands import resolve_command
    from hermes_cli.kanban_db import review_dispatch_enabled
    from plugins.collaboration.dashboard.plugin_api import (
        _REMOTE_RUN_PROFILES,
        _WORKER_TARGET_PROFILES,
        collaboration_role,
    )

    for name in ("bg", "btw", "busy"):
        if resolve_command(name) is None:
            raise SystemExit(f"upstream sync gate: official /{name} command disappeared")
    if resolve_command("review") is not None:
        raise SystemExit("upstream sync gate: retired /review command was reintroduced")
    if review_dispatch_enabled():
        raise SystemExit("upstream sync gate: reviewer dispatch is enabled")
    expected_workers = frozenset(_WORKER_TARGET_PROFILES.values())
    if frozenset(_REMOTE_RUN_PROFILES) != expected_workers:
        raise SystemExit("upstream sync gate: remote run profiles drifted from DBB3/PC/HK workers")
    if any(collaboration_role(role) != "worker" for role in expected_workers):
        raise SystemExit("upstream sync gate: worker role classification drifted")
    if collaboration_role("reviewer") != "retired" or collaboration_role("supervisor") != "retired":
        raise SystemExit("upstream sync gate: retired roles became executable")

    print("upstream sync gate: product-owned invariants passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
