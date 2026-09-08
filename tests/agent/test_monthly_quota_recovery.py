import httpx
import pytest
from openai import RateLimitError

from agent.error_classifier import FailoverReason, classify_api_error


@pytest.mark.parametrize("reset", ["25min", "1day"])
def test_monthly_paid_allowance_does_not_wait_for_retry_after(reset):
    body = {"type": "error", "error": {
        "type": "GoUsageLimitError",
        "message": f"Monthly usage limit reached. Resets in {reset}. "
                   "To continue using this model now, enable usage from your available balance.",
        "metadata": {"limitName": "monthly"},
    }}
    response = httpx.Response(429, headers={"retry-after": "1500"},
                              request=httpx.Request("POST", "https://example.test/v1/chat/completions"))
    error = RateLimitError(body["error"]["message"], response=response, body=body)
    result = classify_api_error(error, provider="custom", model="test-model")
    assert result.reason is FailoverReason.billing
    assert not result.retryable
    assert result.should_fallback
    assert result.should_rotate_credential


def test_short_period_rate_limit_still_respects_retry_policy():
    response = httpx.Response(429, headers={"retry-after": "10"},
                              request=httpx.Request("POST", "https://example.test/v1/chat/completions"))
    body = {"error": {"message": "Usage limit reached. Resets in 10 seconds."}}
    result = classify_api_error(RateLimitError(body["error"]["message"], response=response, body=body))
    assert result.reason is FailoverReason.rate_limit
    assert result.retryable
