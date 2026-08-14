from __future__ import annotations

import json
import multiprocessing
import pickle
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from agent.tool_dispatch_helpers import _plan_tool_batch_execution
from hermes_services.tool_contract import (
    annotate_tool_definitions,
    batch_execution_mode,
    contract_timeout_seconds,
    ToolExecutionContract,
    register_tool_contract,
    reset_tool_contracts_for_tests,
)
from hermes_services.tool_isolation import default_tool_timeout_seconds
from agent.tool_executor import (
    _resolve_concurrent_tool_timeout,
    _run_with_tool_contract_timeout,
    _tool_contract_approval_block,
    _tool_contract_event_metadata,
)
from tests.hermes_services.tool_isolation_test_support import (
    async_echo,
    delayed_write,
    echo,
    never_return,
    spawn_delayed_descendant,
    spawn_detached_delayed_descendant,
)


def _call(identifier: str, name: str, arguments: str = "{}"):
    return SimpleNamespace(
        id=identifier,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def teardown_function():
    reset_tool_contracts_for_tests()


def test_default_registry_timeout_reserves_bounded_spawn_budget(monkeypatch):
    """A short env override must not kill a worker during normal spawn/import."""
    monkeypatch.setenv("HERMES_TOOL_TIMEOUT_S", "0.1")
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "3")

    # Explicit ToolExecutionContract values are tested separately and remain
    # exact; this floor applies only to ordinary registry-tool defaults.
    assert default_tool_timeout_seconds() == 4.0


def test_default_registry_exposes_process_safe_identity_without_pickling_handlers():
    """Every built-in entry crosses isolation as metadata, never a callable."""
    import model_tools  # noqa: F401 - imports the production registry modules
    from tools.registry import registry

    for name in registry.get_all_tool_names():
        entry = registry.get_entry(name)
        if entry is None or str(entry.toolset).startswith("mcp-"):
            continue
        identity = registry.get_isolation_identity(name)
        assert identity["tool_name"] == name
        assert identity["handler_module"]
        if identity["isolation_mode"] == "parent_runtime":
            # Browser/terminal/desktop handlers intentionally retain the
            # owning runtime; their state cannot be reconstructed in a child.
            assert identity["isolation_contract"] == "parent_cooperative"
            continue
        assert identity["resolution"] in {
            "registry_name",
            "module_qualname",
            "unresolvable",
        }
        assert identity["isolation_mode"] == "registry_child"
        assert identity["isolation_contract"] in {
            "child_rehydratable",
            "unsupported",
        }
        # This is intentionally the identity envelope, not entry.handler.
        pickle.dumps(identity, protocol=pickle.HIGHEST_PROTOCOL)


def test_production_lambda_tools_dispatch_in_a_fresh_worker(tmp_path):
    """Core lambda registrations resolve by name in the spawned registry."""
    import model_tools  # noqa: F401
    from tools.registry import registry

    source = tmp_path / "isolation-read.txt"
    source.write_text("registry isolation", encoding="utf-8")
    read_result = registry.dispatch(
        "read_file",
        {"path": str(source)},
        isolate=True,
        task_id="registry-isolation-test",
    )
    assert "registry isolation" in read_result

    # These providers may be unavailable in a hermetic environment, but the
    # real production dispatch must return a provider result/error rather than
    # a PicklingError or a thread fallback.
    web_result = registry.dispatch(
        "web_search",
        {"query": "hermes registry isolation probe", "limit": 1},
        isolate=True,
    )
    assert "PicklingError" not in web_result
    assert "tool_isolation_resolution" not in web_result

    code_result = registry.dispatch(
        "execute_code",
        {"code": "print('registry execute_code isolation probe')"},
        isolate=True,
        task_id="registry-isolation-test",
        enabled_tools=["execute_code"],
    )
    assert "PicklingError" not in code_result
    assert "tool_isolation_resolution" not in code_result


def test_import_registered_lambda_and_closure_dispatch_in_a_fresh_worker():
    from tests.hermes_services.registry_isolation_test_support import (
        CLOSURE_TOOL_NAME,
        LAMBDA_TOOL_NAME,
    )
    from tools.registry import registry

    assert registry.dispatch(
        LAMBDA_TOOL_NAME, {"value": "ok"}, isolate=True,
    ) == "lambda:ok"
    assert registry.dispatch(
        CLOSURE_TOOL_NAME, {"value": "ok"}, isolate=True,
    ) == "closure:ok"


def test_runtime_lambda_and_closure_have_explicit_non_retryable_errors():
    from tools.registry import registry

    lambda_name = f"runtime_lambda_{uuid.uuid4().hex}"
    closure_name = f"runtime_closure_{uuid.uuid4().hex}"

    def make_handler(prefix):
        def _handler(args, **_kwargs):
            return f"{prefix}:{args.get('value', '')}"

        return _handler

    registry.register(
        name=lambda_name,
        toolset="test-runtime-identity",
        schema={"name": lambda_name, "parameters": {"type": "object"}},
        handler=lambda args, **_kwargs: args.get("value", ""),
    )
    registry.register(
        name=closure_name,
        toolset="test-runtime-identity",
        schema={"name": closure_name, "parameters": {"type": "object"}},
        handler=make_handler("closure"),
    )
    try:
        for name in (lambda_name, closure_name):
            result = json.loads(registry.dispatch(name, {"value": "x"}, isolate=True))
            assert result["error_type"] == "tool_isolation_resolution"
            assert result["retryable"] is False
            assert "PicklingError" not in result["error"]
    finally:
        registry.deregister(lambda_name)
        registry.deregister(closure_name)


def test_dynamic_mcp_handler_stays_in_owner_runtime():
    from tools.registry import registry

    name = f"mcp__runtime_probe__{uuid.uuid4().hex}"
    registry.register(
        name=name,
        toolset="mcp-runtime-probe",
        schema={"name": name, "parameters": {"type": "object"}},
        handler=lambda args, **_kwargs: "should not run in parent",
    )
    try:
        identity = registry.get_isolation_identity(name)
        assert identity["isolation_mode"] == "parent_runtime"
        assert registry.dispatch(name, {}, isolate=True) == "should not run in parent"
    finally:
        registry.deregister(name)


def test_stateful_browser_and_terminal_calls_stay_in_the_owner_runtime(monkeypatch):
    """Stateful sessions remain coherent across sequential registry calls."""
    import model_tools  # noqa: F401
    import tools.browser_tool as browser_module
    import tools.close_terminal_tool as close_module
    import tools.terminal_tool as terminal_module
    from tools.registry import registry

    state: dict[str, object] = {}

    def fake_navigate(url, task_id=None):
        state["url"] = url
        state["task_id"] = task_id
        return json.dumps({"ok": True, "url": url})

    def fake_snapshot(full=False, task_id=None, user_task=None):
        return json.dumps({"url": state.get("url"), "task_id": task_id})

    def fake_terminal(**kwargs):
        state["command"] = kwargs.get("command")
        return json.dumps({"ok": True, "command": kwargs.get("command")})

    class _ProcessRegistry:
        def request_close_terminal(self, process_id):
            state["closed"] = process_id
            return {"closed": process_id}

    monkeypatch.setattr(browser_module, "browser_navigate", fake_navigate)
    monkeypatch.setattr(browser_module, "browser_snapshot", fake_snapshot)
    monkeypatch.setattr(terminal_module, "terminal_tool", fake_terminal)
    monkeypatch.setattr(close_module, "process_registry", _ProcessRegistry())

    assert registry.get_isolation_identity("browser_navigate")["isolation_mode"] == "parent_runtime"
    assert registry.dispatch(
        "browser_navigate", {"url": "https://example.test"},
        isolate=True, task_id="browser-state",
    ).find("example.test") >= 0
    snapshot = registry.dispatch(
        "browser_snapshot", {}, isolate=True, task_id="browser-state",
    )
    assert "example.test" in snapshot

    assert registry.dispatch(
        "terminal", {"command": "printf state"},
        isolate=True, task_id="terminal-state",
    ).find("printf state") >= 0
    terminal_read = registry.dispatch(
        "read_terminal", {}, isolate=True,
        callback=lambda **_kwargs: json.dumps({"text": str(state["command"])}),
    )
    assert "printf state" in terminal_read
    terminal_close = registry.dispatch(
        "close_terminal", {"process_id": "terminal-state"}, isolate=True,
    )
    assert "terminal-state" in terminal_close


def test_independent_path_scoped_calls_keep_overlap_aware_parallelism():
    calls = [
        _call("read-1", "read_file", '{"path":"a"}'),
        _call("write-1", "write_file", '{"path":"b"}'),
        _call("read-2", "read_file", '{"path":"c"}'),
    ]

    plan = _plan_tool_batch_execution(calls)

    assert len(plan) == 1
    assert plan[0][0] == "parallel"
    assert [call.id for call in plan[0][1]] == ["read-1", "write-1", "read-2"]


def test_explicit_parallel_contract_can_enable_a_custom_read_only_tool():
    register_tool_contract(
        "custom_lookup",
        ToolExecutionContract(
            execution_mode="parallel",
            side_effect_class="read",
        ),
    )
    calls = [_call("one", "custom_lookup"), _call("two", "custom_lookup")]

    plan = _plan_tool_batch_execution(calls)

    assert len(plan) == 1
    assert plan[0][0] == "parallel"


def test_malformed_arguments_force_whole_batch_to_sequential():
    calls = [
        _call("one", "read_file", '{"path":"a"}'),
        _call("broken", "read_file", "{"),
        _call("two", "read_file", '{"path":"b"}'),
    ]

    plan = _plan_tool_batch_execution(calls)

    assert plan == [("sequential", calls)]


def test_explicit_contract_timeout_tightens_hard_deadline(monkeypatch):
    register_tool_contract(
        "bounded_lookup",
        ToolExecutionContract(
            execution_mode="parallel",
            side_effect_class="read",
            timeout_seconds=2.5,
        ),
    )
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "30")

    assert contract_timeout_seconds(["read_file", "bounded_lookup"]) == 2.5
    assert _resolve_concurrent_tool_timeout(["read_file", "bounded_lookup"]) == 2.5


def test_contract_approval_uses_existing_human_gate(monkeypatch):
    register_tool_contract(
        "sensitive_lookup",
        ToolExecutionContract(
            execution_mode="sequential",
            side_effect_class="external",
            requires_approval=True,
        ),
    )
    seen = []
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: "callback")
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda tool_name, reason, **kwargs: seen.append((tool_name, reason, kwargs))
        or {"approved": False, "message": "approval denied"},
    )

    assert _tool_contract_approval_block("sensitive_lookup") == "approval denied"
    assert seen[0][0] == "sensitive_lookup"
    assert seen[0][2]["rule_key"] == "tool_contract:sensitive_lookup"


def test_irreversible_effect_metadata_uses_existing_human_gate(monkeypatch):
    from tools.registry import registry

    name = f"irreversible_test_{uuid.uuid4().hex}"
    registry.register(
        name=name,
        toolset="effect-metadata-test",
        schema={"description": name, "parameters": {"type": "object"}},
        handler=lambda: None,
        effect_metadata={
            "durability": "external",
            "external_boundary": "payment-provider",
            "reversibility": "irreversible",
        },
    )
    seen = []
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda tool_name, reason, **kwargs: seen.append((tool_name, reason, kwargs))
        or {"approved": False, "message": "approval denied"},
    )
    try:
        assert _tool_contract_approval_block(name) == "approval denied"
        assert seen[0][2]["rule_key"] == f"irreversible_effect:{name}"
    finally:
        registry.deregister(name)


def test_side_effect_class_prevents_unsafe_parallel_execution():
    register_tool_contract(
        "mislabelled_writer",
        ToolExecutionContract(
            execution_mode="parallel",
            side_effect_class="write",
        ),
    )

    assert batch_execution_mode(["mislabelled_writer", "read_file"]) == "sequential"


def _register_bounded_test_tool(name, handler, *, is_async=False, timeout=1.5):
    from tools.registry import registry

    registry.register(
        name=name,
        toolset="hard-timeout-test",
        schema={"description": name, "parameters": {"type": "object"}},
        handler=handler,
        is_async=is_async,
    )
    register_tool_contract(
        name,
        ToolExecutionContract(
            execution_mode="sequential",
            side_effect_class="read",
            timeout_seconds=timeout,
        ),
    )
    return registry


def _wait_for_path(path, *, timeout=3.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


def _pid_exists(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        try:
            import os

            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def _assert_descendant_stopped(pid: int, target) -> None:
    deadline = time.monotonic() + 3
    while _pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _pid_exists(pid)
    # The descendant's scheduled write is after every timeout/cancel asserted
    # here, so this catches both escaped processes and late side effects.
    time.sleep(1.3)
    assert not target.exists()


def test_hard_deadline_terminates_a_never_returning_handler(tmp_path):
    registry = _register_bounded_test_tool("never_return", never_return)
    started = tmp_path / "started.txt"
    before_children = {child.pid for child in multiprocessing.active_children()}
    started_at = time.monotonic()
    try:
        result = registry.dispatch("never_return", {"started_path": str(started)})
    finally:
        registry.deregister("never_return")

    elapsed = time.monotonic() - started_at
    assert elapsed < 3
    assert started.exists()
    assert "hard 1.5s deadline" in json.loads(result)["error"]
    assert {child.pid for child in multiprocessing.active_children()} == before_children


def test_hard_deadline_includes_worker_bootstrap_time():
    registry = _register_bounded_test_tool(
        "bootstrap_deadline_probe",
        echo,
        timeout=0.01,
    )
    started_at = time.monotonic()
    try:
        result = registry.dispatch("bootstrap_deadline_probe", {"value": "too-late"})
    finally:
        registry.deregister("bootstrap_deadline_probe")

    elapsed = time.monotonic() - started_at
    error = json.loads(result)["error"]
    assert "hard 0.01s deadline" in error
    # Process bootstrap is scheduler-sensitive on shared CI runners; the
    # contract is the deadline error and bounded return, not a sub-second
    # wall-clock guarantee for spawning a fresh worker.
    assert elapsed < 2


def test_hard_deadline_prevents_a_late_result_and_late_side_effect(tmp_path):
    registry = _register_bounded_test_tool("delayed_write", delayed_write)
    target = tmp_path / "must-not-exist.txt"
    try:
        result = registry.dispatch(
            "delayed_write",
            {"delay": 2.2, "target": str(target)},
        )
    finally:
        registry.deregister("delayed_write")

    assert "hard 1.5s deadline" in json.loads(result)["error"]
    time.sleep(2.3)
    assert not target.exists()


def test_hard_deadline_terminates_descendant_before_late_side_effect(tmp_path):
    registry = _register_bounded_test_tool(
        "timeout_descendant_tree",
        spawn_delayed_descendant,
    )
    target = tmp_path / "descendant-timeout-must-not-exist.txt"
    descendant_started = tmp_path / "descendant-timeout-pid.txt"
    try:
        result = registry.dispatch(
            "timeout_descendant_tree",
            {
                "target": str(target),
                "descendant_started_path": str(descendant_started),
                "delay": 2.2,
            },
        )
        _wait_for_path(descendant_started)
        assert "hard 1.5s deadline" in json.loads(result)["error"]
        _assert_descendant_stopped(int(descendant_started.read_text()), target)
    finally:
        registry.deregister("timeout_descendant_tree")


def test_hard_deadline_terminates_detached_descendant_before_late_side_effect(tmp_path):
    registry = _register_bounded_test_tool(
        "timeout_detached_descendant_tree",
        spawn_detached_delayed_descendant,
    )
    target = tmp_path / "detached-timeout-must-not-exist.txt"
    descendant_started = tmp_path / "detached-timeout-pid.txt"
    try:
        result = registry.dispatch(
            "timeout_detached_descendant_tree",
            {
                "target": str(target),
                "descendant_started_path": str(descendant_started),
                "delay": 2.2,
            },
        )
        _wait_for_path(descendant_started)
        assert "hard 1.5s deadline" in json.loads(result)["error"]
        _assert_descendant_stopped(int(descendant_started.read_text()), target)
    finally:
        registry.deregister("timeout_detached_descendant_tree")


def test_interrupt_terminates_isolated_handler_before_deadline(tmp_path):
    from tools.interrupt import set_interrupt

    registry = _register_bounded_test_tool(
        "cancelled_isolated_handler",
        delayed_write,
        timeout=30,
    )
    target = tmp_path / "cancelled-must-not-exist.txt"
    child_started = tmp_path / "cancelled-child-started.txt"
    started = threading.Event()
    result: list[str] = []
    before_children = {child.pid for child in multiprocessing.active_children()}

    def dispatch() -> None:
        started.set()
        result.append(registry.dispatch(
            "cancelled_isolated_handler",
            {
                "delay": 5,
                "target": str(target),
                "started_path": str(child_started),
            },
        ))

    worker = threading.Thread(target=dispatch, daemon=True)
    worker.start()
    assert started.wait(timeout=1)
    assert worker.ident is not None
    deadline = time.monotonic() + 3
    while not child_started.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_started.exists()
    set_interrupt(True, worker.ident)
    try:
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert "cancelled during isolated execution" in json.loads(result[0])["error"]
        assert {child.pid for child in multiprocessing.active_children()} == before_children
        time.sleep(0.3)
        assert not target.exists()
    finally:
        set_interrupt(False, worker.ident)
        registry.deregister("cancelled_isolated_handler")


def test_interrupt_terminates_descendant_before_late_side_effect(tmp_path):
    from tools.interrupt import set_interrupt

    registry = _register_bounded_test_tool(
        "cancelled_descendant_tree",
        spawn_delayed_descendant,
        timeout=30,
    )
    target = tmp_path / "descendant-cancelled-must-not-exist.txt"
    descendant_started = tmp_path / "descendant-cancelled-pid.txt"
    handler_started = tmp_path / "descendant-handler-started.txt"
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(registry.dispatch(
            "cancelled_descendant_tree",
            {
                "target": str(target),
                "descendant_started_path": str(descendant_started),
                "handler_started_path": str(handler_started),
                "delay": 1.2,
            },
        )),
        daemon=True,
    )
    worker.start()
    assert worker.ident is not None
    try:
        _wait_for_path(descendant_started)
        set_interrupt(True, worker.ident)
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert "cancelled during isolated execution" in json.loads(result[0])["error"]
        _assert_descendant_stopped(int(descendant_started.read_text()), target)
    finally:
        set_interrupt(False, worker.ident)
        registry.deregister("cancelled_descendant_tree")


@pytest.mark.parametrize(
    ("name", "handler", "is_async", "expected"),
    [
        ("bounded_echo", echo, False, "echo:ok"),
        ("bounded_async_echo", async_echo, True, "async:ok"),
    ],
)
def test_hard_deadline_preserves_successful_sync_and_async_results(
    name,
    handler,
    is_async,
    expected,
):
    registry = _register_bounded_test_tool(
        name,
        handler,
        is_async=is_async,
        timeout=2,
    )
    try:
        assert registry.dispatch(name, {"value": "ok"}) == expected
    finally:
        registry.deregister(name)


def test_side_effecting_tools_cannot_claim_a_hard_deadline():
    with pytest.raises(ValueError, match="none/read"):
        ToolExecutionContract(
            execution_mode="sequential",
            side_effect_class="write",
            timeout_seconds=1,
        )


def test_contract_is_model_visible_without_nonstandard_tool_envelope_fields():
    register_tool_contract(
        "streaming_lookup",
        ToolExecutionContract(
            execution_mode="parallel",
            side_effect_class="read",
            timeout_seconds=4,
            supports_progress=True,
            output_policy="artifact",
        ),
    )

    definitions = annotate_tool_definitions([{
        "type": "function",
        "function": {
            "name": "streaming_lookup",
            "description": "Look up a resource.",
            "parameters": {"type": "object", "properties": {}},
        },
    }])

    assert set(definitions[0]) == {"type", "function"}
    description = definitions[0]["function"]["description"]
    assert "mode=parallel" in description
    assert "side_effect=read" in description
    assert "timeout=hard-deadline-after-4s" in description
    assert "timeout_policy=hard" in description
    assert "progress=supported" in description
    assert "output=artifact" in description


def test_final_tool_search_bridge_definitions_receive_execution_contracts(monkeypatch):
    import model_tools
    from tools import tool_search

    deferred_definition = {
        "type": "function",
        "function": {
            "name": "deferred_probe",
            "description": "Probe a deferred capability.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    monkeypatch.setattr(
        model_tools.registry,
        "get_definitions",
        lambda _names, quiet=False: [deferred_definition],
    )
    monkeypatch.setattr(
        tool_search,
        "load_config",
        lambda: tool_search.ToolSearchConfig(
            enabled="on",
            threshold_pct=10,
            search_default_limit=5,
            max_search_limit=20,
        ),
    )
    monkeypatch.setattr(
        tool_search,
        "is_deferrable_tool_name",
        lambda name: name == "deferred_probe",
    )

    definitions = model_tools._compute_tool_definitions(
        enabled_toolsets=[],
        quiet_mode=True,
    )

    assert {definition["function"]["name"] for definition in definitions} == {
        "tool_search",
        "tool_describe",
        "tool_call",
    }
    assert all(
        "Hermes execution contract:" in definition["function"]["description"]
        for definition in definitions
    )


def test_advertised_contract_binding_blocks_runtime_registration_drift():
    from tools.registry import registry

    called = []

    def original_handler(_args, **_kwargs):
        called.append("original")
        return "original"

    def replacement_handler(_args, **_kwargs):
        called.append("replacement")
        return "replacement"

    schema = {
        "description": "Binding probe.",
        "parameters": {"type": "object", "properties": {}},
    }
    registry.register(
        name="contract_binding_probe",
        toolset="contract-binding-test",
        schema=schema,
        handler=original_handler,
    )
    try:
        register_tool_contract(
            "contract_binding_probe",
            ToolExecutionContract(
                execution_mode="sequential",
                side_effect_class="read",
            ),
        )
        definitions = annotate_tool_definitions(
            registry.get_definitions({"contract_binding_probe"})
        )
        assert definitions
        snapshot_generation = registry._generation
        agent = SimpleNamespace(_tool_snapshot_generation=snapshot_generation)

        assert _run_with_tool_contract_timeout(
            agent,
            "contract_binding_probe",
            lambda: registry.dispatch("contract_binding_probe", {}),
        ) == "original"
        metadata = _tool_contract_event_metadata("contract_binding_probe")
        assert metadata["execution_contract"]["side_effect_class"] == "read"
        assert metadata["execution_contract_binding"]["registry_generation"] == snapshot_generation

        registry.register(
            name="contract_binding_probe",
            toolset="contract-binding-test",
            schema=schema,
            handler=replacement_handler,
        )
        with pytest.raises(RuntimeError, match="registration changed"):
            _run_with_tool_contract_timeout(
                agent,
                "contract_binding_probe",
                lambda: registry.dispatch("contract_binding_probe", {}),
            )
        assert called == ["original"]
    finally:
        registry.deregister("contract_binding_probe")
