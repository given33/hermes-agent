"""Compatibility radar data model for externally hosted plugins/MCP adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class PluginCompatibility:
    plugin_id: str
    version: str
    source_digest: str
    license: str
    required_permissions: tuple[str, ...] = ()
    supported_protocols: tuple[str, ...] = ()
    health: str = "unknown"
    rollback_supported: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "source_digest": self.source_digest,
            "license": self.license,
            "required_permissions": list(self.required_permissions),
            "supported_protocols": list(self.supported_protocols),
            "health": self.health,
            "rollback_supported": self.rollback_supported,
        }


def validate_plugin_compatibility(item: PluginCompatibility, *, allowed_licenses: Iterable[str] = ()) -> list[str]:
    issues: list[str] = []
    if not item.plugin_id or not item.version or not item.source_digest:
        issues.append("plugin identity/version/source digest is incomplete")
    if not item.license:
        issues.append("license is missing")
    allowed = {str(value).strip().lower() for value in allowed_licenses if str(value).strip()}
    if allowed and item.license.lower() not in allowed:
        issues.append("license is not allowlisted")
    if not item.rollback_supported:
        issues.append("unload rollback is not supported")
    return issues
