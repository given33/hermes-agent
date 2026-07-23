from types import SimpleNamespace
from unittest.mock import patch

from agent.conversation_loop import (
    _hosted_should_retry_client_error,
    _model_retry_wait_seconds,
)


def test_hosted_client_auth_errors_are_retryable_without_changing_other_surfaces():
    hosted = SimpleNamespace(_api_retry_client_errors=True)
    regular = SimpleNamespace()

    assert _hosted_should_retry_client_error(hosted, 401) is True
    assert _hosted_should_retry_client_error(hosted, 403) is True
    assert _hosted_should_retry_client_error(hosted, 400) is False
    assert _hosted_should_retry_client_error(hosted, 503) is False
    assert _hosted_should_retry_client_error(regular, 401) is False


def test_hosted_retry_wait_is_sixty_seconds_and_respects_retry_after():
    hosted = SimpleNamespace(_api_retry_delay_seconds=60.0)

    assert _model_retry_wait_seconds(
        hosted,
        retry_after=None,
        retry_count=1,
    ) == 60.0
    assert _model_retry_wait_seconds(
        hosted,
        retry_after=90.0,
        retry_count=2,
    ) == 90.0


def test_regular_retry_wait_keeps_the_existing_backoff_policy():
    with patch("agent.conversation_loop.jittered_backoff", return_value=7.5) as backoff:
        assert _model_retry_wait_seconds(
            SimpleNamespace(),
            retry_after=None,
            retry_count=3,
        ) == 7.5

    backoff.assert_called_once_with(3, base_delay=2.0, max_delay=60.0)
