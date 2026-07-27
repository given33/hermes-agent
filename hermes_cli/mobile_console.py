"""Authenticated mobile facade for the curated Hermes Console.

The local console intentionally exposes more maintenance commands than a
remote phone should.  This module keeps the parser and handlers single-source
while applying a smaller, explicit remote policy before anything executes.
Fleet installation is deliberately excluded: skills, MCP servers and projects
must use ``managed_installations`` so server/DBB3/WSL targets remain durable
and observable.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import Any

from hermes_cli.console_engine import ConsoleCommand, ConsoleResult, HermesConsoleEngine
from hermes_cli.profiles import resolve_profile_env
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


_REMOTE_MUTATING_PATHS = frozenset(
    {
        ("config", "migrate"),
        ("config", "set"),
        ("cron", "pause"),
        ("cron", "resume"),
        ("cron", "run"),
        ("memory", "off"),
        ("memory", "reset"),
        ("plugins", "disable"),
        ("plugins", "enable"),
        ("tools", "disable"),
        ("tools", "enable"),
    }
)

_REMOTE_BLOCKED_PATHS = frozenset(
    {
        ("config", "env-path"),
        ("config", "path"),
        ("dump",),
        ("logs",),
    }
)

_ALIASES: dict[str, str] = {
    "commands": "help",
    "memory": "memory status",
    "sessions": "sessions list",
    "skills": "skills list",
}


def _normalize_line(line: str) -> str:
    normalized = str(line or "").strip()
    if normalized.startswith("/"):
        normalized = normalized[1:].lstrip()
    if not normalized:
        return "help"
    head, separator, tail = normalized.partition(" ")
    alias = _ALIASES.get(head.lower())
    if alias:
        return f"{alias}{separator}{tail}".strip()
    return normalized


def _tokens(line: str) -> list[str]:
    try:
        tokens = shlex.split(line, comments=False, posix=True)
    except ValueError:
        return []
    if tokens and tokens[0].lower() == "hermes":
        tokens = tokens[1:]
    return tokens


def _resolve_path(
    commands: dict[tuple[str, ...], ConsoleCommand],
    tokens: Iterable[str],
) -> tuple[str, ...] | None:
    materialized = tuple(tokens)
    for size in range(min(len(materialized), 3), 0, -1):
        candidate = materialized[:size]
        if candidate in commands:
            return candidate
    return None


def _remote_command_allowed(command: ConsoleCommand) -> bool:
    if command.path in _REMOTE_BLOCKED_PATHS:
        return False
    return not command.mutating or command.path in _REMOTE_MUTATING_PATHS


def mobile_console_catalog(
    engine: HermesConsoleEngine | None = None,
) -> list[dict[str, Any]]:
    active = engine or HermesConsoleEngine()
    return [
        {
            "command": f"/{' '.join(command.path)}",
            "usage": command.usage,
            "summary": command.summary,
            "mutating": command.mutating,
            "confirmation": command.confirmation if command.mutating else "",
        }
        for command in sorted(active.commands.values(), key=lambda item: item.usage)
        if _remote_command_allowed(command)
    ]


def _remote_help(engine: HermesConsoleEngine) -> ConsoleResult:
    lines = ["Hermes mobile console", "", "Available commands:"]
    for item in mobile_console_catalog(engine):
        marker = " *" if item["mutating"] else "  "
        lines.append(f"{marker} {item['usage']:<32} {item['summary']}")
    lines.extend(["", "* requires confirmation"])
    return ConsoleResult("ok", output="\n".join(lines), command="help")


def execute_mobile_console_command(
    line: str,
    *,
    confirmed: bool = False,
    profile: str = "default",
    engine: HermesConsoleEngine | None = None,
) -> ConsoleResult:
    """Execute one allowlisted command in the selected Profile boundary."""

    normalized = _normalize_line(line)
    active = engine or HermesConsoleEngine()
    tokens = _tokens(normalized)
    if tokens and tokens[0].lower() == "help" and len(tokens) == 1:
        return _remote_help(active)

    path = _resolve_path(active.commands, tokens)
    command = active.commands.get(path) if path else None
    if command is None or not _remote_command_allowed(command):
        return ConsoleResult(
            "error",
            output=(
                "This command is not available from Hermes iOS. "
                "Use the dedicated Skills, MCP, Projects, Files, or Account screen."
            ),
            command=normalized,
        )

    home_token = set_hermes_home_override(resolve_profile_env(profile.strip() or "default"))
    try:
        return active.execute(normalized, confirmed=confirmed)
    finally:
        reset_hermes_home_override(home_token)
