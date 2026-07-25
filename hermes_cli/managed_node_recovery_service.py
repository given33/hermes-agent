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
from hermes_cli.managed_installations import (
    accept_managed_installation,
    get_received_managed_installation,
    load_managed_installation_receiver_config,
    resume_received_managed_installations,
)


MAX_REQUEST_BYTES = 64 * 1024


class RecoveryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config_path: Path | None):
        recovery_config = load_managed_node_recovery_config(config_path)
        installation_config = load_managed_installation_receiver_config(config_path)
        super().__init__(address, RecoveryRequestHandler)
        self.config_path = config_path
        self.recovery_config = recovery_config
        self.installation_config = installation_config
        if installation_config is not None:
            resume_received_managed_installations(config_path)


class RecoveryRequestHandler(BaseHTTPRequestHandler):
    server: RecoveryHTTPServer

    def do_GET(self) -> None:
        if self.path.startswith("/installations/"):
            if self.server.installation_config is None:
                self._json(404, {"error": "not_found"})
                return
            operation_id = self.path.removeprefix("/installations/").split("?", 1)[0]
            if not operation_id:
                self._json(404, {"error": "not_found"})
                return
            try:
                result = get_received_managed_installation(
                    operation_id,
                    self.headers.get("X-DBB3-Token", ""),
                    self.server.config_path,
                )
            except PermissionError:
                self._json(401, {"error": "invalid_credential"})
                return
            except KeyError:
                self._json(404, {"error": "not_found"})
                return
            except (ValueError, RuntimeError) as exc:
                self._json(503, {"error": str(exc)[:256]})
                return
            self._json(200, result)
            return
        if self.path != "/health":
            self._json(404, {"error": "not_found"})
            return
        recovery = self.server.recovery_config
        installation = self.server.installation_config
        if installation is not None:
            try:
                # This is a read-only authenticated capability probe. It verifies
                # the dedicated installation credential without creating work.
                from hermes_cli.managed_installations import _authenticate_receiver

                _authenticate_receiver(
                    installation,
                    self.headers.get("X-DBB3-Token", ""),
                )
            except PermissionError:
                self._json(401, {"error": "invalid_credential"})
                return
            except (ValueError, RuntimeError) as exc:
                self._json(503, {"error": str(exc)[:256]})
                return
        config = recovery or installation
        self._json(200 if config else 503, {
            "ok": config is not None,
            "node_id": str((config or {}).get("node_id") or ""),
            "recovery": recovery is not None,
            "installations": installation is not None,
        })

    def do_POST(self) -> None:
        if self.path not in {"/recover", "/installations"}:
            self._json(404, {"error": "not_found"})
            return
        if (
            (self.path == "/recover" and self.server.recovery_config is None)
            or (
                self.path == "/installations"
                and self.server.installation_config is None
            )
        ):
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
            if self.path == "/installations":
                result = accept_managed_installation(
                    payload,
                    self.headers.get("X-DBB3-Token", ""),
                    self.server.config_path,
                )
            else:
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
