from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = REPO_ROOT / "coding-pi-server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

import network_auth  # noqa: E402


def _load_script(name: str):
    path = SERVER_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"coding_pi_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "peer",
    ["127.0.0.1", "127.255.255.254", "::1", "[::1]", "::1%lo0", "::ffff:127.0.0.1"],
)
def test_peer_loopback_accepts_only_concrete_loopback_addresses(peer):
    assert network_auth.peer_is_loopback(peer)


@pytest.mark.parametrize("peer", [None, "", "localhost", "0.0.0.0", "::", "192.168.1.20"])
def test_peer_loopback_rejects_names_wildcards_and_non_loopback(peer):
    assert not network_auth.peer_is_loopback(peer)


def test_bind_hostname_requires_every_resolution_to_be_loopback(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        if host == "loop.test":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
                (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0)),
            ]
        if host == "mixed.test":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.20", 0)),
            ]
        raise socket.gaierror("unresolved")

    monkeypatch.setattr(network_auth.socket, "getaddrinfo", fake_getaddrinfo)

    assert network_auth.bind_host_is_loopback("loop.test")
    assert not network_auth.bind_host_is_loopback("mixed.test")
    assert not network_auth.bind_host_is_loopback("missing.test")


def test_listener_and_coordinator_preflights_fail_closed():
    network_auth.require_safe_listener("127.0.0.1", None, "test service")
    network_auth.require_safe_listener("0.0.0.0", "secret", "test service")
    with pytest.raises(RuntimeError, match="requires a bearer token"):
        network_auth.require_safe_listener("0.0.0.0", " ", "test service")
    with pytest.raises(RuntimeError, match="CODING_PI_COORDINATOR_TOKEN"):
        network_auth.require_coordinator_token("https://coordinator.example", None)


def test_non_ascii_authorization_is_a_clean_mismatch():
    assert not network_auth.bearer_or_loopback_authorized(
        "secret",
        "Bearer s\N{LATIN SMALL LETTER E WITH ACUTE}cret",
        "198.51.100.9",
    )


def test_standalone_api_denies_remote_peer_without_token_even_with_spoofed_headers(monkeypatch):
    standalone = _load_script("standalone_server")
    monkeypatch.delenv("CODING_PI_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CODING_PI_COORDINATOR_URL", raising=False)
    app = standalone.build_app()

    with TestClient(app, client=("198.51.100.9", 50000)) as client:
        response = client.get(
            "/api/coding-pi/not-a-route",
            headers={"host": "localhost", "x-forwarded-for": "127.0.0.1"},
        )

    assert response.status_code == 401


def test_standalone_api_preserves_explicit_cli_loopback_and_authenticated_access(monkeypatch):
    standalone = _load_script("standalone_server")
    monkeypatch.delenv("CODING_PI_COORDINATOR_URL", raising=False)
    monkeypatch.delenv("CODING_PI_SERVER_TOKEN", raising=False)
    app = standalone.build_app(allow_unauthenticated_loopback=True)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/coding-pi/not-a-route").status_code == 404

    monkeypatch.setenv("CODING_PI_SERVER_TOKEN", "correct-secret")
    app = standalone.build_app()
    with TestClient(app, client=("198.51.100.9", 50000)) as client:
        assert client.get("/api/coding-pi/not-a-route").status_code == 401
        response = client.get(
            "/api/coding-pi/not-a-route",
            headers={"authorization": "Bearer correct-secret"},
        )
        assert response.status_code == 404


def test_standalone_mounted_app_does_not_skip_authentication(monkeypatch):
    from fastapi import FastAPI

    standalone = _load_script("standalone_server")
    monkeypatch.setenv("CODING_PI_SERVER_TOKEN", "mount-secret")
    monkeypatch.delenv("CODING_PI_COORDINATOR_URL", raising=False)
    parent = FastAPI()
    parent.mount("/prefix", standalone.build_app())

    with TestClient(parent, client=("198.51.100.9", 50000)) as client:
        assert client.get("/prefix/api/coding-pi/discovery").status_code == 401
        assert (
            client.get(
                "/prefix/api/coding-pi/discovery",
                headers={"authorization": "Bearer mount-secret"},
            ).status_code
            == 200
        )


def test_exported_standalone_app_rejects_proxy_rewritten_loopback(monkeypatch):
    standalone = _load_script("standalone_server")
    monkeypatch.delenv("CODING_PI_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CODING_PI_COORDINATOR_URL", raising=False)
    proxied = ProxyHeadersMiddleware(standalone.build_app(), trusted_hosts="*")

    with TestClient(proxied, client=("198.51.100.9", 50000)) as client:
        response = client.get(
            "/api/coding-pi/discovery",
            headers={"x-forwarded-for": "127.0.0.1"},
        )

    assert response.status_code == 401


def test_exported_standalone_app_rejects_proxy_rewritten_node_tunnel(monkeypatch):
    standalone = _load_script("standalone_server")
    monkeypatch.delenv("CODING_PI_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CODING_PI_COORDINATOR_TOKEN", raising=False)
    monkeypatch.delenv("CODING_PI_COORDINATOR_URL", raising=False)
    proxied = ProxyHeadersMiddleware(standalone.build_app(), trusted_hosts="*")

    with TestClient(proxied, client=("198.51.100.9", 50000)) as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/api/coding-pi/nodes/attacker/tunnel",
                headers={"x-forwarded-for": "127.0.0.1"},
            ):
                pass

    assert rejected.value.code == 4401


class _StubSupervisor:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def snapshot(self) -> dict[str, Any]:
        return {"ok": True}

    async def start(self) -> dict[str, Any]:
        self.started += 1
        return self.snapshot()

    async def stop(self) -> dict[str, Any]:
        self.stopped += 1
        return self.snapshot()


def test_node_agent_control_routes_deny_remote_peer_without_token(monkeypatch):
    node_agent = _load_script("node_agent")
    monkeypatch.delenv("CODING_PI_NODE_AGENT_TOKEN", raising=False)
    supervisor = _StubSupervisor()

    with TestClient(node_agent.build_app(supervisor), client=("203.0.113.8", 50000)) as client:
        assert client.get("/health").status_code == 200
        response = client.post(
            "/wake",
            headers={"host": "localhost", "x-forwarded-for": "127.0.0.1"},
        )

    assert response.status_code == 401
    assert supervisor.started == 0


def test_node_agent_control_routes_preserve_loopback_and_authenticated_access(monkeypatch):
    node_agent = _load_script("node_agent")
    supervisor = _StubSupervisor()
    monkeypatch.delenv("CODING_PI_NODE_AGENT_TOKEN", raising=False)
    with TestClient(
        node_agent.build_app(supervisor, allow_unauthenticated_loopback=True),
        client=("::ffff:127.0.0.1", 50000),
    ) as client:
        assert client.post("/start").status_code == 200

    monkeypatch.setenv("CODING_PI_NODE_AGENT_TOKEN", "node-secret")
    with TestClient(node_agent.build_app(supervisor), client=("203.0.113.8", 50000)) as client:
        assert client.post("/stop", headers={"authorization": "Bearer wrong"}).status_code == 401
        assert (
            client.post("/stop", headers={"authorization": "Bearer node-secret"}).status_code
            == 200
        )

    assert supervisor.started == 1
    assert supervisor.stopped == 1


def test_node_agent_rejects_proxy_rewritten_loopback(monkeypatch):
    node_agent = _load_script("node_agent")
    monkeypatch.delenv("CODING_PI_NODE_AGENT_TOKEN", raising=False)
    supervisor = _StubSupervisor()
    proxied = ProxyHeadersMiddleware(node_agent.build_app(supervisor), trusted_hosts="*")

    with TestClient(proxied, client=("203.0.113.8", 50000)) as client:
        response = client.post("/wake", headers={"x-forwarded-for": "127.0.0.1"})

    assert response.status_code == 401
    assert supervisor.started == 0


def test_coordinator_launcher_reuses_loopback_hardened_launcher():
    script = (SERVER_ROOT / "start_local_pi_coordinator.ps1").read_text(encoding="utf-8")
    assert "start_local_pi.ps1" in script
    assert "CODING_PI_COORDINATOR_TOKEN" in script
    assert "--host 0.0.0.0" not in script


def test_bundled_uvicorn_launchers_disable_proxy_header_rewriting():
    standalone = (SERVER_ROOT / "standalone_server.py").read_text(encoding="utf-8")
    node_agent = (SERVER_ROOT / "node_agent.py").read_text(encoding="utf-8")
    assert "proxy_headers=False" in standalone
    assert "proxy_headers=False" in node_agent
