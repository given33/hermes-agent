"""Agent-facing control for durable fleet Skill/MCP/project installations."""

from __future__ import annotations

import json
from uuid import uuid4

from tools.registry import registry


def _available() -> bool:
    try:
        from hermes_cli.managed_nodes import load_managed_nodes_config

        return any(node.get("installation_urls") for node in load_managed_nodes_config())
    except Exception:
        return False


def managed_installation(args: dict, **_kwargs) -> str:
    from hermes_cli.managed_installations import (
        create_managed_installation,
        get_managed_installation,
        list_managed_installations,
    )

    action = str(args.get("action") or "install").strip().lower()
    if action == "status":
        operation_id = str(args.get("operation_id") or "").strip()
        if operation_id:
            try:
                return json.dumps(get_managed_installation(operation_id), ensure_ascii=False)
            except KeyError:
                return json.dumps({"error": "installation_not_found"})
        return json.dumps(
            list_managed_installations(
                kind=str(args.get("kind") or ""),
                profile=str(args.get("profile") or ""),
                limit=50,
            ),
            ensure_ascii=False,
        )
    if action != "install":
        return json.dumps({"error": "action must be install or status"})
    try:
        operation = create_managed_installation(
            kind=str(args.get("kind") or ""),
            identifier=str(args.get("identifier") or ""),
            profile=str(args.get("profile") or "default"),
            request_id=str(args.get("request_id") or f"agent-install-{uuid4()}"),
            scope=str(args.get("scope") or "auto"),
            locality=str(args.get("locality") or "portable"),
            targets=args.get("targets") or [],
            project_name=str(args.get("project_name") or ""),
            require_topology=True,
        )
    except (RuntimeError, ValueError) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    return json.dumps({
        "accepted": True,
        "operation": operation,
        "guidance": (
            "The main server owns this durable operation. Report the operation id and "
            "target states; use action=status for progress instead of installing again."
        ),
    }, ensure_ascii=False)


registry.register(
    name="managed_installation",
    toolset="managed_installations",
    schema={
        "name": "managed_installation",
        "description": (
            "Install a Skill, MCP catalog entry, or HTTPS git project through the main-server "
            "managed fleet. Skill auto-targets server+DBB3+WSL; project auto-targets "
            "DBB3+WSL; portable/network/iOS-relay MCP auto-targets all three. Use explicit "
            "targets for node-local MCPs. The operation survives iOS disconnects."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["install", "status"]},
                "kind": {"type": "string", "enum": ["skill", "mcp", "project"]},
                "identifier": {
                    "type": "string",
                    "description": (
                        "Skill identifier, MCP catalog name, or HTTPS git URL. "
                        "Credential-bearing and non-HTTPS project URLs are rejected."
                    ),
                },
                "scope": {"type": "string", "enum": ["auto", "fleet", "server", "workers"]},
                "locality": {
                    "type": "string",
                    "enum": ["portable", "network", "ios-relay", "server", "workers", "node"],
                },
                "targets": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["server", "dbb3", "wsl"]},
                },
                "profile": {"type": "string"},
                "project_name": {"type": "string"},
                "request_id": {"type": "string"},
                "operation_id": {"type": "string"},
            },
        },
    },
    handler=managed_installation,
    check_fn=_available,
)
