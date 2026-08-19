"""Idempotent cleanup for data owned by the dashboard's single account."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_runtime.config import atomic_config_write
from hermes_cli.profiles import list_profiles
from utils import fast_safe_load


_MODEL_CONFIG_SECTIONS = ("model", "fallback_model", "auxiliary")


def _account_cleanup_plugins():
    from plugins import account_cleanup_backend

    return account_cleanup_backend


class AccountOperationalCleanupPending(RuntimeError):
    """Account-owned runtime work is still draining durable cancellation."""


def begin_account_owned_cloud_deletion(
    owner_id: str,
    *,
    account_generation: str,
) -> dict[str, Any]:
    """Fence collaboration writes before mobile sessions are revoked."""

    normalized = str(owner_id or "").strip()
    generation = str(account_generation or "").strip()
    if not normalized or not generation:
        raise ValueError("owner_id and account_generation are required")
    return _account_cleanup_plugins().plugin_api.begin_owner_account_deletion(
        normalized,
        account_generation=generation,
    )


def purge_account_owned_cloud_data(
    owner_id: str,
    *,
    account_generation: str = "",
) -> dict[str, Any]:
    """Purge collaboration content and model configuration for one owner.

    The iOS intelligence tombstone drives retries, so every operation here is
    deliberately idempotent. Importing the collaboration plugin lazily avoids
    coupling dashboard startup to an optional plugin during non-iOS commands.
    """

    normalized = str(owner_id or "").strip()
    if not normalized:
        raise ValueError("owner_id is required")
    generation = str(account_generation or "").strip()
    if not generation:
        from hermes_cli.dashboard_auth.mobile_device_store import MobileDeviceStore

        generation = MobileDeviceStore().account_generation(normalized, create=False)
    if not generation:
        raise RuntimeError("active account generation is unavailable for deletion")

    return {
        "collaboration": _account_cleanup_plugins().plugin_api.delete_owner_account_data(
            normalized,
            account_generation=generation,
        ),
        "models": purge_owner_model_configuration(
            normalized,
            account_generation=generation,
        ),
        "operational": purge_owner_operational_state(
            normalized,
            account_generation=generation,
        ),
    }


def _cleanup_generation_is_current(owner_id: str, account_generation: str) -> bool:
    generation = str(account_generation or "").strip()
    if not generation:
        return True
    from hermes_cli.dashboard_auth.mobile_device_store import MobileDeviceStore

    active = MobileDeviceStore().account_generation(owner_id, create=False)
    if not active:
        # Generation record missing (store rebuild, migration failure):
        # fail CLOSED. Returning True here let an old-era tombstone replay
        # wipe a re-registered account's data because "no record = match
        # anything". The delete-recreate-same-id scenario is exactly when
        # the record is most likely to be inconsistent.
        logger.warning(
            "account cleanup generation fence: no active generation record "
            "for owner=%s; refusing to proceed (fail-closed)",
            owner_id,
        )
        return False
    return active == generation


def purge_owner_operational_state(
    owner_id: str,
    *,
    account_generation: str = "",
) -> dict[str, Any]:
    """Remove account-scoped approvals, session branches, and workflows."""

    normalized = str(owner_id or "").strip()
    if not normalized:
        raise ValueError("owner_id is required")
    generation = str(account_generation or "").strip()
    if not _cleanup_generation_is_current(normalized, generation):
        return {
            "skipped_stale_generation": True,
            "account_generation": generation,
        }

    from hermes_cli.account_session_facade import AccountSessionFacade
    from hermes_cli.account_write_approvals import AccountWriteApprovalStore
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
        approval_store = AccountWriteApprovalStore(root / "write-approvals.db")
        approval_result = approval_store.delete_owner(
            normalized,
            account_generation=generation,
        )
        for key, value in approval_result.items():
            approvals[key] = approvals.get(key, 0) + int(value)
        # Legacy pending/*.json carry no owner, but the migration sidecars
        # prove which owner consumed them: delete exactly those files (and
        # their sidecars) so a re-registered same-id account cannot have the
        # old era's approvals re-imported ("revived") on first read.
        try:
            legacy_files = approval_store.legacy_json_sidecars_for_owner(normalized)
        except Exception:
            legacy_files = []
        for legacy_file in legacy_files:
            for victim in (
                legacy_file,
                legacy_file.with_name(legacy_file.name + ".migrated.json"),
            ):
                try:
                    victim.unlink()
                    approvals["legacy_files"] = approvals.get("legacy_files", 0) + 1
                except OSError:
                    pass

        branch_result = AccountSessionFacade(root, profile_name).delete_owner(
            normalized,
            account_generation=generation,
        )
        for key, value in branch_result.items():
            session_branches[key] = session_branches.get(key, 0) + int(value)

        workflow_result = _account_cleanup_plugins().WorkflowStore(
            root / "workflows.db"
        ).delete_account_all_generations(normalized)
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


def purge_owner_model_configuration(
    owner_id: str,
    *,
    account_generation: str = "",
) -> dict[str, int | bool | str]:
    """Remove account-owned model assignments and inline credentials.

    Hermes Profiles are agent configurations under the same single owner, so
    account deletion must visit every profile instead of only the active one.
    Server integration credentials outside model configuration remain intact.
    """

    if not str(owner_id or "").strip():
        raise ValueError("owner_id is required")
    normalized_owner = str(owner_id).strip()
    generation = str(account_generation or "").strip()
    if not _cleanup_generation_is_current(normalized_owner, generation):
        return {
            "profiles_changed": 0,
            "sections_removed": 0,
            "credentials_removed": 0,
            "skipped_stale_generation": True,
            "account_generation": generation,
        }

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
