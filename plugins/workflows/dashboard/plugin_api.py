"""Account-scoped API for the bundled Workflow runtime."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from hermes_cli.cloud_file_library import owner_id_from_request
from hermes_cli.profiles import normalize_profile_name
from plugins.workflows.models import (
    SecurityVerdict,
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowScope,
    WorkflowSecurityError,
    WorkflowValidationError,
)
from plugins.workflows.runtime import WorkflowRuntime
from plugins.workflows.store import WorkflowStore, default_store_path


_STORE: WorkflowStore | None = None
_RUNTIME: WorkflowRuntime | None = None


def workflow_store() -> WorkflowStore:
    global _STORE, _RUNTIME
    expected = Path(default_store_path())
    if _STORE is None or _STORE.path != expected:
        _STORE = WorkflowStore(expected)
        _RUNTIME = None
    return _STORE


def workflow_runtime() -> WorkflowRuntime:
    global _RUNTIME
    store = workflow_store()
    if _RUNTIME is None or _RUNTIME.store is not store:
        _RUNTIME = WorkflowRuntime(store)
    return _RUNTIME


@asynccontextmanager
async def workflow_lifespan(_app):
    stop = asyncio.Event()

    async def reconcile() -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(workflow_runtime().tick)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                continue

    task = asyncio.create_task(reconcile())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


router = APIRouter(lifespan=workflow_lifespan)


class DefinitionBody(BaseModel):
    profile_id: str = "default"
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    spec: dict[str, Any]


class VersionBody(BaseModel):
    profile_id: str = "default"
    expected_revision: int = Field(gt=0)
    spec: dict[str, Any]


class StartRunBody(BaseModel):
    profile_id: str = "default"
    version: int | None = Field(default=None, gt=0)
    inputs: dict[str, Any] = Field(default_factory=dict)


class CancelRunBody(BaseModel):
    profile_id: str = "default"
    expected_revision: int = Field(gt=0)
    reason: str = Field(default="", max_length=2000)


class RetryNodeBody(BaseModel):
    profile_id: str = "default"
    expected_revision: int = Field(gt=0)


class ApprovalBody(BaseModel):
    profile_id: str = "default"
    expected_revision: int = Field(gt=0)
    decision: str = "approve"
    request_id: str = ""


def _scope(request: Request, profile_id: str) -> WorkflowScope:
    try:
        profile = normalize_profile_name(profile_id or "default")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Deleted usernames are permanently retired, so generation 1 remains a
    # stable account boundary for the current mobile authentication contract.
    return WorkflowScope(owner_id_from_request(request), 1, profile)


def _key(value: str | None) -> str:
    key = str(value or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    return key


def _raise(exc: Exception) -> None:
    if isinstance(exc, WorkflowNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, WorkflowConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, WorkflowSecurityError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, WorkflowValidationError):
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.get("/health")
def health() -> dict[str, Any]:
    store = workflow_store()
    with store.connect() as conn:
        recoverable = int(conn.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE state='running'"
        ).fetchone()[0])
    return {"ok": True, "schema_version": 1, "recoverable_runs": recoverable}


@router.get("/definitions")
def list_definitions(request: Request, profile_id: str = "default"):
    try:
        return {"definitions": workflow_store().list_definitions(_scope(request, profile_id))}
    except Exception as exc:
        _raise(exc)


@router.post("/definitions")
def create_definition(
    body: DefinitionBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        result = workflow_store().create_definition(
            _scope(request, body.profile_id),
            name=body.name,
            description=body.description,
            spec=body.spec,
            idempotency_key=_key(idempotency_key),
        )
        return {"definition": result}
    except Exception as exc:
        _raise(exc)


@router.get("/definitions/{definition_id}")
def get_definition(definition_id: str, request: Request, profile_id: str = "default"):
    try:
        return {
            "definition": workflow_store().get_definition(
                _scope(request, profile_id), definition_id
            )
        }
    except Exception as exc:
        _raise(exc)


@router.post("/definitions/{definition_id}/versions")
def add_version(
    definition_id: str,
    body: VersionBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        result = workflow_store().add_version(
            _scope(request, body.profile_id),
            definition_id,
            spec=body.spec,
            expected_revision=body.expected_revision,
            idempotency_key=_key(idempotency_key),
        )
        return {"definition": result}
    except Exception as exc:
        _raise(exc)


@router.post("/definitions/{definition_id}/runs")
def start_run(
    definition_id: str,
    body: StartRunBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        result = workflow_store().start_run(
            _scope(request, body.profile_id),
            definition_id,
            version=body.version,
            inputs=body.inputs,
            idempotency_key=_key(idempotency_key),
        )
        workflow_runtime().tick()
        return {"run": workflow_store().get_run(_scope(request, body.profile_id), result["id"])}
    except Exception as exc:
        _raise(exc)


@router.get("/runs")
def list_runs(request: Request, profile_id: str = "default", limit: int = 100):
    try:
        workflow_runtime().tick()
        return {"runs": workflow_store().list_runs(_scope(request, profile_id), limit=limit)}
    except Exception as exc:
        _raise(exc)


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request, profile_id: str = "default"):
    try:
        workflow_runtime().tick()
        return {"run": workflow_store().get_run(_scope(request, profile_id), run_id)}
    except Exception as exc:
        _raise(exc)


@router.get("/runs/{run_id}/workspace-changes")
def list_workspace_changes(
    run_id: str,
    request: Request,
    profile_id: str = "default",
    limit: int = 100,
):
    try:
        scope = _scope(request, profile_id)
        return {
            "change_sets": workflow_store().list_workspace_changes(
                scope, run_id, limit=limit
            ),
            "workspace_audits": workflow_store().list_workspace_audits(scope, run_id),
        }
    except Exception as exc:
        _raise(exc)


@router.get("/runs/{run_id}/workspace-changes/{change_set_id}")
def get_workspace_changes(
    run_id: str,
    change_set_id: str,
    request: Request,
    profile_id: str = "default",
):
    try:
        return {
            "change_set": workflow_store().get_workspace_changes(
                _scope(request, profile_id), run_id, change_set_id
            )
        }
    except Exception as exc:
        _raise(exc)


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    body: CancelRunBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        scope = _scope(request, body.profile_id)
        result = workflow_store().cancel_run(
            scope,
            run_id,
            expected_revision=body.expected_revision,
            reason=body.reason,
            idempotency_key=_key(idempotency_key),
        )
        workflow_runtime().tick()
        return {"run": result}
    except Exception as exc:
        _raise(exc)


@router.post("/runs/{run_id}/nodes/{node_run_id}/retry")
def retry_node(
    run_id: str,
    node_run_id: str,
    body: RetryNodeBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        result = workflow_store().retry_node(
            _scope(request, body.profile_id),
            run_id,
            node_run_id,
            expected_revision=body.expected_revision,
            idempotency_key=_key(idempotency_key),
        )
        workflow_runtime().tick()
        return {"node_run": result}
    except Exception as exc:
        _raise(exc)


@router.post("/runs/{run_id}/nodes/{node_run_id}/approval")
def approve_node(
    run_id: str,
    node_run_id: str,
    body: ApprovalBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        if body.decision.strip().lower() != "approve":
            raise ValueError("workflow node approval only accepts approve")
        scope = _scope(request, body.profile_id)
        run = workflow_store().get_run(scope, run_id)
        node = next((item for item in run["node_runs"] if item["id"] == node_run_id), None)
        if node is None:
            raise WorkflowNotFound("node run was not found")
        spec = node.get("spec") or {}
        arguments = spec.get("tool_args", spec.get("config", {}))
        tool_name = str(spec.get("tool_name") or spec.get("type") or "agent")
        pending = workflow_store().request_tool_approval(
            scope, run_id, node_run_id, tool_name=tool_name, arguments=arguments
        )
        approved = workflow_store().decide_tool_approval(
            scope,
            pending["id"],
            expected_revision=body.expected_revision,
            decision="approve",
            verdict=SecurityVerdict(),
            idempotency_key=_key(idempotency_key),
        )
        consumed = workflow_store().consume_tool_approval(
            scope,
            pending["id"],
            run_id=run_id,
            node_run_id=node_run_id,
            tool_name=tool_name,
            arguments=arguments,
            grant_token=approved["grant_token"],
        )
        if not consumed:
            current = workflow_store().get_tool_approval(scope, pending["id"])
            if current.get("state") != "consumed":
                raise WorkflowConflict("workflow approval could not be consumed")
        workflow_store().release_node_after_approval(scope, run_id, node_run_id)
        workflow_runtime().tick()
        approved.pop("grant_token", None)
        return {"approval": approved}
    except Exception as exc:
        _raise(exc)
