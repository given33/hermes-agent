from __future__ import annotations

import pytest

from hermes_config_values import parse_enabled_flag


@pytest.mark.parametrize("value", [True, 1, -1, "true", "1", "yes", "on", " YES "])
def test_parse_enabled_flag_accepts_supported_truthy_values(value: object) -> None:
    assert parse_enabled_flag(value, default=False) is True


@pytest.mark.parametrize("value", [False, 0, "false", "0", "no", "off", " OFF "])
def test_parse_enabled_flag_accepts_supported_falsey_values(value: object) -> None:
    assert parse_enabled_flag(value, default=True) is False


@pytest.mark.parametrize("value", [None, "future-value", object()])
def test_parse_enabled_flag_uses_caller_default_for_unknown_values(value: object) -> None:
    assert parse_enabled_flag(value, default=True) is True
    assert parse_enabled_flag(value, default=False) is False
