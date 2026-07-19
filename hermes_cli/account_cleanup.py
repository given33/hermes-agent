"""Idempotent cleanup for data owned by the dashboard's single account."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli.config import atomic_config_write
from hermes_cli.profiles import list_profiles
from utils import fast_safe_load


_MODEL_CONFIG_SECTIONS = ("model", "fallback_model", "auxiliary")


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
