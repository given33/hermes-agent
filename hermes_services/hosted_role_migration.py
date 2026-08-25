"""Migration helpers for retired hosted AI roles.

The migration is deliberately structural: it only examines role/profile/stage
fields and event envelopes.  User text, tool output, and arbitrary metadata are
never searched, so mentioning a word such as ``reviewer`` in a chat does not
delete an otherwise valid turn.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


LEGACY_MANAGER_PROFILE = "dbb3-manager"
CURRENT_MANAGER_PROFILE = "hermes-manager"
RETIRED_ROLE_MARKERS = frozenset({"supervisor", "reviewer"})
ROLE_KEYS = frozenset(
    {
        "role",
        "profile",
        "profile_name",
        "role_name",
        "role_stage",
        "stage",
        "event_type",
        "type",
        "kind",
        "role_marker",
        "role_stage_marker",
    }
)


def _is_retired_marker(value: Any) -> bool:
    text = str(value or "").strip().lower().replace("_", ".")
    if text in RETIRED_ROLE_MARKERS:
        return True
    return any(
        text == marker
        or text.startswith(marker + ".")
        or text.endswith("." + marker)
        for marker in RETIRED_ROLE_MARKERS
    )


def _rewrite_role_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str) and value.strip().lower() == LEGACY_MANAGER_PROFILE:
        return CURRENT_MANAGER_PROFILE, True
    return value, False


def _migrate(value: Any, *, role_context: bool = False) -> tuple[Any, int, bool]:
    if isinstance(value, list):
        migrated: list[Any] = []
        removed = 0
        changed = False
        for entry in value:
            next_value, count, entry_changed = _migrate(entry, role_context=role_context)
            removed += count
            changed = changed or entry_changed
            if next_value is None and isinstance(entry, (dict, list)):
                changed = True
                continue
            migrated.append(next_value)
        return migrated, removed, changed
    if not isinstance(value, Mapping):
        if role_context and _is_retired_marker(value):
            return None, 1, True
        rewritten, changed = _rewrite_role_value(value) if role_context else (value, False)
        return rewritten, 0, changed

    # A turn/run/member/participant is removed as one unit when a structural
    # role marker identifies a retired AI role or its verdict event.
    for key, raw in value.items():
        if str(key).lower() in ROLE_KEYS and _is_retired_marker(raw):
            return None, 1, True

    output: dict[Any, Any] = {}
    removed = 0
    changed = False
    for key, raw in value.items():
        role_key = str(key).lower() in ROLE_KEYS
        next_value, count, entry_changed = _migrate(raw, role_context=role_key)
        removed += count
        changed = changed or entry_changed
        if next_value is None and isinstance(raw, (dict, list)):
            changed = True
            continue
        output[key] = next_value
    return output, removed, changed


def migrate_hosted_container(value: Any) -> tuple[Any, int, bool]:
    """Return ``(migrated_value, removed_units, changed)``.

    ``removed_units`` counts retired structural entries, not arbitrary nested
    keys.  The input is never mutated.
    """

    migrated, removed, changed = _migrate(deepcopy(value))
    return migrated, removed, changed


__all__ = [
    "CURRENT_MANAGER_PROFILE",
    "LEGACY_MANAGER_PROFILE",
    "RETIRED_ROLE_MARKERS",
    "migrate_hosted_container",
]
