"""Retirement contract for the legacy Nous subscription CLI surface."""

from cli import HermesCLI


def test_subscription_screen_is_not_exposed():
    assert not hasattr(HermesCLI, "_show_subscription")


def test_subscription_slash_command_is_not_advertised():
    from hermes_cli import cli_commands_mixin

    source = cli_commands_mixin.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        assert "/subscription" not in handle.read()
