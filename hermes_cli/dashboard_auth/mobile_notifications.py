"""APNs delivery primitives for durable server-owned completion notifications.

The collaboration state owns the persistent outbox. This module performs one
idempotent delivery attempt, returning per-registration state for the caller to
persist before retrying. Task execution never waits on Apple or fails because
push credentials are absent.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from hermes_cli.dashboard_auth.mobile_device_store import MobileDeviceStore

_MAX_PAYLOAD_BYTES = 4096
_DEFAULT_BUNDLE_ID = "app.sunstone1029.fig1171"
_DELIVERY_TERMINAL_STATES = {"delivered", "permanent_failure"}
_INVALID_DEVICE_REASONS = {
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
    "Unregistered",
}
_PROVIDER_TOKEN_LOCK = threading.Lock()
_PROVIDER_TOKEN_CACHE: dict[str, tuple[str, int]] = {}


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


def task_completion_collapse_id(conversation_id: str, turn_id: str) -> str:
    digest = hashlib.sha256(
        f"{conversation_id}\0{turn_id}".encode("utf-8")
    ).hexdigest()[:40]
    return f"hermes-turn-{digest}"


def schedule_task_completion_push(
    *,
    owner_id: str,
    conversation_id: str,
    turn_id: str,
    status: str,
    result: str,
) -> None:
    """Keep the direct TUI gateway notification entrypoint non-blocking.

    Hosted collaboration turns use their persisted notification outbox. This
    adapter remains for direct gateway clients and retries transient APNs
    responses while the gateway process remains alive.
    """

    normalized_owner_id = str(owner_id or "").strip()
    if not normalized_owner_id:
        return
    collapse_id = task_completion_collapse_id(conversation_id, turn_id)

    def deliver() -> None:
        deliveries: dict[str, dict[str, Any]] = {}
        attempts = 0
        while attempts < 5:
            outcome = deliver_task_completion_push(
                owner_id=normalized_owner_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                status=status,
                result=result,
                collapse_id=collapse_id,
                previous_deliveries=deliveries,
            )
            deliveries = dict(outcome.get("deliveries") or {})
            if outcome.get("state") != "retry":
                return
            if outcome.get("error") == "apns_not_configured":
                return
            attempts += 1
            if attempts >= 5:
                return
            time.sleep(min(60, 2 ** (attempts - 1)))

    threading.Thread(
        target=deliver,
        name=f"hermes-apns-direct-{turn_id[-12:]}",
        daemon=True,
    ).start()


def deliver_task_completion_push(
    *,
    owner_id: str,
    conversation_id: str,
    turn_id: str,
    status: str,
    result: str,
    collapse_id: str,
    previous_deliveries: Optional[dict[str, dict[str, Any]]] = None,
    progress_callback: Optional[Callable[[dict[str, dict[str, Any]]], None]] = None,
    device_store: Optional[MobileDeviceStore] = None,
    sender: Optional[Callable[[dict[str, Any], dict[str, Any], str], tuple[int, str]]] = None,
) -> dict[str, Any]:
    """Attempt one completion push and return durable per-device state."""

    normalized_owner_id = str(owner_id or "").strip()
    if not normalized_owner_id:
        return {
            "state": "no_recipients",
            "deliveries": {},
            "error": "notification_owner_missing",
        }
    if sender is None and not apns_configured():
        return {
            "state": "retry",
            "deliveries": dict(previous_deliveries or {}),
            "error": "apns_not_configured",
        }

    bundle_id = os.environ.get("HERMES_APNS_BUNDLE_ID", _DEFAULT_BUNDLE_ID).strip()
    environment = os.environ.get("HERMES_APNS_ENVIRONMENT", "production").strip().lower()
    store = device_store or MobileDeviceStore()
    registrations = [
        registration
        for registration in store.list_active_apns_registrations(
            user_id=normalized_owner_id,
            environment=environment,
        )
        if str(registration.get("bundle_id") or "") == bundle_id
    ]
    deliveries = _normalize_deliveries(previous_deliveries)
    current_keys = {_registration_key(registration) for registration in registrations}
    for registration_key, delivery in list(deliveries.items()):
        if (
            registration_key not in current_keys
            and delivery.get("state") not in _DELIVERY_TERMINAL_STATES
        ):
            deliveries[registration_key] = {
                **delivery,
                "state": "permanent_failure",
                "last_error": "registration_inactive",
                "updated_at": int(time.time() * 1000),
            }
    if not registrations:
        prior_states = {
            str(delivery.get("state") or "")
            for delivery in deliveries.values()
        }
        if "delivered" in prior_states:
            return {"state": "delivered", "deliveries": deliveries, "error": ""}
        if prior_states and prior_states <= {"permanent_failure"}:
            return {
                "state": "permanent_failure",
                "deliveries": deliveries,
                "error": "all_apns_deliveries_failed_permanently",
            }
        return {
            "state": "no_recipients",
            "deliveries": deliveries,
            "error": "no_active_apns_registrations",
        }

    payload = build_task_completion_payload(
        conversation_id=conversation_id,
        turn_id=turn_id,
        status=status,
        result=result,
    )
    send = sender or _send_apns
    transient_error = ""
    for registration in registrations:
        registration_key = _registration_key(registration)
        previous = deliveries.get(registration_key, {})
        if previous.get("state") in _DELIVERY_TERMINAL_STATES:
            continue
        attempts = int(previous.get("attempts") or 0) + 1
        try:
            response_status, reason = send(registration, payload, collapse_id)
            next_state, error = _classify_apns_response(response_status, reason)
            if next_state == "permanent_failure" and reason in _INVALID_DEVICE_REASONS:
                store.disable_apns_registration(
                    registration_id=str(registration.get("id") or ""),
                    error=f"APNs {reason}",
                )
            if next_state == "retry":
                transient_error = error
        except Exception as exc:  # pragma: no cover - network/credential dependent
            next_state = "retry"
            error = _bounded_error(exc)
            transient_error = error
        deliveries[registration_key] = {
            "state": next_state,
            "attempts": attempts,
            "last_error": error,
            "updated_at": int(time.time() * 1000),
        }
        if progress_callback is not None:
            progress_callback(dict(deliveries))

    states = {str(item.get("state") or "") for item in deliveries.values()}
    if "retry" in states:
        return {
            "state": "retry",
            "deliveries": deliveries,
            "error": transient_error or "apns_transient_failure",
        }
    if "delivered" in states:
        return {"state": "delivered", "deliveries": deliveries, "error": ""}
    return {
        "state": "permanent_failure",
        "deliveries": deliveries,
        "error": "all_apns_deliveries_failed_permanently",
    }


def _registration_key(registration: dict[str, Any]) -> str:
    registration_id = str(registration.get("id") or "").strip()
    return hashlib.sha256(registration_id.encode("utf-8")).hexdigest()[:32]


def _normalize_deliveries(
    deliveries: Optional[dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(deliveries, dict):
        return {}
    return {
        str(key): {
            "state": str(value.get("state") or "retry"),
            "attempts": max(0, int(value.get("attempts") or 0)),
            "last_error": str(value.get("last_error") or "")[:240],
            "updated_at": max(0, int(value.get("updated_at") or 0)),
        }
        for key, value in deliveries.items()
        if isinstance(value, dict) and str(key)
    }


def _classify_apns_response(status_code: int, reason: str) -> tuple[str, str]:
    normalized_reason = str(reason or "").strip()[:120]
    if 200 <= int(status_code) < 300:
        return "delivered", ""
    error = normalized_reason or f"APNs HTTP {int(status_code)}"
    if (
        int(status_code) in {401, 403, 429}
        or int(status_code) >= 500
        or normalized_reason
        in {
            "ExpiredProviderToken",
            "InvalidProviderToken",
            "TooManyProviderTokenUpdates",
        }
    ):
        return "retry", error
    return "permanent_failure", error


def _send_apns(
    registration: dict[str, Any],
    payload: dict[str, Any],
    collapse_id: str,
) -> tuple[int, str]:
    import httpx
    import jwt

    key_id = os.environ["HERMES_APNS_KEY_ID"].strip()
    team_id = os.environ["HERMES_APNS_TEAM_ID"].strip()
    bundle_id = os.environ.get("HERMES_APNS_BUNDLE_ID", _DEFAULT_BUNDLE_ID).strip()
    environment = os.environ.get("HERMES_APNS_ENVIRONMENT", "production").strip().lower()
    host = "api.sandbox.push.apple.com" if environment == "sandbox" else "api.push.apple.com"
    token = _apns_provider_token(
        jwt,
        key_id=key_id,
        team_id=team_id,
        private_key=_private_key(),
    )
    device_token = str(registration.get("token") or "")
    with httpx.Client(http2=True, timeout=10.0) as client:
        response = client.post(
            f"https://{host}/3/device/{device_token}",
            headers={
                "authorization": f"bearer {token}",
                "apns-topic": bundle_id,
                "apns-push-type": "alert",
                "apns-priority": "10",
                "apns-collapse-id": str(collapse_id or "")[:64],
            },
            json=payload,
        )
    reason = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            reason = str(body.get("reason") or "")
    except (ValueError, TypeError):
        pass
    return int(response.status_code), reason


def _apns_provider_token(
    jwt_module: Any,
    *,
    key_id: str,
    team_id: str,
    private_key: str,
) -> str:
    now = int(time.time())
    cache_key = hashlib.sha256(
        f"{key_id}\0{team_id}\0{private_key}".encode("utf-8")
    ).hexdigest()
    with _PROVIDER_TOKEN_LOCK:
        cached = _PROVIDER_TOKEN_CACHE.get(cache_key)
        if cached is not None and now - cached[1] < 50 * 60:
            return cached[0]
        token = str(
            jwt_module.encode(
                {"iss": team_id, "iat": now},
                private_key,
                algorithm="ES256",
                headers={"kid": key_id},
            )
        )
        _PROVIDER_TOKEN_CACHE.clear()
        _PROVIDER_TOKEN_CACHE[cache_key] = (token, now)
        return token


def _bounded_error(error: Any) -> str:
    error_type = type(error).__name__ if error is not None else "UnknownError"
    return f"notification_delivery_failed:{error_type}"[:240]


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
