import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hermes_cli.managed_nodes import fetch_managed_nodes, load_managed_nodes_config


def test_managed_nodes_reads_live_dbb3_and_wsl_status(tmp_path):
    token = "test-private-token"
    token_path = tmp_path / "status-token"
    token_path.write_text(token, encoding="utf-8")
    observed_headers = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed_headers.append(self.headers.get("X-DBB3-Token"))
            body = json.dumps({
                "timestamp": "2026-07-16T09:00:00+00:00",
                "devices": {
                    "dbb3": {
                        "cpu_percent": 12.5,
                        "memory": {"used_percent": 50, "total_bytes": 1000, "available_bytes": 500},
                        "disk": {"used_percent": 25, "total_bytes": 2000, "free_bytes": 1500},
                        "uptime_seconds": 3600,
                    },
                    "pc": {
                        "available": True,
                        "cpu_percent": 22.5,
                        "memory": {"used_percent": 60, "total_bytes": 2000, "available_bytes": 800},
                        "disk": {"used_percent": 30, "total_bytes": 4000, "free_bytes": 2800},
                        "uptime_seconds": 7200,
                        "source": "windows_psutil_push",
                    },
                },
                "gateways": {
                    "agent": {"alive": True, "state": "active", "version": "v0.18.2"},
                    "rainday": {"alive": True, "state": "active", "version": "v0.18.3"},
                },
                "services": {"hermes_gateway": "active"},
                "tasks": {"running": 2},
                "wsl": {"gateway_running": True, "worker_ready": True, "tunnel_up": True},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config_path = tmp_path / "managed-nodes.json"
        config_path.write_text(json.dumps({
            "nodes": [{
                "id": "home",
                "label": "Home Hermes",
                "status_url": f"http://127.0.0.1:{server.server_port}/status",
                "token_file": str(token_path),
            }],
        }), encoding="utf-8")

        result = fetch_managed_nodes(config_path)
    finally:
        server.shutdown()
        server.server_close()

    assert observed_headers == [token]
    assert [node["id"] for node in result["nodes"]] == ["dbb3", "wsl"]
    assert result["nodes"][0]["online"] is True
    assert result["nodes"][0]["active_tasks"] == 2
    assert result["nodes"][0]["metrics"]["cpu_percent"] == 12.5
    assert result["nodes"][0]["version"] == "v0.18.2"
    assert result["nodes"][1]["online"] is True
    assert result["nodes"][1]["version"] == "v0.18.3"
    assert result["nodes"][1]["metrics_source"] == "windows_psutil_push"
    assert "status_url" not in json.dumps(result)
    assert token not in json.dumps(result)


def test_managed_nodes_config_rejects_duplicate_ids(tmp_path):
    config_path = tmp_path / "managed-nodes.json"
    config_path.write_text(json.dumps({
        "nodes": [
            {"id": "same", "status_url": "http://127.0.0.1/a", "token_file": "/a"},
            {"id": "same", "status_url": "http://127.0.0.1/b", "token_file": "/b"},
        ],
    }), encoding="utf-8")

    try:
        load_managed_nodes_config(config_path)
    except ValueError as exc:
        assert "unique" in str(exc)
    else:
        raise AssertionError("duplicate node ids must be rejected")
