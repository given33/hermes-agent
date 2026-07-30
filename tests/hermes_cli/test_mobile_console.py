from __future__ import annotations

from hermes_cli.console_engine import HermesConsoleEngine
from hermes_cli.mobile_console import (
    _completion_values,
    execute_mobile_console_command,
    mobile_console_catalog,
    mobile_console_completions,
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
    catalog = mobile_console_catalog(_engine())
    commands = {item["command"] for item in catalog}

    assert commands == {"/status"}
    status = catalog[0]
    assert status["category"] == "status"
    assert status["requires_confirmation"] is False
    assert status["arguments"] == []
    assert status["autocomplete_endpoint"] == ""


def test_mobile_argument_completion_does_not_expose_global_config(monkeypatch):
    monkeypatch.setattr(
        "hermes_runtime.config.load_config",
        lambda: {"model": {"provider": "secret-provider", "api_key": "sk-secret"}},
    )
    monkeypatch.setattr("hermes_cli.mobile_console.resolve_profile_env", lambda _profile: "/p")
    monkeypatch.setattr("hermes_cli.mobile_console.set_hermes_home_override", lambda _path: "t")
    monkeypatch.setattr("hermes_cli.mobile_console.reset_hermes_home_override", lambda _token: None)

    result = mobile_console_completions(
        "/config set model.", profile="default", engine=_engine()
    )

    assert result["suggestions"] == []


def test_mobile_cron_completion_is_profile_scoped_and_bounded(monkeypatch):
    engine = _engine()
    engine.register(
        ("cron", "run"),
        "cron run <job>",
        "Run job.",
        lambda _engine, _args: "ok",
        mutating=True,
    )
    monkeypatch.setattr("hermes_cli.mobile_console.resolve_profile_env", lambda _profile: "/p")
    monkeypatch.setattr("hermes_cli.mobile_console.set_hermes_home_override", lambda _path: "t")
    monkeypatch.setattr("hermes_cli.mobile_console.reset_hermes_home_override", lambda _token: None)
    monkeypatch.setattr(
        "cron.jobs.list_jobs",
        lambda include_disabled=False: [
            {"id": "nightly-1", "name": "Nightly audit", "enabled": include_disabled},
            {"id": "weekly-2", "name": "Weekly report", "enabled": include_disabled},
        ],
    )

    command = engine.commands[("cron", "run")]
    values = _completion_values(command, 0, profile="ops")

    assert values == [
        ("nightly-1", "Nightly audit"),
        ("weekly-2", "Weekly report"),
    ]


def test_mobile_cron_completion_never_falls_back_to_private_prompt(monkeypatch):
    engine = _engine()
    engine.register(
        ("cron", "run"),
        "cron run <job>",
        "Run job.",
        lambda _engine, _args: "ok",
        mutating=True,
    )
    monkeypatch.setattr("hermes_cli.mobile_console.resolve_profile_env", lambda _profile: "/p")
    monkeypatch.setattr("hermes_cli.mobile_console.set_hermes_home_override", lambda _path: "t")
    monkeypatch.setattr("hermes_cli.mobile_console.reset_hermes_home_override", lambda _token: None)
    monkeypatch.setattr(
        "cron.jobs.list_jobs",
        lambda include_disabled=False: [{
            "id": "private-1",
            "name": "",
            "prompt": "read C:/secret and use token sk-private",
        }],
    )

    values = _completion_values(engine.commands[("cron", "run")], 0, profile="default")

    assert values == [("private-1", "Unnamed scheduled task")]
    assert "sk-private" not in str(values)


def test_mobile_command_cannot_switch_global_profile_home(monkeypatch):
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
        "/status",
        profile="ios-native",
        engine=_engine(),
    )

    assert result.status == "ok"
    assert result.output == "healthy"
    assert calls == []


def test_mobile_global_mutations_and_remote_install_are_blocked(monkeypatch):
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

    assert pending.status == "error"
    assert "not available" in pending.output
    assert confirmed.status == "error"
    assert "not available" in confirmed.output
    assert blocked.status == "error"
    assert "dedicated Skills" in blocked.output


def test_mobile_help_only_lists_remote_commands():
    result = execute_mobile_console_command("/commands", engine=_engine())

    assert result.status == "ok"
    assert "status" in result.output
    assert "sessions list" not in result.output
    assert "config set" not in result.output
    assert "skills install" not in result.output
    assert "logs" not in result.output
