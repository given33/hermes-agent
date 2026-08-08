"""Small local Pi node supervisor.

The agent is intentionally independent from the Pi runtime. It is the stable
LAN-facing bootstrap port (8786 by default): Hermes can discover it after a
router address change and ask it to start/restart the real Pi service on 8787.
The service process, source checkout, RPC protocol, and collab-web relay stay
untouched and run as a child of this supervisor.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware


LOGGER = logging.getLogger("coding-pi-node-agent")


class PiNodeSupervisor:
    def __init__(self, python_path: str, script: str, args: list[str], cwd: str | None) -> None:
        self.python_path = python_path
        self.script = script
        self.args = args
        self.cwd = cwd or str(Path(script).resolve().parents[1])
        self.process: subprocess.Popen[Any] | None = None
        self.started_at: float | None = None
        self.last_error: str | None = None
        self.lock = asyncio.Lock()
        self.monitor_task: asyncio.Task[None] | None = None

    def _running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def start(self) -> dict[str, Any]:
        async with self.lock:
            if self._running():
                return self.snapshot()
            command = [self.python_path, self.script, *self.args]
            try:
                self.process = subprocess.Popen(
                    command,
                    cwd=self.cwd,
                    env=os.environ.copy(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.started_at = time.time()
                self.last_error = None
                if self.monitor_task is None or self.monitor_task.done():
                    self.monitor_task = asyncio.create_task(self.monitor())
            except OSError as exc:
                self.last_error = str(exc)
                raise RuntimeError(f"Pi service failed to start: {exc}") from exc
            return self.snapshot()

    async def stop(self) -> dict[str, Any]:
        async with self.lock:
            process = self.process
            if process is None or process.poll() is not None:
                return self.snapshot()
            with suppress(OSError):
                process.terminate()
            return self.snapshot()

    async def monitor(self) -> None:
        while True:
            await asyncio.sleep(2)
            process = self.process
            if process is None or process.poll() is None:
                continue
            code = process.returncode
            self.last_error = f"Pi service exited with code {code}"
            self.process = None
            # A manually stopped service should stay stopped. Automatic restart
            # is opt-in because repeated model/provider failures should not spin.
            if os.environ.get("CODING_PI_AUTO_RESTART", "1").strip().lower() in {"1", "true", "yes", "on"}:
                await asyncio.sleep(2)
                with suppress(Exception):
                    await self.start()

    def snapshot(self) -> dict[str, Any]:
        pid = self.process.pid if self._running() and self.process else None
        service_origin = os.environ.get("CODING_PI_NODE_SERVICE_ORIGIN", "http://127.0.0.1:8787").strip()
        return {
            "ok": True,
            "service": "pi-node-agent",
            "node_id": os.environ.get("CODING_PI_NODE_ID", "local-pc").strip() or "local-pc",
            "origin": advertised_origin(),
            "service_origin": service_origin.rstrip("/"),
            "service_running": pid is not None,
            "service_pid": pid,
            "started_at": self.started_at,
            "last_error": self.last_error,
        }


def advertised_origin() -> str:
    host = os.environ.get("CODING_PI_PUBLIC_HOST", "auto").strip() or "auto"
    if host in {"auto", "dynamic"}:
        try:
            import socket

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("8.8.8.8", 80))
                host = str(probe.getsockname()[0])
        except OSError:
            host = "127.0.0.1"
    port = os.environ.get("CODING_PI_NODE_AGENT_PORT", "8786").strip() or "8786"
    return f"http://{host}:{port}"


def require_agent_token(token: str | None) -> None:
    expected = os.environ.get("CODING_PI_NODE_AGENT_TOKEN", "").strip()
    if expected and not hmac.compare_digest(token or "", f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="Pi node agent authentication required")


def coordinator_tunnel_url() -> str | None:
    raw = os.environ.get("CODING_PI_COORDINATOR_URL", "").strip().rstrip("/")
    if not raw:
        return None
    if raw.endswith("/api/coding-pi") or raw.endswith("/api/plugins/coding-pi"):
        base = raw
    else:
        suffix = os.environ.get("CODING_PI_COORDINATOR_BASE_PATH", "/api/plugins/coding-pi").strip("/")
        base = f"{raw}/{suffix}" if suffix else raw
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        return None
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    node_id = quote(os.environ.get("CODING_PI_NODE_ID", "local-pc").strip() or "local-pc", safe="")
    return urlunsplit((scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/nodes/{node_id}/tunnel", "", ""))


def tunnel_record() -> dict[str, Any]:
    return {
        "node_id": os.environ.get("CODING_PI_NODE_ID", "local-pc").strip() or "local-pc",
        "label": os.environ.get("CODING_PI_NODE_LABEL", "Local PC").strip() or "Local PC",
        "kind": os.environ.get("CODING_PI_NODE_KIND", "local").strip() or "local",
        "workspaces": [],
        "capabilities": ["pi-rpc", "collab-web", "tool-execution", "handoff-import", "reverse-tunnel"],
    }


async def proxy_tunnel_request(
    supervisor: PiNodeSupervisor,
    websocket: Any,
    send_lock: asyncio.Lock,
    payload: dict[str, Any],
) -> None:
    """Execute one coordinator request against the loopback Pi service."""

    request_id = str(payload.get("request_id") or "")
    if not request_id:
        return
    try:
        import httpx

        await ensure_local_pi_ready(supervisor)
        method = str(payload.get("method") or "GET").upper()
        path = str(payload.get("path") or "/")
        service_origin = os.environ.get("CODING_PI_NODE_SERVICE_ORIGIN", "http://127.0.0.1:8787").strip().rstrip("/")
        headers = {
            str(key).lower(): str(value)
            for key, value in (payload.get("headers") or {}).items()
            if isinstance(key, str) and isinstance(value, (str, int, float))
        }
        # The coordinator has already authenticated the mobile user. The
        # service process gets its own optional local token instead of the
        # coordinator credential; the owner header preserves session isolation.
        headers.pop("authorization", None)
        service_token = os.environ.get("CODING_PI_SERVER_TOKEN", "").strip()
        if service_token:
            headers["authorization"] = f"Bearer {service_token}"
        body_value = payload.get("body_b64")
        body = base64.b64decode(body_value.encode("ascii"), validate=True) if isinstance(body_value, str) and body_value else b""
        stream = bool(payload.get("stream"))
        async with httpx.AsyncClient(timeout=None) as client:
            if stream:
                async with client.stream(method, service_origin + path, headers=headers, content=body) as response:
                    async with send_lock:
                        await websocket.send(json.dumps({
                            "type": "stream_start",
                            "request_id": request_id,
                            "status": response.status_code,
                            "headers": dict(response.headers),
                        }))
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        async with send_lock:
                            await websocket.send(json.dumps({
                                "type": "stream_chunk",
                                "request_id": request_id,
                                "body_b64": base64.b64encode(chunk).decode("ascii"),
                            }))
                    async with send_lock:
                        await websocket.send(json.dumps({"type": "stream_end", "request_id": request_id}))
                return
            response = await client.request(method, service_origin + path, headers=headers, content=body)
            async with send_lock:
                await websocket.send(json.dumps({
                    "type": "response",
                    "request_id": request_id,
                    "status": response.status_code,
                    "headers": dict(response.headers),
                    "body_b64": base64.b64encode(response.content).decode("ascii") if response.content else "",
                }))
    except Exception as exc:
        logging.getLogger("coding-pi-node-agent").warning("reverse tunnel request failed: %s", exc)
        with suppress(Exception):
            async with send_lock:
                await websocket.send(json.dumps({"type": "error", "request_id": request_id, "detail": str(exc)}))


async def proxy_tunnel_collab(
    supervisor: PiNodeSupervisor,
    websocket: Any,
    send_lock: asyncio.Lock,
    payload: dict[str, Any],
    sockets: dict[str, Any],
) -> None:
    """Bridge the official encrypted collab WebSocket over the same tunnel."""

    stream_id = str(payload.get("stream_id") or "")
    if not stream_id:
        return
    try:
        import websockets

        await ensure_local_pi_ready(supervisor)
        path = str(payload.get("path") or "/")
        service_origin = os.environ.get("CODING_PI_NODE_SERVICE_ORIGIN", "http://127.0.0.1:8787").strip().rstrip("/")
        parsed = urlsplit(service_origin)
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
        local_url = urlunsplit((scheme, parsed.netloc, path, "", ""))
        token = os.environ.get("CODING_PI_SERVER_TOKEN", "").strip()
        headers = {"Authorization": f"Bearer {token}"} if token else None
        async with websockets.connect(local_url, additional_headers=headers, max_size=4 * 1024 * 1024, ping_interval=20, ping_timeout=20) as local:
            sockets[stream_id] = local
            async with send_lock:
                await websocket.send(json.dumps({"type": "collab_opened", "stream_id": stream_id, "status": "ok"}))
            async for frame in local:
                binary = isinstance(frame, bytes)
                data = frame if binary else str(frame).encode("utf-8")
                async with send_lock:
                    await websocket.send(json.dumps({
                        "type": "collab_frame",
                        "stream_id": stream_id,
                        "binary": binary,
                        "body_b64": base64.b64encode(data).decode("ascii"),
                    }))
    except Exception as exc:
        logging.getLogger("coding-pi-node-agent").warning("reverse collab tunnel failed: %s", exc)
        with suppress(Exception):
            async with send_lock:
                await websocket.send(json.dumps({"type": "collab_opened", "stream_id": stream_id, "status": "error", "detail": str(exc)}))
    finally:
        sockets.pop(stream_id, None)
        with suppress(Exception):
            async with send_lock:
                await websocket.send(json.dumps({"type": "collab_closed", "stream_id": stream_id}))


async def ensure_local_pi_ready(supervisor: PiNodeSupervisor) -> None:
    """Wake the child and wait until its HTTP API accepts requests.

    The coordinator can remain online while the local PC is rebooting. A
    request arriving during that window must start the independent Pi service
    first; otherwise Hermes would surface a transient connection failure even
    though the node agent is already reachable.
    """

    await supervisor.start()
    import httpx

    origin = os.environ.get("CODING_PI_NODE_SERVICE_ORIGIN", "http://127.0.0.1:8787").strip().rstrip("/")
    last_error = "Pi service did not become ready"
    for _ in range(40):
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                response = await client.get(f"{origin}/api/coding-pi/health")
            if response.status_code < 500:
                return
            last_error = f"Pi service health returned HTTP {response.status_code}"
        except Exception as exc:
            last_error = str(exc)
        await asyncio.sleep(0.5)
    raise RuntimeError(last_error)


async def reverse_tunnel_loop(supervisor: PiNodeSupervisor) -> None:
    """Keep one outbound coordinator connection alive across LAN changes."""

    tunnel_url = coordinator_tunnel_url()
    if not tunnel_url:
        return
    import websockets

    logger = logging.getLogger("coding-pi-node-agent")
    while True:
        tasks: set[asyncio.Task[None]] = set()
        collab_sockets: dict[str, Any] = {}
        try:
            token = os.environ.get("CODING_PI_COORDINATOR_TOKEN", "").strip()
            headers = {"Authorization": f"Bearer {token}"} if token else None
            async with websockets.connect(
                tunnel_url,
                additional_headers=headers,
                max_size=4 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
            ) as websocket:
                await websocket.send(json.dumps({"type": "hello", "node_id": tunnel_record()["node_id"], "record": tunnel_record()}))
                hello = json.loads(await asyncio.wait_for(websocket.recv(), timeout=15))
                if not isinstance(hello, dict) or hello.get("type") != "hello_ack":
                    raise RuntimeError("Pi coordinator rejected tunnel hello")
                send_lock = asyncio.Lock()
                async for raw in websocket:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        continue
                    message_type = payload.get("type")
                    if message_type == "request":
                        task = asyncio.create_task(proxy_tunnel_request(supervisor, websocket, send_lock, payload))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif message_type == "collab_open":
                        task = asyncio.create_task(proxy_tunnel_collab(supervisor, websocket, send_lock, payload, collab_sockets))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif message_type == "collab_frame":
                        stream_id = str(payload.get("stream_id") or "")
                        local = collab_sockets.get(stream_id)
                        if local is not None:
                            data = base64.b64decode(str(payload.get("body_b64") or "").encode("ascii"), validate=True)
                            await local.send(data if payload.get("binary") else data.decode("utf-8", errors="replace"))
                    elif message_type == "collab_close":
                        stream_id = str(payload.get("stream_id") or "")
                        local = collab_sockets.pop(stream_id, None)
                        if local is not None:
                            await local.close()
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise
        except Exception as exc:
            logger.warning("Pi coordinator tunnel unavailable: %s", exc)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(5)


def build_app(supervisor: PiNodeSupervisor) -> FastAPI:
    app = FastAPI(title="Pi Node Agent", version="1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def index() -> dict[str, Any]:
        return supervisor.snapshot()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return supervisor.snapshot()

    @app.post("/start")
    @app.post("/wake")
    async def wake(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_agent_token(authorization)
        return await supervisor.start()

    @app.post("/stop")
    async def stop(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_agent_token(authorization)
        return await supervisor.stop()

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("CODING_PI_NODE_AGENT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CODING_PI_NODE_AGENT_PORT", "8786")))
    parser.add_argument("--python", dest="python_path", default=sys.executable)
    parser.add_argument("--script", required=True, help="standalone_server.py path")
    parser.add_argument("--cwd")
    parser.add_argument("--service-arg", dest="service_args", action="append", default=[])
    parser.add_argument("--no-autostart", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    supervisor = PiNodeSupervisor(args.python_path, args.script, args.service_args, args.cwd)
    app = build_app(supervisor)
    if not args.no_autostart:
        # Uvicorn imports the app before serving; schedule startup from the
        # lifespan hook below so the child inherits the configured environment.
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            await supervisor.start()
            tunnel_task = asyncio.create_task(reverse_tunnel_loop(supervisor))
            try:
                yield
            finally:
                tunnel_task.cancel()
                with suppress(asyncio.CancelledError):
                    await tunnel_task
                if supervisor.monitor_task:
                    supervisor.monitor_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await supervisor.monitor_task
                await supervisor.stop()

        app.router.lifespan_context = lifespan

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
