"""Account-scoped HTTP relay for native iOS context and smart weather."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path
import threading
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hermes_cli.cloud_file_library import owner_id_from_request
from hermes_cli.config import get_hermes_home
from hermes_cli.dashboard_auth.mobile_device_store import MobileDeviceStore
from hermes_cli.dashboard_auth.owner_mobile import delete_owner_account_credentials
from hermes_cli.dashboard_auth.mobile_notifications import process_account_deletion_outbox
from hermes_cli.dashboard_auth.token_auth import extract_bearer_token
from hermes_cli.ios_intelligence import AMapClient, IOSIntelligenceStore, QWeatherClient
from hermes_cli.ios_intelligence_config import load_ios_intelligence_config
from hermes_cli.ios_mcp_supervisor import IOSMCPRuntimeSupervisor

try:
    from hermes_cli.ios_intelligence_scheduler import (
        CloudBehaviorSemanticAnalyzer,
        IOSIntelligenceScheduler,
    )
except ImportError:  # During a staged mixed-version rollback.
    CloudBehaviorSemanticAnalyzer = None  # type: ignore[assignment,misc]
    IOSIntelligenceScheduler = None  # type: ignore[assignment,misc]


logger = logging.getLogger(__name__)
_STORE_LOCK = threading.Lock()
_STORE: Optional[IOSIntelligenceStore] = None
_SCHEDULER: Optional[Any] = None
_MCP_RUNTIME: Optional[IOSMCPRuntimeSupervisor] = None


def intelligence_store(database_path: str = "") -> IOSIntelligenceStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            configured = Path(database_path).expanduser() if database_path else None
            if configured is not None and not configured.is_absolute():
                configured = Path(get_hermes_home()) / configured
            _STORE = IOSIntelligenceStore(configured)
        return _STORE


@asynccontextmanager
async def ios_intelligence_lifespan(_app):
    global _MCP_RUNTIME, _SCHEDULER
    scheduler = None
    runtime = None
    if IOSIntelligenceScheduler is not None:
        config = load_ios_intelligence_config()
        store = (
            intelligence_store(config.database_path)
            if config.database_path
            else intelligence_store()
        )
        if config.enabled:
            runtime = IOSMCPRuntimeSupervisor(
                db_dir=store.path,
                host="127.0.0.1",
                base_port=config.supervisor.base_port,
                health_interval_seconds=config.supervisor.health_interval_seconds,
                failure_threshold=config.supervisor.failure_threshold,
                restart_backoff_seconds=config.supervisor.restart_backoff_seconds,
                blue_green_port_offset=config.supervisor.blue_green_port_offset,
                drain_timeout_seconds=config.supervisor.drain_timeout_seconds,
                owner_id=config.owner_id,
                log_directory=(
                    config.log_directory
                    if Path(config.log_directory).is_absolute()
                    else Path(get_hermes_home()) / config.log_directory / "mcp"
                ),
            )
            if config.supervisor.enabled:
                runtime.start()
                _MCP_RUNTIME = runtime
        scheduler = IOSIntelligenceScheduler(
            store=store,
            qweather=(
                QWeatherClient(store, base_url=config.weather.qweather_base_url)
                if config.enabled
                else None
            ),
            amap=(
                AMapClient(base_url=config.weather.amap_base_url)
                if config.enabled
                else None
            ),
            config=config,
            supervisor=runtime.supervisor if runtime else None,
            semantic_analyzer=(
                CloudBehaviorSemanticAnalyzer()
                if (
                    config.enabled
                    and config.semantic.enabled
                    and CloudBehaviorSemanticAnalyzer is not None
                )
                else None
            ),
            cleanup_only=not config.enabled,
        )
        scheduler.start()
        _SCHEDULER = scheduler
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.stop()
        if runtime is not None:
            runtime.stop()
        _MCP_RUNTIME = None
        _SCHEDULER = None


router = APIRouter(lifespan=ios_intelligence_lifespan)


class ContextEvent(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    kind: str = Field(min_length=1, max_length=64)
    timestamp: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class ContextEventBatch(BaseModel):
    device_id: str = Field(min_length=1, max_length=256)
    cursor: str = Field(default="", max_length=256)
    timezone: str = Field(default="Asia/Shanghai", max_length=128)
    # The active profile controls the effective limit at request time.
    events: list[ContextEvent] = Field(default_factory=list, max_length=10_000)


class DeviceCommandPull(BaseModel):
    device_id: str = Field(min_length=1, max_length=256)
    cursor: str = Field(default="", max_length=256)
    limit: int = Field(default=50, ge=1, le=100)
    lease_seconds: int = Field(default=120, ge=15, le=3600)


class DeviceCommandAck(BaseModel):
    device_id: str = Field(min_length=1, max_length=256)
    status: str = Field(default="completed", max_length=32)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = Field(default="", max_length=1024)


class CapabilityBody(BaseModel):
    device_id: str = Field(min_length=1, max_length=256)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    observed_at: int = Field(ge=0)


class AccountExportBody(BaseModel):
    encrypt: Literal[True] = True
    export_passphrase: str = Field(min_length=12, max_length=1024)
    include_cold: bool = True


class AccountDeleteBody(BaseModel):
    confirm: bool = False
    owner_scope: str = Field(min_length=3, max_length=2048)


class BehaviorFeedbackBody(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    observed_at: int | None = Field(default=None, ge=0)
    feedback_id: str = Field(default="", max_length=256)


def _bound_relay_identity(
    request: Request,
    requested_device_id: str,
    *,
    relay_policy: Any | None = None,
) -> tuple[str, str]:
    """Bind a device-directed relay operation to its mobile bearer session."""

    owner_id = owner_id_from_request(request)
    policy = relay_policy or load_ios_intelligence_config().relay
    if not policy.require_device_token:
        return owner_id, requested_device_id

    token = extract_bearer_token(request)
    session = MobileDeviceStore().verify_access(token, touch=False) if token else None
    if session is None:
        raise HTTPException(status_code=401, detail="A valid mobile device token is required")
    if str(session.user_id) != owner_id:
        raise HTTPException(status_code=403, detail="Mobile token account does not match this request")
    if str(session.device_id) != requested_device_id:
        raise HTTPException(status_code=403, detail="Relay device does not match the mobile token")
    return owner_id, str(session.device_id)


@router.get("/health")
def health() -> dict[str, Any]:
    store = intelligence_store()
    runtime_health = _MCP_RUNTIME.health() if _MCP_RUNTIME is not None else {
        "ok": False,
        "running": False,
        "healthy_count": 0,
        "required_count": 0,
        "services": [],
    }
    return {
        "ok": bool(runtime_health["ok"]),
        "schema_version": int(getattr(store, "schema_version", 1)),
        "scheduler_running": bool(_SCHEDULER and _SCHEDULER.running),
        "mcp_supervisor_running": bool(runtime_health["running"]),
        "mcp_runtime": runtime_health,
    }


@router.post("/events/batch")
def ingest_event_batch(request: Request, body: ContextEventBatch) -> dict[str, Any]:
    relay_policy = load_ios_intelligence_config().relay
    owner_id, device_id = _bound_relay_identity(
        request,
        body.device_id,
        relay_policy=relay_policy,
    )
    if len(body.events) > relay_policy.maximum_event_batch:
        raise HTTPException(
            status_code=422,
            detail=f"event batch exceeds configured limit ({relay_policy.maximum_event_batch})",
        )
    events = [event.model_dump() for event in body.events]
    try:
        result = intelligence_store().ingest_events(
            owner_id,
            device_id,
            events,
            body.cursor,
            timezone=body.timezone,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=410, detail="account data was deleted") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return dict(result)


@router.get("/snapshot")
def context_snapshot(
    request: Request,
    timezone: str = Query(default="Asia/Shanghai", max_length=128),
) -> dict[str, Any]:
    return intelligence_store().today_snapshot(
        owner_id_from_request(request),
        timezone,
    )


@router.get("/forecast/active")
def active_forecast(
    request: Request,
    timezone: str = Query(default="Asia/Shanghai", max_length=128),
) -> dict[str, Any]:
    snapshot = intelligence_store().today_snapshot(
        owner_id_from_request(request),
        timezone,
    )
    return {
        "forecast": snapshot.get("active_forecast"),
        "server_time": snapshot.get("server_time"),
    }


@router.post("/capabilities")
def record_capabilities(request: Request, body: CapabilityBody) -> dict[str, Any]:
    owner_id, device_id = _bound_relay_identity(request, body.device_id)
    event = {
        "id": f"capabilities:{device_id}:{body.observed_at}",
        "kind": "device",
        "timestamp": body.observed_at,
        "payload": {"capabilities": body.capabilities},
    }
    return intelligence_store().ingest_events(
        owner_id,
        device_id,
        [event],
        event["id"],
    )


@router.post("/commands/pull")
def pull_commands(request: Request, body: DeviceCommandPull) -> dict[str, Any]:
    relay_policy = load_ios_intelligence_config().relay
    owner_id, device_id = _bound_relay_identity(
        request,
        body.device_id,
        relay_policy=relay_policy,
    )
    return intelligence_store().pull_device_commands(
        owner_id,
        device_id,
        cursor=body.cursor,
        limit=body.limit,
        lease_seconds=min(body.lease_seconds, relay_policy.command_lease_seconds),
        max_attempts=relay_policy.command_max_attempts,
    )


@router.post("/commands/{command_id}/ack")
def acknowledge_command(
    command_id: str,
    request: Request,
    body: DeviceCommandAck,
) -> dict[str, Any]:
    owner_id, device_id = _bound_relay_identity(request, body.device_id)
    applied = intelligence_store().ack_device_command(
        owner_id,
        device_id,
        command_id,
        status=body.status,
        result=body.result,
        error=body.error,
    )
    if not applied:
        raise HTTPException(status_code=404, detail="Device command not found")
    return {"ok": True, "applied": True}


@router.post("/evaluate")
def evaluate_now(request: Request) -> dict[str, Any]:
    owner_id = owner_id_from_request(request)
    if _SCHEDULER is not None:
        return _SCHEDULER.evaluate_account(owner_id, force=True)
    return intelligence_store().evaluate_behavior(owner_id)


@router.get("/places")
def learned_places(request: Request, limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
    """Return the account's durable weighted place graph."""

    store = intelligence_store()
    return {"places": store.list_places(owner_id_from_request(request), limit)}


@router.get("/routes")
def learned_routes(
    request: Request,
    origin_place_id: str = Query(default="", max_length=256),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    store = intelligence_store()
    return {
        "routes": store.list_routes(
            owner_id_from_request(request),
            origin_place_id=origin_place_id or None,
            limit=limit,
        )
    }


@router.post("/feedback")
def behavior_feedback(request: Request, body: BehaviorFeedbackBody) -> dict[str, Any]:
    store = intelligence_store()
    return store.record_behavior_feedback(
        owner_id_from_request(request),
        body.label,
        body.payload,
        observed_at=body.observed_at,
        feedback_id=body.feedback_id or None,
    )


@router.post("/account/export")
def export_account(request: Request, body: AccountExportBody) -> dict[str, Any]:
    """Export every hot, derived and cold account row without exposing secrets."""

    return intelligence_store().export_account(
        owner_id_from_request(request),
        encrypt=True,
        export_passphrase=body.export_passphrase,
        include_cold=body.include_cold,
    )


@router.post("/account/delete")
def delete_account(request: Request, body: AccountDeleteBody) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="confirm must be true")
    owner_id = owner_id_from_request(request)
    owner_scope = body.owner_scope.strip()
    _scope_origin, separator, scope_owner = owner_scope.rpartition("|")
    if not separator or scope_owner.strip().casefold() != owner_id.casefold():
        raise HTTPException(status_code=400, detail="owner_scope does not match account")
    store = intelligence_store()
    store.begin_account_deletion(owner_id, owner_scope)
    mobile_store = MobileDeviceStore()
    try:
        mobile_deletion = mobile_store.begin_account_deletion(owner_id, owner_scope)
    except Exception as exc:
        logger.exception("mobile account deletion intent deferred")
        mobile_deletion = {
            "state": "pending",
            "devices": 0,
            "sessions": 0,
            "apns": 0,
            "error": type(exc).__name__,
        }
    try:
        cleanup_outcomes = process_account_deletion_outbox(
            device_store=mobile_store,
            owner_id=owner_id,
            limit=1,
        )
    except Exception as exc:
        logger.exception("mobile account deletion cleanup deferred")
        cleanup_outcomes = [{
            "state": "retry",
            "deliveries": {},
            "error": type(exc).__name__,
        }]
    cleanup_delivery = cleanup_outcomes[0] if cleanup_outcomes else {
        "state": str(mobile_deletion.get("state") or "pending"),
        "deliveries": {},
        "error": "",
    }
    try:
        result = store.delete_account(owner_id, delete_cold=True)
    except Exception as exc:
        logger.exception("iOS account data cleanup deferred")
        result = {
            "owner_id": owner_id,
            "deleted": {},
            "cold_files_removed": 0,
            "state": "pending",
            "error": type(exc).__name__,
        }
    try:
        credential_cleanup = delete_owner_account_credentials(owner_id)
    except Exception as exc:
        logger.exception("owner credential cleanup deferred")
        credential_cleanup = {
            "disabled": False,
            "config_cleared": False,
            "error": type(exc).__name__,
        }
    result["mobile_auth"] = {
        key: int(mobile_deletion.get(key) or 0)
        for key in ("devices", "sessions", "apns")
    }
    result["device_cleanup"] = {
        "state": str(cleanup_delivery.get("state") or "retry"),
        "devices": len(cleanup_delivery.get("deliveries") or {}),
        "error": str(cleanup_delivery.get("error") or "")[:256],
    }
    result["credential_cleanup"] = credential_cleanup
    result["accepted"] = True
    result["state"] = (
        "complete"
        if result.get("state") == "complete"
        and credential_cleanup.get("config_cleared") is True
        and result["device_cleanup"]["state"]
        in {"delivered", "no_recipients", "permanent_failure"}
        else "pending"
    )
    return result
