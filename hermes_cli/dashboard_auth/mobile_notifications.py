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

from hermes_cli.dashboard_auth.mobile_device_store import (
    ACCOUNT_DELETION_LEASE_SECONDS,
    MobileDeviceStore,
)

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


class _AccountDeletionLeaseLost(RuntimeError):
    pass


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


def build_account_notification_payload(
    *,
    title: str,
    body: str,
    category: str,
    deep_link: str,
    data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a bounded account notification for non-conversation features."""

    normalized_category = _bounded_identifier(category, "hermes")[:64]
    normalized_link = str(deep_link or "").strip()[:1024]
    payload: dict[str, Any] = {
        "aps": {
            "alert": {
                "title": str(title or "Hermes").strip()[:120] or "Hermes",
                "body": str(body or "").strip()[:1200],
            },
            "sound": "default",
            "thread-id": normalized_category,
            "content-available": 1,
        },
        "hermes": {
            "category": normalized_category,
            "deep_link": normalized_link,
            "data": _bounded_notification_data(data or {}),
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        payload["hermes"]["data"] = {}
        payload["aps"]["alert"]["body"] = _fit_notification_body(
            payload,
            str(body or "").strip(),
        )
    return payload


def build_device_relay_wake_payload(
    *,
    command_id: str,
    expires_at: int | None = None,
) -> dict[str, Any]:
    """Build a silent APNs payload that wakes the native Device Relay."""

    data: dict[str, Any] = {
        "command_id": _bounded_identifier(command_id, "command"),
    }
    if expires_at is not None:
        data["valid_until"] = max(0, int(expires_at))
    return {
        "aps": {"content-available": 1},
        "hermes": {
            "category": "device-relay",
            "data": data,
        },
    }


def build_account_deletion_payload(
    *,
    owner_scope: str,
    valid_until: int | None = None,
) -> dict[str, Any]:
    """Build a silent tombstone bound to one exact device account scope."""

    normalized_scope = str(owner_scope or "").strip()
    if not normalized_scope:
        raise ValueError("owner_scope is required")

    expires_at = int(valid_until or (time.time() + 7 * 24 * 60 * 60))
    return {
        "aps": {"content-available": 1},
        "hermes": {
            "category": "account-deletion",
            "data": {
                "action": "delete-account-data",
                "owner_scope": normalized_scope,
                "valid_until": expires_at,
            },
        },
    }


def _bounded_notification_data(data: dict[str, Any]) -> dict[str, Any]:
    bounded: dict[str, Any] = {}
    for raw_key, raw_value in list(data.items())[:32]:
        key = str(raw_key or "").strip()[:64]
        if not key:
            continue
        if isinstance(raw_value, bool) or raw_value is None:
            bounded[key] = raw_value
        elif isinstance(raw_value, (int, float)):
            bounded[key] = raw_value
        elif isinstance(raw_value, str):
            bounded[key] = raw_value[:512]
    return bounded


def _fit_notification_body(payload: dict[str, Any], body: str) -> str:
    low = 0
    high = min(len(body), 1200)
    best = ""
    while low <= high:
        midpoint = (low + high) // 2
        candidate = body[:midpoint]
        payload["aps"]["alert"]["body"] = candidate
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


def account_notification_collapse_id(category: str, notification_id: str) -> str:
    digest = hashlib.sha256(
        f"{category}\0{notification_id}".encode("utf-8")
    ).hexdigest()[:40]
    return f"hermes-notify-{digest}"[:64]


def device_relay_collapse_id(owner_id: str) -> str:
    # One wake is enough to drain every queued command for this account.
    digest = hashlib.sha256(str(owner_id or "").encode("utf-8")).hexdigest()[:40]
    return f"hermes-relay-{digest}"[:64]


def account_deletion_collapse_id(owner_id: str) -> str:
    digest = hashlib.sha256(str(owner_id or "").encode("utf-8")).hexdigest()[:40]
    return f"hermes-delete-{digest}"[:64]


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

    payload = build_task_completion_payload(
        conversation_id=conversation_id,
        turn_id=turn_id,
        status=status,
        result=result,
    )
    return _deliver_account_payload(
        owner_id=owner_id,
        payload=payload,
        collapse_id=collapse_id,
        previous_deliveries=previous_deliveries,
        progress_callback=progress_callback,
        device_store=device_store,
        sender=sender,
    )


def deliver_account_notification_push(
    *,
    owner_id: str,
    notification_id: str,
    title: str,
    body: str,
    category: str,
    deep_link: str,
    data: Optional[dict[str, Any]] = None,
    previous_deliveries: Optional[dict[str, dict[str, Any]]] = None,
    progress_callback: Optional[Callable[[dict[str, dict[str, Any]]], None]] = None,
    device_store: Optional[MobileDeviceStore] = None,
    sender: Optional[Callable[[dict[str, Any], dict[str, Any], str], tuple[int, str]]] = None,
) -> dict[str, Any]:
    """Deliver a generic account-scoped notification through the APNs seam."""

    payload = build_account_notification_payload(
        title=title,
        body=body,
        category=category,
        deep_link=deep_link,
        data={**dict(data or {}), "notification_id": str(notification_id)[:256]},
    )
    return _deliver_account_payload(
        owner_id=owner_id,
        payload=payload,
        collapse_id=account_notification_collapse_id(category, notification_id),
        previous_deliveries=previous_deliveries,
        progress_callback=progress_callback,
        device_store=device_store,
        sender=sender,
    )


def deliver_account_background_wake(
    *,
    owner_id: str,
    command_id: str,
    expires_at: int | None = None,
    previous_deliveries: Optional[dict[str, dict[str, Any]]] = None,
    progress_callback: Optional[Callable[[dict[str, dict[str, Any]]], None]] = None,
    device_store: Optional[MobileDeviceStore] = None,
    sender: Optional[Callable[[dict[str, Any], dict[str, Any], str], tuple[int, str]]] = None,
) -> dict[str, Any]:
    """Wake active iOS devices without presenting a user-visible alert."""

    return _deliver_account_payload(
        owner_id=owner_id,
        payload=build_device_relay_wake_payload(
            command_id=command_id,
            expires_at=expires_at,
        ),
        collapse_id=device_relay_collapse_id(owner_id),
        previous_deliveries=previous_deliveries,
        progress_callback=progress_callback,
        device_store=device_store,
        sender=sender,
        push_type="background",
        priority="5",
    )


def deliver_account_deletion_push(
    *,
    owner_id: str,
    owner_scope: str,
    valid_until: int | None = None,
    previous_deliveries: Optional[dict[str, dict[str, Any]]] = None,
    progress_callback: Optional[Callable[[dict[str, dict[str, Any]]], None]] = None,
    device_store: Optional[MobileDeviceStore] = None,
    sender: Optional[Callable[[dict[str, Any], dict[str, Any], str], tuple[int, str]]] = None,
) -> dict[str, Any]:
    """Tell every active iOS install to purge the deleted account locally."""

    return _deliver_account_payload(
        owner_id=owner_id,
        payload=build_account_deletion_payload(
            owner_scope=owner_scope,
            valid_until=valid_until,
        ),
        collapse_id=account_deletion_collapse_id(owner_id),
        previous_deliveries=previous_deliveries,
        progress_callback=progress_callback,
        device_store=device_store,
        sender=sender,
        push_type="background",
        priority="5",
        include_revoked_devices=True,
    )


def process_account_deletion_outbox(
    *,
    device_store: Optional[MobileDeviceStore] = None,
    owner_id: str = "",
    limit: int = 100,
    sender: Optional[Callable[[dict[str, Any], dict[str, Any], str], tuple[int, str]]] = None,
) -> list[dict[str, Any]]:
    """Deliver durable account tombstones and purge auth rows at a terminal state."""

    store = device_store or MobileDeviceStore()
    outcomes: list[dict[str, Any]] = []
    for item in store.claim_account_deletions(
        limit=limit,
        lease_seconds=ACCOUNT_DELETION_LEASE_SECONDS,
        user_id=str(owner_id or "").strip(),
    ):
        deletion_id = str(item["id"])
        lease_token = str(item.get("lease_token") or "")
        previous = dict(item.get("device_deliveries") or {})
        requested_at = int(item.get("requested_at") or time.time())
        valid_until = max(int(time.time()) + 60, requested_at + 7 * 24 * 60 * 60)

        def persist_progress(deliveries: dict[str, dict[str, Any]]) -> None:
            renewed = store.update_account_deletion_progress(
                deletion_id,
                deliveries,
                lease_token=lease_token,
                lease_seconds=ACCOUNT_DELETION_LEASE_SECONDS,
            )
            if not renewed:
                raise _AccountDeletionLeaseLost("account deletion lease lost")

        try:
            outcome = deliver_account_deletion_push(
                owner_id=str(item["user_id"]),
                owner_scope=str(item["owner_scope"]),
                valid_until=valid_until,
                previous_deliveries=previous,
                progress_callback=persist_progress,
                device_store=store,
                sender=sender,
            )
            delivery_state = str(outcome.get("state") or "retry")
            finalized = store.finish_account_deletion(
                deletion_id,
                delivery_state,
                deliveries=dict(outcome.get("deliveries") or previous),
                lease_token=lease_token,
                error=str(outcome.get("error") or ""),
            )
            outcomes.append({"id": deletion_id, **outcome, "cleanup": finalized})
        except Exception as exc:
            error = (
                "account deletion lease lost"
                if isinstance(exc, _AccountDeletionLeaseLost)
                else _bounded_error(exc)
            )
            finalized = store.finish_account_deletion(
                deletion_id,
                "retry",
                deliveries=previous,
                lease_token=lease_token,
                error=error,
            )
            outcomes.append({
                "id": deletion_id,
                "state": "retry",
                "deliveries": previous,
                "error": error,
                "cleanup": finalized,
            })
    return outcomes


def _deliver_account_payload(
    *,
    owner_id: str,
    payload: dict[str, Any],
    collapse_id: str,
    previous_deliveries: Optional[dict[str, dict[str, Any]]] = None,
    progress_callback: Optional[Callable[[dict[str, dict[str, Any]]], None]] = None,
    device_store: Optional[MobileDeviceStore] = None,
    sender: Optional[Callable[[dict[str, Any], dict[str, Any], str], tuple[int, str]]] = None,
    push_type: str = "alert",
    priority: str = "10",
    include_revoked_devices: bool = False,
) -> dict[str, Any]:
    """Attempt one bounded APNs payload delivery for every active device."""

    normalized_owner_id = str(owner_id or "").strip()
    if not normalized_owner_id:
        return {
            "state": "no_recipients",
            "deliveries": {},
            "error": "notification_owner_missing",
        }
    if not include_revoked_devices and sender is None and not apns_configured():
        return {
            "state": "retry",
            "deliveries": dict(previous_deliveries or {}),
            "error": "apns_not_configured",
        }
    bundle_id = os.environ.get("HERMES_APNS_BUNDLE_ID", _DEFAULT_BUNDLE_ID).strip()
    environment = os.environ.get("HERMES_APNS_ENVIRONMENT", "production").strip().lower()
    store = device_store or MobileDeviceStore()
    registration_loader = store.list_active_apns_registrations
    if include_revoked_devices:
        registration_loader = getattr(
            store,
            "list_account_deletion_apns_registrations",
            registration_loader,
        )
    registrations = [
        registration
        for registration in registration_loader(
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

    if sender is None and not apns_configured():
        return {
            "state": "retry",
            "deliveries": deliveries,
            "error": "apns_not_configured",
        }

    send = sender or (
        lambda registration, current_payload, current_collapse_id: _send_apns(
            registration,
            current_payload,
            current_collapse_id,
            push_type=push_type,
            priority=priority,
        )
    )
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
    *,
    push_type: str = "alert",
    priority: str = "10",
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
    headers = {
        "authorization": f"bearer {token}",
        "apns-topic": bundle_id,
        "apns-push-type": str(push_type or "alert"),
        "apns-priority": str(priority or "10"),
        "apns-collapse-id": str(collapse_id or "")[:64],
    }
    expiration = _payload_expiration(payload)
    if expiration is not None:
        headers["apns-expiration"] = str(expiration)
    with httpx.Client(http2=True, timeout=10.0) as client:
        response = client.post(
            f"https://{host}/3/device/{device_token}",
            headers=headers,
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


def _payload_expiration(payload: dict[str, Any]) -> int | None:
    hermes = payload.get("hermes")
    data = hermes.get("data") if isinstance(hermes, dict) else None
    value = data.get("valid_until") if isinstance(data, dict) else None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return int(timestamp) if timestamp > 0 else None


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
