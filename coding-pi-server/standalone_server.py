"""Standalone HTTP host for the unmodified oh-my-pi RPC runtime.

This process is intentionally separate from the Hermes web server.  Hermes
may still mount its compatibility adapter, but a deployment that wants Pi to
be an independent system can run this file on its own host/process and point
the iOS Coding mode at its origin.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import WebSocket as FastAPIWebSocket


SERVICE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DASHBOARD_ROOT = SERVICE_ROOT / "plugins" / "coding-pi" / "dashboard"


def build_app():
    """Create the standalone FastAPI app without importing Hermes modules."""

    os.environ.setdefault("CODING_PI_STANDALONE", "1")
    plugin_root = str(PLUGIN_DASHBOARD_ROOT)
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)

    from fastapi import FastAPI, HTTPException, Request, WebSocket
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    from plugin_api import _MANAGER, _local_node_record, _local_node_id, _room_id_from_link_path, collab_room_socket, router

    service_logger = logging.getLogger("coding-pi-standalone")

    @asynccontextmanager
    async def lifespan(_app):
        coordinator_task = None
        if os.environ.get("CODING_PI_COORDINATOR_URL", "").strip():
            coordinator_task = asyncio.create_task(_coordinator_loop(_local_node_record, _local_node_id))
        try:
            yield
        finally:
            if coordinator_task is not None:
                coordinator_task.cancel()
                with suppress(asyncio.CancelledError):
                    await coordinator_task
            await _MANAGER.close_all()

    app = FastAPI(
        title="Pi Coding Service",
        version="1.0",
        description="Independent HTTP host for the unmodified oh-my-pi RPC runtime.",
        lifespan=lifespan,
    )

    origins = [
        value.strip()
        for value in os.environ.get("CODING_PI_CORS_ORIGINS", "*").split(",")
        if value.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def require_service_token(request: Request, call_next):
        expected = os.environ.get("CODING_PI_SERVER_TOKEN", "").strip()
        if expected and request.url.path.startswith("/api/coding-pi"):
            received = request.headers.get("authorization", "")
            wanted = f"Bearer {expected}"
            if not hmac.compare_digest(received, wanted):
                return JSONResponse(status_code=401, content={"detail": "Pi service authentication required"})
        return await call_next(request)

    @app.get("/")
    async def service_index() -> dict[str, object]:
        return {
            "ok": True,
            "service": "pi-coding",
            "runtime": "oh-my-pi-rpc",
            "api": "/api/coding-pi",
            "collab": "/collab/",
            "relay": "/r/<roomId>",
            "node_id": _local_node_id(),
            "node_agent": os.environ.get("CODING_PI_NODE_AGENT_ORIGIN", "").strip() or None,
        }

    @app.websocket("/r/{room_id}")
    async def root_collab_socket(websocket: FastAPIWebSocket, room_id: str) -> None:
        # The upstream link grammar intentionally derives `/r/<roomId>` from
        # the relay origin and does not preserve an API prefix.  Keep this
        # root route as the official relay-compatible entrypoint; the HTTP API
        # remains namespaced below `/api/coding-pi`.
        # Resolve and serve the session here instead of calling a decorated
        # FastAPI router function from another WebSocket route.  Starlette
        # treats that wrapper as a separate endpoint and rejects the upgrade
        # before the bridge can accept it, while the same function works when
        # mounted under the API router.
        service_logger.debug("collab root request room=%s", _room_id_from_link_path(room_id))
        # Reuse the adapter's local-or-reverse-tunnel decision so the official
        # root relay path has the same behavior as /api/coding-pi/r/*.
        await collab_room_socket(websocket, room_id)

    app.include_router(router, prefix="/api/coding-pi")

    # collab-web is built from the pinned oh-my-pi checkout by the sync script.
    # It is generated integration output, not a modified copy of Pi source.
    from fastapi.staticfiles import StaticFiles

    collab_root = Path(
        os.environ.get("CODING_PI_COLLAB_WEB_ROOT", str(SERVICE_ROOT / "coding-pi-server" / "collab-web-dist"))
    ).expanduser().resolve()
    if (collab_root / "index.html").is_file():
        app.mount("/collab", StaticFiles(directory=str(collab_root), html=True), name="pi-collab-web")
    return app


def _coordinator_api_base() -> str:
    raw = os.environ.get("CODING_PI_COORDINATOR_URL", "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.endswith("/api/coding-pi") or raw.endswith("/api/plugins/coding-pi"):
        return raw
    suffix = os.environ.get("CODING_PI_COORDINATOR_BASE_PATH", "/api/plugins/coding-pi").strip()
    return raw + "/" + suffix.strip("/")


def _post_coordinator(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = _coordinator_api_base()
    if not base:
        return {}
    token = os.environ.get("CODING_PI_COORDINATOR_TOKEN", "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body if isinstance(body, dict) else {}


async def _coordinator_loop(record_factory, node_id_factory) -> None:
    """Keep the coordinator's endpoint fresh when the router changes leases."""

    first = True
    while True:
        try:
            record = record_factory()
            if first:
                await asyncio.to_thread(_post_coordinator, "/nodes/register", record)
                first = False
            else:
                await asyncio.to_thread(_post_coordinator, f"/nodes/{node_id_factory()}/heartbeat", record)
        except (OSError, urllib.error.URLError, ValueError, RuntimeError) as exc:
            logging.getLogger("coding-pi-standalone").warning("Pi coordinator heartbeat unavailable: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger("coding-pi-standalone").warning("Pi coordinator heartbeat failed: %s", exc)
        await asyncio.sleep(float(os.environ.get("CODING_PI_COORDINATOR_INTERVAL", "20")))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("CODING_PI_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CODING_PI_PORT", "8787")))
    parser.add_argument("--root", help="Path to the cloned oh-my-pi repository")
    parser.add_argument("--repository", help="Expected Git remote for the Pi source checkout")
    parser.add_argument("--ref", help="Expected Pi source commit SHA")
    parser.add_argument("--bun", dest="bun_path", help="Path to bun/bun.exe")
    parser.add_argument("--cli", dest="cli_path", help="Optional Pi RPC CLI entrypoint")
    parser.add_argument("--workspace", help="Default coding workspace")
    parser.add_argument("--home", help="Independent Pi session/data directory")
    parser.add_argument(
        "--allow-workspace",
        dest="allowed_workspaces",
        action="append",
        help="Allowed workspace root; may be provided more than once",
    )
    parser.add_argument("--token", help="Bearer token required by this service")
    parser.add_argument("--public-origin", help="HTTP(S) origin used in persistent collab-web links")
    parser.add_argument("--public-host", help="Advertised host; use auto to follow the active LAN address")
    parser.add_argument("--relay-url", help="WS(S) relay origin used in collab links")
    parser.add_argument("--collab-web-url", help="Static collab-web page URL")
    parser.add_argument("--coordinator-url", help="Hermes/central Pi API origin used for node registration")
    parser.add_argument("--coordinator-token", help="Optional bearer token for coordinator registration")
    parser.add_argument("--node-id", help="Stable Pi node id")
    parser.add_argument(
        "--local-relay-link",
        action="store_true",
        help="Use ws://localhost in links for an integrating client that rewrites the relay target",
    )
    parser.add_argument(
        "--cors-origin",
        dest="cors_origins",
        action="append",
        help="Allowed browser origin; may be provided more than once",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    environment = {
        "CODING_PI_STANDALONE": "1",
        # The collab link builder uses CODING_PI_PORT when no explicit public
        # origin is configured.  Keep generated room URLs aligned with the
        # actual listener, including isolated/custom-port deployments.
        "CODING_PI_PORT": str(args.port),
        "CODING_PI_ROOT": args.root,
        "CODING_PI_REPOSITORY": args.repository,
        "CODING_PI_REF": args.ref,
        "CODING_PI_BUN_PATH": args.bun_path,
        "CODING_PI_CLI_PATH": args.cli_path,
        "CODING_PI_WORKSPACE": args.workspace,
        "CODING_PI_HOME": args.home,
        "CODING_PI_SERVER_TOKEN": args.token,
        "CODING_PI_PUBLIC_ORIGIN": args.public_origin,
        "CODING_PI_PUBLIC_HOST": args.public_host,
        "CODING_PI_RELAY_URL": args.relay_url,
        "CODING_PI_COLLAB_WEB_URL": args.collab_web_url,
        "CODING_PI_COORDINATOR_URL": args.coordinator_url,
        "CODING_PI_COORDINATOR_TOKEN": args.coordinator_token,
        "CODING_PI_NODE_ID": args.node_id,
    }
    for key, value in environment.items():
        if value:
            os.environ[key] = value
    if args.allowed_workspaces:
        os.environ["CODING_PI_ALLOWED_WORKSPACES"] = os.pathsep.join(args.allowed_workspaces)
    if args.cors_origins:
        os.environ["CODING_PI_CORS_ORIGINS"] = ",".join(args.cors_origins)
    if args.local_relay_link:
        os.environ["CODING_PI_LOCAL_RELAY_LINK"] = "1"

    import uvicorn

    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="info")


app = build_app()


if __name__ == "__main__":
    main()
