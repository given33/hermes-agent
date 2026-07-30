"""Bounded-context ownership for incremental Hermes service extraction."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class BoundedContext:
    name: str
    domain_module: str
    port_name: str
    adapters: tuple[str, ...]
    migration_flag: str
    status: str


_CONTEXTS = {
    "account": BoundedContext(
        name="account",
        domain_module="hermes_cli.account_lifecycle",
        port_name="AccountLifecyclePort",
        adapters=("dashboard-auth", "owner-mobile", "profile-overlay"),
        migration_flag="HERMES_ACCOUNT_CONTEXT_MODE",
        status="compatibility",
    ),
    "hosted_task": BoundedContext(
        name="hosted_task",
        domain_module="hermes_services.hosted_event_protocol",
        port_name="HostedTaskPort",
        adapters=("fastapi-collaboration", "dbb3-connector", "wsl-connector"),
        migration_flag="HERMES_HOSTED_TASK_CONTEXT_MODE",
        status="compatibility",
    ),
    "resource_catalog": BoundedContext(
        name="resource_catalog",
        domain_module="hermes_services.resource_catalog",
        port_name="ResourceCatalogPort",
        adapters=("managed-installations", "mobile-sse", "agent-tool"),
        migration_flag="HERMES_RESOURCE_CATALOG_MODE",
        status="canonical",
    ),
    "notification": BoundedContext(
        name="notification",
        domain_module="hermes_cli.dashboard_auth.mobile_notifications",
        port_name="NotificationPort",
        adapters=("apns", "hosted-outbox", "account-deletion-outbox"),
        migration_flag="HERMES_NOTIFICATION_CONTEXT_MODE",
        status="compatibility",
    ),
    "intelligence": BoundedContext(
        name="intelligence",
        domain_module="hermes_cli.ios_intelligence",
        port_name="IntelligencePort",
        adapters=("ios-relay", "scheduler", "mcp-supervisor"),
        migration_flag="HERMES_INTELLIGENCE_CONTEXT_MODE",
        status="compatibility",
    ),
}


class BoundedContextRegistry:
    """Immutable context map owned by each application composition root."""

    def __init__(self, contexts: Mapping[str, BoundedContext] | None = None) -> None:
        selected = dict(contexts or _CONTEXTS)
        if set(selected) != set(_CONTEXTS):
            raise ValueError("Hermes bounded-context registry is incomplete")
        self._contexts = MappingProxyType(selected)

    def get(self, name: str) -> BoundedContext:
        try:
            return self._contexts[name]
        except KeyError as exc:
            raise KeyError(f"unknown Hermes bounded context: {name}") from exc

    def snapshot(self) -> dict[str, BoundedContext]:
        return dict(self._contexts)


__all__ = ["BoundedContext", "BoundedContextRegistry"]
