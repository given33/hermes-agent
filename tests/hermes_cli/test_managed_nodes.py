import json
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from hermes_cli.managed_nodes import (
    accept_managed_node_recovery,
    fetch_managed_nodes,
    load_managed_nodes_config,
)
from hermes_cli import managed_nodes
from hermes_cli.managed_node_recovery_service import RecoveryHTTPServer


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
                    "agent": {
                        "alive": True,
                        "state": "active",
                        "version": "v0.18.2",
                        "observed_at": "2026-07-16T09:00:00+00:00",
                    },
                    "rainday": {
                        "alive": True,
                        "state": "active",
                        "version": "v0.18.3",
                        "observed_at": "2026-07-16T09:00:00+00:00",
                    },
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
    assert result["nodes"][0]["metrics_available"] is True
    assert result["nodes"][0]["version"] == "v0.18.2"
    assert result["nodes"][1]["online"] is True
    assert result["nodes"][1]["version"] == "v0.18.3"
    assert result["nodes"][1]["metrics_source"] == "windows_psutil_push"
    assert result["nodes"][1]["metrics_available"] is True
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
                    "observed_at": "2026-07-16T09:00:18+00:00",
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
    assert result["nodes"][0]["observed_at"] == "2026-07-16T09:00:15+00:00"
    assert result["nodes"][1]["online"] is False
    assert result["nodes"][1]["fresh"] is False
    assert result["nodes"][1]["observed_at"] == "2026-07-16T08:55:00+00:00"
    assert result["nodes"][1]["runtime_fresh"] is True
    assert result["nodes"][1]["metrics_available"] is False
    assert result["nodes"][1]["label"] == "Windows PC + WSL"


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


def test_managed_nodes_rejects_plain_http_remote_recovery_url(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("secret", encoding="utf-8")
    config_path = tmp_path / "managed-nodes.json"
    config_path.write_text(json.dumps({
        "nodes": [{
            "id": "home",
            "status_url": "https://status.example/live",
            "token_file": str(token_path),
            "recovery_url": "http://192.168.1.10/recover",
        }],
    }), encoding="utf-8")

    try:
        load_managed_nodes_config(config_path)
    except ValueError as exc:
        assert "recovery_url" in str(exc)
    else:
        raise AssertionError("remote recovery token could be sent over plain HTTP")


def test_recovery_receiver_authenticates_and_executes_each_idempotency_key_once(tmp_path):
    token_path = tmp_path / "recovery-token"
    token_path.write_text("peer-secret", encoding="utf-8")
    config_path = tmp_path / "managed-nodes.json"
    config_path.write_text(json.dumps({
        "nodes": [],
        "recovery_receiver": {
            "node_id": "wsl",
            "token_file": str(token_path),
            "command": ["hermes", "gateway", "restart"],
        },
    }), encoding="utf-8")
    calls = []
    payload = {
        "action": "reconnect",
        "idempotency_key": "recover:wsl:1",
        "reason": "stale_observation",
        "targets": ["wsl"],
    }

    first = accept_managed_node_recovery(
        payload,
        "peer-secret",
        config_path,
        executor=lambda command: calls.append(command),
    )
    replay = accept_managed_node_recovery(
        payload,
        "peer-secret",
        config_path,
        executor=lambda command: calls.append(command),
    )
    second_payload = {**payload, "idempotency_key": "recover:wsl:2"}
    accept_managed_node_recovery(
        second_payload,
        "peer-secret",
        config_path,
        executor=lambda command: calls.append(command),
    )
    late_replay = accept_managed_node_recovery(
        payload,
        "peer-secret",
        config_path,
        executor=lambda command: calls.append(command),
    )

    assert first["accepted"] is True and first["replayed"] is False
    assert replay["accepted"] is True and replay["replayed"] is True
    assert late_replay["replayed"] is True
    assert calls == [
        ["hermes", "gateway", "restart"],
        ["hermes", "gateway", "restart"],
    ]
    failed_payload = {**payload, "idempotency_key": "recover:wsl:3"}
    try:
        accept_managed_node_recovery(
            failed_payload,
            "peer-secret",
            config_path,
            executor=lambda _command: (_ for _ in ()).throw(OSError("spawn failed")),
        )
    except OSError:
        pass
    else:
        raise AssertionError("receiver hid a recovery command launch failure")
    retried = accept_managed_node_recovery(
        failed_payload,
        "peer-secret",
        config_path,
        executor=lambda command: calls.append(command),
    )
    assert retried["replayed"] is False
    assert len(calls) == 3
    try:
        accept_managed_node_recovery(payload, "wrong", config_path, executor=calls.append)
    except PermissionError:
        pass
    else:
        raise AssertionError("recovery receiver accepted the wrong credential")


def test_recovery_routes_dbb3_and_wsl_to_their_own_hooks(tmp_path, monkeypatch):
    token_path = tmp_path / "token"
    token_path.write_text("secret", encoding="utf-8")
    config_path = tmp_path / "managed-nodes.json"
    config_path.write_text(json.dumps({
        "nodes": [{
            "id": "home",
            "status_url": "https://status.example/live",
            "token_file": str(token_path),
            "recovery_urls": {
                "dbb3": "https://dbb3.example/recover",
                "wsl": "https://wsl.example/recover",
            },
        }],
    }), encoding="utf-8")
    config = load_managed_nodes_config(config_path)[0]
    calls = []

    def route(_config, *, recovery_url, targets, reason, force):
        calls.append((recovery_url, targets, reason, force))
        return {"state": "recovering", "accepted": True, "targets": targets}

    monkeypatch.setattr(managed_nodes, "_request_recovery_route", route)

    result = managed_nodes._request_recovery(
        config,
        targets=["wsl", "dbb3"],
        reason="source_unreachable",
    )

    assert result["state"] == "recovering"
    assert result["accepted"] is True
    assert calls == [
        ("https://dbb3.example/recover", ["dbb3"], "source_unreachable", False),
        ("https://wsl.example/recover", ["wsl"], "source_unreachable", False),
    ]

    calls.clear()

    def partial_route(_config, *, recovery_url, targets, reason, force):
        calls.append((recovery_url, targets))
        if targets == ["dbb3"]:
            raise TimeoutError("dbb3 control plane unavailable")
        return {"state": "recovering", "accepted": True}

    monkeypatch.setattr(managed_nodes, "_request_recovery_route", partial_route)
    partial = managed_nodes._request_recovery(
        config,
        targets=["dbb3", "wsl"],
        reason="source_unreachable",
    )

    assert calls == [
        ("https://dbb3.example/recover", ["dbb3"]),
        ("https://wsl.example/recover", ["wsl"]),
    ]
    assert partial["state"] == "failed"
    assert partial["accepted"] is False
    assert partial["target_states"] == {"dbb3": "failed", "wsl": "recovering"}


def test_independent_recovery_http_service_executes_authenticated_request_once(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("peer-secret", encoding="utf-8")
    marker = tmp_path / "executions.txt"
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"p=Path({str(marker)!r}); "
            "p.write_text((p.read_text() if p.exists() else '') + 'x')"
        ),
    ]
    config_path = tmp_path / "managed-nodes.json"
    config_path.write_text(json.dumps({
        "nodes": [],
        "recovery_receiver": {
            "node_id": "wsl",
            "token_file": str(token_path),
            "command": command,
        },
    }), encoding="utf-8")
    server = RecoveryHTTPServer(("127.0.0.1", 0), config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = json.dumps({
        "action": "reconnect",
        "idempotency_key": "service:wsl:1",
        "reason": "test",
        "targets": ["wsl"],
    }).encode("utf-8")
    try:
        for _ in range(2):
            request = Request(
                f"http://127.0.0.1:{server.server_port}/recover",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-DBB3-Token": "peer-secret",
                },
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                assert response.status == 202
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        server.shutdown()
        server.server_close()

    assert marker.read_text(encoding="utf-8") == "x"


def test_stale_node_auto_recovery_enters_cooldown_without_duplicate_post(tmp_path):
    token_path = tmp_path / "status-token"
    token_path.write_text("peer-secret", encoding="utf-8")
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({
                "devices": {"dbb3": {}, "pc": {}},
                "gateways": {"agent": {}, "rainday": {}},
                "services": {},
                "wsl": {},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            posts.append({
                "token": self.headers.get("X-DBB3-Token"),
                "body": json.loads(self.rfile.read(length)),
            })
            body = b'{"accepted":true}'
            self.send_response(202)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        config_path = tmp_path / "managed-nodes.json"
        config_path.write_text(json.dumps({
            "nodes": [{
                "id": "peer",
                "status_url": f"{base}/status",
                "recovery_url": f"{base}/recover",
                "recovery_cooldown_seconds": 90,
                "token_file": str(token_path),
            }],
        }), encoding="utf-8")
        now = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)
        first = fetch_managed_nodes(config_path, now=now)
        second = fetch_managed_nodes(config_path, now=now)
    finally:
        server.shutdown()
        server.server_close()

    assert first["sources"][0]["recovery"]["state"] == "recovering"
    assert second["sources"][0]["recovery"]["state"] == "cooldown"
    assert len(posts) == 2
    assert {item["token"] for item in posts} == {"peer-secret"}
    assert [item["body"]["targets"] for item in posts] == [["dbb3"], ["wsl"]]
