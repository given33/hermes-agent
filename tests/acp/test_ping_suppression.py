"""Tests for acp_adapter.entry._BenignProbeMethodFilter.

Covers both the isolated filter logic and the full end-to-end path where a
client sends a bare JSON-RPC ``ping`` request over stdio and the acp runtime
surfaces the resulting ``RequestError`` via ``logging.exception("Background
task failed", ...)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from io import StringIO

import pytest

from acp.exceptions import RequestError

from acp_adapter.entry import _BenignProbeMethodFilter


@pytest.fixture
def event_loop_policy():
    """Use a pipe-compatible loop for the stdio-shaped ACP test on Windows."""
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


# -- Unit tests on the filter itself ----------------------------------------


def _make_record(msg: str, exc: BaseException | None) -> logging.LogRecord:
    record = logging.LogRecord(
        name="root",
        level=logging.ERROR,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=(),
        exc_info=(type(exc), exc, exc.__traceback__) if exc else None,
    )
    return record


def _bake_tb(exc: BaseException) -> BaseException:
    try:
        raise exc
    except BaseException as e:  # noqa: BLE001
        return e


@pytest.mark.parametrize("method", ["ping", "health", "healthcheck"])
def test_filter_suppresses_benign_probe(method: str) -> None:
    f = _BenignProbeMethodFilter()
    exc = _bake_tb(RequestError.method_not_found(method))
    record = _make_record("Background task failed", exc)
    assert f.filter(record) is False






def test_filter_allows_different_message_even_for_ping() -> None:
    """Only 'Background task failed' is muted — other messages pass through."""
    f = _BenignProbeMethodFilter()
    exc = _bake_tb(RequestError.method_not_found("ping"))
    record = _make_record("Some other context", exc)
    assert f.filter(record) is True






# -- End-to-end: drive a real JSON-RPC `ping` through acp.run_agent ---------


class _FakeAgent:
    """Minimal acp.Agent stub — we only need the router to build."""

    async def initialize(self, **kwargs):  # noqa: ANN003
        from acp.schema import AgentCapabilities, InitializeResponse

        return InitializeResponse(protocol_version=1, agent_capabilities=AgentCapabilities())

    async def new_session(self, cwd, mcp_servers=None, **kwargs):  # noqa: ANN001, ANN003
        from acp.schema import NewSessionResponse

        return NewSessionResponse(session_id="test")

    async def prompt(self, session_id, prompt, **kwargs):  # noqa: ANN001, ANN003
        from acp.schema import PromptResponse

        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id, **kwargs):  # noqa: ANN001, ANN003
        pass

    async def authenticate(self, **kwargs):  # noqa: ANN003
        pass

    def on_connect(self, conn):  # noqa: ANN001
        pass


@pytest.mark.asyncio
async def test_bare_ping_request_produces_proper_response_and_no_stderr_noise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bare ``ping`` must get a JSON-RPC -32601 back AND leave stderr clean
    when the filter is installed on the handler.
    """
    import acp

    # Attach the filter to a fresh stream handler that mirrors entry._setup_logging.
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s|%(levelname)s|%(message)s"))
    handler.addFilter(_BenignProbeMethodFilter())
    root = logging.getLogger()
    prior_handlers = root.handlers[:]
    prior_level = root.level
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # Also suppress propagation of caplog's default handler interfering with
    # our stream (caplog still captures via its own propagation hook).
    try:
        # Use a loopback stream instead of anonymous pipes.  Windows' Proactor
        # loop cannot register ordinary pipe handles with IOCP, while Selector
        # loops do not implement pipe transports.  A TCP stream exercises the
        # same ACP JSON-RPC framing and works on every supported platform.
        agent_streams: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
            asyncio.get_running_loop().create_future()
        )

        async def accept_agent_streams(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            if not agent_streams.done():
                agent_streams.set_result((reader, writer))
            else:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(accept_agent_streams, "127.0.0.1", 0)
        address = server.sockets[0].getsockname()
        client_input, client_output = await asyncio.open_connection(*address)
        agent_input, agent_output = await asyncio.wait_for(agent_streams, timeout=2.0)

        agent_task = asyncio.create_task(
            acp.run_agent(
                _FakeAgent(),
                input_stream=agent_output,
                output_stream=agent_input,
                use_unstable_protocol=True,
            )
        )

        # Send a bare `ping`
        request = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
        client_output.write((json.dumps(request) + "\n").encode())
        await client_output.drain()

        response_line = await asyncio.wait_for(client_input.readline(), timeout=5.0)
        # Give the supervisor task a tick to fire (filter should eat it)
        await asyncio.sleep(0.2)

        response = json.loads(response_line.decode())
        assert response["error"]["code"] == -32601, response
        assert response["error"]["data"] == {"method": "ping"}, response

        logs = stream.getvalue()
        assert "Background task failed" not in logs, (
            f"ping noise leaked to stderr:\n{logs}"
        )

        # Clean shutdown
        client_output.close()
        await client_output.wait_closed()
        try:
            await asyncio.wait_for(agent_task, timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            agent_task.cancel()
            try:
                await agent_task
            except BaseException:  # noqa: BLE001
                pass
        agent_output.close()
        await agent_output.wait_closed()
        server.close()
        await server.wait_closed()
    finally:
        root.handlers = prior_handlers
        root.setLevel(prior_level)
