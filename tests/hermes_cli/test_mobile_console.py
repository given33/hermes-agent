from __future__ import annotations

from hermes_cli.console_engine import HermesConsoleEngine
from hermes_cli.mobile_console import (
    execute_mobile_console_command,
    mobile_console_catalog,
)


def _engine() -> HermesConsoleEngine:
    engine = object.__new__(HermesConsoleEngine)
    engine.output_limit = 20_000
    engine.history = []
    engine.commands = {}
    engine.register(
        ("status",),
        "status",
        "Show status.",
        lambda _engine, _args: "healthy",
    )
    engine.register(
        ("sessions", "list"),
        "sessions list",
        "List sessions.",
        lambda _engine, _args: "session-a",
    )
    engine.register(
        ("config", "set"),
        "config set <key> <value>",
        "Set configuration.",
        lambda _engine, args: " ".join(args),
        mutating=True,
        confirmation="Update configuration?",
    )
    engine.register(
        ("skills", "install"),
        "skills install <source>",
        "Install a skill.",
        lambda _engine, _args: "should not run",
        mutating=True,
    )
    engine.register(
        ("logs",),
        "logs",
        "Read logs.",
        lambda _engine, _args: "sensitive",
    )
    return engine


def test_mobile_catalog_is_an_explicit_remote_subset():
    commands = {item["command"] for item in mobile_console_catalog(_engine())}

    assert commands == {"/config set", "/sessions list", "/status"}


def test_mobile_aliases_execute_inside_profile_override(monkeypatch):
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "hermes_cli.mobile_console.resolve_profile_env",
        lambda profile: f"/profiles/{profile}",
    )
    monkeypatch.setattr(
        "hermes_cli.mobile_console.set_hermes_home_override",
        lambda path: calls.append(("set", path)) or "token",
    )
    monkeypatch.setattr(
        "hermes_cli.mobile_console.reset_hermes_home_override",
        lambda token: calls.append(("reset", token)),
    )

    result = execute_mobile_console_command(
        "/sessions",
        profile="ios-native",
        engine=_engine(),
    )

    assert result.status == "ok"
    assert result.output == "session-a"
    assert calls == [("set", "/profiles/ios-native"), ("reset", "token")]


def test_mobile_mutation_requires_confirmation_and_remote_install_is_blocked(monkeypatch):
    monkeypatch.setattr("hermes_cli.mobile_console.resolve_profile_env", lambda _profile: "/p")
    monkeypatch.setattr("hermes_cli.mobile_console.set_hermes_home_override", lambda _path: "t")
    monkeypatch.setattr("hermes_cli.mobile_console.reset_hermes_home_override", lambda _token: None)
    engine = _engine()

    pending = execute_mobile_console_command(
        "/config set theme dark",
        engine=engine,
    )
    confirmed = execute_mobile_console_command(
        "/config set theme dark",
        confirmed=True,
        engine=engine,
    )
    blocked = execute_mobile_console_command(
        "/skills install https://example.invalid/skill.git",
        confirmed=True,
        engine=engine,
    )

    assert pending.status == "confirm_required"
    assert pending.confirmation_message == "Update configuration?"
    assert confirmed.status == "ok"
    assert confirmed.output == "theme dark"
    assert blocked.status == "error"
    assert "dedicated Skills" in blocked.output


def test_mobile_help_only_lists_remote_commands():
    result = execute_mobile_console_command("/commands", engine=_engine())

    assert result.status == "ok"
    assert "sessions list" in result.output
    assert "skills install" not in result.output
    assert "logs" not in result.output
