"""Independent token-authenticated recovery control plane for a Hermes node."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any

from hermes_cli.managed_nodes import (
    accept_managed_node_recovery,
    load_managed_node_recovery_config,
)


MAX_REQUEST_BYTES = 64 * 1024


class RecoveryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config_path: Path | None):
        super().__init__(address, RecoveryRequestHandler)
        self.config_path = config_path


class RecoveryRequestHandler(BaseHTTPRequestHandler):
    server: RecoveryHTTPServer

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(404, {"error": "not_found"})
            return
        try:
            config = load_managed_node_recovery_config(self.server.config_path)
        except ValueError:
            config = None
        self._json(200 if config else 503, {
            "ok": config is not None,
            "node_id": str((config or {}).get("node_id") or ""),
        })

    def do_POST(self) -> None:
        if self.path != "/recover":
            self._json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 1 or length > MAX_REQUEST_BYTES:
            self._json(413, {"error": "invalid_request_size"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            result = accept_managed_node_recovery(
                payload,
                self.headers.get("X-DBB3-Token", ""),
                self.server.config_path,
            )
        except PermissionError:
            self._json(401, {"error": "invalid_credential"})
            return
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"error": str(exc)[:256]})
            return
        except RuntimeError as exc:
            self._json(503, {"error": str(exc)[:256]})
            return
        self._json(202, result)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes managed-node recovery receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9121)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    server = RecoveryHTTPServer((args.host, args.port), args.config)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
