"""Tests for MCP tool-handler transport-closure auto-reconnect."""
import json
import threading
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# _is_transport_closed_error — unit coverage
# ---------------------------------------------------------------------------


def test_is_transport_closed_detects_stale_pipe_variants():
    """Stdio/AnyIO stale-pipe failures usually surface as closed-resource
    or broken-pipe text, not an HTTP session-expired JSON-RPC error."""
    from tools.mcp_tool import _is_transport_closed_error
    assert _is_transport_closed_error(RuntimeError("ClosedResourceError")) is True
    assert _is_transport_closed_error(RuntimeError("closed resource in MCP child")) is True
    assert _is_transport_closed_error(RuntimeError("transport is closed")) is True
    assert _is_transport_closed_error(RuntimeError("Broken pipe while writing request")) is True
    assert _is_transport_closed_error(RuntimeError("End of file from MCP server")) is True


def test_is_transport_closed_is_case_insensitive():
    """Match uses lower-cased comparison so servers that emit the
    message in different cases (SDK formatter quirks) still trigger."""
    from tools.mcp_tool import _is_transport_closed_error
    assert _is_transport_closed_error(RuntimeError("CLOSED RESOURCE")) is True


def test_is_transport_closed_rejects_session_and_application_errors():
    """Only transport failures trigger; application/session text does not."""
    from tools.mcp_tool import _is_transport_closed_error
    assert _is_transport_closed_error(RuntimeError("Tool failed to execute")) is False
    assert _is_transport_closed_error(ValueError("Missing parameter")) is False
    assert _is_transport_closed_error(Exception("Connection refused")) is False
    assert _is_transport_closed_error(RuntimeError("Invalid or expired session")) is False
    assert _is_transport_closed_error(RuntimeError("Session expired")) is False
    assert _is_transport_closed_error(RuntimeError("Unknown session")) is False
    # 401 is handled by the sibling _is_auth_error path, not here.
    assert _is_transport_closed_error(RuntimeError("401 Unauthorized")) is False


def test_is_transport_closed_rejects_interrupted_error():
    """InterruptedError is user cancellation and must never reconnect."""
    from tools.mcp_tool import _is_transport_closed_error
    assert _is_transport_closed_error(InterruptedError()) is False
    assert _is_transport_closed_error(InterruptedError("transport is closed")) is False


def test_is_transport_closed_rejects_empty_message():
    """Bare exceptions with no message shouldn't match."""
    from tools.mcp_tool import _is_transport_closed_error
    assert _is_transport_closed_error(RuntimeError("")) is False
    assert _is_transport_closed_error(Exception()) is False


# ---------------------------------------------------------------------------
# Handler integration — verify the recovery plumbing wires end-to-end
# ---------------------------------------------------------------------------


def _install_stub_server(name: str = "wpcom"):
    """Register a server stub that replaces its client after reconnect."""
    from tools import mcp_tool

    mcp_tool._ensure_mcp_loop()

    server = MagicMock()
    server.name = name

    ready_flag = threading.Event()
    ready_flag.set()

    class _ReadyAdapter:
        def is_set(self):
            return ready_flag.is_set()

        def clear(self):
            ready_flag.clear()

        def set(self):
            ready_flag.set()

    server._ready = _ReadyAdapter()

    # The production reconnect path must not treat the old client object as
    # fresh, so this test double swaps in a distinct object when requested.
    reconnect_flag = threading.Event()

    class _EventAdapter:
        def set(self):
            reconnect_flag.set()
            old_session = server.session
            new_session = MagicMock()
            for method_name in (
                "call_tool",
                "list_resources",
                "read_resource",
                "list_prompts",
                "get_prompt",
            ):
                if hasattr(old_session, method_name):
                    setattr(new_session, method_name, getattr(old_session, method_name))
            server.session = new_session
            ready_flag.set()

    server._reconnect_event = _EventAdapter()

    # session attr must be truthy for the handler's initial check
    # (``if not server or not server.session``) and for the post-
    # reconnect readiness probe (``srv.session is not None``).
    server.session = MagicMock()
    return server, reconnect_flag


def test_call_tool_handler_reconnects_on_transport_closed(monkeypatch, tmp_path):
    """A closed transport reconnects once and returns the retry result."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    server, reconnect_flag = _install_stub_server("wpcom")
    mcp_tool._servers["wpcom"] = server
    mcp_tool._server_error_counts.pop("wpcom", None)

    # First call reports transport closure; the post-reconnect call succeeds.
    call_count = {"n": 0}

    async def _call_sequence(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("ClosedResourceError: transport is closed")
        # Second call: mimic the MCP SDK's structured success response.
        result = MagicMock()
        result.is_error = False
        result.content = [MagicMock(type="text", text="tool completed")]
        result.structured_content = None
        return result

    server.session.call_tool = _call_sequence

    try:
        handler = _make_tool_handler("wpcom", "wpcom-mcp-content-authoring", 10.0)
        out = handler({"slug": "hello"})
        parsed = json.loads(out)
        # Retry succeeded; no error surfaced to caller.
        assert "error" not in parsed, (
            f"Expected retry to succeed after reconnect; got: {parsed}"
        )
        # _reconnect_event was signalled exactly once.
        assert reconnect_flag.is_set(), (
            "Handler did not reconnect after the transport closed."
        )
        # Exactly 2 call attempts (original + one retry).
        assert call_count["n"] == 2, (
            f"Expected 1 original + 1 retry = 2 calls; got {call_count['n']}"
        )
    finally:
        mcp_tool._servers.pop("wpcom", None)
        mcp_tool._server_error_counts.pop("wpcom", None)


def test_transport_closed_retry_waits_for_new_client(monkeypatch, tmp_path):
    """Regression for long-lived MCP transports.

    If the reconnect helper only checks readiness and a non-null client, it can
    return while the client still points at the stale transport. The retry then
    hits the same closed transport
    and the circuit breaker eventually reports the server as unreachable. The
    handler must wait for a distinct client object before retrying.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    mcp_tool._ensure_mcp_loop()
    server = MagicMock()
    server.name = "hindsight"
    ready_flag = threading.Event()
    ready_flag.set()

    class _ReadyAdapter:
        def is_set(self):
            return ready_flag.is_set()

        def clear(self):
            ready_flag.clear()

        def set(self):
            ready_flag.set()

    old_session = MagicMock()

    async def _old_call(*a, **kw):
        raise RuntimeError("ClosedResourceError: connection closed")

    old_session.call_tool = _old_call
    new_session = MagicMock()

    async def _new_call(*a, **kw):
        result = MagicMock()
        result.is_error = False
        result.content = [MagicMock(type="text", text="bank ok")]
        result.structured_content = None
        return result

    new_session.call_tool = _new_call
    server.session = old_session
    server._ready = _ReadyAdapter()

    class _ReconnectAdapter:
        def set(self):
            server.session = new_session
            ready_flag.set()

    server._reconnect_event = _ReconnectAdapter()
    mcp_tool._servers["hindsight"] = server
    mcp_tool._server_error_counts.pop("hindsight", None)

    try:
        handler = _make_tool_handler("hindsight", "get_bank", 10.0)
        parsed = json.loads(handler({}))
        assert parsed.get("result") == "bank ok", parsed
        assert mcp_tool._server_error_counts.get("hindsight", 0) == 0
    finally:
        mcp_tool._servers.pop("hindsight", None)
        mcp_tool._server_error_counts.pop("hindsight", None)


def test_call_tool_handler_application_error_falls_through(
    monkeypatch, tmp_path
):
    """An application exception must not trigger transport reconnect."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    server, reconnect_flag = _install_stub_server("srv")
    mcp_tool._servers["srv"] = server
    mcp_tool._server_error_counts.pop("srv", None)

    async def _raises(*a, **kw):
        raise RuntimeError("Tool execution failed: unrelated error")

    server.session.call_tool = _raises

    try:
        handler = _make_tool_handler("srv", "mytool", 10.0)
        out = handler({"arg": "v"})
        parsed = json.loads(out)
        # Generic error path surfaced the failure.
        assert "MCP call failed" in parsed.get("error", "")
        # Reconnect was NOT triggered for this unrelated failure.
        assert not reconnect_flag.is_set(), (
            "Reconnect must not fire for application errors."
        )
    finally:
        mcp_tool._servers.pop("srv", None)
        mcp_tool._server_error_counts.pop("srv", None)


def test_transport_closed_handler_returns_none_without_loop(monkeypatch):
    """Defensive: if the MCP loop isn't running (cold start / shutdown
    race), the handler must fall through cleanly instead of hanging
    or raising."""
    from tools import mcp_tool
    from tools.mcp_tool import _handle_transport_closed_and_retry

    # Install a server stub but make the event loop unavailable.
    server = MagicMock()
    server._reconnect_event = MagicMock()
    server._ready = MagicMock()
    server._ready.is_set = MagicMock(return_value=True)
    server.session = MagicMock()
    mcp_tool._servers["srv-noloop"] = server

    monkeypatch.setattr(mcp_tool, "_mcp_loop", None)

    try:
        out = _handle_transport_closed_and_retry(
            "srv-noloop",
            RuntimeError("ClosedResourceError: transport is closed"),
            lambda: '{"ok": true}',
            "tools/call",
        )
        assert out is None, (
            "Without an event loop, reconnect must fall through cleanly."
        )
    finally:
        mcp_tool._servers.pop("srv-noloop", None)


def test_transport_closed_handler_returns_none_without_server_record():
    """If the server has been torn down / isn't in _servers, fall
    through cleanly because there is nothing to reconnect."""
    from tools.mcp_tool import _handle_transport_closed_and_retry
    out = _handle_transport_closed_and_retry(
        "does-not-exist",
        RuntimeError("ClosedResourceError: transport is closed"),
        lambda: '{"ok": true}',
        "tools/call",
    )
    assert out is None


def test_transport_closed_handler_returns_none_when_retry_also_fails(
    monkeypatch, tmp_path
):
    """If the retry after reconnect also raises, fall through to the
    generic error path (don't loop forever, don't mask the second
    failure)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _handle_transport_closed_and_retry

    server, _ = _install_stub_server("srv-retry-fail")
    mcp_tool._servers["srv-retry-fail"] = server

    def _retry_raises():
        raise RuntimeError("retry blew up too")

    try:
        out = _handle_transport_closed_and_retry(
            "srv-retry-fail",
            RuntimeError("ClosedResourceError: transport is closed"),
            _retry_raises,
            "tools/call",
        )
        assert out is None, (
            "When the retry itself fails, the handler must return None "
            "so the caller's generic error path runs without a retry loop."
        )
    finally:
        mcp_tool._servers.pop("srv-retry-fail", None)


# ---------------------------------------------------------------------------
# Parallel coverage for resources/list, resources/read, prompts/list,
# prompts/get — all four handlers share the same exception path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler_factory, handler_kwargs, session_method, op_label",
    [
        ("_make_list_resources_handler", {"tool_timeout": 10.0}, "list_resources", "list_resources"),
        ("_make_read_resource_handler", {"tool_timeout": 10.0}, "read_resource", "read_resource"),
        ("_make_list_prompts_handler", {"tool_timeout": 10.0}, "list_prompts", "list_prompts"),
        ("_make_get_prompt_handler", {"tool_timeout": 10.0}, "get_prompt", "get_prompt"),
    ],
)
def test_non_tool_handlers_also_reconnect_on_transport_closed(
    monkeypatch, tmp_path, handler_factory, handler_kwargs, session_method, op_label
):
    """All four non-``tools/call`` MCP handlers share the recovery
    pattern and must reconnect the same way on transport closure."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool

    server, reconnect_flag = _install_stub_server(f"srv-{op_label}")
    mcp_tool._servers[f"srv-{op_label}"] = server
    mcp_tool._server_error_counts.pop(f"srv-{op_label}", None)

    call_count = {"n": 0}

    async def _sequence(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("ClosedResourceError: transport is closed")
        # Return something with the shapes each handler expects.
        # Explicitly set primitive attrs — MagicMock's default auto-attr
        # behaviour surfaces ``MagicMock`` values for optional fields
        # like ``description``, which break ``json.dumps`` downstream.
        result = MagicMock()
        result.resources = []
        result.prompts = []
        result.contents = []
        result.messages = []  # get_prompt
        result.description = None  # get_prompt optional field
        return result

    setattr(server.session, session_method, _sequence)

    factory = getattr(mcp_tool, handler_factory)
    # list_resources / list_prompts take (server_name, timeout).
    # read_resource / get_prompt take the same signature.
    try:
        handler = factory(f"srv-{op_label}", **handler_kwargs)
        if op_label == "read_resource":
            out = handler({"uri": "file://foo"})
        elif op_label == "get_prompt":
            out = handler({"name": "p1"})
        else:
            out = handler({})
        parsed = json.loads(out)
        assert "error" not in parsed, (
            f"{op_label}: expected retry success, got {parsed}"
        )
        assert reconnect_flag.is_set(), (
            f"{op_label}: reconnect should fire for transport closure"
        )
        assert call_count["n"] == 2, (
            f"{op_label}: expected 1 original + 1 retry"
        )
    finally:
        mcp_tool._servers.pop(f"srv-{op_label}", None)
        mcp_tool._server_error_counts.pop(f"srv-{op_label}", None)
