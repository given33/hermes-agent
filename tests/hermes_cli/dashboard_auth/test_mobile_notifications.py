from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import hermes_cli.dashboard_auth.mobile_notifications as mobile_notifications
from hermes_cli.dashboard_auth.mobile_notifications import (
    _MAX_PAYLOAD_BYTES,
    _apns_provider_token,
    _send_apns,
    account_deletion_collapse_id,
    account_notification_collapse_id,
    build_account_deletion_payload,
    build_account_notification_payload,
    build_device_relay_wake_payload,
    build_task_completion_payload,
    deliver_account_background_wake,
    deliver_account_deletion_push,
    deliver_account_notification_push,
    deliver_task_completion_push,
    task_completion_collapse_id,
)


def test_task_completion_payload_contains_deep_link_and_server_result():
    payload = build_task_completion_payload(
        conversation_id="conversation-1",
        turn_id="turn-1",
        status="completed",
        result="任务已完成，完整结果在服务器会话中。",
    )

    assert payload["aps"]["alert"]["title"] == "Hermes 任务已完成"
    assert payload["hermes"] == {
        "conversation_id": "conversation-1",
        "deep_link": "hermes-agent://conversation/conversation-1?turn=turn-1",
        "result": "任务已完成，完整结果在服务器会话中。",
        "status": "completed",
        "turn_id": "turn-1",
    }


def test_oversized_results_keep_a_valid_apns_payload_and_deep_link():
    payload = build_task_completion_payload(
        conversation_id="conversation-1",
        turn_id="turn-1",
        status="failed",
        result="结果 " * 20_000,
    )
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    assert len(encoded) <= _MAX_PAYLOAD_BYTES
    assert payload["aps"]["alert"]["body"] == "打开 Hermes 查看完整结果。"
    assert payload["hermes"]["deep_link"].startswith("hermes-agent://conversation/")
    assert payload["hermes"]["status"] == "failed"


def test_multibyte_results_are_truncated_by_final_utf8_payload_size():
    payload = build_task_completion_payload(
        conversation_id="conversation/with spaces",
        turn_id="turn?unsafe=value",
        status="completed",
        result="完成🚀" * 20_000,
    )
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    assert len(encoded) <= _MAX_PAYLOAD_BYTES
    assert payload["hermes"]["result"]
    assert payload["hermes"]["deep_link"] == (
        "hermes-agent://conversation/conversation%2Fwith%20spaces"
        "?turn=turn%3Funsafe%3Dvalue"
    )


def test_generic_account_notification_is_bounded_and_routes_to_weather():
    payload = build_account_notification_payload(
        title="出行天气提醒",
        body="预计外出时段有雨" * 20_000,
        category="smart-weather",
        deep_link="hermes-agent://weather",
        data={
            "forecast_id": "forecast-1",
            "expires_at": 1_800_000_000_000,
            "ignored": {"nested": "objects"},
        },
    )

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= _MAX_PAYLOAD_BYTES
    assert payload["aps"]["thread-id"] == "smart-weather"
    assert payload["aps"]["content-available"] == 1
    assert payload["hermes"]["deep_link"] == "hermes-agent://weather"
    assert payload["hermes"]["data"] == {
        "forecast_id": "forecast-1",
        "expires_at": 1_800_000_000_000,
    }


def test_generic_account_notification_uses_stable_collapse_and_delivery(monkeypatch):
    monkeypatch.setenv("HERMES_APNS_BUNDLE_ID", "app.sunstone1029.fig1171")
    store = _DeviceStore(
        [{
            "id": "registration-weather",
            "token": "ee" * 16,
            "bundle_id": "app.sunstone1029.fig1171",
        }]
    )
    calls = []

    result = deliver_account_notification_push(
        owner_id="owner-a",
        notification_id="forecast-1",
        title="出行天气提醒",
        body="预计九点有小雨",
        category="smart-weather",
        deep_link="hermes-agent://weather",
        data={"forecast_id": "forecast-1"},
        device_store=store,
        sender=lambda registration, payload, collapse_id: (
            calls.append((registration["id"], payload, collapse_id)) or (200, "")
        ),
    )

    assert result["state"] == "delivered"
    assert calls[0][0] == "registration-weather"
    assert calls[0][1]["hermes"]["category"] == "smart-weather"
    assert calls[0][2] == account_notification_collapse_id(
        "smart-weather",
        "forecast-1",
    )


def test_device_relay_wake_is_silent_and_uses_background_delivery():
    payload = build_device_relay_wake_payload(
        command_id="command-1",
        expires_at=1_900_000_000,
    )
    assert payload == {
        "aps": {"content-available": 1},
        "hermes": {
            "category": "device-relay",
            "data": {
                "command_id": "command-1",
                "valid_until": 1_900_000_000,
            },
        },
    }

    calls = []
    result = deliver_account_background_wake(
        owner_id="owner-a",
        command_id="command-1",
        expires_at=1_900_000_000,
        device_store=_DeviceStore([{
            "id": "registration-relay",
            "token": "ef" * 16,
            "bundle_id": "app.sunstone1029.fig1171",
        }]),
        sender=lambda registration, current_payload, collapse_id: (
            calls.append((registration, current_payload, collapse_id)) or (200, "")
        ),
    )
    assert result["state"] == "delivered"
    assert calls[0][1]["aps"] == {"content-available": 1}
    assert calls[0][2].startswith("hermes-relay-")


def test_account_deletion_push_is_silent_and_targets_every_active_device(monkeypatch):
    monkeypatch.setenv("HERMES_APNS_BUNDLE_ID", "app.sunstone1029.fig1171")
    calls = []
    result = deliver_account_deletion_push(
        owner_id="owner-a",
        owner_scope="https://hermes.example|owner-a",
        valid_until=1_900_000_000,
        device_store=_DeviceStore([
            {
                "id": "registration-a",
                "token": "aa" * 16,
                "bundle_id": "app.sunstone1029.fig1171",
            },
            {
                "id": "registration-b",
                "token": "bb" * 16,
                "bundle_id": "app.sunstone1029.fig1171",
            },
        ]),
        sender=lambda registration, payload, collapse_id: (
            calls.append((registration["id"], payload, collapse_id)) or (200, "")
        ),
    )

    assert result["state"] == "delivered"
    assert [item[0] for item in calls] == ["registration-a", "registration-b"]
    assert all(
        item[1] == build_account_deletion_payload(
            owner_scope="https://hermes.example|owner-a",
            valid_until=1_900_000_000,
        )
        for item in calls
    )
    assert all(
        item[1]["hermes"]["data"]["owner_scope"]
        == "https://hermes.example|owner-a"
        for item in calls
    )
    assert all(item[2] == account_deletion_collapse_id("owner-a") for item in calls)


class _DeviceStore:
    def __init__(self, registrations):
        self.registrations = registrations
        self.disabled = []

    def list_active_apns_registrations(self, *, user_id, environment):
        assert user_id == "owner-a"
        assert environment == "production"
        return list(self.registrations)

    def disable_apns_registration(self, *, registration_id, error):
        self.disabled.append((registration_id, error))
        return True


def test_delivery_replay_skips_successful_devices_and_keeps_one_collapse_id(monkeypatch):
    monkeypatch.setenv("HERMES_APNS_BUNDLE_ID", "app.sunstone1029.fig1171")
    store = _DeviceStore(
        [
            {
                "id": "registration-a",
                "token": "aa" * 16,
                "bundle_id": "app.sunstone1029.fig1171",
            },
            {
                "id": "registration-b",
                "token": "bb" * 16,
                "bundle_id": "app.sunstone1029.fig1171",
            },
        ]
    )
    collapse_id = task_completion_collapse_id("conversation-a", "turn-a")
    calls = []
    progress = []

    def first_sender(registration, _payload, supplied_collapse_id):
        calls.append((registration["id"], supplied_collapse_id))
        return (200, "") if registration["id"] == "registration-a" else (503, "Shutdown")

    first = deliver_task_completion_push(
        owner_id="owner-a",
        conversation_id="conversation-a",
        turn_id="turn-a",
        status="completed",
        result="done",
        collapse_id=collapse_id,
        progress_callback=lambda deliveries: progress.append(deliveries),
        device_store=store,
        sender=first_sender,
    )

    assert first["state"] == "retry"
    assert len(progress) == 2
    assert {call[1] for call in calls} == {collapse_id}
    calls.clear()

    second = deliver_task_completion_push(
        owner_id="owner-a",
        conversation_id="conversation-a",
        turn_id="turn-a",
        status="completed",
        result="done",
        collapse_id=collapse_id,
        previous_deliveries=first["deliveries"],
        device_store=store,
        sender=lambda registration, _payload, supplied_collapse_id: (
            calls.append((registration["id"], supplied_collapse_id)) or (200, "")
        ),
    )

    assert second["state"] == "delivered"
    assert calls == [("registration-b", collapse_id)]
    assert collapse_id == task_completion_collapse_id("conversation-a", "turn-a")
    assert collapse_id != task_completion_collapse_id("conversation-a", "turn-b")
    assert len(collapse_id.encode("utf-8")) <= 64


def test_permanent_bad_device_response_disables_only_that_registration(monkeypatch):
    monkeypatch.setenv("HERMES_APNS_BUNDLE_ID", "app.sunstone1029.fig1171")
    store = _DeviceStore(
        [{
            "id": "registration-invalid",
            "token": "cc" * 16,
            "bundle_id": "app.sunstone1029.fig1171",
        }]
    )

    result = deliver_task_completion_push(
        owner_id="owner-a",
        conversation_id="conversation-a",
        turn_id="turn-invalid",
        status="failed",
        result="failed",
        collapse_id=task_completion_collapse_id("conversation-a", "turn-invalid"),
        device_store=store,
        sender=lambda *_args: (410, "Unregistered"),
    )

    assert result["state"] == "permanent_failure"
    assert store.disabled == [("registration-invalid", "APNs Unregistered")]


def test_missing_provider_configuration_keeps_the_notification_retryable(monkeypatch):
    for name in (
        "HERMES_APNS_KEY_ID",
        "HERMES_APNS_TEAM_ID",
        "HERMES_APNS_PRIVATE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    result = deliver_task_completion_push(
        owner_id="owner-a",
        conversation_id="conversation-a",
        turn_id="turn-pending-config",
        status="completed",
        result="done",
        collapse_id=task_completion_collapse_id(
            "conversation-a",
            "turn-pending-config",
        ),
    )

    assert result == {
        "state": "retry",
        "deliveries": {},
        "error": "apns_not_configured",
    }


def _configure_fake_apns_request(monkeypatch, tmp_path):
    captured = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, *, headers, json):
            captured.update({"url": url, "headers": headers, "json": json})
            return _Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=_Client))
    monkeypatch.setitem(
        sys.modules,
        "jwt",
        SimpleNamespace(encode=lambda *_args, **_kwargs: "signed-provider-token"),
    )
    monkeypatch.setenv("HERMES_APNS_KEY_ID", "KEYID")
    monkeypatch.setenv("HERMES_APNS_TEAM_ID", "TEAMID")
    key_path = tmp_path / "provider-key.p8"
    key_path.write_text("fixture-provider-key", encoding="utf-8")
    monkeypatch.setenv("HERMES_APNS_PRIVATE_KEY", str(key_path))
    return captured


def test_default_sender_sets_the_stable_apns_collapse_header(monkeypatch, tmp_path):
    captured = _configure_fake_apns_request(monkeypatch, tmp_path)
    collapse_id = task_completion_collapse_id("conversation-a", "turn-a")

    status, reason = _send_apns(
        {"token": "dd" * 16},
        {
            "aps": {"alert": "done"},
            "hermes": {"data": {"valid_until": 1_900_000_000_000}},
        },
        collapse_id,
    )

    assert (status, reason) == (200, "")
    assert captured["headers"]["apns-collapse-id"] == collapse_id
    assert captured["headers"]["apns-topic"] == "app.sunstone1029.fig1171"
    assert captured["headers"]["apns-expiration"] == "1900000000"


def test_default_sender_uses_background_headers_for_device_relay(monkeypatch, tmp_path):
    captured = _configure_fake_apns_request(monkeypatch, tmp_path)

    status, reason = _send_apns(
        {"token": "ab" * 16},
        build_device_relay_wake_payload(command_id="command-1"),
        "hermes-relay-owner",
        push_type="background",
        priority="5",
    )

    assert (status, reason) == (200, "")
    assert captured["headers"]["apns-push-type"] == "background"
    assert captured["headers"]["apns-priority"] == "5"


def test_provider_token_is_reused_until_the_cache_window_expires(monkeypatch):
    mobile_notifications._PROVIDER_TOKEN_CACHE.clear()
    now = [1_000_000]
    encoded_at = []
    jwt_module = SimpleNamespace(
        encode=lambda payload, *_args, **_kwargs: (
            encoded_at.append(payload["iat"]) or f"token-{payload['iat']}"
        )
    )
    monkeypatch.setattr(mobile_notifications.time, "time", lambda: now[0])

    first = _apns_provider_token(
        jwt_module,
        key_id="KEYID",
        team_id="TEAMID",
        private_key="fixture-provider-key",
    )
    now[0] += 2_999
    cached = _apns_provider_token(
        jwt_module,
        key_id="KEYID",
        team_id="TEAMID",
        private_key="fixture-provider-key",
    )
    now[0] += 1
    refreshed = _apns_provider_token(
        jwt_module,
        key_id="KEYID",
        team_id="TEAMID",
        private_key="fixture-provider-key",
    )

    assert first == cached == "token-1000000"
    assert refreshed == "token-1003000"
    assert encoded_at == [1_000_000, 1_003_000]
