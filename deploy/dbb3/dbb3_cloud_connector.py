#!/usr/bin/env python3
"""DBB3/PC -> Hermes collaboration connector.

This process is deliberately a small polling bridge.  It creates an idempotent
Kanban root for each leased cloud run, reports only compact per-run checkpoints,
and uploads declared completion artifacts as raw bytes.  The checkpoint file
is the recovery boundary: a restart reuses the same idempotency key, cursor,
and artifact keys instead of creating duplicate work or traffic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


CONTRACT_VERSION = 1
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SESSION_ID_RE = re.compile(r"\b\d{8}_\d{6}_[a-z0-9]+\b", re.IGNORECASE)
_SESSION_REFRESH_SECONDS = 5.0
_DEFAULT_CANCEL_COMMAND = "hermes kanban block {root_id} {reason}"
_SENSITIVE_KEYS = {
    "authorization",
    "proxy_authorization",
    "set_cookie",
    "cookie",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "auth_token",
    "session_token",
    "token",
    "password",
    "passwd",
    "secret",
    "credential",
    "credentials",
    "private_key",
}
_INLINE_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(Authorization|Proxy-Authorization|Cookie|Set-Cookie|"
        r"X-Api-Key|Api-Key|API[_ -]?Key|Access[_ -]?Token|Refresh[_ -]?Token|"
        r"Password|Passwd|Secret|Credential)\b\s*[:=]\s*([^\s,;]+|\"[^\"]*\"|'[^']*')"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z0-9_])((?:[A-Za-z][A-Za-z0-9_]*_)?(?:"
        r"API_KEY|APIKEY|ACCESS_TOKEN|REFRESH_TOKEN|AUTH_TOKEN|SESSION_TOKEN|TOKEN|"
        r"PASSWORD|PASSWD|SECRET|CREDENTIAL|CREDENTIALS|PRIVATE_KEY))"
        r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ),
)


class CloudHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str = "") -> None:
        self.status = int(status)
        self.detail = str(detail or "")[:1000]
        super().__init__(f"cloud HTTP {self.status}: {self.detail}")


class ConnectorAuthError(CloudHTTPError):
    pass


class ConnectorContractError(CloudHTTPError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any, limit: int = 4000) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def _redact_sensitive(value: Any) -> Any:
    """Remove credentials before connector telemetry leaves the device."""

    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _sensitive_key(key)
                else _redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(
                _redact_sensitive(parsed),
                ensure_ascii=False,
                separators=(",", ":"),
            )
    redacted = _INLINE_SECRET_PATTERNS[0].sub(r"\1 [REDACTED]", value)
    for pattern in _INLINE_SECRET_PATTERNS[1:]:
        redacted = pattern.sub(r"\1: [REDACTED]", redacted)
    return redacted


def _sensitive_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return normalized in _SENSITIVE_KEYS or any(
        normalized.endswith(suffix)
        for suffix in (
            "_api_key",
            "_access_token",
            "_refresh_token",
            "_auth_token",
            "_session_token",
            "_password",
            "_passwd",
            "_secret",
            "_credential",
            "_credentials",
            "_private_key",
        )
    )


def _structured_text(value: Any, limit: int = 12000) -> str:
    redacted = _redact_sensitive(value)
    if redacted is None:
        return ""
    if isinstance(redacted, str):
        return _text(redacted, limit)
    if isinstance(redacted, (dict, list)):
        try:
            return _text(
                json.dumps(redacted, ensure_ascii=False, separators=(",", ":")),
                limit,
            )
        except (TypeError, ValueError):
            pass
    return _text(redacted, limit)


def _timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not 0 < numeric:
        return int(numeric)
    return int(numeric * 1000) if numeric < 10_000_000_000 else int(numeric)


def _json_body(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorContractError(502, f"invalid JSON response: {exc}") from exc


def run(command: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except Exception as exc:  # noqa: BLE001 - connector must keep polling
        return 124, f"{type(exc).__name__}: {exc}"


class _FileBody:
    """File-like request body accepted by urllib without base64 buffering."""

    def __init__(self, path: Path) -> None:
        self._handle = path.open("rb")

    def read(self, size: int = -1) -> bytes:
        return self._handle.read(size)

    def close(self) -> None:
        self._handle.close()


class CloudRelayClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        connector_id: str = "dbb3-primary",
        timeout: float = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.connector_id = _text(connector_id, 128) or "dbb3-primary"
        self.timeout = timeout

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        body_path: Path | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        body: bytes | _FileBody | None = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        elif body_path is not None:
            body = _FileBody(body_path)
        request_headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Connector-ID": self.connector_id,
            "User-Agent": "dbb3-cloud-connector/2.0",
            **(headers or {}),
        }
        if payload is not None:
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        if body_path is not None:
            request_headers.setdefault("Content-Length", str(body_path.stat().st_size))
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return _json_body(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", "replace")
            if exc.code == 401:
                raise ConnectorAuthError(exc.code, detail) from exc
            if exc.code in {409, 422}:
                raise ConnectorContractError(exc.code, detail) from exc
            raise CloudHTTPError(exc.code, detail) from exc
        except (OSError, urllib.error.URLError):
            raise
        finally:
            if isinstance(body, _FileBody):
                body.close()

    def probe(self) -> dict[str, Any]:
        result = self._request(
            "/connector/health",
            headers={"Accept": "application/json"},
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ConnectorContractError(502, "connector health contract is invalid")
        if int(result.get("contract_version") or 0) != CONTRACT_VERSION:
            raise ConnectorContractError(409, "unsupported connector contract version")
        return result

    def pull_runs(self, limit: int = 5, lease_seconds: int = 90) -> list[dict[str, Any]]:
        result = self._request(
            "/connector/runs/pull",
            method="POST",
            payload={
                "connector_id": self.connector_id,
                "limit": max(1, min(int(limit), 20)),
                "lease_seconds": max(15, min(int(lease_seconds), 900)),
            },
        )
        return list(result.get("runs") or []) if isinstance(result, dict) else []

    def acknowledge_run(self, run: dict[str, Any], local: dict[str, Any], lease_seconds: int = 90) -> None:
        remote_id = urllib.parse.quote(_text(run.get("remote_run_id"), 256), safe="")
        self._request(
            f"/connector/runs/{remote_id}/ack",
            method="POST",
            payload={
                "connector_id": self.connector_id,
                "idempotency_key": _text(run.get("idempotency_key"), 512),
                "remote_task_id": _text(local.get("remote_task_id"), 256),
                "root_task_id": _text(local.get("root_task_id"), 256),
                "session_id": _text(local.get("session_id"), 256),
                "accepted_at": now_iso(),
                "lease_seconds": max(15, min(int(lease_seconds), 900)),
            },
        )

    def report_status(self, remote_run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = urllib.parse.quote(_text(remote_run_id, 256), safe="")
        return self._request(f"/connector/runs/{encoded}/status", method="POST", payload=payload)

    def fail_run(self, remote_run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = urllib.parse.quote(_text(remote_run_id, 256), safe="")
        return self._request(f"/connector/runs/{encoded}/fail", method="POST", payload=payload)

    def pull_cancellations(self, limit: int = 5, lease_seconds: int = 90) -> list[dict[str, Any]]:
        result = self._request(
            "/connector/cancellations/pull",
            method="POST",
            payload={
                "connector_id": self.connector_id,
                "limit": max(1, min(int(limit), 20)),
                "lease_seconds": max(15, min(int(lease_seconds), 900)),
            },
        )
        return list(result.get("cancellations") or []) if isinstance(result, dict) else []

    def acknowledge_cancel(
        self,
        cancellation: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        remote_id = urllib.parse.quote(_text(cancellation.get("remote_run_id"), 256), safe="")
        result = self._request(
            f"/connector/runs/{remote_id}/cancel-ack",
            method="POST",
            payload=payload,
        )
        return result if isinstance(result, dict) else {}

    def upload_artifact(
        self,
        remote_run_id: str,
        *,
        relative_path: str,
        filename: str,
        sha256: str,
        media_type: str,
        path: Path,
    ) -> dict[str, Any]:
        encoded = urllib.parse.quote(_text(remote_run_id, 256), safe="")
        relative_header = urllib.parse.quote(relative_path, safe="/")
        filename_header = urllib.parse.quote(filename, safe="")
        return self._request(
            f"/connector/runs/{encoded}/artifacts",
            method="POST",
            body_path=path,
            headers={
                "Content-Type": media_type or "application/octet-stream",
                "X-Remote-Run-ID": _text(remote_run_id, 256),
                "X-Relative-Path": relative_header,
                "X-Filename": filename_header,
                "X-Content-SHA256": sha256,
            },
        )

    def list_run_attachments(self, remote_run_id: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(_text(remote_run_id, 256), safe="")
        result = self._request(f"/connector/runs/{encoded}/attachments")
        return list(result.get("attachments") or []) if isinstance(result, dict) else []

    def download_run_attachment(
        self,
        remote_run_id: str,
        file_id: str,
        *,
        target: Path,
        expected_sha256: str,
        expected_size: int,
    ) -> Path:
        encoded_run = urllib.parse.quote(_text(remote_run_id, 256), safe="")
        encoded_file = urllib.parse.quote(_text(file_id, 256), safe="")
        expected_sha256 = _text(expected_sha256, 64).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ConnectorContractError(422, "attachment SHA-256 is invalid")
        if expected_size < 0 or expected_size > MAX_ARTIFACT_BYTES:
            raise ConnectorContractError(422, "attachment size is invalid")
        request = urllib.request.Request(
            self.base_url + f"/connector/runs/{encoded_run}/attachments/{encoded_file}",
            method="GET",
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-Connector-ID": self.connector_id,
                "User-Agent": "dbb3-cloud-connector/2.0",
                "Accept": "application/octet-stream",
            },
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        content_length = response.headers.get("Content-Length")
                        if content_length and int(content_length) > MAX_ARTIFACT_BYTES:
                            raise ConnectorContractError(413, "attachment exceeds 64 MiB limit")
                        for chunk in iter(lambda: response.read(1024 * 1024), b""):
                            total += len(chunk)
                            if total > MAX_ARTIFACT_BYTES:
                                raise ConnectorContractError(413, "attachment exceeds 64 MiB limit")
                            digest.update(chunk)
                            handle.write(chunk)
                except urllib.error.HTTPError as exc:
                    detail = exc.read(4096).decode("utf-8", "replace")
                    if exc.code == 401:
                        raise ConnectorAuthError(exc.code, detail) from exc
                    if exc.code in {409, 422}:
                        raise ConnectorContractError(exc.code, detail) from exc
                    raise CloudHTTPError(exc.code, detail) from exc
                handle.flush()
                os.fsync(handle.fileno())
            if expected_size and total != expected_size:
                raise ConnectorContractError(422, "attachment size mismatch")
            if digest.hexdigest() != expected_sha256:
                raise ConnectorContractError(422, "attachment SHA-256 mismatch")
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            return target
        finally:
            if fd >= 0:
                os.close(fd)
            Path(temporary).unlink(missing_ok=True)


class CheckpointStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "runs": {}, "cancellations": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "runs": {}, "cancellations": {}}
        if not isinstance(value, dict):
            return {"version": 1, "runs": {}, "cancellations": {}}
        value.setdefault("version", 1)
        value.setdefault("runs", {})
        value.setdefault("cancellations", {})
        return value

    def save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:
                os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                Path(temporary).unlink(missing_ok=True)
            except PermissionError:
                # Windows may retain a handle briefly after fdopen closes;
                # the next checkpoint replaces the same temp name safely.
                pass


def _safe_filename(path: Path) -> str:
    name = path.name.replace("\x00", "").strip() or "artifact"
    name = _SAFE_NAME_RE.sub("_", name).strip(" .") or "artifact"
    return name[:180]


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            if size > MAX_ARTIFACT_BYTES:
                raise ValueError("artifact exceeds 64 MiB limit")
            digest.update(chunk)
    return digest.hexdigest(), size


def _artifact_paths(detail: dict[str, Any]) -> list[str]:
    found: list[str] = []
    task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
    events = detail.get("events") if isinstance(detail.get("events"), list) else []
    runs = detail.get("runs") if isinstance(detail.get("runs"), list) else []
    for source in [task, *events, *runs]:
        candidates: Any = None
        if isinstance(source, dict):
            candidates = source.get("artifacts")
            if candidates is None and isinstance(source.get("payload"), dict):
                candidates = source["payload"].get("artifacts")
            if candidates is None and isinstance(source.get("metadata"), dict):
                candidates = source["metadata"].get("artifacts")
        if isinstance(candidates, str):
            candidates = [candidates]
        if isinstance(candidates, (list, tuple)):
            for candidate in candidates:
                value = _text(candidate, 2048)
                if value and value not in found:
                    found.append(value)
    return found[:100]


def _activity_status(*values: Any) -> str:
    joined = " ".join(_text(value, 80).lower() for value in values if value)
    if any(marker in joined for marker in ("fail", "error", "block", "crash", "timeout")):
        return "failed"
    if any(marker in joined for marker in ("cancel", "canceled", "cancelled")):
        return "cancelled"
    if any(
        marker in joined
        for marker in ("start", "running", "progress", "spawn", "claim", "queue", "lease", "wait")
    ):
        return "running"
    return "completed"


def _activity_timing(source: dict[str, Any], fallback: Any) -> tuple[int | None, int | None, int | None]:
    started_at = _timestamp_ms(
        source.get("started_at")
        or source.get("start_time")
        or source.get("created_at")
        or fallback
    )
    completed_at = _timestamp_ms(
        source.get("completed_at")
        or source.get("ended_at")
        or source.get("end_time")
    )
    duration_ms: int | None = None
    raw_duration = source.get("duration_ms")
    if isinstance(raw_duration, (int, float)):
        duration_ms = max(0, int(raw_duration))
    elif isinstance(source.get("duration_seconds"), (int, float)):
        duration_ms = max(0, round(float(source["duration_seconds"]) * 1000))
    elif isinstance(source.get("duration_s"), (int, float)):
        duration_ms = max(0, round(float(source["duration_s"]) * 1000))
    elif started_at is not None and completed_at is not None:
        duration_ms = max(0, completed_at - started_at)
    return started_at, completed_at, duration_ms


def _event_activity(event: dict[str, Any], index: int) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    kind = _text(event.get("kind") or payload.get("kind"), 80).lower() or "status"
    tool_name = _text(
        payload.get("tool_name")
        or payload.get("tool")
        or metadata.get("tool_name")
        or metadata.get("tool"),
        256,
    )
    if not tool_name and any(marker in kind for marker in ("tool", "command", "search", "browser")):
        tool_name = _text(payload.get("name") or kind, 256)
    category = _text(payload.get("category"), 80)
    if not category:
        category = (
            "tool"
            if tool_name
            else "reasoning"
            if any(marker in kind for marker in ("reason", "think"))
            else "search"
            if "search" in kind
            else "status"
        )
    name = _text(
        payload.get("name")
        or payload.get("title")
        or payload.get("label")
        or tool_name
        or kind.replace("_", " "),
        256,
    )
    input_text = _structured_text(
        payload.get("input")
        or payload.get("args")
        or payload.get("arguments")
        or payload.get("command")
        or payload.get("query")
        or payload.get("request"),
        12000,
    )
    output_text = _structured_text(
        payload.get("output")
        or payload.get("result")
        or payload.get("result_text")
        or payload.get("response"),
        12000,
    )
    error_text = _structured_text(payload.get("error"), 4000)
    detail_text = _structured_text(
        payload.get("detail")
        or payload.get("message")
        or payload.get("body")
        or error_text
        or payload,
        12000,
    )
    summary = _text(
        _redact_sensitive(
            payload.get("summary")
            or payload.get("message")
            or payload.get("title")
            or output_text
            or detail_text
            or name
        ),
        2000,
    )
    started_at, completed_at, duration_ms = _activity_timing(payload, event.get("created_at"))
    return {
        "id": _text(
            event.get("id")
            or f"event:{event.get('run_id') or 'task'}:{index}:{kind}:{event.get('created_at') or 0}",
            256,
        ),
        "kind": kind,
        "category": category,
        "name": name,
        "tool_name": tool_name,
        "status": _activity_status(payload.get("status"), kind, error_text),
        "input": input_text,
        "output": output_text,
        "detail": detail_text,
        "summary": summary,
        "error": error_text,
        "model": _text(payload.get("model") or metadata.get("model"), 256),
        "provider": _text(payload.get("provider") or metadata.get("provider"), 256),
        "run_id": event.get("run_id"),
        "created_at": _timestamp_ms(event.get("created_at")),
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
    }


def _comment_activity(comment: dict[str, Any], index: int) -> dict[str, Any]:
    author = _text(comment.get("author"), 256) or "worker"
    body = _structured_text(comment.get("body"), 12000)
    created_at = _timestamp_ms(comment.get("created_at"))
    digest = hashlib.sha256(f"{author}\0{body}".encode("utf-8")).hexdigest()[:16]
    return {
        "id": _text(comment.get("id") or f"comment:{index}:{digest}", 256),
        "kind": "comment",
        "category": "message",
        "name": author,
        "tool_name": "",
        "status": "completed",
        "input": "",
        "output": body,
        "detail": body,
        "summary": body[:2000],
        "model": "",
        "provider": "",
        "created_at": created_at,
        "started_at": created_at,
        "completed_at": created_at,
        "duration_ms": 0,
    }


def _run_activity(run_detail: dict[str, Any], index: int) -> dict[str, Any]:
    metadata = run_detail.get("metadata") if isinstance(run_detail.get("metadata"), dict) else {}
    commands = metadata.get("commands") if isinstance(metadata.get("commands"), list) else []
    tool_name = _text(
        metadata.get("tool_name")
        or metadata.get("tool")
        or ("terminal" if commands else ""),
        256,
    )
    status = _activity_status(
        run_detail.get("status"),
        run_detail.get("outcome"),
        run_detail.get("error"),
    )
    started_at, completed_at, duration_ms = _activity_timing(
        run_detail,
        run_detail.get("started_at"),
    )
    summary = _structured_text(
        run_detail.get("summary")
        or run_detail.get("error")
        or run_detail.get("outcome")
        or run_detail.get("status"),
        2000,
    )
    detail_text = _structured_text(metadata or run_detail.get("error") or summary, 12000)
    return {
        "id": _text(run_detail.get("id") or f"run:{index}", 256),
        "kind": "run",
        "category": "tool" if tool_name else "status",
        "name": _text(run_detail.get("profile") or "Hermes worker run", 256),
        "tool_name": tool_name,
        "status": status,
        "input": _structured_text(
            metadata.get("input")
            or metadata.get("args")
            or metadata.get("objective")
            or commands,
            12000,
        ),
        "output": _structured_text(run_detail.get("summary") or run_detail.get("error"), 12000),
        "detail": detail_text,
        "summary": summary,
        "error": _structured_text(run_detail.get("error"), 4000),
        "model": _text(metadata.get("model") or metadata.get("actual_model"), 256),
        "provider": _text(metadata.get("provider") or metadata.get("actual_provider"), 256),
        "run_id": run_detail.get("id"),
        "created_at": started_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
    }


def _remote_activities(detail: dict[str, Any]) -> list[dict[str, Any]]:
    events = detail.get("events") if isinstance(detail.get("events"), list) else []
    comments = detail.get("comments") if isinstance(detail.get("comments"), list) else []
    runs = detail.get("runs") if isinstance(detail.get("runs"), list) else []
    activities = [
        *(
            _event_activity(item, index)
            for index, item in enumerate(events[-200:])
            if isinstance(item, dict)
        ),
        *(
            _comment_activity(item, index)
            for index, item in enumerate(comments[-200:])
            if isinstance(item, dict)
        ),
        *(
            _run_activity(item, index)
            for index, item in enumerate(runs[-200:])
            if isinstance(item, dict)
        ),
    ]
    activities.sort(
        key=lambda item: (
            int(item.get("started_at") or item.get("created_at") or 0),
            str(item.get("id") or ""),
        )
    )
    return activities[-200:]


def _tool_result_error(value: Any) -> str:
    redacted = _redact_sensitive(value)
    parsed = redacted
    if isinstance(redacted, str):
        try:
            parsed = json.loads(redacted)
        except (TypeError, ValueError):
            parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if error not in (None, "", False):
            return _structured_text(error, 4000)
        exit_code = parsed.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return _structured_text(parsed, 4000)
        return ""
    text = _structured_text(redacted, 4000)
    if any(marker in text.lower() for marker in ("traceback", "exception:")):
        return text
    return ""


def _session_record_activities(
    record: dict[str, Any],
    *,
    profile: str,
    terminal: bool = False,
) -> list[dict[str, Any]]:
    """Project one redacted official Hermes session into native activities."""
    session_id = _text(record.get("id"), 256)
    model = _text(record.get("model"), 256)
    provider = _text(
        record.get("billing_provider") or record.get("provider"),
        256,
    )
    messages = [
        item
        for item in record.get("messages") or []
        if isinstance(item, dict)
    ]
    tool_results = {
        _text(item.get("tool_call_id"), 256): item
        for item in messages
        if str(item.get("role") or "").lower() == "tool"
        and _text(item.get("tool_call_id"), 256)
    }
    consumed_results: set[str] = set()
    activities: list[dict[str, Any]] = []
    started_at = _timestamp_ms(record.get("started_at"))
    completed_at = _timestamp_ms(record.get("ended_at"))
    if terminal and completed_at is None:
        completed_at = max(
            (
                _timestamp_ms(item.get("timestamp")) or 0
                for item in messages
            ),
            default=0,
        ) or None
    duration_ms = (
        max(0, completed_at - started_at)
        if started_at is not None and completed_at is not None
        else None
    )
    activities.append(
        {
            "id": f"session:{session_id}:summary",
            "kind": "session",
            "category": "status",
            "name": profile or "Hermes",
            "tool_name": "",
            "status": "completed" if completed_at is not None else "running",
            "input": "",
            "output": _structured_text(
                {
                    "message_count": record.get("message_count"),
                    "tool_call_count": record.get("tool_call_count"),
                    "api_call_count": record.get("api_call_count"),
                },
                2000,
            ),
            "detail": _structured_text(
                {
                    "session_id": session_id,
                    "model": model,
                    "provider": provider,
                    "message_count": record.get("message_count"),
                    "tool_call_count": record.get("tool_call_count"),
                    "input_tokens": record.get("input_tokens"),
                    "output_tokens": record.get("output_tokens"),
                    "reasoning_tokens": record.get("reasoning_tokens"),
                },
                4000,
            ),
            "summary": f"{int(record.get('message_count') or len(messages))} messages",
            "error": "",
            "model": model,
            "provider": provider,
            "created_at": started_at,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": duration_ms,
        }
    )

    for index, message in enumerate(messages):
        role = str(message.get("role") or "").strip().lower()
        timestamp = _timestamp_ms(message.get("timestamp"))
        if role == "assistant":
            reasoning = _structured_text(
                message.get("reasoning_content") or message.get("reasoning"),
                12000,
            )
            if reasoning:
                activities.append(
                    {
                        "id": f"session:{session_id}:reasoning:{message.get('id') or index}",
                        "kind": "reasoning",
                        "category": "reasoning",
                        "name": "模型思考",
                        "tool_name": "",
                        "status": "completed",
                        "input": "",
                        "output": reasoning,
                        "detail": reasoning,
                        "summary": reasoning[:2000],
                        "error": "",
                        "model": model,
                        "provider": provider,
                        "created_at": timestamp,
                        "started_at": timestamp,
                        "completed_at": timestamp,
                        "duration_ms": 0,
                    }
                )
            content = _structured_text(message.get("content"), 12000)
            if content:
                activities.append(
                    {
                        "id": f"session:{session_id}:message:{message.get('id') or index}",
                        "kind": "message",
                        "category": "message",
                        "name": profile or "Hermes",
                        "tool_name": "",
                        "status": "completed",
                        "input": "",
                        "output": content,
                        "detail": content,
                        "summary": content[:2000],
                        "error": "",
                        "model": model,
                        "provider": provider,
                        "created_at": timestamp,
                        "started_at": timestamp,
                        "completed_at": timestamp,
                        "duration_ms": 0,
                    }
                )
            for call_index, tool_call in enumerate(message.get("tool_calls") or []):
                if not isinstance(tool_call, dict):
                    continue
                function = (
                    tool_call.get("function")
                    if isinstance(tool_call.get("function"), dict)
                    else {}
                )
                call_id = _text(
                    tool_call.get("id") or tool_call.get("call_id"),
                    256,
                ) or f"{message.get('id') or index}:{call_index}"
                tool_name = _text(
                    function.get("name") or tool_call.get("name"),
                    256,
                ) or "tool"
                result = tool_results.get(call_id)
                if result is not None:
                    consumed_results.add(call_id)
                result_text = _structured_text(
                    result.get("content") if result is not None else "",
                    12000,
                )
                completed = (
                    _timestamp_ms(result.get("timestamp"))
                    if result is not None
                    else None
                )
                error = _tool_result_error(
                    result.get("content") if result is not None else ""
                )
                activities.append(
                    {
                        "id": f"session:{session_id}:tool:{call_id}",
                        "kind": "tool",
                        "category": (
                            "search"
                            if any(
                                marker in tool_name.lower()
                                for marker in ("search", "browse", "fetch", "web")
                            )
                            else "tool"
                        ),
                        "name": tool_name,
                        "tool_name": tool_name,
                        "status": "failed" if error else "completed" if result is not None else "running",
                        "input": _structured_text(
                            function.get("arguments") or tool_call.get("arguments"),
                            12000,
                        ),
                        "output": result_text,
                        "detail": result_text,
                        "summary": result_text[:2000] or tool_name,
                        "error": error,
                        "model": model,
                        "provider": provider,
                        "created_at": timestamp,
                        "started_at": timestamp,
                        "completed_at": completed,
                        "duration_ms": (
                            max(0, completed - timestamp)
                            if timestamp is not None and completed is not None
                            else None
                        ),
                    }
                )
        elif role == "tool":
            call_id = _text(message.get("tool_call_id"), 256)
            if call_id in consumed_results:
                continue
            tool_name = _text(message.get("tool_name"), 256) or "tool"
            result_text = _structured_text(message.get("content"), 12000)
            activities.append(
                {
                    "id": f"session:{session_id}:tool-result:{call_id or message.get('id') or index}",
                    "kind": "tool",
                    "category": "tool",
                    "name": tool_name,
                    "tool_name": tool_name,
                    "status": "completed",
                    "input": "",
                    "output": result_text,
                    "detail": result_text,
                    "summary": result_text[:2000] or tool_name,
                    "error": "",
                    "model": model,
                    "provider": provider,
                    "created_at": timestamp,
                    "started_at": timestamp,
                    "completed_at": timestamp,
                    "duration_ms": 0,
                }
            )

    activities.sort(
        key=lambda item: (
            int(item.get("created_at") or item.get("started_at") or 0),
            str(item.get("id") or ""),
        )
    )
    return activities[-200:]


def build_root_task_command(run_payload: dict[str, Any]) -> list[str]:
    objective = _text(run_payload.get("objective"), 12000)
    title = _text(run_payload.get("title"), 120) or next(
        (line.strip()[:120] for line in objective.splitlines() if line.strip()),
        "云端任务",
    )
    profile = _text(run_payload.get("profile"), 128) or "default"
    idempotency = _text(run_payload.get("idempotency_key"), 512)
    body = objective + "\n\nCloud hosted run: " + _text(run_payload.get("remote_run_id"), 256)
    workspace_path = _text(run_payload.get("workspace_path"), 2048)
    workspace = f"dir:{workspace_path}" if workspace_path else "scratch"
    command = [
        "hermes",
        "kanban",
        "create",
        title,
        "--body",
        body,
        "--assignee",
        profile,
        "--triage",
        "--workspace",
        workspace,
        "--created-by",
        "dbb3-cloud-connector",
        "--idempotency-key",
        idempotency,
        "--json",
    ]
    max_runtime = run_payload.get("max_runtime_seconds")
    try:
        if max_runtime:
            command.extend(["--max-runtime", f"{max(60, int(max_runtime))}s"])
    except (TypeError, ValueError):
        pass
    return command


class DBB3CloudConnector:
    def __init__(
        self,
        cloud_client: CloudRelayClient,
        *,
        command_runner: Callable[..., tuple[int, str]] = run,
        state_file: Path | str | None = None,
        artifact_roots: Iterable[Path | str] | None = None,
        cancel_command: str | None = None,
        clock: Callable[[], str] = now_iso,
    ) -> None:
        self.cloud_client = cloud_client
        self.command_runner = command_runner
        self.clock = clock
        path = Path(
            state_file
            or os.environ.get(
                "DBB3_CONNECTOR_STATE_FILE",
                "/home/hermes/.local/state/dbb3-cloud-connector/checkpoint.json",
            )
        )
        self.checkpoints = CheckpointStore(path)
        self.attachment_root = path.parent / "attachments"
        self.attachment_root.mkdir(parents=True, exist_ok=True)
        roots = artifact_roots or os.environ.get(
            "DBB3_CONNECTOR_ARTIFACT_ROOTS",
            "/home/hermes/.hermes:/opt/dbb3-team",
        ).split(os.pathsep)
        self.artifact_roots = [Path(root).expanduser().resolve() for root in roots if str(root).strip()]
        private_artifact_root = self.attachment_root.resolve()
        if private_artifact_root not in self.artifact_roots:
            self.artifact_roots.append(private_artifact_root)
        self._session_cache: dict[str, dict[str, Any]] = {}
        self.cancel_command = (
            str(cancel_command or "").strip()
            or os.environ.get("HERMES_CONNECTOR_CANCEL_COMMAND", "").strip()
            or _DEFAULT_CANCEL_COMMAND
        )

    def _build_cancel_command(self, root_id: str, reason: str) -> list[str]:
        try:
            template = shlex.split(self.cancel_command)
        except ValueError as exc:
            raise RuntimeError("connector cancellation command is invalid") from exc
        if not template:
            raise RuntimeError("connector cancellation command is empty")
        has_root_placeholder = any("{root_id}" in item for item in template)
        has_reason_placeholder = any("{reason}" in item for item in template)
        command = [
            item.replace("{root_id}", root_id).replace("{reason}", reason)
            for item in template
        ]
        if not has_root_placeholder:
            command.append(root_id)
        if not has_reason_placeholder:
            command.append(reason)
        return command

    def _materialize_attachments(
        self,
        run_payload: dict[str, Any],
        local: dict[str, Any],
        state: dict[str, Any],
    ) -> list[Path]:
        requested = [
            _text(item, 256)
            for item in run_payload.get("attachment_ids") or []
            if _text(item, 256).startswith("file_")
        ][:32]
        if not requested:
            return []
        remote_id = _text(run_payload.get("remote_run_id"), 256)
        metadata = {
            _text(item.get("id"), 256): item
            for item in self.cloud_client.list_run_attachments(remote_id)
            if isinstance(item, dict) and _text(item.get("id"), 256)
        }
        missing = [file_id for file_id in requested if file_id not in metadata]
        if missing:
            raise ConnectorContractError(404, "one or more run attachments are unavailable")
        remote_dir = self.attachment_root / (_SAFE_NAME_RE.sub("_", remote_id) or "run")
        remote_dir.mkdir(parents=True, exist_ok=True)
        downloaded = local.setdefault("attachments", {})
        paths: list[Path] = []
        for file_id in requested:
            item = metadata[file_id]
            sha256 = _text(item.get("sha256"), 64).lower()
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError) as exc:
                raise ConnectorContractError(422, "attachment size is invalid") from exc
            filename = _SAFE_NAME_RE.sub("_", _text(item.get("name"), 180)).strip(" .") or file_id
            target = remote_dir / f"{sha256[:16]}-{filename}"
            valid_existing = False
            if target.is_file():
                try:
                    actual_sha, actual_size = _sha256(target)
                    valid_existing = actual_sha == sha256 and (not size or actual_size == size)
                except (OSError, ValueError):
                    valid_existing = False
            if not valid_existing:
                self.cloud_client.download_run_attachment(
                    remote_id,
                    file_id,
                    target=target,
                    expected_sha256=sha256,
                    expected_size=size,
                )
            downloaded[file_id] = {
                "name": _text(item.get("name"), 180),
                "path": str(target),
                "sha256": sha256,
                "size": size,
            }
            paths.append(target)
        self.checkpoints.save(state)
        return paths

    def _show_task(self, task_id: str) -> dict[str, Any]:
        code, output = self.command_runner(
            ["hermes", "kanban", "show", task_id, "--json"],
            timeout=10,
        )
        if code != 0:
            raise RuntimeError(output[-1000:] or "hermes kanban show failed")
        value = json.loads(output)
        if not isinstance(value, dict):
            raise ValueError("kanban show returned a non-object")
        return value

    def _discover_session_id(
        self,
        detail: dict[str, Any],
        local: dict[str, Any],
    ) -> str:
        existing = _text(local.get("worker_session_id"), 256)
        if existing:
            return existing
        runs = detail.get("runs") if isinstance(detail.get("runs"), list) else []
        for item in reversed(runs):
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            session_id = _text(
                metadata.get("worker_session_id") or metadata.get("session_id"),
                256,
            )
            if session_id and _SESSION_ID_RE.fullmatch(session_id):
                local["worker_session_id"] = session_id
                return session_id

        profile = _text(local.get("profile"), 128)
        task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
        task_id = _text(local.get("root_task_id") or task.get("id"), 256)
        workspace = _text(task.get("workspace_path"), 2048)
        if (
            profile not in {"default", "dbb3-worker", "pc-worker", "reviewer"}
            or not task_id
            or not workspace
        ):
            return ""
        base_command = [
            "hermes",
            "-p",
            profile,
            "sessions",
            "list",
            "--limit",
            "20",
        ]
        code, output = self.command_runner(
            [*base_command, "--workspace", workspace],
            timeout=15,
        )
        if code != 0:
            code, output = self.command_runner(base_command, timeout=15)
        if code != 0:
            return ""
        candidates = [
            match.group(0)
            for line in output.splitlines()
            if task_id in line
            for match in [_SESSION_ID_RE.search(line)]
            if match is not None
        ]
        if not candidates:
            return ""
        local["worker_session_id"] = candidates[-1]
        return candidates[-1]

    def _session_snapshot(
        self,
        detail: dict[str, Any],
        local: dict[str, Any],
        *,
        terminal: bool,
    ) -> dict[str, Any]:
        session_id = self._discover_session_id(detail, local)
        profile = _text(local.get("profile"), 128)
        remote_id = _text(local.get("remote_run_id"), 256)
        if not session_id or profile not in {
            "default",
            "dbb3-worker",
            "pc-worker",
            "reviewer",
        }:
            return {}
        cached = self._session_cache.get(remote_id) or {}
        refreshed_at = float(cached.get("refreshed_at") or 0.0)
        terminal_loaded = bool(cached.get("terminal_loaded"))
        if (
            cached.get("session_id") == session_id
            and time.monotonic() - refreshed_at < _SESSION_REFRESH_SECONDS
            and (not terminal or terminal_loaded)
        ):
            return dict(cached.get("snapshot") or {})
        code, output = self.command_runner(
            [
                "hermes",
                "-p",
                profile,
                "sessions",
                "export",
                "-",
                "--format",
                "jsonl",
                "--session-id",
                session_id,
                "--redact",
            ],
            timeout=30,
        )
        if code != 0:
            return dict(cached.get("snapshot") or {})
        record: dict[str, Any] = {}
        for line in output.splitlines():
            if not line.lstrip().startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except (TypeError, ValueError):
                continue
            if (
                isinstance(candidate, dict)
                and _text(candidate.get("id"), 256) == session_id
            ):
                record = candidate
                break
        if not record:
            return dict(cached.get("snapshot") or {})
        snapshot = {
            "session_id": session_id,
            "model": _text(record.get("model"), 256),
            "provider": _text(
                record.get("billing_provider") or record.get("provider"),
                256,
            ),
            "activities": _session_record_activities(
                record,
                profile=profile,
                terminal=terminal,
            ),
        }
        self._session_cache[remote_id] = {
            "session_id": session_id,
            "refreshed_at": time.monotonic(),
            "terminal_loaded": terminal,
            "snapshot": snapshot,
        }
        return snapshot

    def _create_root(self, run_payload: dict[str, Any]) -> str:
        code, output = self.command_runner(build_root_task_command(run_payload), timeout=60)
        if code != 0:
            raise RuntimeError(output[-1000:] or "hermes kanban create failed")
        value = json.loads(output)
        if isinstance(value, dict):
            return _text(value.get("id") or (value.get("task") or {}).get("id"), 256)
        return ""

    def _write_objective_file(
        self,
        run_payload: dict[str, Any],
        local: dict[str, Any],
        state: dict[str, Any],
    ) -> Path:
        """Keep the authoritative UTF-8 prompt outside the ASCII Kanban argv."""

        existing = Path(_text(local.get("objective_path"), 2048))
        if existing.is_file() and existing.is_relative_to(self.attachment_root):
            return existing
        remote_id = _text(run_payload.get("remote_run_id"), 256)
        directory = self.attachment_root / (_SAFE_NAME_RE.sub("_", remote_id) or "run")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "objective.txt"
        target.write_text(
            _text(run_payload.get("objective"), 50000) + "\n",
            encoding="utf-8",
        )
        target.chmod(0o600)
        local["objective_path"] = str(target)
        self.checkpoints.save(state)
        return target

    def _accept_run(self, run_payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        remote_id = _text(run_payload.get("remote_run_id"), 256)
        if not remote_id:
            return {}
        current = state.setdefault("runs", {}).setdefault(remote_id, {})
        current.update(
            {
                "remote_run_id": remote_id,
                "idempotency_key": _text(run_payload.get("idempotency_key"), 512),
                "profile": _text(run_payload.get("profile"), 128),
                "artifact_required": bool(run_payload.get("artifact_required")),
                "status": current.get("status") or "running",
            }
        )
        if not current.get("root_task_id"):
            attachment_paths = self._materialize_attachments(run_payload, current, state)
            objective_path = self._write_objective_file(run_payload, current, state)
            prepared = dict(run_payload)
            prepared["title"] = "Hermes hosted run " + (
                _text(run_payload.get("remote_run_id"), 64) or "task"
            )
            prepared["objective"] = (
                "Read the authoritative UTF-8 user objective from this local path "
                f"before executing: {objective_path}. "
                "Do not infer or replace the user request. "
                "Before exiting, finish the root task by calling kanban_complete "
                "with the verified result, or kanban_block with a concrete blocker. "
                "A comment alone is not a terminal outcome."
            )
            if bool(run_payload.get("artifact_required")):
                workspace = objective_path.parent / "workspace"
                workspace.mkdir(parents=True, exist_ok=True)
                workspace.chmod(0o700)
                prepared["workspace_path"] = str(workspace)
                current["workspace_path"] = str(workspace)
            if attachment_paths:
                attachment_lines = "\n".join(f"- {path.name}: {path}" for path in attachment_paths)
                prepared["objective"] += (
                    "\nVerified user attachments are available at these paths:\n"
                    + attachment_lines
                )
            root_id = self._create_root(prepared)
            if not root_id:
                raise RuntimeError("Hermes did not return a root task id")
            current.update(
                {
                    "root_task_id": root_id,
                    "remote_task_id": root_id,
                    "session_id": f"task:{root_id}",
                }
            )
            self.checkpoints.save(state)
        if not current.get("acked"):
            self.cloud_client.acknowledge_run(run_payload, current)
            current["acked"] = True
            self.checkpoints.save(state)
        return current

    def _compact_status(self, detail: dict[str, Any], local: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
        raw_status = _text(task.get("status"), 64).lower()
        status = (
            "completed" if raw_status in {"done", "completed"}
            else "cancelled" if raw_status in {"cancelled", "canceled"}
            else "failed" if raw_status in {"failed", "blocked"}
            else "running"
        )
        summary = _structured_text(detail.get("latest_summary") or task.get("result"), 4000)
        runs = detail.get("runs") if isinstance(detail.get("runs"), list) else []
        errors = [
            _structured_text(item.get("error"), 1000)
            for item in runs[-10:]
            if isinstance(item, dict) and item.get("error")
        ]
        activities = _remote_activities(detail)
        session_snapshot = self._session_snapshot(
            detail,
            local,
            terminal=status in TERMINAL_STATUSES,
        )
        session_activities = [
            item
            for item in session_snapshot.get("activities") or []
            if isinstance(item, dict)
        ]
        merged = {
            _text(item.get("id"), 256) or f"activity:{index}": item
            for index, item in enumerate([*activities, *session_activities])
        }
        activities = sorted(
            merged.values(),
            key=lambda item: (
                int(item.get("created_at") or item.get("started_at") or 0),
                str(item.get("id") or ""),
            ),
        )[-200:]
        actual_model = _text(
            session_snapshot.get("model") or task.get("model_override"),
            256,
        )
        actual_provider = _text(session_snapshot.get("provider"), 256)
        for activity in reversed(activities):
            actual_model = actual_model or _text(activity.get("model"), 256)
            actual_provider = actual_provider or _text(activity.get("provider"), 256)
            if actual_model and actual_provider:
                break
        payload = {
            "connector_id": self.cloud_client.connector_id,
            "checkpoint_cursor": int(local.get("checkpoint_cursor") or 0) + 1,
            "status": status,
            "terminal": status in TERMINAL_STATUSES,
            "summary": summary,
            "result": _structured_text(task.get("result"), 8000),
            "error": errors[-1] if errors else "",
            "activities": activities,
            "remote_task_id": _text(local.get("remote_task_id"), 256),
            "root_task_id": _text(local.get("root_task_id"), 256),
            "session_id": _text(
                session_snapshot.get("session_id") or local.get("session_id"),
                256,
            ),
            "actual_model": actual_model,
            "actual_provider": actual_provider,
            "observed_at": self.clock(),
        }
        return payload, _artifact_paths(detail)

    def _allowed_artifact(self, raw_path: str) -> Path | None:
        candidate = Path(raw_path).expanduser()
        if candidate.is_symlink():
            return None
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if not resolved.is_file():
            return None
        if not any(resolved.is_relative_to(root) for root in self.artifact_roots):
            return None
        return resolved

    def _upload_artifacts(
        self,
        remote_id: str,
        local: dict[str, Any],
        paths: list[str],
        state: dict[str, Any],
    ) -> tuple[int, bool, list[str], bool]:
        uploaded = local.setdefault("artifacts", {})
        count = 0
        permanent_errors: list[str] = []
        transient_failure = False
        root_id = _text(local.get("root_task_id"), 256)
        for raw_path in paths:
            path = self._allowed_artifact(raw_path)
            if path is None:
                permanent_errors.append(f"Artifact is missing or outside allowed roots: {raw_path}")
                continue
            try:
                digest, size = _sha256(path)
            except ValueError as exc:
                permanent_errors.append(f"Artifact is invalid: {path.name}: {exc}")
                continue
            except OSError:
                transient_failure = True
                continue
            filename = _safe_filename(path)
            key = f"{remote_id}:{path}:{digest}"
            if key in uploaded:
                continue
            relative = f"{root_id}/{digest[:16]}-{filename}"
            media_type = "application/octet-stream"
            try:
                import mimetypes

                media_type = mimetypes.guess_type(filename)[0] or media_type
                self.cloud_client.upload_artifact(
                    remote_id,
                    relative_path=relative,
                    filename=filename,
                    sha256=digest,
                    media_type=media_type,
                    path=path,
                )
            except (OSError, urllib.error.URLError):
                transient_failure = True
                continue
            except CloudHTTPError as exc:
                if exc.status in {401, 409}:
                    raise
                if exc.status == 422:
                    permanent_errors.append(f"Artifact was rejected: {filename}")
                    continue
                transient_failure = True
                continue
            uploaded[key] = {
                "sha256": digest,
                "size": size,
                "relative_path": relative,
                "uploaded_at": self.clock(),
            }
            count += 1
            self.checkpoints.save(state)
        complete = self._artifact_uploads_complete(local, paths)
        return count, complete, permanent_errors, transient_failure

    def _artifact_uploads_complete(self, local: dict[str, Any], paths: list[str]) -> bool:
        uploaded = local.get("artifacts") if isinstance(local.get("artifacts"), dict) else {}
        for raw_path in paths:
            path = self._allowed_artifact(raw_path)
            if path is None:
                return False
            try:
                digest, _size = _sha256(path)
            except (OSError, ValueError):
                return False
            if f"{local.get('remote_run_id')}:{path}:{digest}" not in uploaded:
                return False
        return bool(paths)

    def _sync_local_run(
        self,
        remote_id: str,
        local: dict[str, Any],
        state: dict[str, Any],
    ) -> tuple[int, int]:
        detail = self._show_task(_text(local.get("root_task_id"), 256))
        payload, artifact_paths = self._compact_status(detail, local)
        if artifact_paths:
            local["artifact_paths"] = list(artifact_paths)
            self.checkpoints.save(state)
        elif payload.get("terminal"):
            artifact_paths = [
                _text(path, 2048)
                for path in local.get("artifact_paths") or []
                if _text(path, 2048)
            ]

        uploaded = 0
        uploads_complete = not artifact_paths
        artifact_errors: list[str] = []
        transient_upload_failure = False
        if artifact_paths:
            (
                uploaded,
                uploads_complete,
                artifact_errors,
                transient_upload_failure,
            ) = self._upload_artifacts(remote_id, local, artifact_paths, state)
            if uploads_complete:
                local["artifacts_synced"] = True
                local.pop("artifact_errors", None)
                self.checkpoints.save(state)
            elif artifact_errors:
                local["artifact_errors"] = list(artifact_errors)
                self.checkpoints.save(state)

        if payload.get("terminal") and bool(local.get("artifact_required")):
            if not artifact_paths:
                artifact_errors = ["Required artifact was not declared by the worker"]
            if artifact_errors:
                payload.update(
                    {
                        "status": "failed",
                        "terminal": True,
                        "summary": "DBB3 task did not produce a valid required deliverable",
                        "error": "; ".join(artifact_errors)[:4000],
                    }
                )
            elif transient_upload_failure or not uploads_complete:
                pending = local.get("pending_status")
                if isinstance(pending, dict) and pending.get("terminal"):
                    local.pop("pending_status", None)
                    local.pop("pending_status_fingerprint", None)
                    self.checkpoints.save(state)
                return 0, uploaded
        elif payload.get("terminal") and artifact_paths:
            # Optional but explicitly declared deliverables still reach the
            # cloud before the terminal checkpoint. Invalid optional paths do
            # not change the task result, while network failures remain
            # retryable and keep the remote run active.
            if transient_upload_failure or (not uploads_complete and not artifact_errors):
                pending = local.get("pending_status")
                if isinstance(pending, dict) and pending.get("terminal"):
                    local.pop("pending_status", None)
                    local.pop("pending_status_fingerprint", None)
                    self.checkpoints.save(state)
                return 0, uploaded

        fingerprint_payload = dict(payload)
        fingerprint_payload.pop("checkpoint_cursor", None)
        fingerprint_payload.pop("observed_at", None)
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        pending = local.get("pending_status")
        if isinstance(pending, dict) and pending.get("terminal"):
            pending_fingerprint_payload = dict(pending)
            pending_fingerprint_payload.pop("checkpoint_cursor", None)
            pending_fingerprint_payload.pop("observed_at", None)
            pending_fingerprint = str(local.get("pending_status_fingerprint") or "") or hashlib.sha256(
                json.dumps(
                    pending_fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            if pending_fingerprint != fingerprint:
                local.pop("pending_status", None)
                local.pop("pending_status_fingerprint", None)
                self.checkpoints.save(state)
                pending = None
        reported = 0
        if pending is None and fingerprint != local.get("last_status_fingerprint"):
            pending = payload
            local["pending_status"] = pending
            local["pending_status_fingerprint"] = fingerprint
            self.checkpoints.save(state)
        if isinstance(pending, dict):
            try:
                self.cloud_client.report_status(remote_id, pending)
            except CloudHTTPError as exc:
                if exc.status in {401, 409, 422}:
                    raise
                return 0, 0
            except (OSError, urllib.error.URLError):
                return 0, 0
            else:
                local["checkpoint_cursor"] = int(pending["checkpoint_cursor"])
                local["last_status_fingerprint"] = str(
                    local.get("pending_status_fingerprint") or fingerprint
                )
                local["status"] = pending["status"]
                local.pop("pending_status", None)
                local.pop("pending_status_fingerprint", None)
                self.checkpoints.save(state)
                reported = 1
        return reported, uploaded

    def _process_run(self, run_payload: dict[str, Any], state: dict[str, Any]) -> tuple[int, int]:
        local = self._accept_run(run_payload, state)
        remote_id = _text(run_payload.get("remote_run_id"), 256)
        if not local or not remote_id:
            return 0, 0
        return self._sync_local_run(remote_id, local, state)

    def _process_cancellation(self, item: dict[str, Any], state: dict[str, Any]) -> int:
        remote_id = _text(item.get("remote_run_id"), 256)
        if not remote_id:
            return 0
        local = state.setdefault("runs", {}).setdefault(remote_id, {})
        if local.get("cancel_acked"):
            return 0
        root_id = _text(item.get("root_task_id") or local.get("root_task_id"), 256)
        reason = _text(item.get("reason"), 500) or "Cancelled by cloud user"
        code, output = self.command_runner(
            self._build_cancel_command(root_id, reason),
            timeout=30,
        )
        if code != 0:
            return 0
        cursor = max(
            int(local.get("checkpoint_cursor") or 0),
            int(item.get("checkpoint_cursor") or 0),
        ) + 1
        try:
            response = self.cloud_client.acknowledge_cancel(
                item,
                {
                    "connector_id": self.cloud_client.connector_id,
                    "checkpoint_cursor": cursor,
                    "summary": _text(output, 1000) or "Cancellation applied",
                    "observed_at": self.clock(),
                },
            )
        except ConnectorContractError as exc:
            if exc.status != 409:
                raise
            # Older servers used 409 when completion won the cancellation
            # race. Treat that terminal conflict as acknowledged so the user
            # service keeps polling instead of exiting permanently.
            local.update({"cancel_acked": True, "checkpoint_cursor": cursor})
            self.checkpoints.save(state)
            return 1
        remote = response.get("run") if isinstance(response, dict) else None
        if isinstance(remote, dict):
            remote_cursor = int(remote.get("checkpoint_cursor") or 0)
            cursor = max(cursor, remote_cursor)
            remote_status = str(remote.get("status") or "")
            if remote_status in TERMINAL_STATUSES:
                local.update(
                    {
                        "cancel_acked": True,
                        "status": remote_status,
                        "checkpoint_cursor": cursor,
                    }
                )
                self.checkpoints.save(state)
                return 1
            if remote_status != "cancelled":
                local["checkpoint_cursor"] = cursor
                self.checkpoints.save(state)
                return 0
        local.update({"cancel_acked": True, "status": "cancelled", "checkpoint_cursor": cursor})
        self.checkpoints.save(state)
        return 1

    def sync_once(self) -> dict[str, int]:
        state = self.checkpoints.load()
        created = 0
        statuses = 0
        artifacts = 0
        cancelled = 0
        processed: set[str] = set()
        for run_payload in self.cloud_client.pull_runs(limit=5, lease_seconds=90):
            if not isinstance(run_payload, dict):
                continue
            remote_id = _text(run_payload.get("remote_run_id"), 256)
            if remote_id:
                processed.add(remote_id)
            try:
                made, uploaded = self._process_run(run_payload, state)
            except RuntimeError as exc:
                # A root that cannot be created is a durable terminal failure;
                # a root that already exists is left pending for the next
                # cycle so a temporary local `show` failure is retryable.
                remote_id = _text(run_payload.get("remote_run_id"), 256)
                local = state.setdefault("runs", {}).setdefault(remote_id, {})
                if remote_id and not local.get("root_task_id"):
                    cursor = int(local.get("checkpoint_cursor") or 0) + 1
                    self.cloud_client.fail_run(
                        remote_id,
                        {
                            "connector_id": self.cloud_client.connector_id,
                            "checkpoint_cursor": cursor,
                            "error": _text(exc, 1000),
                            "summary": "DBB3 could not create the Kanban root",
                            "observed_at": self.clock(),
                        },
                    )
                    local.update({"status": "failed", "checkpoint_cursor": cursor})
                    self.checkpoints.save(state)
                continue
            created += made
            artifacts += uploaded
            statuses += int(made > 0)
        for remote_id, local in list((state.get("runs") or {}).items()):
            if remote_id in processed or not isinstance(local, dict):
                continue
            if not local.get("acked") or not local.get("root_task_id"):
                continue
            if str(local.get("status") or "") in TERMINAL_STATUSES:
                artifact_paths = [
                    _text(path, 2048)
                    for path in local.get("artifact_paths") or []
                    if _text(path, 2048)
                ]
                if artifact_paths and not local.get("artifacts_synced"):
                    uploaded, complete, _errors, _transient = self._upload_artifacts(
                        remote_id,
                        local,
                        artifact_paths,
                        state,
                    )
                    artifacts += uploaded
                    if complete:
                        local["artifacts_synced"] = True
                        self.checkpoints.save(state)
                continue
            try:
                made, uploaded = self._sync_local_run(remote_id, local, state)
            except (RuntimeError, ValueError, json.JSONDecodeError):
                continue
            created += made
            artifacts += uploaded
            statuses += int(made > 0)
        for item in self.cloud_client.pull_cancellations(limit=5, lease_seconds=90):
            if isinstance(item, dict):
                cancelled += self._process_cancellation(item, state)
        return {
            "created": created,
            "statuses": statuses,
            "artifacts": artifacts,
            "cancelled": cancelled,
        }


def _load_token(path: str) -> str:
    token_path = Path(path)
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("connector token file is empty")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cloud-url", default=os.environ.get("HERMES_CLOUD_URL", ""))
    parser.add_argument(
        "--token-file",
        default=os.environ.get("HERMES_CLOUD_TOKEN_FILE", "/etc/dbb3-team/cloud_connector_token"),
    )
    parser.add_argument("--connector-id", default=os.environ.get("DBB3_CONNECTOR_ID", "dbb3-primary"))
    parser.add_argument("--state-file", default=os.environ.get("DBB3_CONNECTOR_STATE_FILE", ""))
    parser.add_argument("--artifact-roots", default=os.environ.get("DBB3_CONNECTOR_ARTIFACT_ROOTS", ""))
    parser.add_argument(
        "--cancel-command",
        default=os.environ.get("HERMES_CONNECTOR_CANCEL_COMMAND", ""),
    )
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if not args.cloud_url:
        parser.error("--cloud-url or HERMES_CLOUD_URL is required")
    token = _load_token(args.token_file)
    client = CloudRelayClient(args.cloud_url, token, connector_id=args.connector_id)
    if args.probe:
        result = client.probe()
        print(json.dumps({"ok": True, "contract_version": result.get("contract_version")}, ensure_ascii=False))
        return 0
    roots = args.artifact_roots.split(os.pathsep) if args.artifact_roots else None
    connector = DBB3CloudConnector(
        client,
        state_file=args.state_file or None,
        artifact_roots=roots,
        cancel_command=args.cancel_command or None,
    )
    while True:
        try:
            result = connector.sync_once()
            if not args.quiet or any(int(value) for value in result.values()):
                print(json.dumps({"timestamp": now_iso(), **result}, ensure_ascii=False), flush=True)
        except ConnectorAuthError as exc:
            print(json.dumps({"timestamp": now_iso(), "error": "cloud authentication failed"}), flush=True)
            return 78
        except ConnectorContractError as exc:
            print(json.dumps({"timestamp": now_iso(), "error": f"connector contract error ({exc.status})"}), flush=True)
            return 65
        except CloudHTTPError as exc:
            # 5xx responses are transient and leave the checkpoint pending;
            # auth/contract failures are handled above and stop the unit.
            if exc.status < 500:
                print(json.dumps({"timestamp": now_iso(), "error": f"cloud request failed ({exc.status})"}), flush=True)
                return 65
            print(json.dumps({"timestamp": now_iso(), "error": f"cloud temporary failure ({exc.status})"}), flush=True)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
            print(json.dumps({"timestamp": now_iso(), "error": f"{type(exc).__name__}: {_text(exc, 500)}"}), flush=True)
        if args.once:
            return 0
        time.sleep(max(0.5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
