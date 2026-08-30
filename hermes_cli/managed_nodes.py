"""Health aggregation and recovery controls for privately connected Hermes nodes.

Managed nodes are configured by the operator in ``managed-nodes.json`` under
``HERMES_HOME``. Credentials remain in root-owned files referenced by that
configuration; the dashboard response never includes a URL or token.
"""
from __future__ import annotations

from contextlib import contextmanager
import hmac
import ipaddress
import json
import math
import os
import re
import stat
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from hermes_constants import get_hermes_home
from hermes_services.worker_channel import (
    WorkerChannelRegistry,
    get_worker_channel_registry,
)


DEFAULT_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_FRESHNESS_SECONDS = 60.0
DEFAULT_RECOVERY_COOLDOWN_SECONDS = 90.0
# Managed-node health/recovery has historically supported the DBB3 and WSL
# lanes.  HK is a first-class worker now, so keep the allow-list in one place
# and use it for both the status relay and the authenticated recovery hook.
MANAGED_WORKER_TARGETS = frozenset({"dbb3", "wsl", "hk"})
# The public managed-installation API still has only DBB3/WSL receivers. HK is
# deployed by its dedicated fabric updater (deploy/hk) and must not be accepted
# in installation_urls until a matching authenticated receiver exists there.
MANAGED_INSTALLATION_TARGETS = frozenset({"dbb3", "wsl"})
_RECOVERY_LOCK = threading.Lock()
_RECOVERY_LAST_ATTEMPT: dict[str, float] = {}
_RECOVERY_RECEIVER_LOCK = threading.Lock()
MIN_MANAGED_TOKEN_LENGTH = 32
MAX_MANAGED_TOKEN_LENGTH = 4096


def _is_posix_token_runtime() -> bool:
    return os.name == "posix"


def managed_nodes_config_path() -> Path:
    return Path(get_hermes_home()) / "managed-nodes.json"


def read_private_token(path: str | Path, *, label: str = "credential") -> str:
    """Read a single-line credential from a non-symlink private file."""

    token_path = Path(path).expanduser()
    try:
        metadata = token_path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} file is missing or unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} file must be a regular non-symlink file")
    if _is_posix_token_runtime():
        mode = stat.S_IMODE(metadata.st_mode)
        get_effective_uid = getattr(os, "geteuid", None)
        get_effective_gid = getattr(os, "getegid", None)
        get_groups = getattr(os, "getgroups", None)
        if not callable(get_effective_uid) or not callable(get_effective_gid) or not callable(get_groups):
            raise ValueError(f"{label} ownership checks are unavailable on this platform")
        current_uid = get_effective_uid()
        allowed_uids = {0, current_uid}
        if metadata.st_uid not in allowed_uids:
            raise ValueError(f"{label} file owner is not trusted")
        if mode not in {0o600, 0o640}:
            raise ValueError(f"{label} file mode must be 0600 or controlled 0640")
        if mode == 0o640:
            controlled_groups = {get_effective_gid(), *get_groups()}
            if metadata.st_gid not in controlled_groups:
                raise ValueError(f"{label} file group is not available to this service")
    try:
        raw = token_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{label} file is missing or unreadable") from exc
    lines = raw.splitlines()
    if len(lines) != 1 or raw.rstrip("\r\n") != lines[0]:
        raise ValueError(f"{label} must contain exactly one line")
    token = lines[0]
    if token != token.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")
    if not (MIN_MANAGED_TOKEN_LENGTH <= len(token) <= MAX_MANAGED_TOKEN_LENGTH):
        raise ValueError(
            f"{label} length must be between {MIN_MANAGED_TOKEN_LENGTH} and "
            f"{MAX_MANAGED_TOKEN_LENGTH} characters"
        )
    return token


def load_managed_nodes_config(path: Path | None = None) -> list[dict[str, Any]]:
    config_path = path or managed_nodes_config_path()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid managed-nodes configuration: {exc}") from exc
    rows = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("managed-nodes configuration must contain a nodes array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("managed node entries must be objects")
        node_id = str(raw.get("id") or "").strip().lower()
        url = str(raw.get("status_url") or "").strip()
        token_file = str(raw.get("token_file") or "").strip()
        raw_installation_token_file = raw.get("installation_token_file")
        installation_token_file = (
            str(raw_installation_token_file).strip()
            if raw_installation_token_file is not None
            else ""
        )
        recovery_url = str(raw.get("recovery_url") or "").strip()
        raw_recovery_urls = raw.get("recovery_urls") or {}
        raw_recovery_token_files = raw.get("recovery_token_files") or {}
        raw_installation_urls = raw.get("installation_urls") or {}
        if not node_id or node_id in seen:
            raise ValueError("managed node ids must be non-empty and unique")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(f"managed node {node_id!r} requires an HTTP(S) status_url")
        if not token_file:
            raise ValueError(f"managed node {node_id!r} requires token_file")
        if raw_installation_token_file is not None and not installation_token_file:
            raise ValueError(
                f"managed node {node_id!r} installation_token_file must be non-empty"
            )
        if recovery_url and not _is_secure_recovery_url(recovery_url):
            raise ValueError(f"managed node {node_id!r} has an invalid recovery_url")
        if not isinstance(raw_recovery_urls, dict):
            raise ValueError(f"managed node {node_id!r} recovery_urls must be an object")
        if not isinstance(raw_recovery_token_files, dict):
            raise ValueError(
                f"managed node {node_id!r} recovery_token_files must be an object"
            )
        if not isinstance(raw_installation_urls, dict):
            raise ValueError(f"managed node {node_id!r} installation_urls must be an object")
        recovery_urls: dict[str, str] = {}
        for target, target_url in raw_recovery_urls.items():
            normalized_target = str(target).strip().lower()
            normalized_url = str(target_url or "").strip()
            if (
                normalized_target not in MANAGED_WORKER_TARGETS
                or not _is_secure_recovery_url(normalized_url)
            ):
                raise ValueError(f"managed node {node_id!r} has an invalid recovery_urls entry")
            recovery_urls[normalized_target] = normalized_url
        recovery_token_files: dict[str, str] = {}
        for target, target_token_file in raw_recovery_token_files.items():
            normalized_target = str(target).strip().lower()
            normalized_token_file = str(target_token_file or "").strip()
            if (
                normalized_target not in MANAGED_WORKER_TARGETS
                or not normalized_token_file
                or not Path(normalized_token_file).expanduser().is_absolute()
            ):
                raise ValueError(
                    f"managed node {node_id!r} has an invalid recovery_token_files entry"
                )
            if normalized_target not in recovery_urls:
                raise ValueError(
                    f"managed node {node_id!r} has a recovery credential without a route"
                )
            recovery_token_files[normalized_target] = normalized_token_file
        installation_urls: dict[str, str] = {}
        for target, target_url in raw_installation_urls.items():
            normalized_target = str(target).strip().lower()
            normalized_url = str(target_url or "").strip()
            if (
                normalized_target not in MANAGED_INSTALLATION_TARGETS
                or not _is_secure_recovery_url(normalized_url)
            ):
                raise ValueError(f"managed node {node_id!r} has an invalid installation_urls entry")
            installation_urls[normalized_target] = normalized_url
        if installation_urls:
            if not installation_token_file:
                raise ValueError(
                    f"managed node {node_id!r} requires installation_token_file "
                    "when installation_urls are configured"
                )
            if (
                Path(installation_token_file).expanduser().resolve(strict=False)
                == Path(token_file).expanduser().resolve(strict=False)
            ):
                raise ValueError(
                    f"managed node {node_id!r} must not reuse token_file as "
                    "installation_token_file"
                )
            status_token = read_private_token(token_file, label="managed-node status credential")
            installation_token = read_private_token(
                installation_token_file,
                label="managed installation credential",
            )
            if hmac.compare_digest(status_token, installation_token):
                raise ValueError(
                    f"managed node {node_id!r} must use different status and "
                    "installation credential contents"
                )
        if recovery_token_files:
            status_token = read_private_token(
                token_file,
                label="managed-node status credential",
            )
            installation_token = (
                read_private_token(
                    installation_token_file,
                    label="managed installation credential",
                )
                if installation_token_file
                else ""
            )
            seen_recovery_tokens: list[str] = []
            for target, recovery_token_file in recovery_token_files.items():
                recovery_token = read_private_token(
                    recovery_token_file,
                    label=f"{target} recovery credential",
                )
                if hmac.compare_digest(status_token, recovery_token) or (
                    installation_token
                    and hmac.compare_digest(installation_token, recovery_token)
                ):
                    raise ValueError(
                        f"managed node {node_id!r} must use a dedicated {target} "
                        "recovery credential"
                    )
                if any(
                    hmac.compare_digest(existing, recovery_token)
                    for existing in seen_recovery_tokens
                ):
                    raise ValueError(
                        f"managed node {node_id!r} recovery credentials must be unique"
                    )
                seen_recovery_tokens.append(recovery_token)
        timeout = float(raw.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 15:
            raise ValueError(f"managed node {node_id!r} has an invalid timeout")
        recovery_cooldown = float(
            raw.get("recovery_cooldown_seconds") or DEFAULT_RECOVERY_COOLDOWN_SECONDS
        )
        if not math.isfinite(recovery_cooldown) or recovery_cooldown < 15 or recovery_cooldown > 3600:
            raise ValueError(f"managed node {node_id!r} has an invalid recovery cooldown")
        seen.add(node_id)
        result.append({
            "id": node_id,
            "label": str(raw.get("label") or node_id).strip() or node_id,
            "status_url": url,
            "token_file": token_file,
            "installation_token_file": installation_token_file,
            "timeout_seconds": timeout,
            "recovery_url": recovery_url,
            "recovery_urls": recovery_urls,
            "recovery_token_files": recovery_token_files,
            "installation_urls": installation_urls,
            "auto_recover": raw.get("auto_recover") is not False,
            "recovery_cooldown_seconds": recovery_cooldown,
        })
    return result


def load_managed_node_recovery_config(path: Path | None = None) -> dict[str, Any] | None:
    """Load the fixed local action exposed to the peer recovery hook."""

    config_path = path or managed_nodes_config_path()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid managed-nodes configuration: {exc}") from exc
    raw = payload.get("recovery_receiver") if isinstance(payload, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("managed-nodes recovery_receiver must be an object")
    node_id = str(raw.get("node_id") or "").strip().lower()
    if node_id not in MANAGED_WORKER_TARGETS:
        raise ValueError("recovery_receiver node_id must be dbb3, wsl, or hk")
    token_file = str(raw.get("token_file") or "").strip()
    if not token_file:
        raise ValueError("recovery_receiver requires token_file")
    command = raw.get("command")
    if (
        not isinstance(command, list)
        or not command
        or len(command) > 32
        or any(not isinstance(item, str) or not item.strip() or len(item) > 1024 for item in command)
    ):
        raise ValueError("recovery_receiver command must be a non-empty argv array")
    state_file = str(raw.get("state_file") or "").strip()
    return {
        "node_id": node_id,
        "token_file": token_file,
        "command": list(command),
        "state_file": (
            (config_path.parent / state_file).resolve()
            if state_file and not Path(state_file).is_absolute()
            else Path(state_file)
            if state_file
            else config_path.with_name("managed-node-recovery-state.json")
        ),
    }


def fetch_managed_nodes(
    path: Path | None = None,
    *,
    now: datetime | None = None,
    worker_registry: WorkerChannelRegistry | None = None,
) -> dict[str, Any]:
    fetched_datetime = now or datetime.now(timezone.utc)
    if fetched_datetime.tzinfo is None:
        fetched_datetime = fetched_datetime.replace(tzinfo=timezone.utc)
    fetched_datetime = fetched_datetime.astimezone(timezone.utc)
    fetched_at = fetched_datetime.isoformat(timespec="seconds")
    configs = load_managed_nodes_config(path)
    legacy_nodes: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    source_configs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for config in configs:
        try:
            payload = _fetch_status(config)
            normalized = _normalize_dbb3_status(payload, fetched_datetime)
            for node in normalized:
                node_id = str(node.get("id") or "")
                previous = legacy_nodes.get(node_id)
                if previous is None or (
                    node.get("metrics_available") is True
                    and previous.get("metrics_available") is not True
                ):
                    legacy_nodes[node_id] = node
            source_fresh = bool(normalized) and all(
                node.get("metrics_available") is True for node in normalized
            )
            source = {
                "id": config["id"],
                "label": config["label"],
                "online": source_fresh,
                "observed_at": str(payload.get("timestamp") or fetched_at),
                "fresh": source_fresh,
                **({"error": "stale_observation"} if not source_fresh else {}),
            }
        except Exception as exc:
            source = {
                "id": config["id"],
                "label": config["label"],
                "online": False,
                "observed_at": fetched_at,
                "error": _public_error(exc),
            }
        sources.append(source)
        source_configs.append((config, source))

    registry = worker_registry or get_worker_channel_registry()
    nodes = [
        _merge_worker_status(snapshot, legacy_nodes.get(str(snapshot.get("id") or "")))
        for snapshot in registry.managed_snapshots()
    ]
    offline_ids = {
        str(node.get("id") or "")
        for node in nodes
        if node.get("online") is not True
    }
    for config, source in source_configs:
        recovery_targets = [
            target
            for target in _recovery_targets_for_config(config)
            if target in offline_ids
        ]
        if not (
            recovery_targets
            and config["auto_recover"]
            and _has_recovery_route(config, recovery_targets)
        ):
            continue
        try:
            recovery = _request_recovery(
                config,
                targets=recovery_targets,
                reason="worker_channel_offline",
            )
        except Exception as exc:
            recovery = {"state": "failed", "error": _public_error(exc)}
        source["recovery"] = recovery
        target_states = recovery.get("target_states") or {}
        for node in nodes:
            node_id = str(node.get("id") or "")
            if node_id in recovery_targets:
                node["recovery_state"] = str(
                    target_states.get(node_id) or recovery.get("state") or "failed"
                )
    return {
        "fetched_at": fetched_at,
        # The three worker lanes are part of the hosted topology even when the
        # optional legacy metrics/recovery source is not configured.
        "configured": True,
        "nodes": nodes,
        "sources": sources,
    }


def _merge_worker_status(
    worker: dict[str, Any],
    legacy: dict[str, Any] | None,
) -> dict[str, Any]:
    """Combine authoritative WS liveness with optional legacy device metrics."""

    legacy = legacy or {}
    worker_metrics = _dict(worker.get("metrics"))
    legacy_metrics = _dict(legacy.get("metrics"))
    metrics = worker_metrics or legacy_metrics
    if worker_metrics:
        metrics_available = (
            worker.get("fresh") is True
            and worker_metrics.get("available") is not False
        )
        metrics_observed_at = str(
            worker_metrics.get("sampled_at") or worker.get("observed_at") or ""
        )
        metrics_source = "worker_ws"
    else:
        metrics_available = legacy.get("metrics_available") is True
        metrics_observed_at = str(legacy.get("metrics_observed_at") or "")
        metrics_source = str(legacy.get("metrics_source") or "legacy_device_metrics")
    return {
        "id": str(worker.get("id") or ""),
        "label": str(worker.get("label") or legacy.get("label") or ""),
        "worker_node_id": str(worker.get("worker_node_id") or ""),
        "online": worker.get("online") is True,
        "gateway_state": str(worker.get("gateway_state") or "offline"),
        "version": str(worker.get("version") or ""),
        "observed_at": str(worker.get("observed_at") or ""),
        "fresh": worker.get("fresh") is True,
        "age_seconds": worker.get("age_seconds"),
        "metrics": metrics,
        "metrics_available": metrics_available,
        "metrics_observed_at": metrics_observed_at,
        "runtime_fresh": worker.get("fresh") is True,
        "active_tasks": int(worker.get("active_tasks") or 0),
        "metrics_source": metrics_source,
        "runtime": _dict(worker.get("runtime")),
        "release": _dict(worker.get("release")),
    }


def _fetch_status(config: dict[str, Any]) -> dict[str, Any]:
    token_path = Path(config["token_file"])
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("empty node credential")
    request = Request(
        config["status_url"],
        headers={
            "Accept": "application/json",
            "X-DBB3-Token": token,
        },
        method="GET",
    )
    with urlopen(request, timeout=config["timeout_seconds"]) as response:
        if response.status != 200:
            raise HTTPError(
                request.full_url,
                response.status,
                "status request failed",
                response.headers,
                None,
            )
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("node response exceeded the size limit")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("node response must be an object")
    return payload


def recover_managed_nodes(
    node_id: str = "",
    path: Path | None = None,
) -> dict[str, Any]:
    """Manually request peer recovery through configured control planes."""

    target = str(node_id or "").strip().lower()
    if target and target not in MANAGED_WORKER_TARGETS:
        raise ValueError("node_id must be dbb3, wsl, or hk")
    outcomes = []
    for config in load_managed_nodes_config(path):
        requested_targets = [target] if target else _recovery_targets_for_config(config)
        if not _has_recovery_route(config, requested_targets):
            outcomes.append({"id": config["id"], "state": "unconfigured"})
            continue
        try:
            outcome = _request_recovery(
                config,
                targets=requested_targets,
                reason="manual_reconnect",
                force=True,
            )
        except Exception as exc:
            outcome = {
                "state": "failed",
                "accepted": False,
                "error": _public_error(exc),
            }
        outcomes.append({"id": config["id"], **outcome})
    return {
        "requested": any(outcome.get("state") != "unconfigured" for outcome in outcomes),
        "target": target or "all",
        "outcomes": outcomes,
    }


def accept_managed_node_recovery(
    payload: dict[str, Any],
    presented_token: str,
    path: Path | None = None,
    *,
    executor: Any = None,
) -> dict[str, Any]:
    """Authenticate and idempotently launch this node's fixed recovery action."""

    config = load_managed_node_recovery_config(path)
    if config is None:
        raise RuntimeError("managed node recovery receiver is not configured")
    expected_token = Path(config["token_file"]).read_text(encoding="utf-8").strip()
    supplied_token = str(presented_token or "").strip()
    if not expected_token or not supplied_token or not hmac.compare_digest(
        expected_token.encode("utf-8"),
        supplied_token.encode("utf-8"),
    ):
        raise PermissionError("invalid managed node recovery credential")
    if str(payload.get("action") or "") != "reconnect":
        raise ValueError("unsupported managed node recovery action")
    targets = payload.get("targets")
    if not isinstance(targets, list) or any(
        str(target).strip().lower() not in MANAGED_WORKER_TARGETS for target in targets
    ):
        raise ValueError("managed node recovery targets are invalid")
    normalized_targets = {str(target).strip().lower() for target in targets}
    if config["node_id"] not in normalized_targets:
        raise ValueError("managed node recovery request does not target this node")
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if not idempotency_key or len(idempotency_key) > 256:
        raise ValueError("managed node recovery idempotency_key is required")

    state_path = Path(config["state_file"])
    with _RECOVERY_RECEIVER_LOCK, _recovery_state_lock(state_path):
        state = _read_recovery_state(state_path)
        idempotency_keys = [
            str(key)
            for key in state.get("idempotency_keys") or [state.get("idempotency_key")]
            if str(key or "")
        ]
        if idempotency_key in idempotency_keys:
            return {
                "accepted": True,
                "node_id": config["node_id"],
                "state": "recovering",
                "replayed": True,
            }
        claimed_state = {
            "idempotency_key": idempotency_key,
            "idempotency_keys": [*idempotency_keys, idempotency_key][-256:],
            "node_id": config["node_id"],
            "reason": str(payload.get("reason") or "")[:256],
            "requested_at": int(time.time()),
            "state": "launching",
        }
        _write_recovery_state(state_path, claimed_state)
        launch = executor or _spawn_recovery_command
        try:
            launch(list(config["command"]))
        except Exception:
            claimed_state["idempotency_keys"] = idempotency_keys
            claimed_state["idempotency_key"] = idempotency_keys[-1] if idempotency_keys else ""
            claimed_state["state"] = "failed_to_launch"
            _write_recovery_state(state_path, claimed_state)
            raise
        claimed_state["state"] = "recovering"
        _write_recovery_state(state_path, claimed_state)
    return {
        "accepted": True,
        "node_id": config["node_id"],
        "state": "recovering",
        "replayed": False,
    }


def _spawn_recovery_command(command: list[str]) -> None:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)


def _read_recovery_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_recovery_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def _recovery_state_lock(state_path: Path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f".{state_path.name}.lock")
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _request_recovery(
    config: dict[str, Any],
    *,
    targets: list[str],
    reason: str,
    force: bool = False,
) -> dict[str, Any]:
    normalized_targets = sorted(set(targets))
    routes: list[tuple[str, str]] = []
    unconfigured_targets: list[str] = []
    target_routes = config.get("recovery_urls") or {}
    fallback_url = str(config.get("recovery_url") or "")
    for target in normalized_targets:
        recovery_url = str(target_routes.get(target) or fallback_url)
        if recovery_url:
            routes.append((target, recovery_url))
        else:
            unconfigured_targets.append(target)
    if not routes:
        return {"state": "unconfigured"}
    outcomes: list[dict[str, Any]] = []
    target_states = {target: "unconfigured" for target in unconfigured_targets}
    for target, recovery_url in sorted(routes):
        try:
            route_config = dict(config)
            route_config["token_file"] = str(
                (config.get("recovery_token_files") or {}).get(target)
                or config["token_file"]
            )
            outcome = _request_recovery_route(
                route_config,
                recovery_url=recovery_url,
                targets=[target],
                reason=reason,
                force=force,
            )
        except Exception as exc:
            outcome = {"state": "failed", "error": _public_error(exc)}
        outcome = {"targets": [target], **outcome}
        outcomes.append(outcome)
        target_states[target] = str(outcome.get("state") or "failed")
    states = {str(outcome.get("state") or "failed") for outcome in outcomes}
    state = (
        "failed" if "failed" in states
        else "recovering" if "recovering" in states
        else "cooldown" if "cooldown" in states
        else "unconfigured"
    )
    result: dict[str, Any] = {
        "state": state,
        "targets": normalized_targets,
        "target_states": target_states,
    }
    if len(outcomes) > 1:
        result["routes"] = outcomes
    elif outcomes:
        result.update(outcomes[0])
        result["targets"] = normalized_targets
        result["target_states"] = target_states
    result["accepted"] = not unconfigured_targets and all(
        outcome.get("accepted") is True for outcome in outcomes
    )
    if unconfigured_targets:
        result["unconfigured_targets"] = unconfigured_targets
    return result


def _has_recovery_route(config: dict[str, Any], targets: list[str]) -> bool:
    routes = config.get("recovery_urls") or {}
    fallback = str(config.get("recovery_url") or "")
    return any(str(routes.get(target) or fallback) for target in targets)


def _recovery_targets_for_config(config: dict[str, Any]) -> list[str]:
    """Return the stable worker order while preserving legacy two-lane output.

    Existing installations commonly have a single fallback recovery URL that
    only controls DBB3/WSL.  Do not add an unconfigured HK target to those
    requests (which would make old payloads noisy and fail closed); include HK
    automatically when the operator supplied an HK-specific route or a
    fallback route intended for it.
    """
    targets = ["dbb3", "wsl"]
    routes = config.get("recovery_urls") or {}
    if str(routes.get("hk") or "").strip():
        targets.append("hk")
    return targets


def _request_recovery_route(
    config: dict[str, Any],
    *,
    recovery_url: str,
    targets: list[str],
    reason: str,
    force: bool,
) -> dict[str, Any]:
    normalized_targets = sorted(set(targets))
    monotonic_now = time.monotonic()
    cooldown = float(config["recovery_cooldown_seconds"])
    cooldown_key = f"{recovery_url}|{','.join(normalized_targets)}"
    with _RECOVERY_LOCK:
        previous = _RECOVERY_LAST_ATTEMPT.get(cooldown_key, 0.0)
        if not force and previous and monotonic_now - previous < cooldown:
            return {
                "state": "cooldown",
                "accepted": True,
                "retry_after_seconds": round(cooldown - (monotonic_now - previous), 1),
            }
        _RECOVERY_LAST_ATTEMPT[cooldown_key] = monotonic_now

    token = Path(config["token_file"]).read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("empty node credential")
    body = json.dumps({
        "action": "reconnect",
        "idempotency_key": (
            f"managed-recovery:{config['id']}:{'-'.join(normalized_targets)}:"
            f"{int(time.time() // max(15, cooldown))}"
        ),
        "reason": reason,
        "targets": normalized_targets,
    }).encode("utf-8")
    request = Request(
        recovery_url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-DBB3-Token": token,
        },
        method="POST",
    )
    with urlopen(request, timeout=config["timeout_seconds"]) as response:
        if response.status not in {200, 202}:
            raise HTTPError(
                request.full_url,
                response.status,
                "recovery request failed",
                response.headers,
                None,
            )
        response_body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(response_body) > MAX_RESPONSE_BYTES:
        raise ValueError("recovery response exceeded the size limit")
    payload = json.loads(response_body.decode("utf-8")) if response_body else {}
    accepted = not isinstance(payload, dict) or payload.get("accepted") is not False
    return {
        "state": "recovering" if accepted else "failed",
        "targets": normalized_targets,
        "accepted": accepted,
        **({"error": "recovery_rejected"} if not accepted else {}),
    }


def _normalize_dbb3_status(
    payload: dict[str, Any],
    fetched_at: datetime,
) -> list[dict[str, Any]]:
    """Extract optional device metrics without inferring worker liveness.

    The historical relay also reports worker-side gateways. Those processes
    no longer own the hosted runtime, so only the official worker WebSocket
    lease may decide whether DBB3, WSL, or HK is online.
    """

    devices = _dict(payload.get("devices"))
    dbb3_device = _dict(devices.get("dbb3"))
    dbb3_metrics = _device_metrics(dbb3_device)
    dbb3_metrics_observation = _observation_status(
        fetched_at,
        dbb3_device,
        fallback="",
    )

    pc_device = _dict(devices.get("pc"))
    pc_metrics = _device_metrics(pc_device)
    pc_metrics_observation = _observation_status(
        fetched_at,
        pc_device,
        fallback="",
    )

    normalized = [
        {
            "id": "dbb3",
            "label": "DBB3",
            "metrics": dbb3_metrics,
            "metrics_available": dbb3_metrics_observation[2],
            "metrics_observed_at": dbb3_metrics_observation[0],
            "metrics_source": "linux_procfs",
        },
        {
            "id": "wsl",
            "label": "Windows PC + WSL",
            "metrics": pc_metrics,
            "metrics_available": (
                pc_metrics_observation[2]
                and pc_device.get("available") is not False
            ),
            "metrics_observed_at": pc_metrics_observation[0],
            "metrics_source": str(_dict(devices.get("pc")).get("source") or "windows_psutil_push"),
        },
    ]

    # HK metrics are optional in older aggregate payloads. The WS registry
    # still emits the HK worker row even when this enrichment is absent.
    hk_aliases = ("hk", "hk-worker", "hk_primary", "hk-primary", "hong_kong", "hong-kong", "hongkong")
    hk_device = _first_dict(devices, *hk_aliases)
    hk_runtime = _first_dict(payload, *hk_aliases)
    if hk_device or hk_runtime:
        hk_source = {**hk_runtime, **hk_device}
        hk_metrics = _device_metrics(hk_source)
        hk_metrics_observation = _observation_status(
            fetched_at,
            hk_device,
            hk_source,
            fallback="",
        )
        normalized.append({
            "id": "hk",
            "label": str(hk_source.get("label") or "Hong Kong Worker"),
            "metrics": hk_metrics,
            "metrics_available": hk_metrics_observation[2] and hk_metrics.get("available") is not False,
            "metrics_observed_at": hk_metrics_observation[0],
            "metrics_source": str(hk_source.get("source") or "hk_worker"),
        })

    return normalized


def _combine_observations(
    *observations: tuple[str, float | None, bool],
) -> tuple[str, float | None, bool]:
    if not observations:
        return "", None, False
    oldest = max(
        observations,
        key=lambda item: float("inf") if item[1] is None else item[1],
    )
    ages = [item[1] for item in observations if item[1] is not None]
    return oldest[0], (max(ages) if ages else None), all(item[2] for item in observations)


def _observation_status(
    fetched_at: datetime,
    *sources: dict[str, Any],
    fallback: str,
) -> tuple[str, float | None, bool]:
    observed_at = _observation_value(*sources) or fallback
    observation = _parse_observation_time(observed_at)
    age_seconds = (
        max(0.0, (fetched_at - observation).total_seconds())
        if observation is not None
        else None
    )
    fresh = (
        age_seconds is not None
        and age_seconds <= DEFAULT_FRESHNESS_SECONDS
        and observation <= fetched_at + timedelta(seconds=30)
    )
    return observed_at, age_seconds, fresh


def _observation_value(*sources: dict[str, Any]) -> str:
    for source in sources:
        for key in (
            "observed_at",
            "sampled_at",
            "heartbeat_at",
            "last_heartbeat_at",
            "updated_at",
            # The DBB3 status relay names gateway probe timestamps checked_at.
            "checked_at",
            "timestamp",
        ):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            # Native/worker relays sometimes use Unix seconds instead of an
            # ISO-8601 string. Preserve the value as text so the parser can
            # apply the same future-skew and freshness fence.
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                return str(value)
    return ""


def _parse_observation_time(value: str) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", normalized):
            parsed = datetime.fromtimestamp(float(normalized), tz=timezone.utc)
        else:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _device_metrics(device: dict[str, Any]) -> dict[str, Any]:
    memory = _dict(device.get("memory"))
    disk = _dict(device.get("disk"))
    network = _dict(device.get("network"))
    return {
        "cpu_percent": _number(device.get("cpu_percent")),
        "memory_percent": _number(memory.get("used_percent")),
        "memory_total_bytes": int(memory.get("total_bytes") or 0),
        "memory_available_bytes": int(memory.get("available_bytes") or 0),
        "disk_percent": _number(disk.get("used_percent")),
        "disk_total_bytes": int(disk.get("total_bytes") or 0),
        "disk_free_bytes": int(disk.get("free_bytes") or 0),
        "network_rx_bytes_per_second": _number(network.get("rx_bytes_per_second")),
        "network_tx_bytes_per_second": _number(network.get("tx_bytes_per_second")),
        "uptime_seconds": int(device.get("uptime_seconds") or 0),
        "sampled_at": str(device.get("sampled_at") or ""),
        "available": device.get("available") is not False,
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_dict(container: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Return the first object value under any compatibility alias."""
    for key in keys:
        value = container.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else 0.0


def _public_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"upstream_http_{exc.code}"
    if isinstance(exc, (TimeoutError, URLError)):
        return "upstream_unreachable"
    if isinstance(exc, (OSError, ValueError, json.JSONDecodeError)):
        return "upstream_invalid"
    return "upstream_error"


def _is_secure_recovery_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        parsed.port
    except ValueError:
        return False
    if parsed.scheme == "https" and hostname and parsed.username is None:
        return True
    if parsed.scheme != "http" or not hostname or parsed.username is not None:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
