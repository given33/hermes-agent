"""Idempotent cleanup for data owned by the dashboard's single account."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli.config import atomic_config_write
from hermes_cli.profiles import list_profiles
from utils import fast_safe_load


_MODEL_CONFIG_SECTIONS = ("model", "fallback_model", "auxiliary")


class AccountOperationalCleanupPending(RuntimeError):
    """Account-owned runtime work is still draining durable cancellation."""


def purge_account_owned_cloud_data(owner_id: str) -> dict[str, Any]:
    """Purge collaboration content and model configuration for one owner.

    The iOS intelligence tombstone drives retries, so every operation here is
    deliberately idempotent. Importing the collaboration plugin lazily avoids
    coupling dashboard startup to an optional plugin during non-iOS commands.
    """

    normalized = str(owner_id or "").strip()
    if not normalized:
        raise ValueError("owner_id is required")

    from plugins.collaboration.dashboard.plugin_api import (  # noqa: PLC0415
        delete_owner_account_data,
    )

    return {
        "collaboration": delete_owner_account_data(normalized),
        "models": purge_owner_model_configuration(normalized),
        "operational": purge_owner_operational_state(normalized),
    }


def purge_owner_operational_state(owner_id: str) -> dict[str, Any]:
    """Remove account-scoped approvals, session branches, and workflows."""

    normalized = str(owner_id or "").strip()
    if not normalized:
        raise ValueError("owner_id is required")

    from hermes_cli.account_session_facade import AccountSessionFacade
    from hermes_cli.account_write_approvals import AccountWriteApprovalStore
    from plugins.workflows.store import WorkflowStore

    profile_roots: list[tuple[Path, str]] = []
    visited: set[Path] = set()
    for profile in list_profiles():
        root = Path(profile.path).resolve()
        if root in visited:
            continue
        visited.add(root)
        profile_roots.append((root, str(profile.name or "default")))

    active_root = Path(get_hermes_home()).resolve()
    if active_root not in visited:
        profile_roots.append((active_root, "default"))

    approvals = {"rows": 0, "migrations": 0}
    session_branches = {"branch_sessions": 0, "fork_records": 0, "bindings": 0}
    workflows = {"definitions": 0, "runs": 0}
    for root, profile_name in profile_roots:
        approval_result = AccountWriteApprovalStore(
            root / "write-approvals.db"
        ).delete_owner(normalized)
        for key, value in approval_result.items():
            approvals[key] = approvals.get(key, 0) + int(value)

        branch_result = AccountSessionFacade(root, profile_name).delete_owner(normalized)
        for key, value in branch_result.items():
            session_branches[key] = session_branches.get(key, 0) + int(value)

        workflow_result = WorkflowStore(root / "workflows.db").delete_account(normalized)
        for key, value in workflow_result.items():
            workflows[key] = workflows.get(key, 0) + int(value)
    pending_cancellations = int(workflows.get("pending_cancellations", 0))
    if pending_cancellations:
        raise AccountOperationalCleanupPending(
            f"{pending_cancellations} workflow cancellation(s) are still pending"
        )
    return {
        "write_approvals": approvals,
        "session_facade": session_branches,
        "workflows": workflows,
    }


def purge_owner_model_configuration(owner_id: str) -> dict[str, int]:
    """Remove account-owned model assignments and inline credentials.

    Hermes Profiles are agent configurations under the same single owner, so
    account deletion must visit every profile instead of only the active one.
    Server integration credentials outside model configuration remain intact.
    """

    if not str(owner_id or "").strip():
        raise ValueError("owner_id is required")

    roots = {Path(get_hermes_home()).resolve()}
    roots.update(profile.path.resolve() for profile in list_profiles())
    profiles_changed = 0
    sections_removed = 0
    credentials_removed = 0

    for root in sorted(roots, key=str):
        config_path = root / "config.yaml"
        if not config_path.exists():
            continue
        with config_path.open(encoding="utf-8") as handle:
            config = fast_safe_load(handle) or {}
        if not isinstance(config, dict):
            raise ValueError(f"Profile config must be an object: {config_path}")

        changed = False
        for section in _MODEL_CONFIG_SECTIONS:
            value = config.pop(section, None)
            if value is None:
                continue
            sections_removed += 1
            credentials_removed += _count_credentials(value)
            changed = True
        if not changed:
            continue
        atomic_config_write(
            config_path,
            config,
            sort_keys=False,
            default_flow_style=False,
        )
        profiles_changed += 1

    return {
        "profiles_changed": profiles_changed,
        "sections_removed": sections_removed,
        "credentials_removed": credentials_removed,
    }


def _count_credentials(value: Any) -> int:
    if isinstance(value, dict):
        return sum(
            (1 if key in {"api", "api_key", "key", "secret", "token"} and item else 0)
            + _count_credentials(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return sum(_count_credentials(item) for item in value)
    return 0
