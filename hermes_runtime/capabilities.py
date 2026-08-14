"""Capability tags and conservative role-level tool visibility policy."""

from __future__ import annotations

from typing import Iterable, Mapping


CAPABILITY_TAGS = frozenset({
    "read_fs", "write_fs", "network", "secret", "external_emit",
    "process", "provider", "session", "visual_evidence", "artifact",
})
ROLE_NAMES = frozenset({"dispatcher", "worker", "reviewer", "reporter"})

# These are defaults, not a replacement for tool-specific approval and effect
# contracts.  A caller may only narrow a role's set, never widen it implicitly.
ROLE_DENY_TAGS = {
    "reporter": frozenset({"write_fs", "external_emit", "secret", "process", "network", "provider"}),
    "reviewer": frozenset({"write_fs", "external_emit", "secret", "process"}),
    "worker": frozenset({"external_emit", "secret"}),
    "dispatcher": frozenset({"secret"}),
}


def normalize_capability_tags(tags: Iterable[str] | None) -> frozenset[str]:
    normalized = frozenset(str(item).strip().lower() for item in (tags or ()) if str(item).strip())
    unknown = normalized - CAPABILITY_TAGS
    if unknown:
        raise ValueError(f"unknown capability tag(s): {', '.join(sorted(unknown))}")
    return normalized


def normalize_role_names(roles: Iterable[str] | None) -> frozenset[str]:
    normalized = frozenset(str(item).strip().lower() for item in (roles or ()) if str(item).strip())
    unknown = normalized - ROLE_NAMES
    if unknown:
        raise ValueError(f"unknown tool role(s): {', '.join(sorted(unknown))}")
    return normalized


def role_allows(role: str, tags: Iterable[str], *, explicit_roles: Iterable[str] | None = None) -> bool:
    normalized_role = str(role or "worker").strip().lower()
    if normalized_role not in ROLE_NAMES:
        raise ValueError(f"unknown role: {role}")
    normalized_tags = normalize_capability_tags(tags)
    allowed_roles = normalize_role_names(explicit_roles)
    if allowed_roles and normalized_role not in allowed_roles:
        return False
    return not (normalized_tags & ROLE_DENY_TAGS[normalized_role])


def filter_tools_for_role(entries: Iterable[Mapping[str, object]], role: str) -> list[str]:
    """Return visible names; untagged legacy tools remain visible for compatibility."""

    result: list[str] = []
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        try:
            if role_allows(role, entry.get("capability_tags") or (), explicit_roles=entry.get("allowed_roles") or ()):
                result.append(name)
        except ValueError:
            continue
    return sorted(set(result))
