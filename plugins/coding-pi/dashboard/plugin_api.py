"""Hermes API bridge for the unmodified oh-my-pi RPC runtime.

The mobile app talks to this small adapter over the normal authenticated Hermes
transport.  Pi itself stays in its upstream repository and is started as one
headless Bun process per coding session.  No Pi UI, source file, or dependency
is bundled into the React Native application.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
import logging
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

try:
    from .collab_bridge import PiCollabBridge, public_collab_metadata, public_origin
except ImportError:
    try:
        # The standalone service normally places this directory on sys.path.
        from collab_bridge import PiCollabBridge, public_collab_metadata, public_origin
    except ImportError:
        # Some Hermes plugin loaders execute plugin_api.py directly from a
        # manifest path without adding its sibling directory to sys.path.
        # Load the official sibling bridge by file location rather than
        # requiring a second package/import convention.
        import importlib.util

        _bridge_path = Path(__file__).with_name("collab_bridge.py")
        _bridge_spec = importlib.util.spec_from_file_location(
            "hermes_coding_pi_collab_bridge", _bridge_path,
        )
        if _bridge_spec is None or _bridge_spec.loader is None:
            raise
        _bridge_module = importlib.util.module_from_spec(_bridge_spec)
        sys.modules[_bridge_spec.name] = _bridge_module
        _bridge_spec.loader.exec_module(_bridge_module)
        PiCollabBridge = _bridge_module.PiCollabBridge
        public_collab_metadata = _bridge_module.public_collab_metadata
        public_origin = _bridge_module.public_origin

_STANDALONE_MODE = os.environ.get("CODING_PI_STANDALONE") == "1"

if _STANDALONE_MODE:
    # The same adapter can be mounted by Hermes for backwards compatibility,
    # or imported by the independent Pi service.  Keep the independent path
    # free of Hermes imports so the service can be deployed as its own process.
    def owner_id_from_request(request: Request) -> str:
        value = (
            request.headers.get("x-coding-pi-owner")
            or request.headers.get("x-client-id")
            or ""
        ).strip()
        if not value:
            raise HTTPException(status_code=401, detail="Coding Pi owner identity required")
        return value[:256]

    def normalize_profile_name(value: str) -> str:
        normalized = str(value or "default").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", normalized):
            raise ValueError("Invalid profile name")
        return normalized

    def get_hermes_home() -> Path:
        configured = (
            os.environ.get("CODING_PI_HOME")
            or os.environ.get("PI_SERVER_HOME")
            or str(Path.cwd() / ".coding-pi")
        )
        return Path(configured).expanduser().resolve()

    def load_config() -> dict[str, Any]:
        # Standalone deployments are configured with environment variables so
        # no Hermes configuration package is required.  A YAML file is also
        # accepted when PyYAML is available, which makes the service pleasant
        # to run outside the Hermes release tree.
        config: dict[str, Any] = {}
        config_path = os.environ.get("CODING_PI_CONFIG", "").strip()
        if config_path:
            try:
                import yaml

                loaded = yaml.safe_load(Path(config_path).expanduser().read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    config = loaded
            except (ImportError, OSError, ValueError):
                config = {}
        coding = config.get("coding_pi")
        coding_config = dict(coding) if isinstance(coding, dict) else {}
        environment_keys = {
            "root": "CODING_PI_ROOT",
            "bun_path": "CODING_PI_BUN_PATH",
            "cli_path": "CODING_PI_CLI_PATH",
            "repository": "CODING_PI_REPOSITORY",
            "ref": "CODING_PI_REF",
            "workspace": "CODING_PI_WORKSPACE",
            "provider": "CODING_PI_PROVIDER",
            "model": "CODING_PI_MODEL",
        }
        for key, environment_key in environment_keys.items():
            value = os.environ.get(environment_key, "").strip()
            if value:
                coding_config[key] = value
        allowed = os.environ.get("CODING_PI_ALLOWED_WORKSPACES", "").strip()
        if allowed:
            coding_config["allowed_workspaces"] = [item for item in allowed.split(os.pathsep) if item]
        args = os.environ.get("CODING_PI_ARGS", "").strip()
        if args:
            coding_config["args"] = [item for item in args.split(" ") if item]
        return {"coding_pi": coding_config}
else:
    from hermes_cli.cloud_file_library import owner_id_from_request
    from hermes_cli.profiles import normalize_profile_name
    from hermes_runtime.config import get_hermes_home, load_config


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,79}$")
_MAX_EVENT_BUFFER = 512
_MAX_EVENT_BYTES = 2 * 1024 * 1024
_COMMAND_TIMEOUT_SECONDS = 120.0
_START_TIMEOUT_SECONDS = 60.0
_AGENT_CONTROL_TIMEOUT_SECONDS = 60.0
_HEARTBEAT_SECONDS = 15.0
# Pi can spend a little time resolving a provider, but a missing credential or
# an unreachable endpoint must become visible in Coding instead of looking
# like an accepted prompt that never started. These notices do not abort the
# official Pi turn; they only give the user an actionable status update.
_PROMPT_START_NOTICE_SECONDS = 10.0
_PROMPT_START_ERROR_SECONDS = 30.0
_RPC_FRAME_BYTES = 1024 * 1024
_RPC_CHUNK_PAYLOAD_BYTES = 256 * 1024
_RPC_REASSEMBLED_BYTES = 64 * 1024 * 1024
_RPC_READ_BYTES = 64 * 1024

# Keep this list in sync with oh-my-pi's public RPC command union.  The adapter
# deliberately forwards the complete command surface instead of reimplementing
# a smaller Hermes-specific agent API.
_RPC_COMMAND_TYPES = frozenset({
    "negotiate_protocol",
    "prompt",
    "steer",
    "follow_up",
    "abort",
    "abort_and_prompt",
    "new_session",
    "get_state",
    "set_fast_mode",
    "get_available_commands",
    "set_todos",
    "set_host_tools",
    "set_host_uri_schemes",
    "set_subagent_subscription",
    "get_subagents",
    "get_subagent_messages",
    "set_model",
    "cycle_model",
    "get_available_models",
    "set_thinking_level",
    "cycle_thinking_level",
    "set_steering_mode",
    "set_follow_up_mode",
    "set_interrupt_mode",
    "compact",
    "set_auto_compaction",
    "set_auto_retry",
    "abort_retry",
    "bash",
    "abort_bash",
    "get_session_stats",
    "export_html",
    "switch_session",
    "branch",
    "get_branch_messages",
    "get_last_assistant_text",
    "set_session_name",
    "handoff",
    "get_messages",
    "get_messages_page",
    "get_login_providers",
    "login",
})
_HOST_FRAME_TYPES = frozenset({
    "extension_ui_response",
    "host_tool_update",
    "host_tool_result",
    "host_uri_result",
})


class CreateSessionBody(BaseModel):
    profile_id: str | None = None
    name: str = Field(default="", max_length=160)
    workspace: str | None = Field(default=None, max_length=4096)
    provider: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=240)
    args: list[str] = Field(default_factory=list, max_length=32)


class PromptBody(BaseModel):
    message: str = Field(min_length=1, max_length=200_000)
    images: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    streaming_behavior: str | None = None


class CommandBody(BaseModel):
    command: dict[str, Any]


class AgentCommandBody(BaseModel):
    command: str = Field(min_length=1, max_length=16)
    agent_id: str = Field(min_length=1, max_length=160)
    text: str | None = Field(default=None, max_length=20_000)


class StopBody(BaseModel):
    force: bool = False


class DispatchBody(BaseModel):
    task: str = Field(min_length=1, max_length=200_000)
    session_id: str | None = Field(default=None, max_length=96)
    workspace: str | None = Field(default=None, max_length=4096)
    node_id: str | None = Field(default=None, max_length=128)
    instructions: str | None = Field(default=None, max_length=20_000)


class ImportHandoffBody(BaseModel):
    source_node_id: str = Field(default="unknown", max_length=128)
    source_session_id: str = Field(min_length=1, max_length=96)
    title: str = Field(default="Coding handoff", max_length=160)
    workspace: str | None = Field(default=None, max_length=4096)
    context: str = Field(default="", max_length=100_000)
    instructions: str = Field(default="Continue the handed-off coding task.", max_length=20_000)


class NodeRegistrationBody(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    label: str = Field(default="Pi node", max_length=160)
    kind: str = Field(default="remote", max_length=64)
    endpoint: str | None = Field(default=None, max_length=4096)
    workspaces: list[str] = Field(default_factory=list, max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=64)


class HandoffBody(BaseModel):
    target_node_id: str = Field(min_length=1, max_length=128)
    workspace: str | None = Field(default=None, max_length=4096)
    instructions: str = Field(default="Continue the handed-off coding task.", max_length=20_000)


@dataclass
class _RpcChunkState:
    chunk_id: str
    count: int
    byte_length: int
    next_index: int
    received_bytes: int
    chunks: list[bytes]


@dataclass(frozen=True)
class PiRuntimeSettings:
    root: Path
    bun: str
    cli: Path
    workspace: Path
    provider: str | None
    model: str | None
    args: tuple[str, ...]
    source_repository: str | None
    source_ref: str | None

    @classmethod
    def resolve(cls) -> "PiRuntimeSettings":
        config = _coding_pi_config()
        root = _resolve_pi_root(config)
        bun = _resolve_bun(config)
        cli_value = str(config.get("cli_path") or "").strip()
        cli = Path(cli_value).expanduser() if cli_value else root / "packages" / "coding-agent" / "src" / "cli.ts"
        if not cli.is_absolute():
            cli = root / cli
        cli = cli.resolve()
        if not cli.is_file():
            raise RuntimeError(
                f"oh-my-pi RPC entrypoint was not found: {cli}. "
                "Set coding_pi.root or coding_pi.cli_path in config.yaml."
            )
        workspace = _resolve_default_workspace(config)
        provider = _optional_string(config.get("provider"))
        model = _optional_string(config.get("model"))
        args = _normalize_cli_args(config.get("args"))
        source_repository = _optional_string(config.get("repository"))
        source_ref = _optional_string(config.get("ref"))
        _verify_configured_source(root, source_repository, source_ref)
        return cls(
            root=root,
            bun=bun,
            cli=cli,
            workspace=workspace,
            provider=provider,
            model=model,
            args=args,
            source_repository=source_repository,
            source_ref=source_ref,
        )

    def as_public_dict(self) -> dict[str, Any]:
        native_ready = _native_addon_ready(self.root)
        generated_ready = (self.root / "packages" / "coding-agent" / "src" / "export" / "html" / "tool-views.generated.js").is_file()
        available = bool(shutil.which(self.bun) or Path(self.bun).is_file()) and native_ready and generated_ready
        return {
            "enabled": True,
            "available": self.cli.is_file() and available,
            "runtime": "oh-my-pi-rpc",
            "host": "standalone" if _STANDALONE_MODE else "hermes-plugin",
            "source_repository": self.source_repository,
            "source_ref": self.source_ref,
            "root": str(self.root),
            "cli": str(self.cli),
            "bun": self.bun,
            "workspace": str(self.workspace),
            "provider": self.provider,
            "model": self.model,
            "native_ready": native_ready,
            "generated_ready": generated_ready,
            "commands": sorted(_RPC_COMMAND_TYPES),
        }


def _coding_pi_config() -> dict[str, Any]:
    try:
        config = load_config() or {}
    except Exception:
        config = {}
    raw = config.get("coding_pi")
    if isinstance(raw, dict):
        return raw
    plugins = config.get("plugins")
    if isinstance(plugins, dict) and isinstance(plugins.get("coding_pi"), dict):
        return plugins["coding_pi"]
    return {}


def _release_root() -> Path:
    # plugin_api.py -> dashboard -> coding-pi -> plugins -> release root
    return Path(__file__).resolve().parents[3]


def _agent_control_extension_path() -> Path | None:
    """Return the external adapter that enables Pi's native Agent Hub controls."""

    candidate = _release_root() / "plugins" / "coding-pi" / "pi-agent-control.ts"
    return candidate if candidate.is_file() else None


def _resolve_pi_root(config: dict[str, Any]) -> Path:
    configured = str(config.get("root") or "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend([
        _release_root().parent / "oh-my-pi",
        Path.cwd() / "oh-my-pi",
        get_hermes_home() / "oh-my-pi",
    ])
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "packages" / "coding-agent" / "src" / "cli.ts").is_file():
            return resolved
    wanted = str(candidates[0]) if candidates else "<configured coding_pi.root>"
    raise RuntimeError(
        f"oh-my-pi was not found ({wanted}). Clone can1357/oh-my-pi and set "
        "coding_pi.root in config.yaml, or place it beside the Hermes release."
    )


def _verify_configured_source(root: Path, repository: str | None, source_ref: str | None) -> None:
    """Fail closed when config claims a private source but the checkout differs."""

    if not repository and not source_ref:
        return
    git_dir = root / ".git"
    if not git_dir.exists():
        raise RuntimeError(
            f"Configured Pi source is not a Git checkout: {root}. "
            "Clone the configured coding_pi.repository into coding_pi.root."
        )
    try:
        remote_result = subprocess.run(
            ["git", "-C", str(root), "config", "--get-regexp", r"^remote\..*\.url$"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        remotes = []
        for line in remote_result.stdout.splitlines():
            _name, separator, remote = line.partition(" ")
            if separator and remote.strip():
                remotes.append(remote.strip())
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not inspect configured Pi Git source: {exc}") from exc
    if repository and not any(_normalize_git_remote(remote) == _normalize_git_remote(repository) for remote in remotes):
        raise RuntimeError(
            f"Pi source remote mismatch: expected {repository}, found {', '.join(remotes) or '<none>'}."
        )
    if source_ref and re.fullmatch(r"[0-9a-fA-F]{40}", source_ref) and head.lower() != source_ref.lower():
        raise RuntimeError(
            f"Pi source commit mismatch: expected {source_ref}, found {head or '<none>'}."
        )


def _normalize_git_remote(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.lower()


def _resolve_bun(config: dict[str, Any]) -> str:
    configured = str(config.get("bun_path") or "").strip()
    if configured:
        found = shutil.which(configured) or configured
        if Path(found).is_file() or shutil.which(found):
            return found
        raise RuntimeError(f"Configured coding_pi.bun_path was not found: {configured}")
    found = shutil.which("bun") or shutil.which("bun.exe")
    if found:
        return found
    raise RuntimeError(
        "Bun is required for oh-my-pi RPC. Install Bun 1.3.14+ or set "
        "coding_pi.bun_path in config.yaml."
    )


def _native_addon_ready(root: Path) -> bool:
    native_dir = root / "packages" / "natives" / "native"
    if not native_dir.is_dir():
        return False
    platform = sys.platform
    candidates = list(native_dir.glob(f"pi_natives.{platform}-*.node"))
    return bool(candidates)


def _resolve_default_workspace(config: dict[str, Any]) -> Path:
    configured = str(config.get("workspace") or "").strip()
    candidate = Path(configured).expanduser() if configured else Path.cwd()
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise RuntimeError(f"Coding workspace is not a directory: {resolved}")
    if not _workspace_allowed(resolved, config):
        raise RuntimeError(
            f"Coding workspace is outside the configured allowlist: {resolved}. "
            "Set coding_pi.allowed_workspaces in config.yaml."
        )
    return resolved


def _allowed_workspace_roots(config: dict[str, Any]) -> list[Path]:
    raw = config.get("allowed_workspaces")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [value for value in raw if isinstance(value, str)]
    else:
        values = []
    if values:
        roots = [Path(value).expanduser().resolve() for value in values if value.strip()]
        return roots
    # The no-config default is intentionally narrow.  A deployment that wants
    # to expose additional repositories should list them explicitly.
    return [Path.cwd().resolve()]


def _workspace_allowed(path: Path, config: dict[str, Any]) -> bool:
    for root in _allowed_workspace_roots(config):
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _resolve_workspace(value: str | None, settings: PiRuntimeSettings) -> Path:
    config = _coding_pi_config()
    candidate = Path(value).expanduser() if str(value or "").strip() else settings.workspace
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise HTTPException(status_code=422, detail=f"Coding workspace is not a directory: {resolved}")
    if not _workspace_allowed(resolved, config):
        raise HTTPException(status_code=403, detail="Coding workspace is not in coding_pi.allowed_workspaces")
    return resolved


def _optional_string(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_cli_args(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    args = tuple(str(item) for item in value if isinstance(item, (str, int, float)))
    if any(item in {"--mode", "--session-dir"} for item in args):
        raise RuntimeError("coding_pi.args may not override Hermes RPC session isolation")
    return args


def _normalize_profile(value: str | None) -> str:
    try:
        return normalize_profile_name(value or "default")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _safe_session_id(value: str) -> str:
    if not _SESSION_ID_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail="Invalid coding session id")
    return value


def _owner_key(owner_id: str) -> str:
    return hashlib.sha256(owner_id.encode("utf-8", errors="replace")).hexdigest()[:40]


def _session_dir(owner_id: str, profile: str, session_id: str) -> Path:
    return Path(get_hermes_home()) / "coding-pi" / _owner_key(owner_id) / profile / "sessions" / session_id


def _metadata_path(directory: Path) -> Path:
    return directory / "hermes-session.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _timestamp() -> int:
    return int(time.time() * 1000)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    pieces.append(text)
                elif item.get("type") == "thinking" and isinstance(item.get("thinking"), str):
                    pieces.append(item["thinking"])
            elif isinstance(item, str):
                pieces.append(item)
        return "".join(pieces)
    return ""


def _command_name(command: dict[str, Any]) -> str:
    value = command.get("type")
    if not isinstance(value, str) or value not in _RPC_COMMAND_TYPES | _HOST_FRAME_TYPES:
        raise HTTPException(status_code=422, detail=f"Unsupported oh-my-pi RPC frame: {value!r}")
    return value


def _response_data(frame: Any) -> Any:
    return frame.get("data") if isinstance(frame, dict) else None


_NODE_REGISTRY: dict[str, dict[str, Any]] = {}

# Wall-clock budget for a non-streaming request forwarded through a node
# tunnel. Without it a half-open TCP connection (or a hung local Pi
# service on the worker) parks the pending future forever.
_TUNNEL_REQUEST_TIMEOUT_SECONDS = 300.0


@dataclass
class _TunnelCall:
    request_id: str
    stream: bool
    future: asyncio.Future[dict[str, Any]] | None = None
    queue: asyncio.Queue[dict[str, Any] | None] | None = None


@dataclass
class _TunnelCollab:
    stream_id: str
    ready: asyncio.Future[dict[str, Any]]
    queue: asyncio.Queue[dict[str, Any] | None]


class _NodeTunnel:
    """One outbound WebSocket from a local PC to the Hermes coordinator.

    The coordinator never dials a private LAN address. It sends the same HTTP
    method/path/body that the mobile client requested through this connection;
    the local agent performs that request against its loopback Pi service and
    sends the response (or SSE chunks) back over the already-open socket.
    """

    def __init__(self, node_id: str, websocket: WebSocket) -> None:
        self.node_id = node_id
        self.websocket = websocket
        self.pending: dict[str, _TunnelCall] = {}
        self.collabs: dict[str, _TunnelCollab] = {}
        self.send_lock = asyncio.Lock()

    async def send(self, payload: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send_json(payload)

    async def request(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        stream: bool,
    ) -> _TunnelCall | dict[str, Any]:
        request_id = uuid.uuid4().hex
        call = _TunnelCall(
            request_id=request_id,
            stream=stream,
            future=None if stream else asyncio.get_running_loop().create_future(),
            queue=asyncio.Queue() if stream else None,
        )
        self.pending[request_id] = call
        try:
            await self.send({
                "type": "request",
                "request_id": request_id,
                "method": method,
                "path": path,
                "headers": headers,
                "body_b64": base64.b64encode(body).decode("ascii") if body else "",
                "stream": stream,
            })
        except Exception:
            self.pending.pop(request_id, None)
            raise
        if stream:
            return call
        assert call.future is not None
        try:
            return await asyncio.wait_for(call.future, timeout=_TUNNEL_REQUEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            error = RuntimeError(
                f"Pi node tunnel request timed out after {_TUNNEL_REQUEST_TIMEOUT_SECONDS}s"
            )
            if not call.future.done():
                call.future.set_exception(error)
            raise
        finally:
            self.pending.pop(request_id, None)

    async def handle(self, payload: dict[str, Any]) -> None:
        request_id = str(payload.get("request_id") or "")
        stream_id = str(payload.get("stream_id") or "")
        message_type = str(payload.get("type") or "")
        if stream_id and message_type.startswith("collab_"):
            pipe = self.collabs.get(stream_id)
            if pipe is None:
                return
            if message_type == "collab_opened" and not pipe.ready.done():
                pipe.ready.set_result(payload)
            elif message_type in {"collab_frame", "collab_closed", "collab_error"}:
                await pipe.queue.put(payload)
                if message_type in {"collab_closed", "collab_error"}:
                    self.collabs.pop(stream_id, None)
            return
        call = self.pending.get(request_id)
        if call is None:
            return
        if not call.stream:
            if message_type == "response" and call.future is not None and not call.future.done():
                call.future.set_result(payload)
            elif message_type == "error" and call.future is not None and not call.future.done():
                call.future.set_result({"type": "response", "status": 502, "body_b64": "", "error": str(payload.get("detail") or "tunnel error")})
            return
        assert call.queue is not None
        if message_type in {"stream_start", "stream_chunk", "stream_end", "error"}:
            await call.queue.put(payload)
        if message_type in {"stream_end", "error"}:
            self.pending.pop(request_id, None)

    async def open_collab(self, path: str) -> _TunnelCollab:
        stream_id = uuid.uuid4().hex
        pipe = _TunnelCollab(
            stream_id=stream_id,
            ready=asyncio.get_running_loop().create_future(),
            queue=asyncio.Queue(),
        )
        self.collabs[stream_id] = pipe
        try:
            await self.send({"type": "collab_open", "stream_id": stream_id, "path": path})
            opened = await asyncio.wait_for(pipe.ready, timeout=15)
            if str(opened.get("status") or "error") != "ok":
                raise RuntimeError(str(opened.get("detail") or "Pi collab relay failed to open"))
            return pipe
        except Exception:
            self.collabs.pop(stream_id, None)
            raise

    async def close_collab(self, pipe: _TunnelCollab) -> None:
        self.collabs.pop(pipe.stream_id, None)
        with suppress(Exception):
            await self.send({"type": "collab_close", "stream_id": pipe.stream_id})

    async def fail_all(self, detail: str) -> None:
        error = RuntimeError(detail)
        for request_id, call in list(self.pending.items()):
            if call.future is not None and not call.future.done():
                call.future.set_exception(error)
            if call.queue is not None:
                await call.queue.put({"type": "error", "detail": detail})
                await call.queue.put(None)
            self.pending.pop(request_id, None)
        for stream_id, pipe in list(self.collabs.items()):
            if not pipe.ready.done():
                pipe.ready.set_exception(error)
            await pipe.queue.put({"type": "collab_error", "stream_id": stream_id, "detail": detail})
            await pipe.queue.put(None)
            self.collabs.pop(stream_id, None)


_NODE_TUNNELS: dict[str, _NodeTunnel] = {}
_COLLAB_NODE_BY_ROOM: dict[str, str] = {}


def _local_node_id() -> str:
    value = os.environ.get("CODING_PI_NODE_ID", "local-pc").strip()
    return value[:128] or "local-pc"


def _local_node_record() -> dict[str, Any]:
    workspaces: list[str] = []
    try:
        workspaces = [str(PiRuntimeSettings.resolve().workspace)]
    except Exception:
        pass
    return {
        "node_id": _local_node_id(),
        "label": os.environ.get("CODING_PI_NODE_LABEL", "Local PC").strip() or "Local PC",
        "kind": os.environ.get("CODING_PI_NODE_KIND", "local").strip() or "local",
        "endpoint": _optional_string(public_origin()),
        "workspaces": workspaces,
        "capabilities": ["pi-rpc", "collab-web", "tool-execution", "handoff-import"],
        "status": "online",
        "last_seen": _timestamp(),
        "local": True,
    }


def _configured_peer_nodes() -> dict[str, dict[str, Any]]:
    raw = os.environ.get("CODING_PI_NODE_PEERS", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    peers: dict[str, dict[str, Any]] = {}
    for node_id, endpoint in value.items():
        if not isinstance(node_id, str) or not isinstance(endpoint, str) or not endpoint.strip():
            continue
        peers[node_id[:128]] = {
            "node_id": node_id[:128],
            "label": node_id[:128],
            "kind": "remote",
            "endpoint": endpoint.strip().rstrip("/"),
            "workspaces": [],
            "capabilities": ["pi-rpc", "handoff-import"],
            "status": "configured",
            "last_seen": None,
            "local": False,
        }
    return peers


def _nodes_snapshot() -> list[dict[str, Any]]:
    nodes = {_local_node_id(): _local_node_record()}
    nodes.update(_configured_peer_nodes())
    for node_id, record in _NODE_REGISTRY.items():
        item = dict(record)
        item["status"] = "online" if _timestamp() - int(item.get("last_seen") or 0) < 90_000 else "stale"
        # A reverse-tunnel node may intentionally use the stable id
        # ``local-pc`` even when the coordinator itself has no local Pi. The
        # live tunnel record must win over the coordinator's placeholder.
        if node_id != _local_node_id() or item.get("tunnel"):
            nodes[node_id] = item
    return sorted(nodes.values(), key=lambda item: str(item.get("node_id")))


def _find_node(node_id: str) -> dict[str, Any] | None:
    return next((item for item in _nodes_snapshot() if item.get("node_id") == node_id), None)


def _forward_json_sync(endpoint: str, path: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    url = endpoint.rstrip("/") + path
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise RuntimeError("remote Pi node returned an invalid JSON object")
    return body


class PiRpcSession:
    def __init__(
        self,
        *,
        owner_id: str,
        profile: str,
        session_id: str,
        directory: Path,
        metadata: dict[str, Any],
        settings: PiRuntimeSettings,
        workspace: Path,
        provider: str | None,
        model: str | None,
        args: tuple[str, ...],
    ) -> None:
        self.owner_id = owner_id
        self.profile = profile
        self.session_id = session_id
        self.directory = directory
        self.metadata = metadata
        self.settings = settings
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.args = args
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._events: deque[tuple[int, dict[str, Any]]] = deque(maxlen=_MAX_EVENT_BUFFER)
        self._event_condition = asyncio.Condition()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._rpc_chunk_state: _RpcChunkState | None = None
        self._sequence = 0
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._last_error: str | None = None
        self._prompt_watchdog_task: asyncio.Task[None] | None = None
        self._prompt_activity = False
        self._collab = PiCollabBridge(self, self._persist_metadata)

    def _persist_metadata(self) -> None:
        _write_json(_metadata_path(self.directory), self.metadata)

    @property
    def status(self) -> str:
        process = self.process
        if process is not None and process.returncode is None:
            return "running"
        if self._last_error:
            return "error"
        return "stopped"

    def public_metadata(self) -> dict[str, Any]:
        return {
            **self.metadata,
            "id": self.session_id,
            "profile": self.profile,
            "workspace": str(self.workspace),
            "status": self.status,
            "last_error": self._last_error,
            "collab": self._collab.public_metadata(),
            "node_id": str(self.metadata.get("node_id") or _local_node_id()),
            "node_kind": str(self.metadata.get("node_kind") or "local"),
        }

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.process is not None and self.process.returncode is None:
                return
            try:
                cli_exists = self.settings.cli.is_file()
            except OSError as exc:
                self._last_error = str(exc)
                raise RuntimeError(f"oh-my-pi CLI is unavailable: {exc}") from exc
            if not cli_exists:
                raise RuntimeError(f"oh-my-pi CLI is unavailable: {self.settings.cli}")
            if not _native_addon_ready(self.settings.root):
                raise RuntimeError(
                    "oh-my-pi native addon is missing. Run `bun run build:native` "
                    "from the oh-my-pi repository before starting coding sessions."
                )
            generated_file = self.settings.root / "packages" / "coding-agent" / "src" / "export" / "html" / "tool-views.generated.js"
            if not generated_file.is_file():
                raise RuntimeError(
                    "oh-my-pi generated tool views are missing. Run "
                    "`bun --cwd=packages/coding-agent run gen:tool-views`."
                )
            self.directory.mkdir(parents=True, exist_ok=True)
            command = [self.settings.bun, str(self.settings.cli), "--mode", "rpc", "--session-dir", str(self.directory)]
            control_extension = _agent_control_extension_path()
            if control_extension is not None:
                command.extend(["--extension", str(control_extension)])
            if self.provider:
                command.extend(["--provider", self.provider])
            if self.model:
                command.extend(["--model", self.model])
            # A Hermes server restart must re-open the same Pi transcript. The
            # official CLI supports resuming by JSONL path; each Hermes
            # session has its own session directory, so the newest transcript
            # is the one created by this session before the process went down.
            # Do not add a second resume flag when an operator explicitly
            # supplied --resume/--continue in the configured CLI arguments.
            has_resume_arg = any(
                arg in {"--resume", "-r", "--continue", "-c"}
                or arg.startswith("--resume=")
                for arg in self.args
            )
            if not has_resume_arg:
                try:
                    existing_session_files = sorted(
                        (
                            path
                            for path in self.directory.glob("*.jsonl")
                            if path.is_file() and not path.name.startswith("__advisor")
                        ),
                        key=lambda path: path.stat().st_mtime_ns,
                    )
                except OSError:
                    existing_session_files = []
                if existing_session_files:
                    command.extend(["--resume", str(existing_session_files[-1])])
            command.extend(self.args)
            environment = os.environ.copy()
            environment["PI_NOTIFICATIONS"] = "off"
            environment["OMP_SKIP_SETUP"] = "1"
            # The external Agent Hub extension imports the same pinned checkout
            # inside this Pi process.  Pass the resolved root explicitly even
            # when the host was configured through Hermes YAML rather than env.
            environment["CODING_PI_ROOT"] = str(self.settings.root)
            environment["CODING_PI_AGENT_CONTROL_DIR"] = str(self.directory / "hermes-agent-control")
            # Pi's own auth/config remains account-scoped under Hermes rather
            # than leaking into the server operator's global ~/.omp directory.
            environment["PI_CODING_AGENT_DIR"] = str(self.directory.parent.parent / "agent")
            self._ready = asyncio.Event()
            self._rpc_chunk_state = None
            self._last_error = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(self.workspace),
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (FileNotFoundError, OSError) as exc:
                self._last_error = str(exc)
                raise RuntimeError(f"Failed to start oh-my-pi RPC: {exc}") from exc
            self.process = process
            self._reader_task = asyncio.create_task(self._read_stdout(process))
            self._stderr_task = asyncio.create_task(self._read_stderr(process))
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=_START_TIMEOUT_SECONDS)
            except Exception as exc:
                detail = self._last_error or self._stderr_text() or str(exc)
                # ``start`` owns the lifecycle lock, so calling ``close`` here
                # would wait forever on the same lock after a failed spawn.
                await self._close_unlocked(force=True)
                raise RuntimeError(f"oh-my-pi RPC did not become ready: {detail}") from exc
            if self.process is None:
                detail = self._last_error or self._stderr_text() or "oh-my-pi RPC process exited"
                raise RuntimeError(f"oh-my-pi RPC exited before becoming ready: {detail}")
            try:
                await self._negotiate_protocol_unlocked()
            except Exception as exc:
                detail = self._last_error or self._stderr_text() or str(exc)
                await self._close_unlocked(force=True)
                raise RuntimeError(f"oh-my-pi RPC protocol negotiation failed: {detail}") from exc

    async def control_agent(self, command: str, agent_id: str, text: str | None = None) -> None:
        """Run an official Agent Hub lifecycle action inside Pi's own process."""

        if command not in {"chat", "kill", "revive"}:
            raise RuntimeError(f"unsupported Agent Hub command: {command}")
        if _agent_control_extension_path() is None:
            raise RuntimeError("Pi Agent Hub extension is unavailable")
        await self.start()
        control_dir = self.directory / "hermes-agent-control"
        control_dir.mkdir(parents=True, exist_ok=True)
        request_id = f"hermes-agent-{uuid.uuid4().hex}"
        result_path = control_dir / f"{request_id}.json"
        request = {
            "requestId": request_id,
            "cmd": command,
            "agentId": agent_id,
            **({"text": text} if text else {}),
        }
        encoded = base64.urlsafe_b64encode(json.dumps(request, ensure_ascii=False).encode("utf-8")).decode("ascii").rstrip("=")
        response = await self.send({"type": "prompt", "message": f"/hermes-agent-control {encoded}"})
        if response.get("success") is not True:
            raise RuntimeError(str(response.get("error") or "Pi Agent Hub command was rejected"))
        deadline = time.monotonic() + _AGENT_CONTROL_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline:
                result = _read_json(result_path)
                if result is not None:
                    with suppress(OSError):
                        result_path.unlink()
                    if result.get("ok") is True:
                        return
                    raise RuntimeError(str(result.get("error") or "Pi Agent Hub command failed"))
                await asyncio.sleep(0.1)
            raise RuntimeError("Timed out waiting for Pi Agent Hub command")
        finally:
            with suppress(OSError):
                result_path.unlink()

    async def _negotiate_protocol_unlocked(self) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise RuntimeError("oh-my-pi RPC process is not running")
        request_id = f"hermes-negotiate-{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = waiter
        try:
            await self._write(
                {
                    "type": "negotiate_protocol",
                    "protocolVersion": 2,
                    "id": request_id,
                }
            )
            response = await asyncio.wait_for(waiter, timeout=_COMMAND_TIMEOUT_SECONDS)
        finally:
            self._pending.pop(request_id, None)
        if response.get("success") is not True:
            raise RuntimeError(str(response.get("error") or "oh-my-pi RPC protocol v2 is unavailable"))

    async def _read_stdout(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        buffer = bytearray()
        try:
            while True:
                chunk = await process.stdout.read(_RPC_READ_BYTES)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    separator = buffer.find(b"\n")
                    if separator < 0:
                        if len(buffer) > _RPC_FRAME_BYTES:
                            raise RuntimeError("oh-my-pi RPC physical frame exceeded 1 MiB")
                        break
                    raw_line = bytes(buffer[:separator])
                    del buffer[: separator + 1]
                    if len(raw_line) > _RPC_FRAME_BYTES:
                        raise RuntimeError("oh-my-pi RPC physical frame exceeded 1 MiB")
                    text = raw_line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        physical_frame = json.loads(text)
                    except ValueError:
                        await self._publish(
                            {"type": "hermes_pi_protocol_error", "message": "Invalid JSON from Pi RPC"}
                        )
                        continue
                    if not isinstance(physical_frame, dict):
                        continue
                    frame = self._decode_rpc_frame(physical_frame)
                    if frame is None:
                        continue
                    if frame.get("type") == "ready":
                        self._ready.set()
                    published_frame = frame
                    try:
                        if len(json.dumps(frame, ensure_ascii=False)) > _MAX_EVENT_BYTES:
                            published_frame = {
                                "type": "hermes_pi_event_truncated",
                                "original_type": frame.get("type"),
                                "message": "Pi event exceeded the Hermes event buffer limit",
                            }
                    except (TypeError, ValueError):
                        published_frame = {
                            "type": "hermes_pi_event_invalid",
                            "message": "Pi emitted a non-serializable event",
                        }
                    await self._publish(published_frame)
                    frame_id = frame.get("id")
                    if frame.get("type") == "response" and isinstance(frame_id, str):
                        waiter = self._pending.get(frame_id)
                        if waiter is not None and not waiter.done():
                            waiter.set_result(frame)
            if buffer.strip():
                raise RuntimeError("oh-my-pi RPC ended with an incomplete JSON frame")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = str(exc)
            if process.returncode is None:
                with suppress(ProcessLookupError, OSError):
                    process.kill()
                with suppress(Exception):
                    await process.wait()
        finally:
            if self.process is process:
                self.process = None
            error = self._last_error or self._stderr_text() or "oh-my-pi RPC process exited"
            if not self._ready.is_set():
                self._last_error = error
                self._ready.set()
            for waiter in list(self._pending.values()):
                if not waiter.done():
                    waiter.set_exception(RuntimeError(error))
            self._pending.clear()
            async with self._event_condition:
                self._event_condition.notify_all()

    def _decode_rpc_frame(self, physical_frame: dict[str, Any]) -> dict[str, Any] | None:
        if physical_frame.get("type") != "rpc_chunk":
            if self._rpc_chunk_state is not None:
                raise RuntimeError("oh-my-pi RPC chunk sequence was interrupted")
            return physical_frame

        chunk_id = physical_frame.get("chunkId")
        index = physical_frame.get("index")
        count = physical_frame.get("count")
        byte_length = physical_frame.get("byteLength")
        encoded = physical_frame.get("data")
        if (
            not isinstance(chunk_id, str)
            or not chunk_id
            or len(chunk_id) > 128
            or not isinstance(index, int)
            or isinstance(index, bool)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or not isinstance(byte_length, int)
            or isinstance(byte_length, bool)
            or index < 0
            or count < 2
            or count > (_RPC_REASSEMBLED_BYTES + _RPC_CHUNK_PAYLOAD_BYTES - 1) // _RPC_CHUNK_PAYLOAD_BYTES
            or index >= count
            or byte_length < _RPC_FRAME_BYTES
            or byte_length > _RPC_REASSEMBLED_BYTES
            or not isinstance(encoded, str)
        ):
            raise RuntimeError("Invalid oh-my-pi RPC chunk metadata")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError("Invalid oh-my-pi RPC chunk payload") from exc
        if len(payload) > _RPC_CHUNK_PAYLOAD_BYTES:
            raise RuntimeError("oh-my-pi RPC chunk payload exceeded 256 KiB")

        state = self._rpc_chunk_state
        if state is None:
            if index != 0:
                raise RuntimeError("oh-my-pi RPC chunk sequence must start at index 0")
            state = _RpcChunkState(
                chunk_id=chunk_id,
                count=count,
                byte_length=byte_length,
                next_index=0,
                received_bytes=0,
                chunks=[],
            )
            self._rpc_chunk_state = state
        if (
            state.chunk_id != chunk_id
            or state.count != count
            or state.byte_length != byte_length
            or state.next_index != index
        ):
            raise RuntimeError("oh-my-pi RPC chunk sequence mismatch")
        state.chunks.append(payload)
        state.received_bytes += len(payload)
        state.next_index += 1
        if state.received_bytes > state.byte_length:
            raise RuntimeError("oh-my-pi RPC chunk sequence exceeded its declared length")
        if state.next_index < state.count:
            return None
        if state.received_bytes != state.byte_length:
            raise RuntimeError("oh-my-pi RPC chunk sequence ended at the wrong length")
        logical_bytes = b"".join(state.chunks)
        self._rpc_chunk_state = None
        try:
            logical_frame = json.loads(logical_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("oh-my-pi RPC reassembled frame was invalid JSON") from exc
        if not isinstance(logical_frame, dict):
            raise RuntimeError("oh-my-pi RPC reassembled frame was not an object")
        return logical_frame

    async def _read_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        try:
            while True:
                raw_line = await process.stderr.readline()
                if not raw_line:
                    break
                self._stderr_tail.append(raw_line.decode("utf-8", errors="replace").strip())
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _publish(self, frame: dict[str, Any]) -> None:
        try:
            if len(json.dumps(frame, ensure_ascii=False)) > _MAX_EVENT_BYTES:
                frame = {
                    "type": "hermes_pi_event_truncated",
                    "original_type": frame.get("type"),
                    "message": "Pi event exceeded the Hermes event buffer limit",
                }
        except (TypeError, ValueError):
            frame = {"type": "hermes_pi_event_invalid", "message": "Pi emitted a non-serializable event"}
        self._sequence += 1
        sequence = self._sequence
        self._observe_prompt_frame(frame)
        self._update_metadata_from_frame(frame)
        with suppress(Exception):
            await self._collab.publish_rpc_frame(frame)
        async with self._event_condition:
            self._events.append((sequence, frame))
            self._event_condition.notify_all()

    def _cancel_prompt_watchdog(self) -> None:
        task = self._prompt_watchdog_task
        self._prompt_watchdog_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _arm_prompt_watchdog(self) -> None:
        self._cancel_prompt_watchdog()
        self._prompt_activity = False
        self._prompt_watchdog_task = asyncio.create_task(self._prompt_watchdog())

    def _observe_prompt_frame(self, frame: dict[str, Any]) -> None:
        frame_type = frame.get("type")
        if frame_type in {
            "agent_start",
            "turn_start",
            "message_start",
            "message_update",
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        }:
            self._prompt_activity = True
        if (
            (frame_type == "response" and frame.get("success") is False)
            or frame_type in {"error", "prompt_error", "command_error"}
            or (frame_type == "notice" and frame.get("level") == "error")
        ):
            self._cancel_prompt_watchdog()
        if frame_type in {"agent_end", "turn_end", "session_shutdown"}:
            self._cancel_prompt_watchdog()

    async def _prompt_watchdog(self) -> None:
        try:
            await asyncio.sleep(_PROMPT_START_NOTICE_SECONDS)
            if self._prompt_activity or self.process is None:
                return
            await self._publish(
                {
                    "type": "notice",
                    "level": "warning",
                    "message": (
                        "Pi is still waiting for the provider to start this turn. "
                        "Check the API key, model, base URL, and network if this continues."
                    ),
                    "source": "hermes-pi-watchdog",
                }
            )
            await asyncio.sleep(_PROMPT_START_ERROR_SECONDS - _PROMPT_START_NOTICE_SECONDS)
            if self._prompt_activity or self.process is None:
                return
            await self._publish(
                {
                    "type": "notice",
                    "level": "error",
                    "message": (
                        f"Pi did not start this turn within {_PROMPT_START_ERROR_SECONDS} seconds. "
                        "Verify the provider credentials and network, then retry."
                    ),
                    "source": "hermes-pi-watchdog",
                }
            )
        except asyncio.CancelledError:
            raise
        finally:
            if self._prompt_watchdog_task is asyncio.current_task():
                self._prompt_watchdog_task = None

    def _update_metadata_from_frame(self, frame: dict[str, Any]) -> None:
        frame_type = frame.get("type")
        if frame_type == "session_info_update":
            title = frame.get("title")
            if isinstance(title, str) and title.strip():
                self.metadata["title"] = title.strip()[:160]
        if frame_type == "message_end":
            message = frame.get("message")
            preview = _as_text(message.get("content")) if isinstance(message, dict) else ""
            if preview.strip():
                self.metadata["preview"] = preview.strip()[:280]
        if frame_type in {"agent_end", "turn_end", "session_shutdown"}:
            self.metadata["updated_at"] = _timestamp()

    def _stderr_text(self) -> str:
        return "\n".join(item for item in self._stderr_tail if item)[-2_000:]

    async def send(self, command: dict[str, Any]) -> dict[str, Any]:
        command_type = _command_name(command)
        await self.start()
        process = self.process
        if process is None or process.stdin is None:
            raise RuntimeError("oh-my-pi RPC process is not running")
        request_id = uuid.uuid4().hex
        frame = {key: value for key, value in command.items() if key != "id"}
        frame["id"] = request_id
        if command_type in _HOST_FRAME_TYPES:
            await self._write(frame)
            return {"type": "response", "command": command_type, "success": True, "data": {"accepted": True}}
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = waiter
        try:
            if command_type == "prompt":
                self._arm_prompt_watchdog()
            await self._write(frame)
            response = await asyncio.wait_for(waiter, timeout=_COMMAND_TIMEOUT_SECONDS)
            if response.get("success") is False:
                if command_type == "prompt":
                    self._cancel_prompt_watchdog()
                return response
            return response
        except Exception:
            if command_type == "prompt":
                self._cancel_prompt_watchdog()
            raise
        finally:
            self._pending.pop(request_id, None)

    async def _write(self, frame: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise RuntimeError("oh-my-pi RPC process is not running")
        payload = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            process.stdin.write(payload)
            await process.stdin.drain()

    async def snapshot(self) -> dict[str, Any]:
        await self.start()
        responses: dict[str, Any] = {}
        for command_type in ("get_state", "get_messages", "get_available_commands", "get_subagents"):
            try:
                responses[command_type] = await self.send({"type": command_type})
            except Exception as exc:
                responses[command_type] = {"success": False, "error": str(exc)}
        return {
            "session": self.public_metadata(),
            "state": _response_data(responses["get_state"]),
            "messages": _response_data(responses["get_messages"]),
            "commands": _response_data(responses["get_available_commands"]),
            "subagents": _response_data(responses["get_subagents"]),
            "sequence": self._sequence,
        }

    async def next_event(self, after: int) -> tuple[int, dict[str, Any]] | None:
        async with self._event_condition:
            while True:
                for sequence, frame in self._events:
                    if sequence > after:
                        return sequence, frame
                if self.process is None:
                    return None
                await self._event_condition.wait()

    async def _close_unlocked(self, *, force: bool = False) -> None:
        process = self.process
        if process is not None and process.stdin is not None:
            with suppress(Exception):
                process.stdin.close()
        if process is not None and process.returncode is None:
            if force:
                process.kill()
            else:
                process.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5.0)
        tasks = tuple(
            task
            for task in (self._reader_task, self._stderr_task, self._prompt_watchdog_task)
            if task is not None and task is not asyncio.current_task()
        )
        self._prompt_watchdog_task = None
        for task in tasks:
            if not task.done():
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None:
            for stream in (process.stdout, process.stderr):
                transport = getattr(stream, "_transport", None)
                close = getattr(transport, "close", None)
                if callable(close):
                    with suppress(Exception):
                        close()
        if self.process is process:
            self.process = None
        async with self._event_condition:
            self._event_condition.notify_all()

    async def close(self, *, force: bool = False) -> None:
        async with self._lifecycle_lock:
            await self._close_unlocked(force=force)


class PiSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str, str], PiRpcSession] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        owner_id: str,
        profile: str,
        body: CreateSessionBody,
    ) -> PiRpcSession:
        settings = PiRuntimeSettings.resolve()
        workspace = _resolve_workspace(body.workspace, settings)
        session_id = f"pi_{uuid.uuid4().hex}"
        directory = _session_dir(owner_id, profile, session_id)
        metadata = {
            "title": body.name.strip() or "Coding session",
            "preview": "",
            "created_at": _timestamp(),
            "updated_at": _timestamp(),
            "workspace": str(workspace),
            "provider": body.provider or settings.provider,
            "model": body.model or settings.model,
            "node_id": _local_node_id(),
            "node_kind": "local",
        }
        _write_json(_metadata_path(directory), metadata)
        session = PiRpcSession(
            owner_id=owner_id,
            profile=profile,
            session_id=session_id,
            directory=directory,
            metadata=metadata,
            settings=settings,
            workspace=workspace,
            provider=body.provider or settings.provider,
            model=body.model or settings.model,
            args=_normalize_cli_args(list(settings.args) + body.args),
        )
        async with self._lock:
            self._sessions[(owner_id, profile, session_id)] = session
        try:
            await session.start()
            if body.name.strip():
                await session.send({"type": "set_session_name", "name": body.name.strip()[:160]})
            _write_json(_metadata_path(directory), session.metadata)
            return session
        except Exception:
            async with self._lock:
                self._sessions.pop((owner_id, profile, session_id), None)
            await session.close(force=True)
            raise

    async def get(self, *, owner_id: str, profile: str, session_id: str) -> PiRpcSession:
        _safe_session_id(session_id)
        key = (owner_id, profile, session_id)
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is None:
                directory = _session_dir(owner_id, profile, session_id)
                existing = next(
                    (candidate for candidate in self._sessions.values() if candidate.directory == directory),
                    None,
                )
        if existing is not None:
            await existing.start()
            return existing
        directory = _session_dir(owner_id, profile, session_id)
        metadata = _read_json(_metadata_path(directory))
        if metadata is None:
            raise HTTPException(status_code=404, detail="Coding session not found")
        settings = PiRuntimeSettings.resolve()
        workspace = _resolve_workspace(str(metadata.get("workspace") or ""), settings)
        session = PiRpcSession(
            owner_id=owner_id,
            profile=profile,
            session_id=session_id,
            directory=directory,
            metadata=metadata,
            settings=settings,
            workspace=workspace,
            provider=_optional_string(metadata.get("provider")) or settings.provider,
            model=_optional_string(metadata.get("model")) or settings.model,
            args=settings.args,
        )
        async with self._lock:
            current = self._sessions.setdefault(key, session)
        if current is not session:
            await current.start()
            return current
        await session.start()
        return session

    async def get_by_collab_room(self, room_id: str) -> PiRpcSession:
        """Resolve a persistent room after a supervisor restart.

        The room id is the only public identifier in an official collab link.
        We therefore scan the local metadata index, then reconstruct the same
        Pi session directory.  The room key is still the bearer secret: a
        client with the wrong key fails the AES-GCM handshake before any
        session data is returned.
        """

        async with self._lock:
            existing = next(
                (candidate for candidate in self._sessions.values() if candidate._collab.room_id == room_id),
                None,
            )
        if existing is not None:
            await existing.start()
            return existing
        root = Path(get_hermes_home()) / "coding-pi"
        if not root.is_dir():
            raise HTTPException(status_code=404, detail="Collab room not found")
        metadata_path = next(
            (item for item in root.glob("*/*/sessions/*/hermes-session.json")
             if (_read_json(item) or {}).get("collab", {}).get("room_id") == room_id),
            None,
        )
        if metadata_path is None:
            raise HTTPException(status_code=404, detail="Collab room not found")
        directory = metadata_path.parent
        metadata = _read_json(metadata_path)
        if metadata is None:
            raise HTTPException(status_code=404, detail="Collab room metadata is invalid")
        session_id = directory.name
        _safe_session_id(session_id)
        profile = directory.parent.parent.name
        settings = PiRuntimeSettings.resolve()
        workspace = _resolve_workspace(str(metadata.get("workspace") or ""), settings)
        session = PiRpcSession(
            owner_id=f"collab-room:{room_id}",
            profile=profile,
            session_id=session_id,
            directory=directory,
            metadata=metadata,
            settings=settings,
            workspace=workspace,
            provider=_optional_string(metadata.get("provider")) or settings.provider,
            model=_optional_string(metadata.get("model")) or settings.model,
            args=settings.args,
        )
        async with self._lock:
            current = next(
                (candidate for candidate in self._sessions.values() if candidate.directory == directory),
                None,
            )
            if current is None:
                self._sessions[(session.owner_id, profile, session_id)] = session
                current = session
        if current is not session:
            await session.close(force=True)
        await current.start()
        return current

    async def list(self, *, owner_id: str, profile: str) -> list[dict[str, Any]]:
        root = _session_dir(owner_id, profile, "_placeholder").parent
        if not root.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for directory in sorted(root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
            if not directory.is_dir() or not _SESSION_ID_RE.fullmatch(directory.name):
                continue
            metadata = _read_json(_metadata_path(directory))
            if metadata is None:
                continue
            async with self._lock:
                session = self._sessions.get((owner_id, profile, directory.name))
            if session is not None:
                items.append(session.public_metadata())
            else:
                items.append({
                    **metadata,
                    "id": directory.name,
                    "profile": profile,
                    "status": "stopped",
                    "last_error": None,
                    "collab": public_collab_metadata(metadata.get("collab")),
                })
        return items

    async def stop(self, *, owner_id: str, profile: str, session_id: str, force: bool) -> dict[str, Any]:
        session = await self.get(owner_id=owner_id, profile=profile, session_id=session_id)
        if not force:
            with suppress(Exception):
                await session.send({"type": "abort"})
        await session.close(force=force)
        session.metadata["updated_at"] = _timestamp()
        _write_json(_metadata_path(session.directory), session.metadata)
        return session.public_metadata()

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
        await asyncio.gather(*(session.close(force=True) for session in sessions), return_exceptions=True)


_MANAGER = PiSessionManager()


@asynccontextmanager
async def coding_pi_lifespan(_app: Any) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await _MANAGER.close_all()


router = APIRouter(lifespan=coding_pi_lifespan)


def _owner(request: Request) -> str:
    return owner_id_from_request(request)


def _room_id_from_link_path(value: str) -> str:
    """Extract roomId from the official ``/r/roomId.key`` relay path."""

    return value.split('.', 1)[0].strip()


def _raise_runtime(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise exc


def _remember_collab_node(node_id: str, body: bytes) -> None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    collab = payload.get("collab")
    if not isinstance(collab, dict):
        session = payload.get("session")
        collab = session.get("collab") if isinstance(session, dict) else None
    room_id = collab.get("room_id") if isinstance(collab, dict) else None
    if isinstance(room_id, str) and room_id.strip():
        _COLLAB_NODE_BY_ROOM[room_id.strip()] = node_id


def _tunnel_for_collab(room_path: str) -> _NodeTunnel | None:
    room_id = _room_id_from_link_path(room_path.rsplit("/", 1)[-1])
    node_id = _COLLAB_NODE_BY_ROOM.get(room_id)
    if node_id:
        tunnel = _NODE_TUNNELS.get(node_id)
        if tunnel is not None:
            return tunnel
    # A coordinator restart can lose the in-memory room index while the local
    # PC remains connected. For a single-PC deployment, probing the active
    # tunnel is safe because the room/key is still verified by Pi's AES-GCM
    # bridge on the other side.
    return next(iter(_NODE_TUNNELS.values()), None)


@router.websocket("/r/{room_id}")
async def collab_room_socket(websocket: WebSocket, room_id: str) -> None:
    """Direct host-backed official collab-web guest endpoint."""

    room_path = room_id
    room_id = _room_id_from_link_path(room_path)
    try:
        session = await _MANAGER.get_by_collab_room(room_id)
    except HTTPException:
        tunnel = _tunnel_for_collab(room_path)
        if tunnel is None:
            await websocket.close(code=4004, reason="collab room not found")
            return
        try:
            await websocket.accept()
            pipe = await tunnel.open_collab(f"/r/{room_path}")
            async def client_to_node() -> None:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        return
                    if message.get("text") is not None:
                        payload = str(message["text"]).encode("utf-8")
                        await tunnel.send({"type": "collab_frame", "stream_id": pipe.stream_id, "binary": False, "body_b64": base64.b64encode(payload).decode("ascii")})
                    elif message.get("bytes") is not None:
                        await tunnel.send({"type": "collab_frame", "stream_id": pipe.stream_id, "binary": True, "body_b64": base64.b64encode(message["bytes"]).decode("ascii")})

            async def node_to_client() -> None:
                while True:
                    frame = await pipe.queue.get()
                    if frame is None or frame.get("type") in {"collab_closed", "collab_error"}:
                        return
                    if frame.get("type") != "collab_frame":
                        continue
                    payload = _tunnel_body(frame.get("body_b64"))
                    if frame.get("binary"):
                        await websocket.send_bytes(payload)
                    else:
                        await websocket.send_text(payload.decode("utf-8", errors="replace"))

            client_task = asyncio.create_task(client_to_node())
            node_task = asyncio.create_task(node_to_client())
            done, pending = await asyncio.wait({client_task, node_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*done, return_exceptions=True)
        except Exception:
            with suppress(Exception):
                await websocket.close(code=1011, reason="Pi collab tunnel unavailable")
        finally:
            if "pipe" in locals():
                await tunnel.close_collab(pipe)
        return
    except Exception:
        await websocket.close(code=1011, reason="collab room unavailable")
        return
    await session._collab.serve_guest(websocket)


@router.get("/health")
def health() -> dict[str, Any]:
    try:
        settings = PiRuntimeSettings.resolve()
        return {"ok": True, **settings.as_public_dict()}
    except Exception as exc:
        return {
            "ok": False,
            "enabled": True,
            "available": False,
            "error": str(exc),
            "runtime": "oh-my-pi-rpc",
            "host": "standalone" if _STANDALONE_MODE else "hermes-plugin",
        }


@router.get("/config")
def config() -> dict[str, Any]:
    result = {**health(), "nodes": _nodes_snapshot(), "node_id": _local_node_id()}
    tunnel_node = next(
        (item for item in result["nodes"] if item.get("tunnel") and item.get("status") == "online"),
        None,
    )
    if tunnel_node is not None:
        # The coordinator can expose Coding mode even when it does not have a
        # local Bun/native checkout. Subsequent app requests use
        # /nodes/<id>/proxy/* and travel through the active tunnel.
        result["remote_node_id"] = str(tunnel_node.get("node_id"))
        result["available"] = True
        result["runtime"] = "oh-my-pi-rpc"
        result["remote"] = True
    return result


def _tunnel_authorized(websocket: WebSocket) -> bool:
    expected = (
        os.environ.get("CODING_PI_COORDINATOR_TOKEN", "").strip()
        or os.environ.get("CODING_PI_SERVER_TOKEN", "").strip()
    )
    if not expected:
        client = str(websocket.client.host if websocket.client else "") or ""
        loopback = False
        try:
            import ipaddress

            loopback = ipaddress.ip_address(client.split("%")[0]).is_loopback
        except ValueError:
            loopback = False
        if loopback:
            return True
        # Explicit, loudly-logged legacy opt-out for deployments that have
        # not distributed a coordinator token to their node agents yet.
        # Without it the endpoint is FAIL-CLOSED for non-loopback peers:
        # with the token unset it used to accept anyone who could reach the
        # port — anyone could register a node, capture a node_id, and have
        # the coordinator proxy requests (or hand off session context) to
        # their server. Set CODING_PI_COORDINATOR_TOKEN on the coordinator
        # AND every node agent to migrate off this switch.
        if os.environ.get("CODING_PI_TUNNEL_UNAUTHENTICATED", "").strip() == "1":
            logger.warning(
                "coding-pi node tunnel accepted UNAUTHENTICATED non-loopback peer %s "
                "(CODING_PI_TUNNEL_UNAUTHENTICATED=1); set CODING_PI_COORDINATOR_TOKEN "
                "on the coordinator and all node agents",
                client,
            )
            return True
        return False
    return hmac.compare_digest(websocket.headers.get("authorization", ""), f"Bearer {expected}")


def _tunnel_response_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    blocked = {"connection", "content-length", "transfer-encoding", "upgrade", "server"}
    return {
        str(key).lower(): str(item)
        for key, item in value.items()
        if str(key).lower() not in blocked and item is not None
    }


def _tunnel_body(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        return b""
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=502, detail="Pi node tunnel returned invalid response bytes") from exc


async def _stream_tunnel_body(call: _TunnelCall) -> AsyncIterator[bytes]:
    if call.queue is None:
        return
    while True:
        frame = await call.queue.get()
        if frame is None:
            return
        message_type = str(frame.get("type") or "")
        if message_type == "stream_chunk":
            yield _tunnel_body(frame.get("body_b64"))
        elif message_type == "error":
            raise RuntimeError(str(frame.get("detail") or "Pi node tunnel failed"))
        elif message_type == "stream_end":
            return


@router.api_route(
    "/nodes/{node_id}/proxy/{proxy_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_node_request(node_id: str, proxy_path: str, request: Request) -> Response:
    """Proxy the independent Pi API through an outbound local-PC tunnel."""

    tunnel = _NODE_TUNNELS.get(node_id)
    if tunnel is None:
        raise HTTPException(status_code=503, detail=f"Pi node tunnel is not connected: {node_id}")
    path = "/" + proxy_path.lstrip("/")
    if not path.startswith("/api/coding-pi/") and path != "/api/coding-pi":
        raise HTTPException(status_code=403, detail="Only the Coding Pi API can be tunneled")
    query = str(request.url.query)
    target = path + (f"?{query}" if query else "")
    body = await request.body()
    headers: dict[str, str] = {"x-coding-pi-owner": _owner(request)}
    for name in ("accept", "content-type"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    streaming = "text/event-stream" in request.headers.get("accept", "").lower() or path.endswith("/events")
    result = await tunnel.request(
        method=request.method,
        path=target,
        headers=headers,
        body=body,
        stream=streaming,
    )
    if streaming:
        assert isinstance(result, _TunnelCall)
        if result.queue is None:
            raise HTTPException(status_code=502, detail="Pi node tunnel did not open an event stream")
        first = await result.queue.get()
        if first is None or first.get("type") == "error":
            detail = str((first or {}).get("detail") or "Pi node tunnel failed")
            raise HTTPException(status_code=502, detail=detail)
        status = int(first.get("status") or 200)
        response_headers = _tunnel_response_headers(first.get("headers"))
        if status >= 400:
            chunks: list[bytes] = []
            if first.get("body_b64"):
                chunks.append(_tunnel_body(first.get("body_b64")))
            async for chunk in _stream_tunnel_body(result):
                chunks.append(chunk)
            return Response(content=b"".join(chunks), status_code=status, headers=response_headers)
        return StreamingResponse(
            _stream_tunnel_body(result),
            status_code=status,
            headers=response_headers,
            media_type=None,
        )
    assert isinstance(result, dict)
    status = int(result.get("status") or 502)
    response_headers = _tunnel_response_headers(result.get("headers"))
    response_body = _tunnel_body(result.get("body_b64"))
    if 200 <= status < 300:
        _remember_collab_node(node_id, response_body)
    return Response(
        content=response_body,
        status_code=status,
        headers=response_headers,
    )


@router.websocket("/nodes/{node_id}/tunnel")
async def node_tunnel_socket(websocket: WebSocket, node_id: str) -> None:
    """Accept a persistent outbound connection from a local Pi Node Agent."""

    if not _tunnel_authorized(websocket):
        await websocket.close(code=4401, reason="Pi coordinator authentication required")
        return
    await websocket.accept()
    try:
        first = await asyncio.wait_for(websocket.receive_json(), timeout=15)
    except Exception:
        await websocket.close(code=4400, reason="Pi tunnel hello required")
        return
    if not isinstance(first, dict) or first.get("type") != "hello" or str(first.get("node_id") or "") != node_id:
        await websocket.close(code=4400, reason="Invalid Pi tunnel hello")
        return
    old = _NODE_TUNNELS.get(node_id)
    if old is not None:
        await old.fail_all("Pi node tunnel replaced")
        with suppress(Exception):
            await old.websocket.close(code=4000, reason="replaced by a newer connection")
    tunnel = _NodeTunnel(node_id, websocket)
    _NODE_TUNNELS[node_id] = tunnel
    raw_record = first.get("record") if isinstance(first.get("record"), dict) else {}
    _NODE_REGISTRY[node_id] = {
        "node_id": node_id,
        "label": str(raw_record.get("label") or node_id)[:160],
        "kind": str(raw_record.get("kind") or "local")[:64],
        "endpoint": None,
        "workspaces": [str(item)[:4096] for item in raw_record.get("workspaces", []) if str(item).strip()] if isinstance(raw_record.get("workspaces"), list) else [],
        "capabilities": sorted({str(item)[:128] for item in raw_record.get("capabilities", []) if str(item).strip()} | {"reverse-tunnel"}),
        "status": "online",
        "last_seen": _timestamp(),
        "local": False,
        "tunnel": True,
    }
    try:
        await tunnel.send({"type": "hello_ack", "node_id": node_id})
        while True:
            payload = await websocket.receive_json()
            if isinstance(payload, dict):
                await tunnel.handle(payload)
            record = _NODE_REGISTRY.get(node_id)
            if record is not None:
                record["last_seen"] = _timestamp()
                record["status"] = "online"
    except Exception as exc:
        await tunnel.fail_all(str(exc) or "Pi node tunnel disconnected")
    finally:
        if _NODE_TUNNELS.get(node_id) is tunnel:
            _NODE_TUNNELS.pop(node_id, None)
        record = _NODE_REGISTRY.get(node_id)
        if record is not None:
            record["status"] = "stale"


@router.get("/discovery")
def discovery() -> dict[str, Any]:
    """Small unauthenticated bootstrap document for LAN/node discovery."""

    return {
        "ok": True,
        "service": "pi-coding",
        "runtime": "oh-my-pi-rpc",
        "node_id": _local_node_id(),
        "origin": public_origin(),
        "node_agent_origin": _optional_string(os.environ.get("CODING_PI_NODE_AGENT_ORIGIN")),
        "auto_start": True,
    }


@router.get("/nodes")
def list_nodes() -> dict[str, Any]:
    return {"nodes": _nodes_snapshot(), "local_node_id": _local_node_id()}


@router.post("/nodes/register")
async def register_node(body: NodeRegistrationBody) -> dict[str, Any]:
    node_id = body.node_id.strip()
    if node_id == _local_node_id():
        return {"node": _local_node_record(), "accepted": True, "local": True}
    record = {
        "node_id": node_id,
        "label": body.label.strip() or node_id,
        "kind": body.kind.strip() or "remote",
        "endpoint": body.endpoint.strip().rstrip("/") if body.endpoint else None,
        "workspaces": [str(item)[:4096] for item in body.workspaces if str(item).strip()],
        "capabilities": [str(item)[:128] for item in body.capabilities if str(item).strip()],
        "status": "online",
        "last_seen": _timestamp(),
        "local": False,
    }
    _NODE_REGISTRY[node_id] = record
    return {"node": record, "accepted": True, "local": False}


@router.post("/nodes/{node_id}/heartbeat")
async def heartbeat_node(node_id: str, body: NodeRegistrationBody | None = None) -> dict[str, Any]:
    if node_id == _local_node_id():
        return {"node": _local_node_record(), "accepted": True}
    record = _NODE_REGISTRY.get(node_id)
    if record is None:
        if body is None:
            raise HTTPException(status_code=404, detail="Pi node is not registered")
        result = await register_node(body)
        return result
    record["last_seen"] = _timestamp()
    record["status"] = "online"
    return {"node": record, "accepted": True}


async def _prompt_in_background(session: PiRpcSession, message: str) -> None:
    try:
        await session.send({"type": "prompt", "message": message})
    except Exception as exc:
        session._last_error = str(exc)
        with suppress(Exception):
            await session._collab.broadcast({
                "t": "event",
                "event": {"type": "notice", "level": "error", "message": str(exc), "source": "hermes-dispatch"},
            })


async def _handoff_context(session: PiRpcSession) -> str:
    parts: list[str] = []
    try:
        response = await session.send({"type": "get_last_assistant_text"})
        value = _response_data(response)
        if isinstance(value, str) and value.strip():
            parts.append("Last Pi response:\n" + value[-20_000:])
        elif isinstance(value, dict):
            text = _as_text(value.get("text") or value.get("message"))
            if text:
                parts.append("Last Pi response:\n" + text[-20_000:])
    except Exception:
        pass
    try:
        snapshot = await session.snapshot()
        messages = _response_data(snapshot.get("messages"))
        if isinstance(messages, list):
            recent = messages[-8:]
            parts.append("Recent Pi transcript:\n" + json.dumps(recent, ensure_ascii=False, default=str)[-60_000:])
    except Exception:
        pass
    return "\n\n".join(parts)[-80_000:]


@router.post("/dispatch")
async def dispatch_coding_task(body: DispatchBody, request: Request, profile: str = Query(default="default")) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    target_id = body.node_id or _local_node_id()
    node = _find_node(target_id)
    if node is None:
        raise HTTPException(status_code=409, detail=f"Pi node is not connected: {target_id}")
    if not node.get("local"):
        endpoint = _optional_string(node.get("endpoint"))
        if not endpoint:
            raise HTTPException(status_code=409, detail=f"Pi node has no reachable endpoint: {target_id}")
        headers = {
            "X-Coding-Pi-Owner": _owner(request),
            **({"Authorization": request.headers["authorization"]} if request.headers.get("authorization") else {}),
        }
        payload = body.model_dump(exclude_none=True)
        payload["node_id"] = target_id
        try:
            remote = await asyncio.to_thread(_forward_json_sync, endpoint, "/api/coding-pi/dispatch", payload, headers)
        except (OSError, urllib.error.URLError, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=f"Pi node dispatch failed: {exc}") from exc
        return {"accepted": True, "delegated": True, "node": node, "remote": remote}

    try:
        if body.session_id:
            session = await _MANAGER.get(owner_id=_owner(request), profile=normalized_profile, session_id=body.session_id)
        else:
            session = await _MANAGER.create(
                owner_id=_owner(request),
                profile=normalized_profile,
                body=CreateSessionBody(name="Hermes Coding task", workspace=body.workspace),
            )
        message = body.task.strip()
        if body.instructions and body.instructions.strip():
            message += "\n\nHermes dispatch instructions:\n" + body.instructions.strip()
        asyncio.create_task(_prompt_in_background(session, message))
        return {
            "accepted": True,
            "delegated": False,
            "node": node,
            "session": session.public_metadata(),
            "session_id": session.session_id,
            "collab": session._collab.public_metadata(),
        }
    except Exception as exc:
        _raise_runtime(exc)
        raise AssertionError("unreachable")


@router.post("/sessions/import")
async def import_handoff(body: ImportHandoffBody, request: Request, profile: str = Query(default="default")) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    try:
        session = await _MANAGER.create(
            owner_id=_owner(request),
            profile=normalized_profile,
            body=CreateSessionBody(name=body.title, workspace=body.workspace),
        )
        prompt = (
            "You are continuing a Pi coding session handed off from another node.\n\n"
            "<handoff-context>\n"
            + body.context
            + "\n</handoff-context>\n\n"
            + body.instructions
        )
        asyncio.create_task(_prompt_in_background(session, prompt))
        session.metadata["handoff_from"] = {
            "node_id": body.source_node_id,
            "session_id": body.source_session_id,
            "created_at": _timestamp(),
        }
        _write_json(_metadata_path(session.directory), session.metadata)
        return {"accepted": True, "session": session.public_metadata(), "session_id": session.session_id}
    except Exception as exc:
        _raise_runtime(exc)
        raise AssertionError("unreachable")


@router.get("/sessions")
async def list_sessions(request: Request, profile: str = Query(default="default")) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    return {"sessions": await _MANAGER.list(owner_id=_owner(request), profile=normalized_profile)}


@router.post("/sessions")
async def create_session(body: CreateSessionBody, request: Request, profile: str = Query(default="")) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile or body.profile_id)
    try:
        session = await _MANAGER.create(owner_id=_owner(request), profile=normalized_profile, body=body)
        return {"session": session.public_metadata(), "snapshot": await session.snapshot()}
    except Exception as exc:
        _raise_runtime(exc)
        raise AssertionError("unreachable")


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request, profile: str = Query(default="default")) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    try:
        session = await _MANAGER.get(owner_id=_owner(request), profile=normalized_profile, session_id=session_id)
        return await session.snapshot()
    except Exception as exc:
        _raise_runtime(exc)
        raise AssertionError("unreachable")


@router.get("/sessions/{session_id}/collab")
async def get_collab_links(session_id: str, request: Request, profile: str = Query(default="default")) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    try:
        session = await _MANAGER.get(owner_id=_owner(request), profile=normalized_profile, session_id=session_id)
        return {"collab": session._collab.public_metadata(), "session": session.public_metadata()}
    except Exception as exc:
        _raise_runtime(exc)
        raise AssertionError("unreachable")


@router.post("/sessions/{session_id}/prompt")
async def prompt_session(session_id: str, body: PromptBody, request: Request, profile: str = Query(default="default")) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    try:
        session = await _MANAGER.get(owner_id=_owner(request), profile=normalized_profile, session_id=session_id)
        command: dict[str, Any] = {"type": "prompt", "message": body.message}
        if body.images:
            command["images"] = body.images
        if body.streaming_behavior in {"steer", "followUp"}:
            command["streamingBehavior"] = body.streaming_behavior
        response = await session.send(command)
        session.metadata["preview"] = body.message.strip()[:280]
        session.metadata["updated_at"] = _timestamp()
        _write_json(_metadata_path(session.directory), session.metadata)
        return {"accepted": response.get("success") is True, "response": response, "session": session.public_metadata()}
    except Exception as exc:
        _raise_runtime(exc)
        raise AssertionError("unreachable")


@router.post("/sessions/{session_id}/command")
async def command_session(session_id: str, body: CommandBody, request: Request, profile: str = Query(default="default")) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    _command_name(body.command)
    try:
        session = await _MANAGER.get(owner_id=_owner(request), profile=normalized_profile, session_id=session_id)
        response = await session.send(body.command)
        session.metadata["updated_at"] = _timestamp()
        _write_json(_metadata_path(session.directory), session.metadata)
        return {"response": response, "session": session.public_metadata()}
    except Exception as exc:
        _raise_runtime(exc)
        raise AssertionError("unreachable")


@router.post("/sessions/{session_id}/agent-command")
async def agent_command_session(
    session_id: str,
    body: AgentCommandBody,
    request: Request,
    profile: str = Query(default="default"),
) -> dict[str, Any]:
    """Expose the same Agent Hub controls as official collab-web to Hermes native UI."""

    if body.command not in {"chat", "kill", "revive"}:
        raise HTTPException(status_code=422, detail=f"Unsupported Agent Hub command: {body.command}")
    if body.command == "chat" and not (body.text or "").strip():
        raise HTTPException(status_code=422, detail="Agent Hub chat text cannot be empty")
    normalized_profile = _normalize_profile(profile)
    try:
        session = await _MANAGER.get(owner_id=_owner(request), profile=normalized_profile, session_id=session_id)
        await session.control_agent(body.command, body.agent_id, body.text)
        return {
            "accepted": True,
            "session": session.public_metadata(),
            "snapshot": await session.snapshot(),
        }
    except Exception as exc:
        _raise_runtime(exc)
        raise AssertionError("unreachable")


@router.get("/sessions/{session_id}/events")
async def session_events(
    session_id: str,
    request: Request,
    profile: str = Query(default="default"),
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    normalized_profile = _normalize_profile(profile)
    try:
        session = await _MANAGER.get(owner_id=_owner(request), profile=normalized_profile, session_id=session_id)
    except Exception as exc:
        _raise_runtime(exc)
        raise AssertionError("unreachable")

    async def stream() -> AsyncIterator[str]:
        try:
            snapshot = await session.snapshot()
            yield _sse_frame("snapshot", snapshot, None)
            cursor = after
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(session.next_event(cursor), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": hermes-pi-heartbeat\n\n"
                    continue
                if event is None:
                    break
                cursor, payload = event
                yield _sse_frame("pi", payload, cursor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield _sse_frame("error", {"message": str(exc)}, None)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str, body: StopBody, request: Request, profile: str = Query(default="default")) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    try:
        return {"session": await _MANAGER.stop(
            owner_id=_owner(request),
            profile=normalized_profile,
            session_id=session_id,
            force=body.force,
        )}
    except Exception as exc:
        _raise_runtime(exc)
        raise AssertionError("unreachable")


@router.post("/sessions/{session_id}/handoff")
async def handoff_session(session_id: str, body: HandoffBody, request: Request, profile: str = Query(default="default")) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    target = _find_node(body.target_node_id)
    if target is None:
        raise HTTPException(status_code=409, detail=f"Pi node is not connected: {body.target_node_id}")
    try:
        session = await _MANAGER.get(owner_id=_owner(request), profile=normalized_profile, session_id=session_id)
        context = await _handoff_context(session)
        if target.get("local"):
            session.metadata["node_id"] = _local_node_id()
            session.metadata["node_kind"] = "local"
            session.metadata["handoff_at"] = _timestamp()
            _write_json(_metadata_path(session.directory), session.metadata)
            return {
                "accepted": True,
                "moved": False,
                "reason": "session already runs on the requested local node",
                "source": session.public_metadata(),
                "target": session.public_metadata(),
            }
        endpoint = _optional_string(target.get("endpoint"))
        if not endpoint:
            raise HTTPException(status_code=409, detail=f"Pi node has no reachable endpoint: {body.target_node_id}")
        headers = {
            "X-Coding-Pi-Owner": _owner(request),
            **({"Authorization": request.headers["authorization"]} if request.headers.get("authorization") else {}),
        }
        payload = {
            "source_node_id": _local_node_id(),
            "source_session_id": session_id,
            "title": session.metadata.get("title") or "Coding handoff",
            "workspace": body.workspace,
            "context": context,
            "instructions": body.instructions,
        }
        result = await asyncio.to_thread(_forward_json_sync, endpoint, f"/api/coding-pi/sessions/import?profile={normalized_profile}", payload, headers)
        session.metadata["handoff_to"] = {
            "node_id": body.target_node_id,
            "session_id": result.get("session_id"),
            "created_at": _timestamp(),
        }
        _write_json(_metadata_path(session.directory), session.metadata)
        return {"accepted": True, "moved": True, "source": session.public_metadata(), "target": result}
    except HTTPException:
        raise
    except (OSError, urllib.error.URLError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"Pi node handoff failed: {exc}") from exc
    except Exception as exc:
        _raise_runtime(exc)
        raise AssertionError("unreachable")


def _sse_frame(event_type: str, payload: dict[str, Any], sequence: int | None) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    lines = []
    if sequence is not None:
        lines.append(f"id: {sequence}")
    lines.append(f"event: {event_type}")
    lines.extend(f"data: {line}" for line in serialized.splitlines() or [""])
    return "\n".join(lines) + "\n\n"
