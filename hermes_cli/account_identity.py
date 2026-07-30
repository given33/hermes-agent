"""Immutable account-generation storage scopes shared by persistent stores."""

from __future__ import annotations

from typing import Any


ACCOUNT_GENERATION_SEPARATOR = "\x1eacctgen="
LEGACY_ACCOUNT_GENERATION = "legacy"


def normalize_owner_id(value: Any) -> str:
    owner_id = str(value or "").strip().replace("\x00", "")[:512]
    if not owner_id:
        raise ValueError("owner_id is required")
    return owner_id


def generation_scoped_owner_id(
    value: Any,
    account_generation: str | None = None,
) -> str:
    """Map a public owner plus generation to an idempotent storage key."""

    owner_id = normalize_owner_id(value)
    if ACCOUNT_GENERATION_SEPARATOR in owner_id:
        public_id, generation = owner_id.rsplit(ACCOUNT_GENERATION_SEPARATOR, 1)
        if not public_id or not generation:
            raise ValueError("invalid account generation scope")
        return owner_id
    generation = account_generation
    if generation is None:
        from hermes_cli.dashboard_auth.mobile_device_store import MobileDeviceStore

        generation = MobileDeviceStore().account_generation(owner_id, create=False)
    normalized_generation = str(generation or "").strip().replace("\x00", "")[:256]
    if not normalized_generation or normalized_generation == LEGACY_ACCOUNT_GENERATION:
        return owner_id
    return f"{owner_id}{ACCOUNT_GENERATION_SEPARATOR}{normalized_generation}"


def public_owner_id(value: Any) -> str:
    return str(value or "").split(ACCOUNT_GENERATION_SEPARATOR, 1)[0]


def storage_account_generation(value: Any) -> str:
    text = str(value or "")
    if ACCOUNT_GENERATION_SEPARATOR not in text:
        return LEGACY_ACCOUNT_GENERATION
    return text.rsplit(ACCOUNT_GENERATION_SEPARATOR, 1)[1]
