from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.collaboration.dashboard import plugin_api


class FakeWebSocket:
    client = SimpleNamespace(host="127.0.0.1")

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        return '{"type":"subscribe"}'

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)
        if len(self.sent) >= 2:
            raise RuntimeError("test disconnect")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_hosted_events_websocket_applies_authoritative_cursor_reset(monkeypatch):
    socket = FakeWebSocket()
    observed_delivered: list[int] = []

    monkeypatch.setattr(plugin_api, "_account_generation_for_owner", lambda _owner: "generation-1")
    monkeypatch.setattr(
        plugin_api,
        "_live_conversation_snapshot",
        lambda _conversation_id, _owner: {"id": "chat-1"},
    )
    monkeypatch.setattr(plugin_api, "_acquire_hosted_sse_slot", lambda _owner, _conversation: True)
    monkeypatch.setattr(plugin_api, "_release_hosted_sse_slot", lambda _owner, _conversation: None)
    monkeypatch.setattr(plugin_api, "_sse_presence_enter", lambda _conversation: None)
    monkeypatch.setattr(plugin_api, "_sse_presence_leave", lambda _conversation: None)
    monkeypatch.setattr(plugin_api, "_hosted_update_revision", lambda _conversation: 1)
    monkeypatch.setattr(
        plugin_api,
        "_wait_for_hosted_update",
        lambda revision, _timeout, _conversation: revision + 1,
    )

    def frame(_conversation_id, _owner, *, member_room_id, delivered_cursor, include_snapshot, limit):
        del member_room_id, include_snapshot, limit
        observed_delivered.append(delivered_cursor)
        if len(observed_delivered) == 1:
            return (
                {
                    "cursor": 4,
                    "min_cursor": 1,
                    "has_gap": True,
                    "reset_cursor": True,
                    "events": [],
                    "conversation": {"id": "chat-1"},
                    "account_generation": "generation-1",
                },
                False,
            )
        return (
            {
                "cursor": 5,
                "min_cursor": 1,
                "has_gap": False,
                "reset_cursor": False,
                "events": [],
                "conversation": {"id": "chat-1"},
                "account_generation": "generation-1",
            },
            False,
        )

    monkeypatch.setattr(plugin_api, "_live_hosted_event_stream_frame", frame)

    await plugin_api.stream_hosted_conversation_events_websocket(
        socket,
        "chat-1",
        "owner-1",
        requested_cursor=999,
        expected_account_generation="generation-1",
    )

    assert observed_delivered == [999, 4]
    assert socket.sent[0]["cursor"] == 4
    assert socket.sent[0]["reset_cursor"] is True
