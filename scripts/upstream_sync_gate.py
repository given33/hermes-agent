"""Fail-closed checks for the official-upstream merge workflow.

This runs after an upstream merge and before the automation is allowed to
push ``main``.  It intentionally checks product-owned seams that an upstream
change could silently overwrite (the three worker topology, mobile commands,
and retired reviewer lane) while leaving official Hermes implementation free
to evolve.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
