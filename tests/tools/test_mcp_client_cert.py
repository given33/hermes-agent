"""Tests for mTLS client certificate config on MCP HTTP/SSE transports.

Covers:

1. ``_resolve_client_cert`` helper — string, tuple, encrypted-key, validation
   errors, missing-file errors.

2. HTTP (new SDK ``streamable_http_client``) path forwards ``cert=`` into the
   user-owned ``httpx.AsyncClient``.

3. SSE path forwards ``cert`` and ``ssl_verify`` via an ``httpx_client_factory``
   without breaking the OAuth/headers/timeout passthrough.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _resolve_client_cert helper
# ---------------------------------------------------------------------------


class TestResolveClientCert:
    def test_returns_none_when_unset(self):
        from tools.mcp_tool import _resolve_client_cert

        assert _resolve_client_cert("srv", {}) is None
        assert _resolve_client_cert("srv", {"url": "https://x"}) is None

    def test_string_form_single_pem(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        pem = tmp_path / "combined.pem"
        pem.write_text("dummy")

        result = _resolve_client_cert("srv", {"client_cert": str(pem)})
        assert result == str(pem)

    def test_string_cert_with_separate_key(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        cert = tmp_path / "client.crt"
        key = tmp_path / "client.key"
        cert.write_text("cert")
        key.write_text("key")

        result = _resolve_client_cert("srv", {
            "client_cert": str(cert),
            "client_key": str(key),
        })
        assert result == (str(cert), str(key))

    def test_list_form_two_elements(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        cert = tmp_path / "client.crt"
        key = tmp_path / "client.key"
        cert.write_text("cert")
        key.write_text("key")

        result = _resolve_client_cert("srv", {
            "client_cert": [str(cert), str(key)],
        })
        assert result == (str(cert), str(key))

    def test_list_form_with_passphrase(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        cert = tmp_path / "client.crt"
        key = tmp_path / "client.key"
        cert.write_text("cert")
        key.write_text("key")

        result = _resolve_client_cert("srv", {
            "client_cert": [str(cert), str(key), "passphrase"],
        })
        assert result == (str(cert), str(key), "passphrase")

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        from tools.mcp_tool import _resolve_client_cert

        monkeypatch.setenv("HOME", str(tmp_path))
        pem = tmp_path / "client.pem"
        pem.write_text("dummy")

        result = _resolve_client_cert("srv", {"client_cert": "~/client.pem"})
        assert result == str(pem)

    def test_missing_file_raises(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        with pytest.raises(FileNotFoundError, match=r"srv.*client_cert.*not found"):
            _resolve_client_cert("srv", {
                "client_cert": str(tmp_path / "nope.pem"),
            })

    def test_missing_key_file_raises(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        cert = tmp_path / "client.crt"
        cert.write_text("cert")

        with pytest.raises(FileNotFoundError, match=r"srv.*client_key.*not found"):
            _resolve_client_cert("srv", {
                "client_cert": str(cert),
                "client_key": str(tmp_path / "missing.key"),
            })

    def test_list_with_bad_length_raises(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        with pytest.raises(ValueError, match=r"list form must have 2 or 3"):
            _resolve_client_cert("srv", {"client_cert": [str(tmp_path / "x")]})

    def test_list_plus_client_key_rejected(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        cert = tmp_path / "client.crt"
        key = tmp_path / "client.key"
        cert.write_text("cert")
        key.write_text("key")

        with pytest.raises(ValueError, match=r"either client_cert as a list"):
            _resolve_client_cert("srv", {
                "client_cert": [str(cert), str(key)],
                "client_key": str(key),
            })

    def test_non_string_path_rejected(self):
        from tools.mcp_tool import _resolve_client_cert

        with pytest.raises(ValueError, match=r"client_cert must be a non-empty string"):
            _resolve_client_cert("srv", {"client_cert": 123})

    def test_password_must_be_string(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        cert = tmp_path / "client.crt"
        key = tmp_path / "client.key"
        cert.write_text("cert")
        key.write_text("key")

        with pytest.raises(ValueError, match=r"key passphrase.*must be a string"):
            _resolve_client_cert("srv", {
                "client_cert": [str(cert), str(key), 42],
            })


# ---------------------------------------------------------------------------
# HTTP transport — cert forwarded into httpx.AsyncClient
# ---------------------------------------------------------------------------


class TestHTTPClientCert:
    def test_cert_forwarded_to_async_client(self, tmp_path):
        """When client_cert is set, the v2 path passes it to httpx2."""
        from tools.mcp_tool import MCPServerTask

        cert = tmp_path / "client.pem"
        cert.write_text("dummy")

        server = MCPServerTask("remote")
        captured: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class DummyTransportCtx:
            async def __aenter__(self):
                return MagicMock(), MagicMock()

            async def __aexit__(self, *a):
                return False

        class DummySession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def discover(self):
                return MagicMock()

        async def _discover_tools(self):
            self._shutdown_event.set()

        async def _drive():
            with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True), \
                 patch("httpx2.AsyncClient", DummyAsyncClient), \
                 patch("tools.mcp_tool.streamable_http_client",
                       return_value=DummyTransportCtx()), \
                 patch("tools.mcp_tool.ClientSession", DummySession), \
                 patch.object(MCPServerTask, "_discover_tools", _discover_tools):
                await server._run_http({
                    "url": "https://example.com/mcp",
                    "client_cert": str(cert),
                })

        asyncio.run(_drive())
        assert captured.get("cert") == str(cert)

    def test_cert_tuple_forwarded(self, tmp_path):
        """List/tuple form resolves to a tuple in ``cert=``."""
        from tools.mcp_tool import MCPServerTask

        cert = tmp_path / "client.crt"
        key = tmp_path / "client.key"
        cert.write_text("cert")
        key.write_text("key")

        server = MCPServerTask("remote")
        captured: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class DummyTransportCtx:
            async def __aenter__(self):
                return MagicMock(), MagicMock()

            async def __aexit__(self, *a):
                return False

        class DummySession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def discover(self):
                return MagicMock()

        async def _discover_tools(self):
            self._shutdown_event.set()

        async def _drive():
            with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True), \
                 patch("httpx2.AsyncClient", DummyAsyncClient), \
                 patch("tools.mcp_tool.streamable_http_client",
                       return_value=DummyTransportCtx()), \
                 patch("tools.mcp_tool.ClientSession", DummySession), \
                 patch.object(MCPServerTask, "_discover_tools", _discover_tools):
                await server._run_http({
                    "url": "https://example.com/mcp",
                    "client_cert": [str(cert), str(key)],
                })

        asyncio.run(_drive())
        assert captured.get("cert") == (str(cert), str(key))

    def test_no_cert_means_no_cert_kwarg(self):
        """When client_cert is unset, ``cert`` is not passed to ``httpx2.AsyncClient``
        (matches SDK defaults)."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("remote")
        captured: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class DummyTransportCtx:
            async def __aenter__(self):
                return MagicMock(), MagicMock()

            async def __aexit__(self, *a):
                return False

        class DummySession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def discover(self):
                return MagicMock()

        async def _discover_tools(self):
            self._shutdown_event.set()

        async def _drive():
            with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True), \
                 patch("httpx2.AsyncClient", DummyAsyncClient), \
                 patch("tools.mcp_tool.streamable_http_client",
                       return_value=DummyTransportCtx()), \
                 patch("tools.mcp_tool.ClientSession", DummySession), \
                 patch.object(MCPServerTask, "_discover_tools", _discover_tools):
                await server._run_http({"url": "https://example.com/mcp"})

        asyncio.run(_drive())
        assert "cert" not in captured

    def test_missing_cert_file_surfaces_clear_error(self, tmp_path):
        """A missing cert file fails fast with a server-scoped error message."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("remote")

        async def _drive():
            with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True):
                await server._run_http({
                    "url": "https://example.com/mcp",
                    "client_cert": str(tmp_path / "nope.pem"),
                })

        with pytest.raises(FileNotFoundError, match=r"remote.*client_cert.*not found"):
            asyncio.run(_drive())


def test_deprecated_sse_transport_is_rejected():
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask("sse-test")
    with pytest.raises(ValueError, match=r"deprecated HTTP\+SSE.*Streamable HTTP"):
        asyncio.run(
            server.start(
                {"url": "https://example.com/mcp/sse", "transport": "sse"}
            )
        )
