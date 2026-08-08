"""Official oh-my-pi collab-web wire adapter.

The upstream Pi source owns the agent, tools, sessions, and RPC protocol.  This
module only translates the existing RPC stream into the public ``pi-wire``
collaboration protocol so the unmodified collab-web guest can be used by
Hermes.  The relay never receives plaintext session data: frames are sealed
with the same AES-256-GCM layout used by oh-my-pi (12 byte IV followed by the
ciphertext and GCM tag).

The standalone service is both the Pi supervisor and the collab host.  A
future remote node can attach through the node API without changing this wire
surface; the persistent room credentials live in the Pi session metadata.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import secrets
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import WebSocket, WebSocketDisconnect


COLLAB_PROTO = 3
ROOM_ID_BYTES = 16
ROOM_KEY_BYTES = 32
WRITE_TOKEN_BYTES = 16
ENVELOPE_HEADER_LENGTH = 4
SNAPSHOT_CHUNK_BYTES = 384 * 1024
TRANSCRIPT_READ_CAP = 512 * 1024
_VALID_ENTRY_TYPES = {
    "message",
    "custom_message",
    "compaction",
    "branch_summary",
    "model_change",
    "thinking_level_change",
}
_WIRE_EVENT_TYPES = {
    "agent_start",
    "agent_end",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
    "notice",
    "auto_compaction_start",
    "auto_compaction_end",
    "auto_retry_start",
    "auto_retry_end",
    "thinking_level_changed",
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(value: str) -> bytes:
    text = value.replace("-", "+").replace("_", "/")
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _sealed(key: bytes, frame: dict[str, Any]) -> bytes:
    iv = secrets.token_bytes(12)
    return iv + AESGCM(key).encrypt(iv, _json_bytes(frame), None)


def _open_sealed(key: bytes, data: bytes) -> dict[str, Any]:
    if len(data) <= 12:
        raise ValueError("sealed collab frame is too short")
    plaintext = AESGCM(key).decrypt(data[:12], data[12:], None)
    value = json.loads(plaintext.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("collab frame is not an object")
    return value


def _pack(peer_id: int, payload: bytes) -> bytes:
    return struct.pack(">I", peer_id) + payload


def _unpack(data: bytes) -> tuple[int, bytes]:
    if len(data) < ENVELOPE_HEADER_LENGTH:
        raise ValueError("collab envelope is too short")
    return struct.unpack(">I", data[:ENVELOPE_HEADER_LENGTH])[0], data[ENVELOPE_HEADER_LENGTH:]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def _clean_text(value: Any, fallback: str = "") -> str:
    return value.strip() if isinstance(value, str) else fallback


def _public_origin() -> str:
    configured = (
        os.environ.get("CODING_PI_PUBLIC_ORIGIN", "").strip()
        or os.environ.get("CODING_PI_WEB_ORIGIN", "").strip()
    )
    if configured:
        parsed = urlparse(configured)
        if parsed.hostname not in {"auto", "dynamic"}:
            return configured.rstrip("/")
        host = _auto_public_host()
        scheme = parsed.scheme or os.environ.get("CODING_PI_PUBLIC_SCHEME", "http").strip() or "http"
        port = parsed.port or int(os.environ.get("CODING_PI_PORT", "8787"))
        return f"{scheme}://{host}:{port}"
    default_host = "auto" if os.environ.get("CODING_PI_STANDALONE") == "1" else "127.0.0.1"
    host = os.environ.get("CODING_PI_PUBLIC_HOST", default_host).strip() or default_host
    if host in {"auto", "dynamic"}:
        host = _auto_public_host()
    port = os.environ.get("CODING_PI_PORT", "8787").strip() or "8787"
    scheme = os.environ.get("CODING_PI_PUBLIC_SCHEME", "http").strip() or "http"
    return f"{scheme}://{host}:{port}"


def _auto_public_host() -> str:
    """Return the active LAN IPv4 address without persisting a router lease."""

    candidates: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # connect() only selects the route; it does not send application data.
            probe.connect((os.environ.get("CODING_PI_DISCOVERY_TARGET", "8.8.8.8"), 80))
            candidates.append(str(probe.getsockname()[0]))
    except OSError:
        pass
    try:
        for item in socket.gethostbyname_ex(socket.gethostname())[2]:
            candidates.append(str(item))
    except OSError:
        pass
    for candidate in candidates:
        if candidate and not candidate.startswith("127.") and ":" not in candidate:
            return candidate
    return "127.0.0.1"


def _relay_origin() -> str:
    configured = os.environ.get("CODING_PI_RELAY_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    public = _public_origin()
    parsed = urlparse(public)
    if os.environ.get("CODING_PI_LOCAL_RELAY_LINK", "").strip().lower() in {"1", "true", "yes", "on"}:
        return f"ws://localhost:{parsed.port or os.environ.get('CODING_PI_PORT', '8787')}"
    if parsed.scheme in {"https", "wss"}:
        return f"wss://{parsed.netloc}"
    return f"ws://{parsed.netloc}"


def _web_origin() -> str:
    configured = os.environ.get("CODING_PI_COLLAB_WEB_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    return f"{_public_origin().rstrip('/')}/collab"


def _collab_link(room_id: str, key: bytes, write_token: bytes | None) -> str:
    relay = _relay_origin()
    parsed = urlparse(relay)
    if parsed.scheme not in {"ws", "wss", "http", "https"} or not parsed.netloc:
        raise ValueError("CODING_PI_RELAY_URL must be a ws(s):// or http(s):// origin")
    scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
    secret = key + (write_token or b"")
    return f"{scheme}://{parsed.netloc}/r/{room_id}.{_b64url(secret)}"


def _web_link(room_id: str, key: bytes, write_token: bytes | None) -> str:
    return f"{_web_origin()}/#{_collab_link(room_id, key, write_token)}"


def create_collab_metadata(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create or repair the persisted credentials for one stable Pi room."""

    current = dict(existing) if isinstance(existing, dict) else {}
    try:
        room_id = _clean_text(current.get("room_id"))
        key = _unb64url(_clean_text(current.get("key")))
        write_token = _unb64url(_clean_text(current.get("write_token")))
        if not room_id or len(key) != ROOM_KEY_BYTES or len(write_token) != WRITE_TOKEN_BYTES:
            raise ValueError
    except (ValueError, TypeError, binascii.Error):
        room_id = _b64url(secrets.token_bytes(ROOM_ID_BYTES))
        key = secrets.token_bytes(ROOM_KEY_BYTES)
        write_token = secrets.token_bytes(WRITE_TOKEN_BYTES)
    return {
        "room_id": room_id,
        "key": _b64url(key),
        "write_token": _b64url(write_token),
        "proto": COLLAB_PROTO,
        "persistent": True,
        "created_at": current.get("created_at") or _now_ms(),
    }


def public_collab_metadata(collab: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(collab, dict):
        return None
    room_id = _clean_text(collab.get("room_id"))
    try:
        key = _unb64url(_clean_text(collab.get("key")))
        write_token = _unb64url(_clean_text(collab.get("write_token")))
        if not room_id or len(key) != ROOM_KEY_BYTES or len(write_token) != WRITE_TOKEN_BYTES:
            return None
    except (ValueError, TypeError, binascii.Error):
        return None
    return {
        "proto": COLLAB_PROTO,
        "room_id": room_id,
        "persistent": True,
        "link": _collab_link(room_id, key, write_token),
        "view_link": _collab_link(room_id, key, None),
        "web_link": _web_link(room_id, key, write_token),
        "web_view_link": _web_link(room_id, key, None),
        "permissions": {"personal": "write", "share": "read-only", "guest_protocol": "strict-official"},
    }


@dataclass
class _Peer:
    websocket: WebSocket
    peer_id: int
    name: str
    can_write: bool
    send_lock: asyncio.Lock


class PiCollabBridge:
    """Host one Pi RPC session for official collab-web guests."""

    def __init__(self, session: Any, persist: Callable[[], None]) -> None:
        self.session = session
        self._persist = persist
        self.collab = create_collab_metadata(session.metadata.get("collab"))
        session.metadata["collab"] = self.collab
        self._peers: dict[int, _Peer] = {}
        self._next_peer_id = 1
        self._peer_lock = asyncio.Lock()
        self._state_refresh_task: asyncio.Task[None] | None = None
        self._state_refresh_lock = asyncio.Lock()
        self._entries: list[dict[str, Any]] = []
        self._entry_ids: set[str] = set()
        self._header: dict[str, Any] | None = None
        self._pending_ui: dict[int, dict[str, Any]] = {}
        self._ui_seq = 0
        self._agent_files: dict[str, str] = {}
        self._persist()

    @property
    def room_id(self) -> str:
        return str(self.collab["room_id"])

    @property
    def key(self) -> bytes:
        return _unb64url(str(self.collab["key"]))

    @property
    def write_token(self) -> bytes:
        return _unb64url(str(self.collab["write_token"]))

    def public_metadata(self) -> dict[str, Any]:
        return {
            "proto": COLLAB_PROTO,
            "room_id": self.room_id,
            "persistent": True,
            "link": _collab_link(self.room_id, self.key, self.write_token),
            "view_link": _collab_link(self.room_id, self.key, None),
            "web_link": _web_link(self.room_id, self.key, self.write_token),
            "web_view_link": _web_link(self.room_id, self.key, None),
            "permissions": {
                "personal": "write",
                "share": "read-only",
                "guest_protocol": "strict-official",
            },
        }

    async def publish_rpc_frame(self, frame: dict[str, Any]) -> None:
        frame_type = _clean_text(frame.get("type"))
        if frame_type == "extension_ui_request":
            await self._handle_ui_request(frame)
            return
        if frame_type == "extension_ui_cancel":
            return
        event = self._to_wire_event(frame)
        if event is not None:
            await self.broadcast({"t": "event", "event": event})
            await self._refresh_entries(emit=True)
            if frame_type in {
                "agent_start",
                "agent_end",
                "turn_start",
                "turn_end",
                "message_end",
                "tool_execution_end",
                "notice",
            }:
                self._schedule_state_refresh()
        elif frame_type in {"subagent_lifecycle", "subagent_progress", "subagent_event"}:
            channel = "task:subagent:lifecycle" if frame_type == "subagent_lifecycle" else "task:subagent:progress"
            await self.broadcast({"t": "bus", "channel": channel, "data": frame})
            self._schedule_agents_refresh()
        elif frame_type == "session_info_update":
            self._schedule_state_refresh()
        if frame_type == "session_shutdown":
            await self.broadcast({"t": "bye", "reason": "Pi session stopped"})

    def _to_wire_event(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        frame_type = _clean_text(frame.get("type"))
        if frame_type not in _WIRE_EVENT_TYPES:
            if frame_type == "response" and frame.get("success") is False:
                return {
                    "type": "notice",
                    "level": "error",
                    "message": _clean_text(
                        frame.get("error") or frame.get("message") or frame.get("text"),
                        "Pi RPC command failed",
                    ),
                    "source": "pi-rpc",
                }
            if frame_type in {"error", "prompt_error", "command_error"}:
                return {
                    "type": "notice",
                    "level": "error",
                    "message": _clean_text(
                        frame.get("error") or frame.get("message") or frame.get("text"),
                        "Pi RPC error",
                    ),
                    "source": "pi-rpc",
                }
            if frame_type in {"command_output", "hermes_pi_protocol_error", "hermes_pi_event_truncated"}:
                return {
                    "type": "notice",
                    "level": "warning" if frame_type == "command_output" else "error",
                    "message": _clean_text(frame.get("text") or frame.get("message"), json.dumps(frame, default=str)),
                    "source": "pi-rpc",
                }
            return None
        event = dict(frame)
        event.pop("id", None)
        event.pop("success", None)
        return event

    async def _refresh_entries(self, *, emit: bool) -> None:
        header, entries = _read_session_entries(self.session.directory, self.session.metadata, self.session.session_id)
        self._header = header
        self._entries = entries
        for entry in entries:
            entry_id = _clean_text(entry.get("id"))
            if not entry_id or entry_id in self._entry_ids:
                continue
            self._entry_ids.add(entry_id)
            if len(self._entry_ids) > 50_000:
                self._entry_ids = set(item.get("id") for item in entries if isinstance(item.get("id"), str))
            if emit:
                await self.broadcast({"t": "entry", "entry": entry})

    def _build_state(self, raw: Any = None) -> dict[str, Any]:
        source = dict(raw) if isinstance(raw, dict) else {}
        model_raw = source.get("model")
        model = None
        if isinstance(model_raw, dict):
            model = {
                "id": _clean_text(model_raw.get("id") or model_raw.get("name"), "unknown"),
                "name": _clean_text(model_raw.get("name") or model_raw.get("id"), "Pi"),
                "provider": _clean_text(model_raw.get("provider") or self.session.provider, "unknown"),
                "contextWindow": model_raw.get("contextWindow") if isinstance(model_raw.get("contextWindow"), int) else None,
            }
        context_raw = source.get("contextUsage")
        context = dict(context_raw) if isinstance(context_raw, dict) else None
        return {
            "isStreaming": bool(source.get("isStreaming", source.get("is_streaming", False))),
            "isAborting": bool(source.get("isAborting", source.get("is_aborting", False))),
            "queuedMessageCount": int(source.get("queuedMessageCount", source.get("queued_message_count", 0)) or 0),
            "sessionName": _clean_text(source.get("sessionName") or self.session.metadata.get("title"), "Coding session"),
            "cwd": str(self.session.workspace),
            "model": model,
            "thinkingLevel": _clean_text(source.get("thinkingLevel") or source.get("thinking_level")) or None,
            "contextUsage": context,
            "participants": self._participants(),
        }

    def _participants(self) -> list[dict[str, Any]]:
        participants = [{"name": "Pi host", "role": "host"}]
        participants.extend(
            {
                "name": peer.name,
                "role": "guest",
                **({"readOnly": True} if not peer.can_write else {}),
            }
            for peer in self._peers.values()
        )
        return participants

    async def _snapshot(self, read_only: bool) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        snapshot = await self.session.snapshot()
        await self._refresh_entries(emit=False)
        state = self._build_state(snapshot.get("state"))
        agents = await self._agents()
        return self._header or _fallback_header(self.session), self._entries, {"state": state, "agents": agents, "readOnly": read_only}

    async def _agents(self) -> list[dict[str, Any]]:
        raw: Any = None
        try:
            response = await self.session.send({"type": "get_subagents"})
            raw = response.get("data") if isinstance(response, dict) else None
        except Exception:
            raw = None
        items = raw.get("subagents") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            items = []
        agents: list[dict[str, Any]] = [
            {
                "id": "main",
                "displayName": "Pi",
                "kind": "main",
                "status": "running" if self._build_state().get("isStreaming") else "idle",
                "hasSessionFile": bool(_main_session_file(self.session.directory)),
                "createdAt": int(self.session.metadata.get("created_at") or _now_ms()),
                "lastActivity": int(self.session.metadata.get("updated_at") or _now_ms()),
            }
        ]
        self._agent_files = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            agent_id = _clean_text(item.get("id") or item.get("agentId"))
            if not agent_id:
                continue
            status = _clean_text(item.get("status"), "idle").lower()
            if status not in {"running", "idle", "parked", "aborted"}:
                status = "idle"
            session_file = _clean_text(item.get("sessionFile") or item.get("session_file"))
            if session_file:
                self._agent_files[agent_id] = session_file
            agents.append({
                "id": agent_id,
                "displayName": _clean_text(item.get("displayName") or item.get("name") or item.get("description"), agent_id),
                "kind": "sub",
                "parentId": _clean_text(item.get("parentId") or item.get("parent_id")) or "main",
                "status": status,
                "hasSessionFile": bool(session_file),
                "createdAt": int(item.get("createdAt") or item.get("created_at") or _now_ms()),
                "lastActivity": int(item.get("lastActivity") or item.get("last_activity") or _now_ms()),
            })
        return agents

    def _schedule_state_refresh(self) -> None:
        if self._state_refresh_task is None or self._state_refresh_task.done():
            self._state_refresh_task = asyncio.create_task(self._refresh_state())

    async def _refresh_state(self) -> None:
        async with self._state_refresh_lock:
            try:
                response = await self.session.send({"type": "get_state"})
                raw = response.get("data") if isinstance(response, dict) else None
                await self.broadcast({"t": "state", "state": self._build_state(raw)})
            except Exception:
                return

    def _schedule_agents_refresh(self) -> None:
        async def refresh() -> None:
            with_context = await self._agents()
            await self.broadcast({"t": "agents", "agents": with_context})

        asyncio.create_task(refresh())

    async def _handle_ui_request(self, frame: dict[str, Any]) -> None:
        method = _clean_text(frame.get("method"))
        original_id = _clean_text(frame.get("id"))
        if not original_id:
            return
        if method in {"notify", "open_url"}:
            message = _clean_text(frame.get("message") or frame.get("instructions") or frame.get("url"))
            notify_level = _clean_text(frame.get("notifyType"))
            level = notify_level if notify_level in {"info", "warning", "error"} else "info"
            await self.broadcast({
                "t": "event",
                "event": {"type": "notice", "level": level, "message": message or method, "source": "pi"},
            })
            return
        if method == "cancel":
            target = _clean_text(frame.get("targetId"))
            for request_id, pending in list(self._pending_ui.items()):
                if pending.get("original_id") == target:
                    await self.broadcast({"t": "ui-request-end", "reqId": request_id})
                    self._pending_ui.pop(request_id, None)
            return
        if method not in {"select", "confirm", "input", "editor"}:
            return
        self._ui_seq += 1
        request_id = self._ui_seq
        if method == "select":
            request = {
                "kind": "select",
                "title": _clean_text(frame.get("title"), "Pi needs a selection"),
                "options": frame.get("options") if isinstance(frame.get("options"), list) else [],
                "reqId": request_id,
            }
        elif method == "confirm":
            request = {
                "kind": "select",
                "title": _clean_text(frame.get("title"), "Confirm"),
                "options": ["Allow", "Deny"],
                "helpText": _clean_text(frame.get("message")),
                "reqId": request_id,
            }
        else:
            request = {
                "kind": "editor",
                "title": _clean_text(frame.get("title"), "Pi input"),
                "prefill": _clean_text(frame.get("prefill") or frame.get("placeholder")),
                "reqId": request_id,
            }
        # Keep the complete wire request in the persistent in-memory bridge
        # state.  A guest that reconnects after a network/app restart receives
        # the same select/editor payload instead of a lossy Allow/Deny fallback.
        self._pending_ui[request_id] = {
            "original_id": original_id,
            "method": method,
            "request": request,
        }
        for peer in tuple(self._peers.values()):
            if peer.can_write:
                await self._send(peer, {"t": "ui-request", "request": request})

    async def _handle_guest_frame(self, peer: _Peer, frame: dict[str, Any]) -> None:
        frame_type = _clean_text(frame.get("t"))
        if frame_type == "hello":
            proto = frame.get("proto")
            if proto != COLLAB_PROTO:
                await self._send(peer, {"t": "error", "message": f"protocol mismatch: host speaks v{COLLAB_PROTO}"})
                await peer.websocket.close(code=4001, reason="protocol mismatch")
                return
            token = _clean_text(frame.get("writeToken"))
            try:
                peer.can_write = bool(token) and secrets.compare_digest(_unb64url(token), self.write_token)
            except (ValueError, TypeError):
                peer.can_write = False
            peer.name = _clean_text(frame.get("name"), f"guest-{peer.peer_id}")[:64]
            header, entries, snapshot = await self._snapshot(not peer.can_write)
            await self._send(peer, {
                "t": "welcome",
                "proto": COLLAB_PROTO,
                "header": header,
                "state": snapshot["state"],
                "agents": snapshot["agents"],
                "entryCount": len(entries),
                **({"readOnly": True} if not peer.can_write else {}),
            })
            await self._send_snapshot_chunks(peer, entries)
            for request_id, pending in self._pending_ui.items():
                if peer.can_write:
                    await self._send(peer, {"t": "ui-request", "request": _pending_request(request_id, pending)})
            await self.broadcast({"t": "state", "state": self._build_state(snapshot["state"])})
            return
        if frame_type == "prompt":
            if not peer.can_write:
                await self._reject(peer, "prompting")
                return
            text = _clean_text(frame.get("text"))
            if text:
                command: dict[str, Any] = {"type": "prompt", "message": text}
                if isinstance(frame.get("images"), list):
                    command["images"] = frame["images"]
                await self.session.send(command)
            return
        if frame_type == "abort":
            if not peer.can_write:
                await self._reject(peer, "interrupting")
                return
            await self.session.send({"type": "abort"})
            return
        if frame_type == "ui-response":
            await self._handle_ui_response(peer, frame)
            return
        if frame_type == "fetch-transcript":
            await self._handle_transcript(peer, frame)
            return
        if frame_type == "agent-cmd":
            if not peer.can_write:
                await self._reject(peer, "agent control")
                return
            command = _clean_text(frame.get("cmd"))
            agent_id = _clean_text(frame.get("agentId"))
            text = _clean_text(frame.get("text")) or None
            control = getattr(self.session, "control_agent", None)
            if not callable(control):
                await self._send(peer, {"t": "error", "message": "Pi Agent Hub bridge is unavailable"})
                return
            try:
                await control(command, agent_id, text)
                await self.broadcast({"t": "agents", "agents": await self._agents()})
            except Exception as exc:
                await self._send(peer, {"t": "error", "message": f"agent {agent_id}: {exc}"})
            return

    async def _handle_ui_response(self, peer: _Peer, frame: dict[str, Any]) -> None:
        if not peer.can_write:
            await self._reject(peer, "responding to UI request")
            return
        try:
            request_id = int(frame.get("reqId"))
        except (TypeError, ValueError):
            return
        pending = self._pending_ui.pop(request_id, None)
        if pending is None:
            return
        value = frame.get("value")
        command: dict[str, Any] = {"type": "extension_ui_response", "id": pending["original_id"]}
        if pending["method"] == "confirm":
            command["confirmed"] = _clean_text(value).lower() in {"allow", "yes", "true", "ok"}
        elif isinstance(value, str):
            command["value"] = value
        else:
            command["cancelled"] = True
        await self.session.send(command)
        await self.broadcast({"t": "ui-request-end", "reqId": request_id})

    async def _handle_transcript(self, peer: _Peer, frame: dict[str, Any]) -> None:
        try:
            request_id = int(frame.get("reqId"))
            from_byte = max(0, int(frame.get("fromByte", 0)))
        except (TypeError, ValueError):
            return
        agent_id = _clean_text(frame.get("agentId"))
        file_path = self._agent_files.get(agent_id) if agent_id else None
        if not file_path or agent_id == "main":
            file_path = str(_main_session_file(self.session.directory) or "")
        if not file_path:
            await self._send(peer, {"t": "transcript", "reqId": request_id, "text": "", "newSize": from_byte, "error": "no transcript available"})
            return
        try:
            path = Path(file_path).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(path)
            size = path.stat().st_size
            if size <= from_byte:
                text = ""
                new_size = size
            else:
                with path.open("rb") as handle:
                    handle.seek(from_byte)
                    data = handle.read(min(size - from_byte, TRANSCRIPT_READ_CAP))
                if from_byte + len(data) < size:
                    last_newline = data.rfind(b"\n")
                    if last_newline < 0:
                        raise ValueError("transcript entry exceeds read cap")
                    data = data[: last_newline + 1]
                text = data.decode("utf-8", errors="replace")
                new_size = from_byte + len(data)
            await self._send(peer, {"t": "transcript", "reqId": request_id, "text": text, "newSize": new_size})
        except Exception as exc:
            await self._send(peer, {"t": "transcript", "reqId": request_id, "text": "", "newSize": from_byte, "error": str(exc)})

    async def _reject(self, peer: _Peer, action: str) -> None:
        await self._send(peer, {"t": "error", "message": f"{action} is disabled on a read-only link"})

    async def _send_snapshot_chunks(self, peer: _Peer, entries: list[dict[str, Any]]) -> None:
        if not entries:
            await self._send(peer, {"t": "snapshot-chunk", "entries": [], "final": True})
            return
        batch: list[dict[str, Any]] = []
        batch_bytes = 0
        for entry in entries:
            entry_bytes = len(_json_bytes(entry))
            if batch and batch_bytes + entry_bytes > SNAPSHOT_CHUNK_BYTES:
                await self._send(peer, {"t": "snapshot-chunk", "entries": batch, "final": False})
                batch = []
                batch_bytes = 0
            batch.append(entry)
            batch_bytes += entry_bytes
        await self._send(peer, {"t": "snapshot-chunk", "entries": batch, "final": True})

    async def _send(self, peer: _Peer, frame: dict[str, Any]) -> None:
        payload = _pack(0, _sealed(self.key, frame))
        async with peer.send_lock:
            await peer.websocket.send_bytes(payload)

    async def broadcast(self, frame: dict[str, Any]) -> None:
        for peer in tuple(self._peers.values()):
            try:
                await self._send(peer, frame)
            except Exception:
                self._peers.pop(peer.peer_id, None)

    async def serve_guest(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._peer_lock:
            peer = _Peer(websocket, self._next_peer_id, f"guest-{self._next_peer_id}", False, asyncio.Lock())
            self._next_peer_id += 1
            self._peers[peer.peer_id] = peer
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if not isinstance(data, bytes):
                    continue
                try:
                    _sender, sealed = _unpack(data)
                    frame = _open_sealed(self.key, sealed)
                except Exception:
                    await websocket.close(code=4004, reason="bad key or corrupted frame")
                    break
                await self._handle_guest_frame(peer, frame)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            self._peers.pop(peer.peer_id, None)
            with_context = self._build_state()
            await self.broadcast({"t": "state", "state": with_context})


def _pending_request(request_id: int, pending: dict[str, Any]) -> dict[str, Any]:
    request = pending.get("request")
    if isinstance(request, dict):
        return {**request, "reqId": request_id}
    return {
        "kind": "editor" if pending.get("method") in {"input", "editor"} else "select",
        "title": "Pi request",
        "options": ["Allow", "Deny"],
        "reqId": request_id,
    }


def _fallback_header(session: Any) -> dict[str, Any]:
    return {
        "type": "session",
        "id": str(getattr(session, "session_id", "pi-session")),
        "title": _clean_text(getattr(session, "metadata", {}).get("title"), "Coding session"),
        "timestamp": _iso_timestamp(getattr(session, "metadata", {}).get("created_at")),
        "cwd": str(getattr(session, "workspace", "")),
    }


def _main_session_file(directory: Path) -> Path | None:
    candidates = [
        item
        for item in directory.glob("*.jsonl")
        if item.is_file() and not item.name.startswith("__advisor")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime_ns)


def _read_session_entries(
    directory: Path,
    metadata: dict[str, Any],
    fallback_session_id: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    path = _main_session_file(directory)
    header: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = []
    if path is not None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(value, dict):
                        continue
                    if value.get("type") == "session":
                        header = {
                            "type": "session",
                            "id": _clean_text(value.get("id"), fallback_session_id),
                            "title": _clean_text(value.get("title") or metadata.get("title"), "Coding session"),
                            "timestamp": _iso_timestamp(value.get("timestamp")),
                            "cwd": _clean_text(value.get("cwd"), str(metadata.get("workspace") or "")),
                        }
                        continue
                    if value.get("type") not in _VALID_ENTRY_TYPES:
                        continue
                    if not isinstance(value.get("id"), str):
                        continue
                    normalized = dict(value)
                    normalized.setdefault("parentId", None)
                    normalized.setdefault("timestamp", _iso_timestamp(None))
                    entries.append(normalized)
        except OSError:
            pass
    if header is None:
        header = _fallback_header(type("Session", (), {"session_id": fallback_session_id, "metadata": metadata, "workspace": metadata.get("workspace", "")})())
    return header, entries


def public_origin() -> str:
    """Expose the current advertised origin to the node registry/supervisor."""

    return _public_origin()


__all__ = ["PiCollabBridge", "create_collab_metadata", "public_collab_metadata", "public_origin"]
