"""Best-effort APNs delivery for server-owned Hermes task completions.

The mobile-auth database only records device tokens. Delivery is optional and
activated by APNs credentials in the server environment; task execution never
waits on Apple or fails because push credentials are absent.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from hermes_cli.dashboard_auth.mobile_device_store import MobileDeviceStore

_log = logging.getLogger(__name__)
_MAX_PAYLOAD_BYTES = 4096
_DEFAULT_BUNDLE_ID = "app.sunstone1029.fig1171"


def apns_configured() -> bool:
    return bool(
        os.environ.get("HERMES_APNS_KEY_ID", "").strip()
        and os.environ.get("HERMES_APNS_TEAM_ID", "").strip()
        and _private_key().strip()
    )


def build_task_completion_payload(
    *,
    conversation_id: str,
    turn_id: str,
    status: str,
    result: str,
) -> dict[str, Any]:
    normalized_conversation_id = _bounded_identifier(conversation_id, "conversation")
    normalized_turn_id = _bounded_identifier(turn_id, "turn")
    normalized_status = str(status or "completed").strip().lower()
    title = "Hermes 任务已完成" if normalized_status == "completed" else "Hermes 任务未完成"
    preview = str(result or "").strip()
    payload: dict[str, Any] = {
        "aps": {
            "alert": {
                "title": title,
                "body": preview or "打开 Hermes 查看完整结果。",
            },
            "sound": "default",
        },
        "hermes": {
            "conversation_id": normalized_conversation_id,
            "turn_id": normalized_turn_id,
            "status": normalized_status,
            "result": preview,
            "deep_link": (
                "hermes-agent://conversation/"
                f"{quote(normalized_conversation_id, safe='')}"
                f"?turn={quote(normalized_turn_id, safe='')}"
            ),
        },
    }
    # APNs rejects oversized payloads. Keep the deep link and status intact;
    # the app fetches the full result from the server after opening it.
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        payload["aps"]["alert"]["body"] = "打开 Hermes 查看完整结果。"
        payload["hermes"]["result"] = _fit_payload_result(payload, preview)
    return payload


def _fit_payload_result(payload: dict[str, Any], result: str) -> str:
    low = 0
    high = len(result)
    best = ""
    while low <= high:
        midpoint = (low + high) // 2
        candidate = result[:midpoint]
        payload["hermes"]["result"] = candidate
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(encoded) <= _MAX_PAYLOAD_BYTES:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _bounded_identifier(value: Any, fallback: str) -> str:
    normalized = str(value or "").strip()
    normalized = "".join(
        character
        for character in normalized
        if ord(character) >= 32 and ord(character) != 127
    )[:256]
    return normalized or fallback


def schedule_task_completion_push(
    *,
    owner_id: str,
    conversation_id: str,
    turn_id: str,
    status: str,
    result: str,
) -> None:
    """Queue delivery on a daemon thread and return immediately."""
    normalized_owner_id = str(owner_id or "").strip()
    if not normalized_owner_id or not apns_configured():
        return
    thread = threading.Thread(
        target=_deliver_task_completion_push,
        kwargs={
            "owner_id": normalized_owner_id,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "status": status,
            "result": result,
        },
        name=f"hermes-apns-{turn_id[-12:]}",
        daemon=True,
    )
    thread.start()


def _deliver_task_completion_push(
    *,
    owner_id: str,
    conversation_id: str,
    turn_id: str,
    status: str,
    result: str,
) -> None:
    try:
        import httpx
        import jwt

        key_id = os.environ["HERMES_APNS_KEY_ID"].strip()
        team_id = os.environ["HERMES_APNS_TEAM_ID"].strip()
        bundle_id = os.environ.get("HERMES_APNS_BUNDLE_ID", _DEFAULT_BUNDLE_ID).strip()
        environment = os.environ.get("HERMES_APNS_ENVIRONMENT", "production").strip().lower()
        host = "api.sandbox.push.apple.com" if environment == "sandbox" else "api.push.apple.com"
        token = jwt.encode(
            {"iss": team_id, "iat": int(time.time())},
            _private_key(),
            algorithm="ES256",
            headers={"kid": key_id},
        )
        payload = build_task_completion_payload(
            conversation_id=conversation_id,
            turn_id=turn_id,
            status=status,
            result=result,
        )
        store = MobileDeviceStore()
        registrations = store.list_active_apns_registrations(
            user_id=owner_id,
            environment=environment,
        )
        if not registrations:
            return
        with httpx.Client(http2=True, timeout=10.0) as client:
            for registration in registrations:
                if registration.get("bundle_id") != bundle_id:
                    continue
                device_token = str(registration.get("token") or "")
                response = client.post(
                    f"https://{host}/3/device/{device_token}",
                    headers={
                        "authorization": f"bearer {token}",
                        "apns-topic": bundle_id,
                        "apns-push-type": "alert",
                        "apns-priority": "10",
                    },
                    json=payload,
                )
                if response.status_code in {400, 410}:
                    store.disable_apns_registration(
                        registration_id=str(registration.get("id") or ""),
                        error=f"APNs HTTP {response.status_code}",
                    )
                elif response.status_code >= 300:
                    _log.warning(
                        "APNs delivery failed for %s: HTTP %s",
                        registration.get("id"),
                        response.status_code,
                    )
    except Exception as exc:  # pragma: no cover - network/credential dependent
        _log.warning("APNs task notification skipped: %s", exc)


def _private_key() -> str:
    raw = os.environ.get("HERMES_APNS_PRIVATE_KEY", "").strip()
    if raw.startswith("-----BEGIN"):
        return raw.replace("\\n", "\n")
    if raw:
        path = Path(raw).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""
