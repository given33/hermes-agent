"""Read-only aggregation for privately connected Hermes nodes.

Managed nodes are configured by the operator in ``managed-nodes.json`` under
``HERMES_HOME``. Credentials remain in root-owned files referenced by that
configuration; the dashboard response never includes a URL or token.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from hermes_constants import get_hermes_home


DEFAULT_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def managed_nodes_config_path() -> Path:
    return Path(get_hermes_home()) / "managed-nodes.json"


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
        if not node_id or node_id in seen:
            raise ValueError("managed node ids must be non-empty and unique")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(f"managed node {node_id!r} requires an HTTP(S) status_url")
        if not token_file:
            raise ValueError(f"managed node {node_id!r} requires token_file")
        timeout = float(raw.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 15:
            raise ValueError(f"managed node {node_id!r} has an invalid timeout")
        seen.add(node_id)
        result.append({
            "id": node_id,
            "label": str(raw.get("label") or node_id).strip() or node_id,
            "status_url": url,
            "token_file": token_file,
            "timeout_seconds": timeout,
        })
    return result


def fetch_managed_nodes(path: Path | None = None) -> dict[str, Any]:
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    nodes: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for config in load_managed_nodes_config(path):
        try:
            payload = _fetch_status(config)
            normalized = _normalize_dbb3_status(payload)
            nodes.extend(normalized)
            sources.append({
                "id": config["id"],
                "label": config["label"],
                "online": True,
                "observed_at": str(payload.get("timestamp") or fetched_at),
            })
        except Exception as exc:
            sources.append({
                "id": config["id"],
                "label": config["label"],
                "online": False,
                "observed_at": fetched_at,
                "error": _public_error(exc),
            })
    return {
        "fetched_at": fetched_at,
        "configured": bool(sources),
        "nodes": nodes,
        "sources": sources,
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


def _normalize_dbb3_status(payload: dict[str, Any]) -> list[dict[str, Any]]:
    devices = _dict(payload.get("devices"))
    gateways = _dict(payload.get("gateways"))
    services = _dict(payload.get("services"))
    tasks = _dict(payload.get("tasks"))
    wsl = _dict(payload.get("wsl"))
    observed_at = str(payload.get("timestamp") or "")

    dbb3_metrics = _device_metrics(_dict(devices.get("dbb3")))
    dbb3_gateway = _dict(gateways.get("agent"))
    dbb3_online = bool(dbb3_gateway.get("alive")) and services.get("hermes_gateway") == "active"

    pc_metrics = _device_metrics(_dict(devices.get("pc")))
    wsl_gateway = _dict(gateways.get("rainday"))
    wsl_online = bool(wsl.get("gateway_running")) and bool(wsl.get("worker_ready"))

    return [
        {
            "id": "dbb3",
            "label": "DBB3",
            "online": dbb3_online,
            "gateway_state": str(dbb3_gateway.get("state") or services.get("hermes_gateway") or "unknown"),
            "version": str(dbb3_gateway.get("version") or ""),
            "observed_at": observed_at,
            "metrics": dbb3_metrics,
            "active_tasks": int(tasks.get("running") or 0),
            "metrics_source": "linux_procfs",
        },
        {
            "id": "wsl",
            "label": "WSL",
            "online": wsl_online,
            "gateway_state": str(wsl_gateway.get("state") or wsl.get("state") or "unknown"),
            "version": str(wsl_gateway.get("version") or ""),
            "observed_at": observed_at,
            "metrics": pc_metrics,
            "active_tasks": 0,
            "metrics_source": str(_dict(devices.get("pc")).get("source") or "windows_psutil_push"),
            "runtime": {
                "tunnel_up": bool(wsl.get("tunnel_up")),
                "worker_ready": bool(wsl.get("worker_ready")),
                "gateway_running": bool(wsl.get("gateway_running")),
            },
        },
    ]


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
