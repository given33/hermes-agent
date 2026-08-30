import asyncio
import importlib.util
from pathlib import Path

from hermes_services.worker_channel import (
    WorkerChannelRegistry,
    get_worker_channel_registry,
)
from starlette.websockets import WebSocketDisconnect


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "collaboration"
    / "dashboard"
    / "plugin_api.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "collaboration_plugin_worker_status",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_worker_websocket_and_management_api_share_the_process_registry():
    module = _load_module()
    assert module._WORKER_CHANNEL is get_worker_channel_registry()


def test_worker_websocket_records_sanitized_hello_and_heartbeat_status(monkeypatch):
    module = _load_module()
    registry = WorkerChannelRegistry()
    monkeypatch.setattr(module, "_WORKER_CHANNEL", registry)
    monkeypatch.setattr(module, "_connector_identity_from_websocket", lambda _ws: "hk-primary")

    class FakeWebSocket:
        headers = {}

        def __init__(self):
            self.messages = iter([
                {
                    "type": "hello",
                    "node_id": "hk-worker",
                    "connection_generation": "private-generation",
                    "cursor": 0,
                    "runtime": {"worker_ready": True, "active_tasks": 2},
                    "release": {
                        "node_id": "hk",
                        "commit": "a" * 40,
                        "version": "v1",
                    },
                    "metrics": {"cpu_percent": 10, "available": True},
                },
                {
                    "type": "heartbeat",
                    "runtime": {"worker_ready": True, "active_tasks": 1},
                    "release": {
                        "node_id": "hk",
                        "commit": "b" * 40,
                        "version": "v2",
                    },
                    "metrics": {"cpu_percent": 20, "available": True},
                },
            ])
            self.sent = []

        async def accept(self):
            pass

        async def receive_json(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise WebSocketDisconnect() from None

        async def send_json(self, message):
            self.sent.append(message)

        async def close(self, **_kwargs):
            pass

    websocket = FakeWebSocket()
    asyncio.run(module.worker_websocket(websocket))

    assert [frame["type"] for frame in websocket.sent[:2]] == [
        "hello.accepted",
        "heartbeat.ack",
    ]
    hk = registry.managed_snapshots()[-1]
    assert hk["online"] is False
    assert hk["active_tasks"] == 1
    assert hk["version"] == "v2"
    assert hk["metrics"]["cpu_percent"] == 20.0
    assert "lease_id" not in hk
    assert "private-generation" not in repr(hk)
