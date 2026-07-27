"""Import-safe helpers for resolving the active Hermes profile identity."""

from __future__ import annotations

import re

from hermes_constants import get_default_hermes_root, get_hermes_home


_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def get_active_profile_name() -> str:
    """Infer the active profile name without importing CLI lifecycle code.

    ``default`` identifies the Hermes root itself, a valid single directory
    below ``profiles/`` is returned by name, and custom homes outside that
    layout are reported as ``custom``.
    """

    resolved = get_hermes_home().resolve()
    default_resolved = get_default_hermes_root().resolve()
    if resolved == default_resolved:
        return "default"

    try:
        relative = resolved.relative_to((default_resolved / "profiles").resolve())
    except ValueError:
        return "custom"

    parts = relative.parts
    if len(parts) == 1 and _PROFILE_ID_RE.fullmatch(parts[0]):
        return parts[0]
    return "custom"


__all__ = ["get_active_profile_name"]
