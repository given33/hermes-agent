from __future__ import annotations

import time
from threading import Event

import pytest

import hermes_services.internal_hooks as internal_hooks_module
from hermes_services.internal_hooks import (
    InternalHook,
    InternalHookExecutionError,
    bootstrap_internal_hooks,
    internal_hook_registry_status,
    register_internal_hook,
    run_internal_hooks,
)
from hermes_services.hosted_event_protocol import append_hosted_event
from tests.hermes_services.internal_hook_test_support import (
    register_test_hook as _register_internal_hook_for_tests,
    reset_test_registry as reset_internal_hooks_for_tests,
    restore_production_registry,
)
from tools.budget_config import BudgetConfig
from tools.tool_result_storage import enforce_turn_budget, maybe_persist_tool_result


def teardown_function():
    restore_production_registry()


def setup_function():
    reset_internal_hooks_for_tests()


def _model_context() -> dict:
    return {
        "task_id": "task-1",
        "turn_id": "turn-1",
        "api_request_id": "request-1",
        "session_id": "session-1",
        "model": "model-1",
        "provider": "provider-1",
        "api_call_count": 1,
    }


def test_hooks_are_ordered_and_can_transform_real_tool_result_path():
    _register_internal_hook_for_tests(
        "after_tool_result",
        InternalHook(
            name="first",
            source="hermes.tests",
            version="1",
            order=20,
            callback=lambda value, **_context: value + "-second",
        ),
    )
    _register_internal_hook_for_tests(
        "after_tool_result",
        InternalHook(
            name="earlier",
            source="plugins.builtin.tests",
            version="1",
            order=10,
            callback=lambda value, **_context: value + "-first",
        ),
    )

    result = maybe_persist_tool_result(
        "base",
        "read_file",
        "tool-1",
        threshold=float("inf"),
    )

    assert result == "base-first-second"


def test_fail_open_hook_records_failure_and_keeps_payload():
    def fail(_value, **_context):
        raise RuntimeError("observer failed")

    _register_internal_hook_for_tests(
        "after_tool_result",
        InternalHook(
            name="observer",
            source="hermes.tests",
            version="1",
            callback=fail,
            failure_policy="fail_open",
        ),
    )

    outcome = run_internal_hooks(
        "after_tool_result",
        "result",
        tool_name="read_file",
        tool_use_id="tool-1",
    )
    value, trace = outcome

    assert outcome.blocked is False
    assert outcome.payload == "result"
    assert value == "result"
    assert trace[0]["status"] == "failed"
    assert "observer failed" in trace[0]["error"]
    assert trace[0]["point"] == "after_tool_result"
    assert trace[0]["source"] == "hermes.tests"
    assert trace[0]["version"] == "1"
    assert trace[0]["failure_policy"] == "fail_open"
    assert trace[0]["duration_ms"] >= 0


def test_fail_closed_hook_aborts_caller():
    _register_internal_hook_for_tests(
        "before_hosted_event_commit",
        InternalHook(
            name="guard",
            source="hermes.tests",
            version="1",
            callback=lambda _value, **_context: (_ for _ in ()).throw(
                RuntimeError("blocked")
            ),
            failure_policy="fail_closed",
        ),
    )

    conversation = {}
    with pytest.raises(InternalHookExecutionError, match="blocked") as raised:
        append_hosted_event(
            conversation,
            account_generation="generation-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            role_stage="worker",
            event_type="turn.started",
            idempotency_key="turn-started",
        )
    assert conversation == {}
    assert raised.value.trace[-1]["status"] == "failed"
    assert raised.value.blocked is True
    assert isinstance(raised.value.payload, dict)


def test_hook_audit_log_redacts_callback_errors(caplog):
    secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"

    def fail(_value, **_context):
        raise RuntimeError(f"provider rejected {secret}")

    _register_internal_hook_for_tests(
        "after_tool_result",
        InternalHook(
            name="redacted-observer",
            source="hermes.tests",
            version="1",
            callback=fail,
            failure_policy="fail_open",
        ),
    )

    with caplog.at_level("INFO"):
        outcome = run_internal_hooks(
            "after_tool_result",
            "payload",
            tool_name="read_file",
            tool_use_id="tool-redaction",
        )

    assert outcome.blocked is False
    assert secret not in caplog.text
    assert secret not in outcome.trace[0]["error"]
    assert "internal_hook_trace" in caplog.text


def test_timeout_does_not_wait_for_timed_out_daemon_hook():
    def slow(value, **_context):
        time.sleep(0.25)
        return value

    _register_internal_hook_for_tests(
        "after_tool_result",
        InternalHook(
            name="slow-observer",
            source="hermes.tests",
            version="1",
            callback=slow,
            timeout_seconds=0.01,
            failure_policy="fail_open",
        ),
    )
    started = time.monotonic()

    value, trace = run_internal_hooks(
        "after_tool_result",
        "payload",
        tool_name="read_file",
        tool_use_id="tool-1",
    )

    assert time.monotonic() - started < 0.15
    assert value == "payload"
    assert trace[0]["status"] == "timeout"
    assert trace[0]["point"] == "after_tool_result"
    assert trace[0]["source"] == "hermes.tests"
    assert trace[0]["version"] == "1"
    assert trace[0]["failure_policy"] == "fail_open"
    assert trace[0]["duration_ms"] >= 0


def test_timed_out_hook_cannot_mutate_the_caller_payload_later():
    def late_mutation(value, **_context):
        time.sleep(0.05)
        value["status"] = "corrupted"
        return value

    _register_internal_hook_for_tests(
        "before_model_request",
        InternalHook(
            name="late-mutator",
            source="hermes.tests",
            version="1",
            callback=late_mutation,
            timeout_seconds=0.01,
            failure_policy="fail_open",
        ),
    )
    payload = {"status": "original"}

    value, trace = run_internal_hooks(
        "before_model_request",
        payload,
        **_model_context(),
    )
    time.sleep(0.08)

    assert trace[0]["status"] == "timeout"
    assert value == {"status": "original"}
    assert payload == {"status": "original"}


def test_snapshot_failure_does_not_leak_hook_worker_slots():
    class Uncopyable(dict):
        def __deepcopy__(self, _memo):
            raise TypeError("not copyable")

    _register_internal_hook_for_tests(
        "before_model_request",
        InternalHook(
            name="snapshot-observer",
            source="hermes.tests",
            version="1",
            callback=lambda value, **_context: value,
            failure_policy="fail_open",
        ),
    )

    for _ in range(12):
        value, trace = run_internal_hooks(
            "before_model_request",
            Uncopyable(),
            **_model_context(),
        )
        assert isinstance(value, Uncopyable)
        assert trace[0]["status"] == "failed"
        assert "snapshot failed" in trace[0]["error"]
        assert trace[0]["point"] == "before_model_request"
        assert trace[0]["source"] == "hermes.tests"
        assert trace[0]["version"] == "1"
        assert trace[0]["duration_ms"] >= 0

    reset_internal_hooks_for_tests()
    _register_internal_hook_for_tests(
        "before_model_request",
        InternalHook(
            name="healthy-observer",
            source="hermes.tests",
            version="1",
            callback=lambda value, **_context: {**value, "ok": True},
        ),
    )
    value, trace = run_internal_hooks(
        "before_model_request",
        {"payload": True},
        **_model_context(),
    )
    assert value == {"payload": True, "ok": True}
    assert trace[0]["status"] == "completed"


def test_turn_budget_persistence_does_not_apply_tool_result_hook_twice():
    calls = []
    _register_internal_hook_for_tests(
        "after_tool_result",
        InternalHook(
            name="single-transform",
            source="hermes.tests",
            version="1",
            callback=lambda value, **_context: calls.append(value) or f"{value}-hooked",
        ),
    )
    config = BudgetConfig(default_result_size=1_000, preview_size=80, turn_budget=1)
    content = maybe_persist_tool_result(
        "payload",
        "read_file",
        "tool-1",
        config=config,
    )
    messages = [{"content": content, "tool_call_id": "tool-1"}]

    enforce_turn_budget(messages, config=config)

    assert calls == ["payload"]
    assert "payload-hooked-hooked" not in messages[0]["content"]


def test_before_hosted_event_commit_hook_runs_through_real_append_path():
    observed = []
    _register_internal_hook_for_tests(
        "before_hosted_event_commit",
        InternalHook(
            name="event-observer",
            source="hermes.tests",
            version="1",
            callback=lambda value, **context: observed.append(
                (value["event_type"], context["conversation_id"])
            ),
        ),
    )

    conversation = {}
    result = append_hosted_event(
        conversation,
        account_generation="generation-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        role_stage="worker",
        event_type="turn.started",
        idempotency_key="turn-started",
    )

    assert result.appended is True
    assert observed == [("turn.started", "conversation-1")]
    assert result.event["hook_trace"][0]["status"] == "completed"
    assert conversation["session_entries"][0]["entry_type"] == "hook_trace"
    assert conversation["session_entries"][0]["payload"]["event_id"] == result.event[
        "event_id"
    ]


def test_post_persistence_hook_cannot_claim_fail_closed_semantics():
    with pytest.raises(ValueError, match="must fail open"):
        _register_internal_hook_for_tests(
            "after_hosted_event_persistence",
            InternalHook(
                name="unsafe-post-commit-guard",
                source="hermes.tests",
                version="1",
                callback=lambda value, **_context: value,
                failure_policy="fail_closed",
            ),
        )


def test_runtime_registration_does_not_trust_a_forged_source(monkeypatch):
    with monkeypatch.context() as isolated:
        isolated.delenv("PYTEST_CURRENT_TEST", raising=False)
        with pytest.raises(
            PermissionError,
            match="runtime internal-hook registration",
        ):
            register_internal_hook(
                "after_tool_result",
                InternalHook(
                    name="forged",
                    source="hermes.definitely-trusted-looking",
                    version="1",
                    callback=lambda value, **_context: value,
                ),
            )


def test_forged_pytest_environment_cannot_reset_or_register_production_hooks(
    monkeypatch,
):
    restore_production_registry()
    before = internal_hook_registry_status()
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "attacker::forged (call)")

    assert not hasattr(internal_hooks_module, "reset_internal_hooks_for_tests")
    assert not hasattr(
        internal_hooks_module,
        "_register_internal_hook_for_tests",
    )
    with pytest.raises(PermissionError, match="runtime internal-hook registration"):
        register_internal_hook(
            "after_tool_result",
            InternalHook(
                name="environment-forgery",
                source="attacker",
                version="1",
                callback=lambda value, **_context: value,
            ),
        )

    after = internal_hook_registry_status()
    assert after["sealed"] is True
    assert after["hook_count"] == before["hook_count"]


@pytest.mark.parametrize(
    ("field", "message"),
    [("name", "name"), ("source", "source"), ("version", "version")],
)
def test_empty_hook_provenance_is_rejected(field, message):
    values = {
        "name": "observer",
        "source": "telemetry.test",
        "version": "1",
    }
    values[field] = ""

    with pytest.raises(ValueError, match=f"hook {message} is required"):
        InternalHook(
            **values,
            callback=lambda value, **_context: value,
        )


def test_declared_builtin_is_installed_by_bootstrap(monkeypatch):
    declared = InternalHook(
        name="declared-observer",
        source="telemetry.builtin",
        version="1",
        callback=lambda value, **_context: value,
    )
    with monkeypatch.context() as isolated:
        isolated.setattr(
            internal_hooks_module,
            "_BUILTIN_HOOKS",
            (("after_tool_result", declared),),
        )
        bootstrap_internal_hooks()
        status = internal_hook_registry_status()
        assert status["sealed"] is True
        assert status["hook_count"] == 1
        assert status["hooks"][0]["name"] == "declared-observer"
        reset_internal_hooks_for_tests()


def test_bootstrap_seals_registry_against_late_registration():
    bootstrap_internal_hooks()

    with pytest.raises(RuntimeError, match="sealed"):
        _register_internal_hook_for_tests(
            "after_tool_result",
            InternalHook(
                name="late",
                source="telemetry.only",
                version="1",
                callback=lambda value, **_context: value,
            ),
        )


def test_invalid_contract_has_structured_trace_before_raise():
    _register_internal_hook_for_tests(
        "before_tool_call",
        InternalHook(
            name="contract-guard",
            source="telemetry.test",
            version="1",
            callback=lambda value, **_context: value,
            failure_policy="fail_closed",
        ),
    )
    with pytest.raises(InternalHookExecutionError) as raised:
        run_internal_hooks(
            "before_tool_call",
            "not-a-mapping",
            tool_name="read_file",
            task_id="task-1",
            session_id="session-1",
            tool_call_id="tool-1",
            turn_id="turn-1",
        )

    assert len(raised.value.trace) == 1
    assert raised.value.trace[0]["name"] == "<contract>"
    assert raised.value.trace[0]["status"] == "invalid_input"
    assert raised.value.trace[0]["failure_policy"] == "fail_closed"


def test_empty_production_registry_still_validates_point_contract():
    """Point validation is independent of observer registration."""

    # The fixture deliberately installs an empty, unsealed registry to model a
    # production deployment with no built-in observers.
    with pytest.raises(InternalHookExecutionError) as raised:
        run_internal_hooks(
            "before_tool_call",
            "not-a-mapping",
            tool_name="read_file",
            task_id="task-1",
            session_id="session-1",
            tool_call_id="tool-1",
            turn_id="turn-1",
        )

    trace = list(raised.value.trace)
    assert len(trace) == 1
    assert trace[0]["point"] == "before_tool_call"
    assert trace[0]["name"] == "<contract>"
    assert trace[0]["status"] == "invalid_input"
    assert trace[0]["failure_policy"] == "fail_closed"
    assert trace[0]["duration_ms"] == 0

    value, traces = run_internal_hooks(
        "after_tool_result",
        "valid",
        tool_name="read_file",
        tool_use_id="tool-1",
    )
    assert value == "valid"
    assert traces == []


def test_invalid_hook_result_is_traced_before_fail_closed_raise():
    _register_internal_hook_for_tests(
        "before_tool_call",
        InternalHook(
            name="bad-transform",
            source="telemetry.test",
            version="1",
            callback=lambda _value, **_context: "not-a-mapping",
            failure_policy="fail_closed",
        ),
    )

    with pytest.raises(InternalHookExecutionError) as raised:
        run_internal_hooks(
            "before_tool_call",
            {"path": "README.md"},
            tool_name="read_file",
            task_id="task-1",
            session_id="session-1",
            tool_call_id="tool-1",
            turn_id="turn-1",
        )

    assert raised.value.trace[-1]["status"] == "invalid_result"
    assert "tool argument mapping" in raised.value.trace[-1]["error"]


def test_worker_saturation_is_traced_before_fail_open_policy():
    _register_internal_hook_for_tests(
        "after_tool_result",
        InternalHook(
            name="saturation-observer",
            source="telemetry.test",
            version="1",
            callback=lambda value, **_context: value,
        ),
    )

    class SaturatedSlots:
        @staticmethod
        def acquire(*, blocking):
            assert blocking is False
            return False

    original_slots = internal_hooks_module._HOOK_SLOTS
    internal_hooks_module._HOOK_SLOTS = SaturatedSlots()
    try:
        value, trace = run_internal_hooks(
            "after_tool_result",
            "payload",
            tool_name="read_file",
            tool_use_id="tool-1",
        )
    finally:
        internal_hooks_module._HOOK_SLOTS = original_slots

    assert value == "payload"
    assert trace[0]["status"] == "saturated"
    assert trace[0]["point"] == "after_tool_result"
    assert trace[0]["source"] == "telemetry.test"
    assert trace[0]["version"] == "1"
    assert trace[0]["failure_policy"] == "fail_open"
    assert trace[0]["duration_ms"] >= 0
    assert internal_hook_registry_status()["hooks"][0]["active"] is False


def test_timeout_releases_capacity_and_opens_only_that_hook_circuit():
    release = Event()
    stuck_calls: list[str] = []
    healthy_calls: list[str] = []

    def stuck_callback(value, **_context):
        stuck_calls.append(value)
        release.wait(5)
        return value

    for index in range(8):
        _register_internal_hook_for_tests(
            "after_tool_result",
            InternalHook(
                name=f"stuck-{index}",
                source="telemetry.test",
                version="1",
                callback=stuck_callback,
                order=index,
                timeout_seconds=0.01,
                circuit_breaker_seconds=0.01,
            ),
        )
    _register_internal_hook_for_tests(
        "after_tool_result",
        InternalHook(
            name="healthy",
            source="telemetry.test",
            version="1",
            callback=lambda value, **_context: healthy_calls.append(value) or value,
            order=100,
        ),
    )

    first_value, first_trace = run_internal_hooks(
        "after_tool_result",
        "payload",
        tool_name="read_file",
        tool_use_id="tool-1",
    )
    second_value, second_trace = run_internal_hooks(
        "after_tool_result",
        "payload",
        tool_name="read_file",
        tool_use_id="tool-2",
    )

    assert first_value == second_value == "payload"
    assert [item["status"] for item in first_trace] == [
        *(["timeout"] * 8),
        "completed",
    ]
    assert [item["status"] for item in second_trace] == [
        *(["circuit_open"] * 8),
        "completed",
    ]
    assert stuck_calls == ["payload"] * 8
    assert healthy_calls == ["payload", "payload"]
    status = internal_hook_registry_status()
    stuck_statuses = [
        item for item in status["hooks"] if item["name"].startswith("stuck-")
    ]
    assert len(stuck_statuses) == 8
    assert all(item["circuit_open"] is True for item in stuck_statuses)
    assert all(item["counts"]["timeout"] == 1 for item in stuck_statuses)
    assert all(item["counts"]["circuit_open"] == 1 for item in stuck_statuses)

    release.set()
    time.sleep(0.03)
    _value, recovered_trace = run_internal_hooks(
        "after_tool_result",
        "payload",
        tool_name="read_file",
        tool_use_id="tool-3",
    )
    assert [item["status"] for item in recovered_trace] == ["completed"] * 9


def test_worker_start_failure_is_traced_and_does_not_leave_hook_active(monkeypatch):
    _register_internal_hook_for_tests(
        "after_tool_result",
        InternalHook(
            name="worker-start",
            source="telemetry.test",
            version="1",
            callback=lambda value, **_context: value,
        ),
    )

    def reject_start(_thread):
        raise RuntimeError("thread quota reached")

    monkeypatch.setattr(internal_hooks_module.Thread, "start", reject_start)
    value, trace = run_internal_hooks(
        "after_tool_result",
        "payload",
        tool_name="read_file",
        tool_use_id="tool-1",
    )

    assert value == "payload"
    assert trace[0]["status"] == "failed"
    assert "thread quota reached" in trace[0]["error"]
    hook_status = internal_hook_registry_status()["hooks"][0]
    assert hook_status["active"] is False
