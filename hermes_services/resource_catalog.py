"""Resource provenance, deterministic identity, and collision diagnostics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


RESOURCE_KINDS = frozenset({"skill", "mcp", "project"})
SOURCE_TYPES = frozenset({"builtin", "git", "npm", "local", "managed"})
SCOPES = frozenset({"account", "server", "node", "project"})
TRUST_STATES = frozenset({"builtin", "approved", "pending", "blocked"})


@dataclass(frozen=True)
class ResourceRecord:
    resource_id: str
    kind: str
    name: str
    source_type: str
    source_uri: str
    source_ref: str = ""
    resolved_commit_or_version: str = ""
    content_hash: str = ""
    scope: str = "account"
    target_nodes: tuple[str, ...] = ()
    loaded_nodes: tuple[str, ...] = ()
    aggregate_state: str = "pending"
    node_receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    policy_version: str = ""
    tree_sha: str = ""
    tools: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    last_verified_at: str = ""
    rollback_available: bool = False
    enabled: bool = True
    trust_state: str = "pending"
    health: str = "unknown"
    conflicts: tuple[dict[str, Any], ...] = ()
    installed_at: str = ""
    updated_at: str = ""
    operation_id: str = ""
    install_operation_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in RESOURCE_KINDS:
            raise ValueError("invalid resource kind")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError("invalid resource source_type")
        if self.scope not in SCOPES:
            raise ValueError("invalid resource scope")
        if self.trust_state not in TRUST_STATES:
            raise ValueError("invalid resource trust_state")
        if not self.name.strip():
            raise ValueError("resource name is required")

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["target_nodes"] = list(self.target_nodes)
        payload["loaded_nodes"] = list(self.loaded_nodes)
        payload["node_receipts"] = dict(self.node_receipts)
        payload["tools"] = list(self.tools)
        payload["permissions"] = list(self.permissions)
        payload["conflicts"] = list(self.conflicts)
        return payload


def canonical_source_uri(source_type: str, value: str) -> str:
    raw = str(value or "").strip()
    if source_type != "git":
        return raw.replace("\\", "/")
    if raw.startswith("git@") and ":" in raw:
        host_path = raw[4:]
        host, path = host_path.split(":", 1)
        raw = f"https://{host}/{path}"
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return urlunsplit(("https" if host else parsed.scheme, host, path, "", ""))


def resource_identity(
    *,
    kind: str,
    name: str,
    source_type: str,
    source_uri: str,
    source_ref: str = "",
) -> str:
    canonical = canonical_source_uri(source_type, source_uri)
    material = "\0".join(
        (
            str(kind).lower(),
            normalize_resource_name(name),
            str(source_type).lower(),
            canonical,
            str(source_ref or ""),
        )
    )
    return "resource_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def normalize_resource_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def resolve_resource_collisions(
    records: Iterable[ResourceRecord],
) -> tuple[list[ResourceRecord], list[dict[str, Any]]]:
    """Return deterministic winners and explicit winner/loser diagnostics."""

    source_priority = {"managed": 0, "local": 1, "git": 2, "npm": 3, "builtin": 4}
    scope_priority = {"account": 0, "project": 1, "node": 2, "server": 3}
    trust_priority = {"approved": 0, "builtin": 1, "pending": 2, "blocked": 3}
    health_priority = {"healthy": 0, "degraded": 1, "unknown": 2, "failed": 3}
    groups: dict[tuple[str, str], list[ResourceRecord]] = {}
    for record in records:
        groups.setdefault((record.kind, normalize_resource_name(record.name)), []).append(record)
    winners: list[ResourceRecord] = []
    diagnostics: list[dict[str, Any]] = []
    for (_kind, normalized_name), group in sorted(groups.items()):
        ordered = sorted(
            group,
            key=lambda item: (
                0 if item.enabled else 1,
                trust_priority.get(item.trust_state, 99),
                health_priority.get(item.health, 99),
                scope_priority.get(item.scope, 99),
                source_priority.get(item.source_type, 99),
                canonical_source_uri(item.source_type, item.source_uri),
                item.source_ref,
                item.resource_id,
            ),
        )
        winner = ordered[0]
        winners.append(winner)
        for loser in ordered[1:]:
            diagnostics.append(
                {
                    "code": "resource_name_collision",
                    "name": normalized_name,
                    "kind": winner.kind,
                    "winner": winner.public_dict(),
                    "loser": loser.public_dict(),
                    "reason": (
                        "enabled/trust/health/scope/source priority and canonical identity"
                    ),
                }
            )
    return winners, diagnostics
