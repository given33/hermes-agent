"""Framework-neutral parsing for scalar Hermes configuration values."""

from __future__ import annotations

from typing import Any


def parse_enabled_flag(value: Any, *, default: bool = True) -> bool:
    """Interpret the bool-like values accepted by Hermes configuration."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default
