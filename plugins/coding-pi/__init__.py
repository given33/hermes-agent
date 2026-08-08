"""Expose the independent Pi runtime as an asynchronous Hermes coding tool."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


CODING_PI_DISPATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "The concrete coding task Pi should execute in its isolated session.",
        },
        "session_id": {
            "type": "string",
            "description": "Optional existing Pi session id to continue instead of creating a new session.",
        },
        "workspace": {
            "type": "string",
            "description": "Optional allowed workspace path for a new local Pi session.",
        },
        "node_id": {
            "type": "string",
            "description": "Optional node id such as local-pc or a registered remote server node.",
        },
        "instructions": {
            "type": "string",
            "description": "Optional constraints or acceptance checks to append to the Pi prompt.",
        },
    },
    "required": ["task"],
    "additionalProperties": False,
}


def _dispatch_url() -> str:
    return os.environ.get(
        "CODING_PI_DISPATCH_URL",
        "http://127.0.0.1:8787/api/coding-pi/dispatch",
    ).strip().rstrip("/")


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("CODING_PI_SERVER_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    owner = os.environ.get("CODING_PI_OWNER_ID", "hermes-agent").strip() or "hermes-agent"
    headers["X-Coding-Pi-Owner"] = owner[:256]
    request = urllib.request.Request(
        _dispatch_url(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Pi dispatch returned an invalid response")
    return value


def handle_coding_pi_dispatch(
    task: str,
    session_id: str | None = None,
    workspace: str | None = None,
    node_id: str | None = None,
    instructions: str | None = None,
    **_: Any,
) -> str:
    """Queue work on Pi and return a handle immediately.

    Pi runs in its own process and service, so this tool deliberately returns
    an accepted handle rather than waiting for the coding turn. Hermes can
    continue its own conversation while the user watches the same task in the
    Coding mode.
    """

    payload = {
        "task": task,
        **({"session_id": session_id} if session_id else {}),
        **({"workspace": workspace} if workspace else {}),
        **({"node_id": node_id} if node_id else {}),
        **({"instructions": instructions} if instructions else {}),
    }
    try:
        result = _post(payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2_000]
        return json.dumps({"accepted": False, "error": f"Pi dispatch HTTP {exc.code}: {detail}"}, ensure_ascii=False)
    except (OSError, urllib.error.URLError, RuntimeError, ValueError) as exc:
        return json.dumps({"accepted": False, "error": f"Pi dispatch unavailable: {exc}"}, ensure_ascii=False)
    return json.dumps({
        "accepted": result.get("accepted") is True,
        "session_id": result.get("session_id"),
        "node": result.get("node"),
        "coding_link": (result.get("collab") or {}).get("web_link"),
        "message": "Pi is executing this task in the independent Coding mode.",
    }, ensure_ascii=False)


def register(ctx) -> None:
    ctx.register_tool(
        name="coding_pi_dispatch",
        toolset="coding_pi",
        schema=CODING_PI_DISPATCH_SCHEMA,
        handler=handle_coding_pi_dispatch,
        description=(
            "Dispatch a coding task to the independent oh-my-pi runtime. "
            "Returns immediately with a Pi session/link so Hermes Chat, Group Chat, "
            "and Workflow remain concurrent and independent."
        ),
        emoji="⌘",
    )
