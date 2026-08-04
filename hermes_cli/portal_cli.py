"""Compatibility surface for the retired Nous Portal OAuth command.

Nous inference in this distribution is deliberately API-key only.  Keep the
``hermes portal`` parser as a clear migration message rather than silently
launching a device-code flow that cannot produce credentials usable by the
runtime.
"""
from __future__ import annotations

import sys

from hermes_runtime.colors import Colors, color


_API_KEY_GUIDANCE = (
    "Nous OAuth login is disabled in this distribution. Configure direct "
    "inference with `NOUS_API_KEY` or run "
    "`hermes auth add nous --type api-key`."
)


def portal_command(args) -> int:
    """Explain the supported direct Nous authentication method."""
    subcommand = getattr(args, "portal_command", None)
    if subcommand in {"info", "status"}:
        print()
        print(color("  Nous Research", Colors.MAGENTA))
        print(color("  " + "─" * 14, Colors.MAGENTA))
        print(f"  Auth:    {color('direct API key only', Colors.GREEN)}")
        print("  Env:     NOUS_API_KEY")
        print("  Command: hermes auth add nous --type api-key")
        return 0

    print(_API_KEY_GUIDANCE, file=sys.stderr)
    return 2


def add_parser(subparsers) -> None:
    """Register a migration-safe ``hermes portal`` compatibility command."""
    portal_parser = subparsers.add_parser(
        "portal",
        help="Show direct Nous API key configuration",
        description=_API_KEY_GUIDANCE,
    )
    portal_sub = portal_parser.add_subparsers(dest="portal_command")
    portal_sub.add_parser("info", help="Show direct Nous API key configuration")
    # Preserve the historical read-only alias without retaining a login path.
    portal_sub.add_parser("status", help="Alias for `hermes portal info`")
    portal_parser.set_defaults(func=portal_command)
