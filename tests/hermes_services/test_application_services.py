"""Direct contracts for framework-neutral Hermes application services."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from hermes_services.cron_fire import (
    CronFireCommand,
    accept_cron_fire_request,
)
from hermes_services.application import HermesApplicationKernel
from hermes_services.auth import (
    AuthError,
    CODEX_RATE_LIMITED_CODE,
    has_usable_secret,
    is_rate_limited_auth_error,
)
from hermes_services.http_boundary import (
    HttpBoundaryCompatibilityAdapter,
    HttpBoundaryPolicy,
)
from hermes_services.jsonrpc import JsonRpcMethodRegistry
from hermes_services.http_policy import (
    API_SECURITY_HEADERS,
    DASHBOARD_SECURITY_HEADERS,
    cors_headers_for_origin,
    origin_allowed,
    security_headers,
    validate_content_length,
)


def _cron_config() -> dict:
    return {
        "cron": {
            "chronos": {
                "expected_audience": "hermes-test",
                "nas_jwks_url": "test-public-key",
                "portal_url": "https://portal.example.test",
            }
        }
    }


def test_jsonrpc_registry_validates_request_boundary() -> None:
    registry = JsonRpcMethodRegistry()

    assert registry.handle([])["error"] == {
        "code": -32600,
        "message": "invalid request: expected an object",
    }
    assert registry.handle({"id": "empty", "method": ""})["error"]["code"] == -32600
    assert registry.handle(
        {"id": "params", "method": "test", "params": ["not", "an", "object"]}
    )["error"]["code"] == -32602
    assert registry.handle({"id": "missing", "method": "unknown"}) == {
        "jsonrpc": "2.0",
        "id": "missing",
        "error": {"code": -32601, "message": "unknown method: unknown"},
    }


def test_jsonrpc_registry_dispatches_and_preserves_mutable_method_table() -> None:
    registry = JsonRpcMethodRegistry()
    calls: list[tuple[object, dict]] = []

    @registry.method("echo")
    def echo(request_id, params):
        calls.append((request_id, params))
        return registry.success(request_id, {"value": params["value"]})

    assert registry.handle({"id": 7, "method": "echo", "params": {"value": "ok"}}) == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"value": "ok"},
    }
    assert calls == [(7, {"value": "ok"})]

    registry.methods["echo"] = lambda request_id, params: registry.success(
        request_id, {"replacement": params}
    )
    assert registry.handle({"id": 8, "method": "echo", "params": None})["result"] == {
        "replacement": {}
    }


def test_http_policy_is_framework_neutral_and_surface_specific() -> None:
    assert security_headers("api") is API_SECURITY_HEADERS
    assert security_headers("dashboard") is DASHBOARD_SECURITY_HEADERS
    assert API_SECURITY_HEADERS["Content-Security-Policy"].startswith("default-src")
    assert "Content-Security-Policy" not in DASHBOARD_SECURITY_HEADERS
    assert DASHBOARD_SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    with pytest.raises(ValueError, match="unknown Hermes HTTP surface"):
        security_headers("unknown")


def test_http_policy_cors_decision_and_headers_share_one_allowlist() -> None:
    allowlist = ("https://app.example.test",)
    assert origin_allowed("", allowlist)
    assert origin_allowed("https://app.example.test", allowlist)
    assert not origin_allowed("https://evil.example.test", allowlist)
    assert cors_headers_for_origin("https://evil.example.test", allowlist) is None

    headers = cors_headers_for_origin("https://app.example.test", allowlist)
    assert headers == {
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
        "Access-Control-Max-Age": "600",
        "Access-Control-Allow-Origin": "https://app.example.test",
        "Vary": "Origin",
    }
    assert cors_headers_for_origin("https://any.example.test", ("*",))["Access-Control-Allow-Origin"] == "*"


def test_http_policy_validates_declared_body_size_with_stable_failures() -> None:
    assert validate_content_length("GET", "bad", max_bytes=10) is None
    assert validate_content_length("POST", None, max_bytes=10) is None
    assert validate_content_length("POST", "10", max_bytes=10) is None

    invalid = validate_content_length("PATCH", "bad", max_bytes=10)
    assert invalid is not None
    assert (invalid.status_code, invalid.code, invalid.message) == (
        400,
        "invalid_content_length",
        "Invalid Content-Length header.",
    )
    negative = validate_content_length("PUT", "-1", max_bytes=10)
    assert negative is not None and negative.code == "invalid_content_length"
    oversized = validate_content_length("POST", "11", max_bytes=10)
    assert oversized is not None
    assert (oversized.status_code, oversized.code) == (413, "body_too_large")


def test_http_boundary_composes_auth_cors_limits_and_response_policy() -> None:
    boundary = HttpBoundaryPolicy(
        surface="api",
        bearer_secret="generated-secret-value",
        allowed_origins=("https://app.example.test",),
        max_request_bytes=32,
    )

    assert boundary.authorize("Bearer generated-secret-value").authenticated
    assert not boundary.authorize("Bearer wrong").authenticated
    assert boundary.origin_allowed("https://app.example.test")
    assert not boundary.origin_allowed("https://evil.example.test")
    assert boundary.cors_headers("https://app.example.test") == {
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
        "Access-Control-Max-Age": "600",
        "Access-Control-Allow-Origin": "https://app.example.test",
        "Vary": "Origin",
    }
    assert boundary.validate_content_length("POST", "33").code == "body_too_large"
    assert boundary.response_headers is API_SECURITY_HEADERS


def test_application_kernel_composes_http_rpc_and_session_services() -> None:
    kernel = HermesApplicationKernel.for_http(
        surface="api",
        bearer_secret="generated-secret-value",
        allowed_origins=("https://app.example.test",),
        max_request_bytes=64,
    )
    boundary = kernel.require_http_boundary()
    assert boundary.authorize("Bearer generated-secret-value").authenticated
    assert boundary.max_request_bytes == 64
    kernel.sessions["session-a"] = {"state": "running"}
    assert kernel.sessions.snapshot() == {"session-a": {"state": "running"}}

    @kernel.rpc.method("status")
    def status(request_id, params):
        return kernel.rpc.success(request_id, {"state": params.get("state")})

    assert kernel.rpc.handle(
        {"id": "request-a", "method": "status", "params": {"state": "ok"}}
    )["result"] == {"state": "ok"}


def test_application_kernel_owns_the_five_bounded_contexts() -> None:
    contexts = HermesApplicationKernel.for_local_rpc().contexts.snapshot()

    assert set(contexts) == {
        "account",
        "hosted_task",
        "resource_catalog",
        "notification",
        "intelligence",
    }
    assert contexts["resource_catalog"].status == "canonical"
    assert contexts["resource_catalog"].migration_flag == (
        "HERMES_RESOURCE_CATALOG_MODE"
    )
    assert all(context.port_name.endswith("Port") for context in contexts.values())
    assert all(context.adapters for context in contexts.values())


def test_http_compatibility_adapter_dual_runs_and_fails_closed() -> None:
    canonical = HttpBoundaryPolicy(
        surface="api",
        bearer_secret="canonical-secret",
        allowed_origins=("https://app.example.test",),
    )
    matching = HttpBoundaryPolicy(
        surface="api",
        bearer_secret="canonical-secret",
        allowed_origins=("https://app.example.test",),
    )
    dual = HttpBoundaryCompatibilityAdapter(
        canonical=canonical,
        legacy=matching,
        mode="dual",
    )

    assert dual.authorize("Bearer canonical-secret").authenticated
    assert dual.origin_allowed("https://app.example.test")
    assert dual.response_headers is canonical.response_headers

    divergent = HttpBoundaryCompatibilityAdapter(
        canonical=canonical,
        legacy=HttpBoundaryPolicy(
            surface="api",
            bearer_secret="legacy-secret",
            allowed_origins=("https://legacy.example.test",),
        ),
        mode="dual",
    )
    auth = divergent.authorize("Bearer canonical-secret")
    assert not auth.authenticated
    assert auth.error_code == "http_contract_mismatch"
    with pytest.raises(RuntimeError, match="contracts diverged"):
        divergent.origin_allowed("https://app.example.test")


def test_http_contract_mode_supports_staged_rollback() -> None:
    canonical = HttpBoundaryPolicy(surface="api", bearer_secret="new-secret")
    legacy = HttpBoundaryPolicy(surface="api", bearer_secret="old-secret")

    canonical_mode = HttpBoundaryCompatibilityAdapter(
        canonical=canonical,
        legacy=legacy,
        mode="canonical",
    )
    assert canonical_mode.authorize("Bearer new-secret").authenticated

    rolled_back = HttpBoundaryCompatibilityAdapter(
        canonical=canonical,
        legacy=legacy,
        mode="legacy",
    )
    assert rolled_back.authorize("Bearer old-secret").authenticated
    assert not rolled_back.authorize("Bearer new-secret").authenticated
    with pytest.raises(ValueError, match="invalid HTTP contract"):
        HttpBoundaryCompatibilityAdapter(
            canonical=canonical,
            legacy=legacy,
            mode="invalid",  # type: ignore[arg-type]
        )


def test_http_compatibility_adapter_has_a_bounded_policy_cost() -> None:
    policy = HttpBoundaryPolicy(
        surface="api",
        bearer_secret="performance-secret",
    )
    adapter = HttpBoundaryCompatibilityAdapter(
        canonical=policy,
        legacy=policy,
        mode="dual",
    )

    started = time.perf_counter()
    for _ in range(10_000):
        assert adapter.authorize("Bearer performance-secret").authenticated
    assert time.perf_counter() - started < 2.0


def test_local_rpc_kernel_rejects_http_boundary_access() -> None:
    kernel = HermesApplicationKernel.for_local_rpc()
    with pytest.raises(RuntimeError, match="no HTTP boundary"):
        kernel.require_http_boundary()


def test_usable_secret_contract_is_transport_independent() -> None:
    assert not has_usable_secret("your_api_key_here", min_length=8)
    assert not has_usable_secret("short", min_length=8)
    assert has_usable_secret("b4d59f7fe8b857d0b367ef0f5710b6a4", min_length=8)


def test_auth_error_identity_and_retry_classification_are_transport_independent() -> None:
    limited = AuthError(
        "quota exhausted",
        provider="openai-codex",
        code=CODEX_RATE_LIMITED_CODE,
    )
    relogin = AuthError(
        "token rejected",
        provider="openai-codex",
        code=CODEX_RATE_LIMITED_CODE,
        relogin_required=True,
    )

    assert is_rate_limited_auth_error(limited)
    assert not is_rate_limited_auth_error(relogin)
    assert not is_rate_limited_auth_error(RuntimeError("quota exhausted"))

    from hermes_cli.auth import AuthError as LegacyAuthError

    assert LegacyAuthError is AuthError


@pytest.mark.asyncio
async def test_cron_fire_rejects_auth_and_payload_before_execution() -> None:
    executions: list[CronFireCommand] = []

    async def execute(command, target):
        executions.append(command)

    rejected = await accept_cron_fire_request(
        None,
        {"job_id": "job-a"},
        execute=execute,
        config=_cron_config(),
        verifier=lambda **kwargs: None,
    )
    assert rejected.status_code == 401
    assert rejected.body == {"error": "invalid fire token"}

    malformed = await accept_cron_fire_request(
        "Bearer accepted",
        {"job_id": "  "},
        execute=execute,
        config=_cron_config(),
        verifier=lambda **kwargs: {"purpose": "cron_fire"},
    )
    assert malformed.status_code == 400
    assert malformed.body == {"error": "missing job_id"}
    assert executions == []


@pytest.mark.asyncio
async def test_cron_fire_verifies_off_loop_and_returns_gone_target() -> None:
    loop_thread = threading.get_ident()
    verifier_threads: list[int] = []
    resolutions: list[CronFireCommand] = []

    def verifier(**kwargs):
        verifier_threads.append(threading.get_ident())
        assert kwargs["token"] == "accepted"
        return {"purpose": "cron_fire", "subject": "scheduler"}

    async def resolve(command):
        resolutions.append(command)
        return None

    async def execute(command, target):
        raise AssertionError("gone targets must not execute")

    result = await accept_cron_fire_request(
        "Bearer accepted",
        {"job_id": " job-b "},
        execute=execute,
        resolve_target=resolve,
        config=_cron_config(),
        verifier=verifier,
    )

    assert verifier_threads and verifier_threads[0] != loop_thread
    assert result.status_code == 200
    assert result.body == {"status": "gone", "job_id": "job-b"}
    assert result.background_task is None
    assert [command.job_id for command in resolutions] == ["job-b"]


@pytest.mark.asyncio
async def test_cron_fire_accepts_exact_command_target_and_owns_task_failure() -> None:
    observed: list[tuple[CronFireCommand, str]] = []

    async def resolve(command):
        return "profile-a"

    async def execute(command, target):
        observed.append((command, target))
        await asyncio.sleep(0)
        raise RuntimeError("executor failed after acceptance")

    accepted = await accept_cron_fire_request(
        "Bearer accepted",
        {"job_id": "job-c"},
        execute=execute,
        resolve_target=resolve,
        config=_cron_config(),
        verifier=lambda **kwargs: {"purpose": "cron_fire", "tenant": "test"},
    )

    assert accepted.status_code == 202
    assert accepted.body == {"status": "accepted", "job_id": "job-c"}
    assert accepted.target == "profile-a"
    assert accepted.background_task is not None
    with pytest.raises(RuntimeError, match="executor failed after acceptance"):
        await accepted.background_task
    assert len(observed) == 1
    command, target = observed[0]
    assert command.job_id == "job-c"
    assert command.claims == {"purpose": "cron_fire", "tenant": "test"}
    assert target == "profile-a"
