"""Thin execution adapter from authoritative Workflow runs to Kanban tasks."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from plugins.workflows.models import DispatchAdapter, WorkflowConflict, WorkflowScope
from plugins.workflows.store import WorkflowStore
from plugins.workflows.workspace_audit import (
    WorkspaceUnavailable,
    capture_snapshot,
    diff_snapshots,
)


class KanbanWorkflowAdapter:
    def dispatch(self, intent: dict[str, Any]) -> str:
        from hermes_cli import kanban_db

        payload = intent.get("payload") or {}
        node = payload.get("node") or {}
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        assignee = str(config.get("profile") or intent["profile_id"] or "default")
        title = str(node.get("title") or node.get("label") or node.get("id") or "Workflow node")
        body = str(config.get("prompt") or config.get("instruction") or "")
        if not body:
            body = json.dumps(config, ensure_ascii=False, sort_keys=True)
        kanban_db.init_db()
        conn = kanban_db.connect()
        try:
            reference = kanban_db.create_task(
                conn,
                title=title,
                body=body,
                assignee=assignee,
                created_by="workflow-runtime",
                workspace_kind=str(config.get("workspace_kind") or "scratch"),
                workspace_path=(
                    str(config["workspace_path"])
                    if config.get("workspace_path") is not None
                    else None
                ),
                tenant=f"workflow:{intent['account_id']}",
                # The Workflow runtime releases this gate only after the
                # encrypted pre-execution snapshot is durable.
                initial_status="blocked",
                idempotency_key=f"workflow:{intent['id']}",
                idempotency_includes_archived=True,
                session_id=str(intent["run_id"]),
            )
            task = kanban_db.get_task(conn, reference)
            if task is not None and task.status not in {"archived", "done"}:
                workspace = kanban_db.resolve_workspace(task)
                kanban_db.set_workspace_path(conn, reference, workspace)
            return reference
        finally:
            conn.close()

    def workspace_info(self, external_ref: str) -> dict[str, Any] | None:
        from hermes_cli import kanban_db

        conn = kanban_db.connect()
        try:
            task = kanban_db.get_task(conn, external_ref)
            if task is None:
                return None
            return {
                "workspace_kind": task.workspace_kind,
                "workspace_path": task.workspace_path,
                "status": task.status,
            }
        finally:
            conn.close()

    def release(self, external_ref: str) -> None:
        from hermes_cli import kanban_db

        conn = kanban_db.connect()
        try:
            task = kanban_db.get_task(conn, external_ref)
            if task is None:
                raise RuntimeError("Kanban task is missing before workspace release")
            if task.status == "blocked" and not kanban_db.unblock_task(conn, external_ref):
                raise RuntimeError("Kanban task workspace gate could not be released")
        finally:
            conn.close()

    def probe(self, external_ref: str) -> tuple[str, Any, str]:
        from hermes_cli import kanban_db

        conn = kanban_db.connect()
        try:
            task = kanban_db.get_task(conn, external_ref)
            if task is None:
                return "failed", None, "Kanban task is missing"
            if task.status == "done":
                return "succeeded", {"result": task.result or ""}, ""
            if task.status == "archived":
                return "failed", None, "Kanban task was archived"
            return "running", None, ""
        finally:
            conn.close()

    def cancel(self, external_ref: str) -> None:
        from hermes_cli import kanban_db

        conn = kanban_db.connect()
        try:
            kanban_db.archive_task(conn, external_ref)
        finally:
            conn.close()


class WorkflowRuntime:
    _BATCH_SIZE = 20
    _LEASE_SECONDS = 120
    _RETRY_SECONDS = 30

    def __init__(
        self,
        store: WorkflowStore,
        adapter: DispatchAdapter | None = None,
        *,
        clock: Callable[[], int | float] | None = None,
        dispatch_heartbeat_interval: float | None = None,
    ):
        self.store = store
        self.adapter = adapter or KanbanWorkflowAdapter()
        self._clock = clock or time.time
        interval = (
            min(5.0, self._LEASE_SECONDS / 3.0)
            if dispatch_heartbeat_interval is None
            else float(dispatch_heartbeat_interval)
        )
        if interval <= 0:
            raise ValueError("dispatch_heartbeat_interval must be positive")
        self._dispatch_heartbeat_interval = interval
        self._tick_lock = threading.Lock()

    def _now(self, fixed: int | None) -> int:
        return int(self._clock() if fixed is None else fixed)

    def _dispatch_with_heartbeat(self, claim: dict[str, Any]) -> str:
        """Keep one dispatch claim alive until its synchronous adapter returns."""

        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(self._dispatch_heartbeat_interval):
                try:
                    renewed = self.store.renew_dispatch_claim(
                        str(claim["id"]),
                        str(claim["lease_token"]),
                        lease_seconds=self._LEASE_SECONDS,
                        now=int(self._clock()),
                    )
                except Exception:
                    # A transient SQLite busy error leaves the current lease in
                    # force. The post-dispatch CAS remains authoritative.
                    continue
                if not renewed:
                    return

        thread = threading.Thread(
            target=heartbeat,
            name=f"workflow-dispatch-{str(claim['id'])[:12]}",
            daemon=True,
        )
        thread.start()
        try:
            return self.adapter.dispatch(claim)
        finally:
            stop.set()
            thread.join()

    @staticmethod
    def _scope_for(item: dict[str, Any]) -> WorkflowScope:
        return WorkflowScope(
            str(item["account_id"]),
            int(item["account_generation"]),
            str(item["profile_id"]),
        )

    def _workspace_info(self, external_ref: str) -> dict[str, Any] | None:
        getter = getattr(self.adapter, "workspace_info", None)
        if not callable(getter):
            return None
        value = getter(external_ref)
        return value if isinstance(value, dict) else None

    def _ensure_workspace_baseline(
        self,
        scope: WorkflowScope,
        run_id: str,
        node_run_id: str,
        external_ref: str,
        *,
        now: int,
    ) -> None:
        if not callable(getattr(self.adapter, "workspace_info", None)):
            return
        existing = self.store.get_workspace_baseline(scope, run_id, node_run_id)
        if existing is not None:
            return
        info = self._workspace_info(external_ref)
        if info is None:
            self.store.mark_workspace_audit_unavailable(
                scope,
                run_id,
                node_run_id,
                reason="Kanban task disappeared before its workspace baseline was captured",
                now=now,
            )
            return
        status = str(info.get("status") or "")
        path = str(info.get("workspace_path") or "").strip()
        if status not in {"blocked", "ready", "todo"}:
            self.store.mark_workspace_audit_unavailable(
                scope,
                run_id,
                node_run_id,
                reason="workspace baseline was not captured before node execution started",
                now=now,
            )
            return
        try:
            snapshot = capture_snapshot(path)
        except WorkspaceUnavailable as exc:
            self.store.mark_workspace_audit_unavailable(
                scope,
                run_id,
                node_run_id,
                reason=f"workspace baseline is unavailable: {exc}",
                now=now,
            )
            return
        self.store.save_workspace_baseline(
            scope,
            run_id,
            node_run_id,
            workspace_kind=str(info.get("workspace_kind") or ""),
            workspace_path=str(Path(path).resolve(strict=False)),
            snapshot=snapshot,
            now=now,
        )

    def _release_workspace_gate(self, external_ref: str) -> None:
        release = getattr(self.adapter, "release", None)
        if callable(release):
            release(external_ref)

    def _finalize_workspace_audit(
        self,
        scope: WorkflowScope,
        run_id: str,
        node_run_id: str,
        external_ref: str,
        *,
        now: int,
    ) -> None:
        if not callable(getattr(self.adapter, "workspace_info", None)):
            return
        baseline = self.store.get_workspace_baseline(scope, run_id, node_run_id)
        if baseline is None:
            self.store.mark_workspace_audit_unavailable(
                scope,
                run_id,
                node_run_id,
                reason="workspace baseline was not captured before the node became terminal",
                now=now,
            )
            return
        if baseline["state"] in {"recorded", "unavailable"}:
            return
        payload = baseline.get("payload") if isinstance(baseline.get("payload"), dict) else {}
        workspace_kind = str(payload.get("workspace_kind") or "")
        baseline_path = str(payload.get("workspace_path") or "")
        before = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
        info = self._workspace_info(external_ref)
        current_path = str((info or {}).get("workspace_path") or "")
        if info is None or not current_path:
            reason = "workspace is unavailable before terminal audit capture"
            if workspace_kind == "scratch":
                reason = "scratch workspace was removed before terminal audit capture"
            self.store.mark_workspace_audit_unavailable(
                scope,
                run_id,
                node_run_id,
                reason=reason,
                discard_baseline=True,
                now=now,
            )
            return
        if Path(current_path).resolve(strict=False) != Path(baseline_path).resolve(strict=False):
            self.store.mark_workspace_audit_unavailable(
                scope,
                run_id,
                node_run_id,
                reason="workspace path changed after its baseline was captured",
                discard_baseline=True,
                now=now,
            )
            return
        try:
            tracked_paths = set(before.get("files") or {})
            after = capture_snapshot(current_path, tracked_paths=tracked_paths)
        except WorkspaceUnavailable as exc:
            reason = f"workspace is unavailable before terminal audit capture: {exc}"
            if workspace_kind == "scratch":
                reason = "scratch workspace was removed before terminal audit capture"
            self.store.mark_workspace_audit_unavailable(
                scope,
                run_id,
                node_run_id,
                reason=reason,
                discard_baseline=True,
                now=now,
            )
            return
        change_set = diff_snapshots(before, after)
        self.store.finalize_workspace_audit(
            scope,
            run_id,
            node_run_id,
            files=change_set["files"],
            summary=str(change_set["summary"]),
            now=now,
        )

    def tick(self, *, now: int | None = None) -> dict[str, int]:
        if not self._tick_lock.acquire(blocking=False):
            return {"dispatched": 0, "completed": 0, "failed": 0}
        dispatched = completed = failed = 0
        try:
            # Cancellation is an outbox, not a best-effort side effect.  Claim one row at a time
            # so work that has not started is never exposed to a shared batch lease.
            for _ in range(self._BATCH_SIZE):
                claims = self.store.claim_cancel_intents(
                    limit=1,
                    lease_seconds=self._LEASE_SECONDS,
                    now=self._now(now),
                )
                if not claims:
                    break
                claim = claims[0]
                try:
                    reference = str(claim.get("external_ref") or "").strip()
                    if not reference:
                        # dispatch() is idempotent on claim['id'].  Calling it here recovers the
                        # reference from a create-before-record crash without creating another task.
                        reference = self.adapter.dispatch(claim)
                        if not self.store.record_cancel_external_ref(
                            claim["id"],
                            claim["lease_token"],
                            reference,
                            now=self._now(now),
                        ):
                            return {
                                "dispatched": dispatched,
                                "completed": completed,
                                "failed": failed,
                            }
                    if not self.store.renew_cancel_claim(
                        claim["id"],
                        claim["lease_token"],
                        lease_seconds=self._LEASE_SECONDS,
                        now=self._now(now),
                    ):
                        return {
                            "dispatched": dispatched,
                            "completed": completed,
                            "failed": failed,
                        }
                    self.adapter.cancel(reference)
                    if not self.store.complete_cancel_claim(
                        claim["id"], claim["lease_token"], now=self._now(now)
                    ):
                        return {
                            "dispatched": dispatched,
                            "completed": completed,
                            "failed": failed,
                        }
                except Exception as exc:
                    if not self.store.fail_cancel_claim(
                        claim["id"],
                        claim["lease_token"],
                        str(exc),
                        retry_after=self._RETRY_SECONDS,
                        now=self._now(now),
                    ):
                        return {
                            "dispatched": dispatched,
                            "completed": completed,
                            "failed": failed,
                        }
                    failed += 1

            # Account deletion is two-phase: the store first tombstones and
            # emits cancellation intents, then runtime purges only after every
            # external cancellation has a durable acknowledgement.
            self.store.finalize_account_deletions(limit=self._BATCH_SIZE)

            # Claim each dispatch immediately before its external call.  A slow first item can no
            # longer consume the leases of the remaining nineteen.  Every post-call CAS uses the
            # same injectable clock contract and stops this worker as soon as ownership is lost.
            for _ in range(self._BATCH_SIZE):
                claims = self.store.claim_dispatch_intents(
                    limit=1,
                    lease_seconds=self._LEASE_SECONDS,
                    now=self._now(now),
                )
                if not claims:
                    break
                claim = claims[0]
                try:
                    external_ref = self._dispatch_with_heartbeat(claim)
                    self._ensure_workspace_baseline(
                        self._scope_for(claim),
                        str(claim["run_id"]),
                        str(claim["node_run_id"]),
                        external_ref,
                        now=self._now(now),
                    )
                    self._release_workspace_gate(external_ref)
                    if self.store.complete_dispatch_claim(
                        claim["id"],
                        claim["lease_token"],
                        external_ref,
                        now=self._now(now),
                    ):
                        dispatched += 1
                    else:
                        return {
                            "dispatched": dispatched,
                            "completed": completed,
                            "failed": failed,
                        }
                except Exception as exc:
                    if not self.store.fail_dispatch_claim(
                        claim["id"],
                        claim["lease_token"],
                        str(exc),
                        retry_after=self._RETRY_SECONDS,
                        now=self._now(now),
                    ):
                        return {
                            "dispatched": dispatched,
                            "completed": completed,
                            "failed": failed,
                        }
                    failed += 1
            for node in self.store.list_external_node_runs(limit=500):
                scope = self._scope_for(node)
                run_id = str(node["run_id"])
                node_run_id = str(node["id"])
                external_ref = str(node["external_ref"])
                self._ensure_workspace_baseline(
                    scope,
                    run_id,
                    node_run_id,
                    external_ref,
                    now=self._now(now),
                )
                state, output, error = self.adapter.probe(external_ref)
                if state == "running":
                    continue
                self._finalize_workspace_audit(
                    scope,
                    run_id,
                    node_run_id,
                    external_ref,
                    now=self._now(now),
                )
                try:
                    self.store.finish_node(
                        scope,
                        run_id,
                        node_run_id,
                        succeeded=state == "succeeded",
                        output=output,
                        error=error,
                        external_ref=external_ref,
                        now=self._now(now),
                    )
                    completed += 1
                except WorkflowConflict:
                    # Cancellation may win after the recovery scan.  Its durable outbox remains
                    # authoritative, so the stale probe result must not revive the node.
                    continue
            return {"dispatched": dispatched, "completed": completed, "failed": failed}
        finally:
            self._tick_lock.release()

    def cancel_external_tasks(self, _run: dict[str, Any], *, now: int | None = None) -> None:
        """Compatibility entry point; cancellation work is sourced from the durable outbox."""

        self.tick(now=now)
