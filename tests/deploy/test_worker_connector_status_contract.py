import json
import threading
from pathlib import Path

from deploy.dbb3 import dbb3_cloud_connector as connector_module


ROOT = Path(__file__).resolve().parents[2]


def test_worker_websocket_hello_and_heartbeat_include_bounded_status(monkeypatch):
    client = connector_module.CloudRelayClient(
        "https://example.test/api/plugins/collaboration",
        "token",
        connector_id="hk-primary",
        worker_ws=True,
    )
    monkeypatch.setattr(
        client,
        "_worker_status_fields",
        lambda: {
            "runtime": {"worker_ready": True, "active_tasks": 2},
            "release": {"node_id": "hk", "commit": "a" * 40, "version": "v3"},
            "metrics": {"cpu_percent": 12.5, "available": True},
        },
    )
    wake = threading.Event()
    stop = threading.Event()

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.receives = 0

        def send(self, value):
            self.sent.append(json.loads(value))

        def recv(self, *, timeout):
            self.receives += 1
            if self.receives == 1:
                return json.dumps({"type": "hello.accepted"})
            if self.receives == 2:
                raise TimeoutError
            stop.set()
            raise OSError("done")

        def close(self):
            pass

    websocket = FakeWebSocket()
    monkeypatch.setattr(connector_module, "_websocket_connect", lambda *_a, **_k: websocket)

    client._stream_events(wake, stop)

    hello, heartbeat = websocket.sent
    assert hello["type"] == "hello"
    assert hello["node_id"] == "hk-worker"
    assert hello["version"] == "2.0"
    assert hello["protocol_version"] == "2.0"
    assert hello["release"]["node_id"] == "hk"
    assert heartbeat == {
        "type": "heartbeat",
        "runtime": {"worker_ready": True, "active_tasks": 2},
        "release": {"node_id": "hk", "commit": "a" * 40, "version": "v3"},
        "metrics": {"cpu_percent": 12.5, "available": True},
    }


def test_connector_units_use_each_roles_project_runtime():
    expected = {
        "deploy/dbb3/dbb3-cloud-connector.service": (
            "/usr/local/lib/hermes-agent/venv/bin/python"
        ),
        "deploy/pc/pc-cloud-connector.service": (
            "/mnt/d/Hermes/hermes-agent/venv/bin/python"
        ),
        "deploy/hk/hk-cloud-connector.service": (
            "/opt/hk-team/hermes-agent/.venv/bin/python"
        ),
    }
    for relative, runtime in expected.items():
        unit = (ROOT / relative).read_text(encoding="utf-8")
        assert f"ExecStart={runtime} " in unit
        assert "ExecStart=/usr/bin/python3" not in unit


def test_connector_installers_gate_on_project_runtime_websockets():
    shared = (
        ROOT / "deploy/dbb3/install-dbb3-cloud-connector-user.sh"
    ).read_text(encoding="utf-8")
    pc = (
        ROOT / "deploy/pc/install-pc-cloud-connector-user.sh"
    ).read_text(encoding="utf-8")
    hk = (
        ROOT / "deploy/hk/install-hk-cloud-connector-user.sh"
    ).read_text(encoding="utf-8")

    assert "websockets.sync.client" in shared
    assert '"${runtime_python}" "${source_file}" --probe' in shared
    assert "/mnt/d/Hermes/hermes-agent/venv/bin/python" in pc
    assert "/opt/hk-team/hermes-agent/.venv/bin/python" in hk


def test_fabric_updater_requires_new_online_worker_generation_and_release():
    updater = (
        ROOT / "deploy/automation/update-fabric-node.sh"
    ).read_text(encoding="utf-8")

    assert '"${cloud_url}/connector/deployment-health"' in updater
    assert 'generation != previous_generation' in updater
    assert 'worker.get("online") is True' in updater
    assert 'worker.get("fresh") is True' in updater
    assert 'release.get("commit")' in updater
    assert 'release.get("version")' in updater
    assert "target worker WebSocket generation/release did not become healthy" in updater
