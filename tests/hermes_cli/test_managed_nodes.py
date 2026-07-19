import json
import threading
from datetime import datetime, timezone
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
                        "sampled_at": "2026-07-16T09:00:00+00:00",
                        "cpu_percent": 12.5,
                        "memory": {"used_percent": 50, "total_bytes": 1000, "available_bytes": 500},
                        "disk": {"used_percent": 25, "total_bytes": 2000, "free_bytes": 1500},
                        "uptime_seconds": 3600,
                    },
                    "pc": {
                        "available": True,
                        "sampled_at": "2026-07-16T09:00:00+00:00",
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

        result = fetch_managed_nodes(
            config_path,
            now=datetime(2026, 7, 16, 9, 0, 20, tzinfo=timezone.utc),
        )
    finally:
        server.shutdown()
        server.server_close()

    assert observed_headers == [token]
    assert [node["id"] for node in result["nodes"]] == ["dbb3", "wsl"]
    assert result["nodes"][0]["online"] is True
    assert result["nodes"][0]["fresh"] is True
    assert result["nodes"][0]["age_seconds"] == 20
    assert result["nodes"][0]["active_tasks"] == 2
    assert result["nodes"][0]["metrics"]["cpu_percent"] == 12.5
    assert result["nodes"][0]["version"] == "v0.18.2"
    assert result["nodes"][1]["online"] is True
    assert result["nodes"][1]["version"] == "v0.18.3"
    assert result["nodes"][1]["metrics_source"] == "windows_psutil_push"
    assert "status_url" not in json.dumps(result)
    assert token not in json.dumps(result)


def test_managed_nodes_never_report_stale_heartbeats_online(tmp_path, monkeypatch):
    token_path = tmp_path / "status-token"
    token_path.write_text("token", encoding="utf-8")
    config_path = tmp_path / "managed-nodes.json"
    config_path.write_text(json.dumps({
        "nodes": [{
            "id": "home",
            "label": "Home Hermes",
            "status_url": "https://status.invalid/live",
            "token_file": str(token_path),
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.managed_nodes._fetch_status",
        lambda _config: {
            "timestamp": "2026-07-16T08:55:00+00:00",
            "devices": {"dbb3": {}, "pc": {}},
            "gateways": {
                "agent": {"alive": True, "state": "active", "version": "v1"},
                "rainday": {"alive": True, "state": "active", "version": "v2"},
            },
            "services": {"hermes_gateway": "active"},
            "wsl": {"gateway_running": True, "worker_ready": True},
        },
    )

    result = fetch_managed_nodes(
        config_path,
        now=datetime(2026, 7, 16, 9, 0, 20, tzinfo=timezone.utc),
    )

    assert [node["online"] for node in result["nodes"]] == [False, False]
    assert [node["fresh"] for node in result["nodes"]] == [False, False]
    assert result["sources"][0]["online"] is False
    assert result["sources"][0]["error"] == "stale_observation"


def test_managed_nodes_reject_shared_relay_time_without_device_heartbeats(
    tmp_path,
    monkeypatch,
):
    token_path = tmp_path / "status-token"
    token_path.write_text("token", encoding="utf-8")
    config_path = tmp_path / "managed-nodes.json"
    config_path.write_text(json.dumps({
        "nodes": [{
            "id": "home",
            "status_url": "https://status.invalid/live",
            "token_file": str(token_path),
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.managed_nodes._fetch_status",
        lambda _config: {
            "timestamp": "2026-07-16T09:00:20+00:00",
            "devices": {"dbb3": {}, "pc": {"available": True}},
            "gateways": {
                "agent": {"alive": True, "state": "active"},
                "rainday": {"alive": True, "state": "active"},
            },
            "services": {"hermes_gateway": "active"},
            "wsl": {"gateway_running": True, "worker_ready": True},
        },
    )

    result = fetch_managed_nodes(
        config_path,
        now=datetime(2026, 7, 16, 9, 0, 30, tzinfo=timezone.utc),
    )

    assert [node["online"] for node in result["nodes"]] == [False, False]
    assert [node["fresh"] for node in result["nodes"]] == [False, False]


def test_managed_nodes_use_each_device_heartbeat_instead_of_shared_payload_time(
    tmp_path,
    monkeypatch,
):
    token_path = tmp_path / "status-token"
    token_path.write_text("token", encoding="utf-8")
    config_path = tmp_path / "managed-nodes.json"
    config_path.write_text(json.dumps({
        "nodes": [{
            "id": "home",
            "status_url": "https://status.invalid/live",
            "token_file": str(token_path),
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.managed_nodes._fetch_status",
        lambda _config: {
            "timestamp": "2026-07-16T09:00:20+00:00",
            "devices": {
                "dbb3": {"sampled_at": "2026-07-16T09:00:15+00:00"},
                "pc": {
                    "available": True,
                    "sampled_at": "2026-07-16T08:55:00+00:00",
                },
            },
            "gateways": {
                "agent": {
                    "alive": True,
                    "state": "active",
                    "observed_at": "2026-07-16T09:00:18+00:00",
                },
                "rainday": {
                    "alive": True,
                    "state": "active",
                    "observed_at": "2026-07-16T08:55:00+00:00",
                },
            },
            "services": {"hermes_gateway": "active"},
            "wsl": {"gateway_running": True, "worker_ready": True},
        },
    )

    result = fetch_managed_nodes(
        config_path,
        now=datetime(2026, 7, 16, 9, 0, 30, tzinfo=timezone.utc),
    )

    assert result["nodes"][0]["online"] is True
    assert result["nodes"][0]["observed_at"] == "2026-07-16T09:00:18+00:00"
    assert result["nodes"][1]["online"] is False
    assert result["nodes"][1]["fresh"] is False
    assert result["nodes"][1]["observed_at"] == "2026-07-16T08:55:00+00:00"


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
