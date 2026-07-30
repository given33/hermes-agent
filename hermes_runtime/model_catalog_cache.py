"""Leaf-level helpers for reading the cached model catalog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def validate_manifest(data: Any, *, supported_schema_version: int = 1) -> bool:
    """Return whether ``data`` has a supported model-catalog shape."""
    if not isinstance(data, dict):
        return False
    version = data.get("version")
    if (
        not isinstance(version, int)
        or version < 1
        or version > supported_schema_version
    ):
        return False
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return False
    for provider, block in providers.items():
        if not isinstance(provider, str) or not isinstance(block, dict):
            return False
        models = block.get("models")
        if not isinstance(models, list):
            return False
        for model in models:
            if (
                not isinstance(model, dict)
                or not isinstance(model.get("id"), str)
                or not model["id"].strip()
            ):
                return False
    return True


def read_disk_cache(
    path: Path, *, supported_schema_version: int = 1
) -> tuple[dict[str, Any] | None, float]:
    """Return a validated catalog and its mtime, or ``(None, 0.0)``."""
    try:
        mtime = path.stat().st_mtime
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None, 0.0
    if not validate_manifest(
        data, supported_schema_version=supported_schema_version
    ):
        return None, 0.0
    return data, mtime


def default_model_from_catalog(
    catalog: dict[str, Any] | None, provider: str
) -> str | None:
    """Return the provider model labeled as the silent default."""
    if not isinstance(catalog, dict):
        return None
    providers = catalog.get("providers")
    if not isinstance(providers, dict):
        return None
    block = providers.get(provider)
    if not isinstance(block, dict):
        return None
    models = block.get("models")
    if not isinstance(models, list):
        return None
    for model in models:
        if isinstance(model, dict) and model.get("default"):
            model_id = str(model.get("id") or "").strip()
            if model_id:
                return model_id
    return None


def get_default_model_from_disk_cache(provider: str) -> str | None:
    """Read the cached catalog without importing the CLI or using the network."""
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "cache" / "model_catalog.json"
    catalog, _mtime = read_disk_cache(path)
    return default_model_from_catalog(catalog, provider)
