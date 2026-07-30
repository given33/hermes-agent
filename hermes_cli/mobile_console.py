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
import re
from collections.abc import Iterable
from typing import Any

from hermes_cli.console_engine import ConsoleCommand, ConsoleResult, HermesConsoleEngine
from hermes_cli.profiles import resolve_profile_env
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


# Mobile authentication identifies an account, not an operator of the shared
# Hermes runtime. Keep this list fail-closed: adding a new local Console
# command must never make it remotely reachable by accident. Account-owned
# sessions, files, resources, configuration and schedules already have
# dedicated authenticated APIs and must not be read or mutated through the
# process-wide Profile console.
_REMOTE_SAFE_PATHS = frozenset(
    {
        ("status",),
        ("version",),
    }
)

_ALIASES: dict[str, str] = {
    "commands": "help",
    "memory": "memory status",
    "sessions": "sessions list",
    "skills": "skills list",
}

_USAGE_ARGUMENT = re.compile(r"(<[^>]+>|\[[^\]]+\])")
_OPTION_TOKEN = re.compile(r"--[A-Za-z0-9][A-Za-z0-9-]*")
_MOBILE_COMPLETION_ENDPOINT = "/api/plugins/collaboration/mobile/console/completions"


def _command_category(command: ConsoleCommand) -> str:
    return command.path[0] if command.path else "general"


def _command_arguments(command: ConsoleCommand) -> list[dict[str, Any]]:
    """Turn the console usage string into a bounded mobile descriptor.

    The server remains authoritative for parsing.  This metadata is only for
    completion and confirmation UI, so it deliberately does not expose local
    filesystem completion or shell aliases.
    """

    arguments: list[dict[str, Any]] = []
    for token in _USAGE_ARGUMENT.findall(command.usage):
        required = token.startswith("<")
        name = token[1:-1].strip()
        if not name:
            continue
        values = sorted(set(_OPTION_TOKEN.findall(name)))
        arguments.append(
            {
                "name": name,
                "display_name": name.replace("_", " ").replace("-", " ").title(),
                "required": required,
                "values": values,
            }
        )
    return arguments


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
    return command.path in _REMOTE_SAFE_PATHS and not command.mutating


def mobile_console_catalog(
    engine: HermesConsoleEngine | None = None,
) -> list[dict[str, Any]]:
    active = engine or HermesConsoleEngine()
    return [
        {
            "name": " ".join(command.path),
            "command": f"/{' '.join(command.path)}",
            "display_name": " ".join(part.title() for part in command.path),
            "usage": command.usage,
            "summary": command.summary,
            "description": command.summary,
            "category": _command_category(command),
            "arguments": _command_arguments(command),
            "mutating": command.mutating,
            "mutates_state": command.mutating,
            "requires_confirmation": command.mutating,
            "confirmation": command.confirmation if command.mutating else "",
            "available_when": ["authenticated", "connected"],
            "autocomplete_endpoint": (
                _MOBILE_COMPLETION_ENDPOINT if _command_arguments(command) else ""
            ),
            "examples": [f"/{command.usage}"],
        }
        for command in sorted(active.commands.values(), key=lambda item: item.usage)
        if _remote_command_allowed(command)
    ]


def mobile_console_completions(
    line: str,
    *,
    profile: str = "default",
    limit: int = 30,
    engine: HermesConsoleEngine | None = None,
) -> dict[str, Any]:
    """Return bounded, profile-scoped argument suggestions for Hermes iOS.

    Completion intentionally excludes filesystem paths, raw config values and
    shell aliases. Complete replacement text keeps parser behavior server-owned.
    """

    raw_line = str(line or "")[:4096]
    normalized = raw_line.lstrip()
    if normalized.startswith("/"):
        normalized = normalized[1:]
    active = engine or HermesConsoleEngine()
    commands = {
        path: command
        for path, command in active.commands.items()
        if _remote_command_allowed(command)
    }
    try:
        tokens = shlex.split(normalized, comments=False, posix=True)
    except ValueError:
        tokens = normalized.split()
    path = _resolve_path(commands, tokens)
    bounded_limit = min(50, max(1, int(limit)))
    if path is None:
        probe = " ".join(tokens).lower()
        candidates = []
        for command in commands.values():
            name = " ".join(command.path)
            if probe and probe not in name.lower() and probe not in command.summary.lower():
                continue
            candidates.append(
                {
                    "value": name,
                    "display_name": f"/{name}",
                    "description": command.summary,
                    "replacement": f"/{name} ",
                    "complete": not bool(_command_arguments(command)),
                }
            )
        return {"line": raw_line, "suggestions": candidates[:bounded_limit]}

    command = commands[path]
    arguments = tokens[len(path):]
    trailing_space = normalized.endswith((" ", "\t"))
    argument_index = len(arguments) if trailing_space else max(0, len(arguments) - 1)
    probe = "" if trailing_space else (arguments[-1] if arguments else "")
    values = _completion_values(command, argument_index, profile=profile)
    prefix_tokens = list(path) + arguments[:argument_index]
    descriptors = _command_arguments(command)
    suggestions = []
    for value, description in values:
        if probe and probe.lower() not in value.lower() and probe.lower() not in description.lower():
            continue
        replacement_tokens = [*prefix_tokens, value]
        has_more_required = any(
            item.get("required") for item in descriptors[argument_index + 1:]
        )
        suggestions.append(
            {
                "value": value,
                "display_name": value,
                "description": description,
                "replacement": "/" + " ".join(shlex.quote(token) for token in replacement_tokens) + " ",
                "complete": not has_more_required,
            }
        )
    return {"line": raw_line, "suggestions": suggestions[:bounded_limit]}


def _completion_values(
    command: ConsoleCommand,
    argument_index: int,
    *,
    profile: str,
) -> list[tuple[str, str]]:
    descriptors = _command_arguments(command)
    static_values = (
        descriptors[argument_index].get("values")
        if 0 <= argument_index < len(descriptors)
        else []
    )
    values = [(str(value), "Command option") for value in static_values or []]
    home_token = set_hermes_home_override(resolve_profile_env(profile.strip() or "default"))
    try:
        if command.path == ("config", "set") and argument_index == 0:
            from hermes_runtime.config import load_config

            values.extend((key, "Configuration key") for key in _flatten_config_keys(load_config() or {}))
        elif command.path in {("cron", "pause"), ("cron", "resume"), ("cron", "run")} and argument_index == 0:
            from cron.jobs import list_jobs

            for job in list_jobs(include_disabled=True):
                job_id = str(job.get("id") or job.get("job_id") or "").strip()
                # The prompt is user content and may contain paths, tokens or
                # other secrets. Only an explicitly public job name belongs in
                # completion metadata.
                name = str(job.get("name") or "").strip()
                if job_id:
                    values.append((job_id, name[:160] or "Unnamed scheduled task"))
    except Exception:
        pass
    finally:
        reset_hermes_home_override(home_token)
    deduplicated: dict[str, str] = {}
    for value, description in values:
        normalized = str(value or "").strip()
        if normalized and "\x00" not in normalized:
            deduplicated.setdefault(normalized[:256], str(description or "")[:240])
    return sorted(deduplicated.items(), key=lambda item: item[0].lower())


def _flatten_config_keys(value: Any, prefix: str = "") -> list[str]:
    if not isinstance(value, dict):
        return [prefix] if prefix else []
    keys: list[str] = []
    for raw_key, child in value.items():
        key = str(raw_key or "").strip()
        if not key or "\x00" in key:
            continue
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(child, dict) and child:
            keys.extend(_flatten_config_keys(child, path))
        else:
            keys.append(path)
    return keys


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
    """Execute one explicitly public, read-only mobile command.

    ``profile`` remains in the wire contract for older iOS clients, but it is
    intentionally not used to switch ``HERMES_HOME``. Mobile accounts cannot
    select a process-wide Profile through this facade.
    """

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

    return active.execute(normalized, confirmed=confirmed)
