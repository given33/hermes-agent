"""Persistent Hermes 0.20 TUI gateway sessions for hosted mobile chat.

The collaboration API used to construct a fresh ``AIAgent`` subprocess for
every message.  This client speaks the public JSON-RPC protocol exposed by
``tui_gateway.entry`` so hosted chat gets the same deferred pre-warm, stable
tool contract, prompt cache, session resume, and cooperative interrupt path as
the official TUI and desktop clients.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import atexit
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Optional


class HostedTuiGatewayError(RuntimeError):
    """The persistent official gateway could not complete a hosted turn."""


class HostedTuiGatewayCancelled(HostedTuiGatewayError):
    """The hosted turn was cooperatively interrupted."""


# ``message.complete`` is the user-visible completion boundary.  The gateway
# emits a follow-up ``session.info`` after it clears ``session["running"]``;
# that diagnostic snapshot can be delayed by inventory/probe work and must not
# hold a completed reply hostage.  Keep only a tiny grace period: the child
# gateway continues consuming the eventual session.info after run_turn
# returns, so this boundary does not discard durable state; it only prevents
# diagnostics from extending mobile terminal latency.
_GATEWAY_IDLE_GRACE_SECONDS = 0.05


def _allow_tools_from_context(artifact_context: dict[str, str]) -> bool:
    raw = str(artifact_context.get("allow_tools", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


@dataclass
class _TurnSink:
    callback: Optional[Callable[[dict[str, Any]], None]]
    done: threading.Event = field(default_factory=threading.Event)
    accepted: threading.Event = field(default_factory=threading.Event)
    events: queue.Queue[dict[str, Any] | None] = field(default_factory=queue.Queue)
    result: str = ""
    error: BaseException | None = None


@dataclass
class _HostedSessionState:
    conversation_id: str
    live_session_id: str
    stored_session_id: str
    artifact_context: dict[str, str]
    agent_ready: threading.Event = field(default_factory=threading.Event)
    latest_session_info: dict[str, Any] | None = None
    turn_lock: threading.Lock = field(default_factory=threading.Lock)
    message_complete_seen: threading.Event = field(default_factory=threading.Event)
    idle_after_turn: threading.Event = field(default_factory=threading.Event)
    current_sink: _TurnSink | None = None


class _GatewayProcess:
    def __init__(self, *, env: dict[str, str], cwd: str) -> None:
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._stderr_tail: list[str] = []
        self._sessions_by_conversation: dict[str, _HostedSessionState] = {}
        self._sessions_by_live: dict[str, _HostedSessionState] = {}
        # The gateway can emit session.ready/session.info between writing the
        # session.create response and the caller registering its local state.
        # Keep those boundary events until ensure_session installs the mapping
        # instead of silently losing the readiness signal.
        self._early_session_ready: set[str] = set()
        self._early_session_info: dict[str, dict[str, Any]] = {}
        # Kept as the most recently selected session for diagnostics and
        # backwards-compatible callers. Runtime routing uses the maps above.
        self.live_session_id = ""
        self.stored_session_id = ""
        self.last_used = time.monotonic()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tui_gateway.entry"],
            cwd=cwd,
            env=env,
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        if self.process.stdin is None or self.process.stdout is None:
            self.process.kill()
            raise HostedTuiGatewayError("Hermes 0.20 gateway pipes are unavailable")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        if not self._ready.wait(timeout=20.0):
            detail = self._stderr_summary()
            self.close()
            raise HostedTuiGatewayError(
                "Hermes 0.20 gateway did not become ready"
                + (f": {detail}" if detail else "")
            )

    def alive(self) -> bool:
        return not self._closed.is_set() and self.process.poll() is None

    def _stderr_summary(self) -> str:
        return "".join(self._stderr_tail)[-4000:].strip()

    def _read_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        try:
            for chunk in iter(lambda: stream.read(4096), ""):
                if not chunk:
                    break
                self._stderr_tail.append(chunk)
                if sum(map(len, self._stderr_tail)) > 16000:
                    self._stderr_tail = ["".join(self._stderr_tail)[-8000:]]
        except Exception:
            return

    def _read_stdout(self) -> None:
        try:
            for raw in self.process.stdout or ():
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = str(message.get("id") or "")
                if request_id:
                    with self._pending_lock:
                        waiter = self._pending.pop(request_id, None)
                    if waiter is not None:
                        waiter.put(message)
                    continue
                if message.get("method") != "event":
                    continue
                params = message.get("params")
                if not isinstance(params, dict):
                    continue
                event_type = str(params.get("type") or "")
                if event_type == "gateway.ready":
                    self._ready.set()
                    continue
                event_session_id = str(params.get("session_id") or "")
                payload = params.get("payload")
                payload = dict(payload) if isinstance(payload, dict) else {}
                state: _HostedSessionState | None = None
                if event_type == "session.ready":
                    # The gateway emits this lightweight boundary before it
                    # walks the complete tool/skill/MCP inventory.  Treat it
                    # as sufficient for prompt dispatch; session.info remains
                    # a later, richer UI snapshot.
                    with self._session_lock:
                        state = self._sessions_by_live.get(event_session_id)
                        if state is not None:
                            state.agent_ready.set()
                        elif event_session_id:
                            early_ready = getattr(
                                self, "_early_session_ready", None
                            )
                            if early_ready is None:
                                early_ready = set()
                                self._early_session_ready = early_ready
                            early_ready.add(event_session_id)
                    continue
                if event_type == "session.info":
                    with self._session_lock:
                        state = self._sessions_by_live.get(event_session_id)
                        if state is not None:
                            stored = str(
                                payload.get("stored_session_id")
                                or state.stored_session_id
                            )
                            if stored:
                                state.stored_session_id = stored
                            payload.setdefault("session_id", stored)
                            state.latest_session_info = {
                                "type": event_type,
                                "payload": payload,
                            }
                            state.agent_ready.set()
                            if state.message_complete_seen.is_set() and not payload.get("running"):
                                state.idle_after_turn.set()
                        elif event_session_id:
                            early_info = getattr(self, "_early_session_info", None)
                            if early_info is None:
                                early_info = {}
                                self._early_session_info = early_info
                            early_info[event_session_id] = {
                                "type": event_type,
                                "payload": {
                                    **payload,
                                    "session_id": payload.get("session_id")
                                    or event_session_id,
                                },
                            }
                elif event_session_id:
                    with self._session_lock:
                        state = self._sessions_by_live.get(event_session_id)
                sink = state.current_sink if state is not None else None
                if sink is None:
                    continue
                if event_type == "message.complete":
                    status = str(payload.get("status") or "complete")
                    if status == "complete":
                        payload["status"] = "completed"
                    state.message_complete_seen.set()
                sink.events.put({"type": event_type, "payload": payload})
        finally:
            self._closed.set()
            self._ready.set()
            with self._pending_lock:
                pending = list(self._pending.values())
                self._pending.clear()
            error = {
                "error": {
                    "code": 5032,
                    "message": self._stderr_summary() or "Hermes 0.20 gateway exited",
                }
            }
            for waiter in pending:
                waiter.put(error)
            # Deliver RPC failures before taking the session lock: an
            # ensure_session caller holds that lock while waiting for its RPC,
            # and reversing this order would deadlock on child-process exit.
            with self._session_lock:
                states = list(self._sessions_by_conversation.values())
                for state in states:
                    state.agent_ready.set()
            for state in states:
                sink = state.current_sink
                if sink is not None and not sink.done.is_set():
                    sink.error = HostedTuiGatewayError(error["error"]["message"])
                    sink.done.set()
                    sink.events.put(None)

    def _dispatch_events(self, sink: _TurnSink) -> None:
        while True:
            event = sink.events.get()
            if event is None:
                return
            event_type = str(event.get("type") or "")
            if event_type not in {"session.info", "error"}:
                sink.accepted.wait(timeout=30.0)
            try:
                if sink.callback is not None:
                    sink.callback(event)
            except BaseException as exc:
                sink.error = exc
                sink.done.set()
                continue
            payload = event.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            if event_type == "message.complete":
                sink.result = str(payload.get("text") or "").strip()
                if str(payload.get("status") or "") in {"error", "interrupted"}:
                    sink.error = HostedTuiGatewayError(
                        sink.result or "Hermes model turn did not complete"
                    )
                sink.done.set()
                return
            elif event_type == "error":
                sink.error = HostedTuiGatewayError(
                    str(payload.get("message") or "Hermes model turn failed")
                )
                sink.done.set()
                return

    def rpc(self, method: str, params: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
        if not self.alive():
            raise HostedTuiGatewayError(
                self._stderr_summary() or "Hermes 0.20 gateway is not running"
            )
        request_id = uuid.uuid4().hex
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = waiter
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            with self._write_lock:
                assert self.process.stdin is not None
                self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            response = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise HostedTuiGatewayError(
                f"Hermes 0.20 gateway timed out handling {method}"
            ) from exc
        except BaseException:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        if isinstance(response.get("error"), dict):
            error = response["error"]
            exc = HostedTuiGatewayError(str(error.get("message") or "Gateway request failed"))
            setattr(exc, "code", error.get("code"))
            raise exc
        result = response.get("result")
        return dict(result) if isinstance(result, dict) else {}

    def ensure_session(
        self,
        conversation_id: str,
        requested_session_id: str = "",
        *,
        artifact_context: dict[str, str],
    ) -> _HostedSessionState:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            raise HostedTuiGatewayError("Hosted conversation id is required")
        requested_session_id = str(requested_session_id or "").strip()
        with self._session_lock:
            existing = self._sessions_by_conversation.get(conversation_id)
            if existing is not None:
                self.live_session_id = existing.live_session_id
                self.stored_session_id = existing.stored_session_id
                return existing
            result: dict[str, Any] | None = None
            session_params = {
                "source": "dashboard-group",
                "close_on_disconnect": False,
                "tool_artifact_context": dict(artifact_context),
            }
            if not _allow_tools_from_context(artifact_context):
                # This is a plain mobile chat turn.  Let the official gateway
                # build the persistent session without joining MCP discovery;
                # the server's late-refresh path can still add capabilities
                # before a later explicit tool turn.
                session_params.update({
                    "allow_tools": False,
                })
            if requested_session_id:
                try:
                    result = self.rpc(
                        "session.resume",
                        {
                            **session_params,
                            "session_id": requested_session_id,
                        },
                        timeout=30.0,
                    )
                except HostedTuiGatewayError as exc:
                    if getattr(exc, "code", None) != 4007:
                        raise
            if result is None:
                result = self.rpc(
                    "session.create",
                    session_params,
                    timeout=30.0,
                )
            live_session_id = str(result.get("session_id") or "").strip()
            stored_session_id = str(
                result.get("stored_session_id") or requested_session_id
            ).strip()
            if not live_session_id:
                raise HostedTuiGatewayError("Hermes 0.20 did not return a live session id")
            state = _HostedSessionState(
                conversation_id=conversation_id,
                live_session_id=live_session_id,
                stored_session_id=stored_session_id,
                artifact_context=dict(artifact_context),
            )
            early_ready = getattr(self, "_early_session_ready", set())
            early_info_map = getattr(self, "_early_session_info", {})
            if live_session_id in early_ready:
                early_ready.discard(live_session_id)
                state.agent_ready.set()
            early_info = early_info_map.pop(live_session_id, None)
            if isinstance(early_info, dict):
                early_payload = early_info.get("payload")
                if isinstance(early_payload, dict):
                    if early_payload.get("session_id"):
                        state.stored_session_id = str(
                            early_payload.get("session_id")
                        )
                    state.latest_session_info = {
                        "type": "session.info",
                        "payload": {
                            **early_payload,
                            "session_id": state.stored_session_id,
                        },
                    }
                    state.agent_ready.set()
            info = result.get("info")
            if isinstance(info, dict) and not info.get("lazy", True):
                state.agent_ready.set()
            self._sessions_by_conversation[conversation_id] = state
            self._sessions_by_live[live_session_id] = state
            self.live_session_id = live_session_id
            self.stored_session_id = stored_session_id
            return state

    def wait_until_warm(self, conversation_id: str, timeout: float = 60.0) -> bool:
        with self._session_lock:
            state = self._sessions_by_conversation.get(conversation_id)
        return bool(
            state is not None
            and state.agent_ready.wait(timeout=timeout)
            and self.alive()
        )

    def run_turn(
        self,
        prompt: str,
        *,
        requested_session_id: str,
        turn_id: str,
        event_callback: Optional[Callable[[dict[str, Any]], None]],
        cancel_check: Optional[Callable[[], bool]],
        timeout: float,
        conversation_id: str,
        artifact_context: dict[str, str],
    ) -> str:
        state = self.ensure_session(
            conversation_id,
            requested_session_id,
            artifact_context=artifact_context,
        )
        with state.turn_lock:
            live_session_id = state.live_session_id
            stored_session_id = state.stored_session_id
            sink = _TurnSink(event_callback)
            state.current_sink = sink
            state.message_complete_seen.clear()
            state.idle_after_turn.clear()
            dispatcher = threading.Thread(
                target=self._dispatch_events,
                args=(sink,),
                name=f"hermes-hosted-events-{conversation_id[-12:]}",
                daemon=True,
            )
            dispatcher.start()
            self.last_used = time.monotonic()
            try:
                if state.latest_session_info is not None and event_callback is not None:
                    event_callback(dict(state.latest_session_info))
                response = self.rpc(
                    "prompt.submit",
                    {
                        "session_id": live_session_id,
                        "text": prompt,
                        "tool_artifact_turn_id": str(turn_id or ""),
                        "allow_tools": _allow_tools_from_context(artifact_context),
                    },
                    timeout=30.0 if timeout <= 0 else min(30.0, timeout),
                )
                if str(response.get("status") or "") != "streaming":
                    raise HostedTuiGatewayError("Hermes 0.20 rejected the prompt")
                deadline = None if timeout <= 0 else time.monotonic() + timeout
                while not state.agent_ready.is_set():
                    if cancel_check is not None and cancel_check():
                        self._interrupt(live_session_id)
                        raise HostedTuiGatewayCancelled("Hosted turn cancelled")
                    if sink.done.wait(timeout=0.05):
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        self._interrupt(live_session_id)
                        raise TimeoutError("Hermes 0.20 agent pre-warm timed out")
                if not sink.done.is_set():
                    if event_callback is not None:
                        try:
                            event_callback(
                                {
                                    "type": "request.accepted",
                                    "payload": {
                                        "session_id": stored_session_id,
                                    },
                                }
                            )
                        except BaseException:
                            self._interrupt(live_session_id)
                            sink.accepted.set()
                            raise
                    sink.accepted.set()
                while not sink.done.wait(timeout=0.1):
                    if sink.error is not None:
                        self._interrupt(live_session_id)
                        break
                    if cancel_check is not None and cancel_check():
                        self._interrupt(live_session_id)
                        sink.error = HostedTuiGatewayCancelled("Hosted turn cancelled")
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        self._interrupt(live_session_id)
                        sink.error = TimeoutError("Hermes profile execution timed out")
                        break
                sink.accepted.set()
                self.last_used = time.monotonic()
                if sink.error is not None:
                    raise sink.error
                if not sink.result:
                    raise HostedTuiGatewayError("Hermes profile returned an empty response")
                # message.complete is the authoritative user-visible
                # completion boundary. The following idle session.info is
                # useful bookkeeping, but it may be delayed by diagnostics
                # and inventory work; only give it a short grace window.
                remaining = 0.0 if deadline is None else max(0.0, deadline - time.monotonic())
                state.idle_after_turn.wait(
                    timeout=min(_GATEWAY_IDLE_GRACE_SECONDS, remaining)
                )
                return sink.result
            finally:
                sink.accepted.set()
                if state.current_sink is sink:
                    state.current_sink = None
                sink.events.put(None)
                dispatcher.join(timeout=1.0)

    def _interrupt(self, live_session_id: str) -> None:
        try:
            self.rpc(
                "session.interrupt",
                {"session_id": live_session_id},
                timeout=5.0,
            )
        except Exception:
            return

    def close_session(self, conversation_id: str) -> bool:
        """Release one live gateway session and its slash-worker process."""

        with self._session_lock:
            state = self._sessions_by_conversation.get(str(conversation_id))
        if state is None:
            return False
        sink = state.current_sink
        if sink is not None and not sink.done.is_set():
            self._interrupt(state.live_session_id)
        try:
            self.rpc(
                "session.close",
                {"session_id": state.live_session_id},
                timeout=10.0,
            )
        finally:
            with self._session_lock:
                if self._sessions_by_conversation.get(state.conversation_id) is state:
                    self._sessions_by_conversation.pop(state.conversation_id, None)
                if self._sessions_by_live.get(state.live_session_id) is state:
                    self._sessions_by_live.pop(state.live_session_id, None)
            if sink is not None and not sink.done.is_set():
                sink.error = HostedTuiGatewayCancelled("Hosted conversation closed")
                sink.done.set()
                sink.events.put(None)
        return True

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._session_lock:
            states = list(self._sessions_by_conversation.values())
        for state in states:
            sink = state.current_sink
            if sink is not None and not sink.done.is_set():
                sink.error = HostedTuiGatewayError("Hermes 0.20 gateway closed")
                sink.done.set()
                sink.events.put(None)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.process.kill()


_POOL_LOCK = threading.Lock()
_POOL: dict[tuple[str, str, str, str, str], _GatewayProcess] = {}
_MAX_IDLE_SECONDS = 30 * 60
_MAX_GATEWAYS = 32


def _pool_key(
    *,
    runtime_home: str,
    owner_id: str,
    account_generation: str,
    profile: str,
    artifact_root: str,
) -> tuple[str, str, str, str, str]:
    # One official 0.20 gateway can host many isolated sessions. Reusing it by
    # account/profile avoids repeating Python imports and MCP discovery for
    # every new mobile conversation; artifact ownership remains per session.
    return (runtime_home, owner_id, account_generation, profile, artifact_root)


def _prune_locked(now: float) -> None:
    stale = [
        key
        for key, gateway in _POOL.items()
        if not gateway.alive() or now - gateway.last_used > _MAX_IDLE_SECONDS
    ]
    if len(_POOL) - len(stale) > _MAX_GATEWAYS:
        survivors = sorted(
            ((gateway.last_used, key) for key, gateway in _POOL.items() if key not in stale)
        )
        stale.extend(key for _used, key in survivors[: len(_POOL) - len(stale) - _MAX_GATEWAYS])
    for key in dict.fromkeys(stale):
        gateway = _POOL.pop(key, None)
        if gateway is not None:
            gateway.close()


def _gateway_for(
    *,
    runtime_home: str,
    owner_id: str,
    account_generation: str,
    conversation_id: str,
    profile: str,
    artifact_root: str,
    import_root: str,
    extra_env: Optional[dict[str, str]] = None,
) -> _GatewayProcess:
    key = _pool_key(
        runtime_home=runtime_home,
        owner_id=owner_id,
        account_generation=account_generation,
        profile=profile,
        artifact_root=artifact_root,
    )
    with _POOL_LOCK:
        _prune_locked(time.monotonic())
        existing = _POOL.get(key)
        if existing is not None and existing.alive():
            return existing
        env = {
            **os.environ,
            **(extra_env or {}),
            "HERMES_HOME": runtime_home,
            "HERMES_SESSION_SOURCE": "dashboard-group",
            # Hosted conversations advertise every registered tool schema.
            # MCP service processes remain lazy; only an actual tool call
            # crosses the supervisor proxy and starts its isolated child.
            "HERMES_FULL_TOOL_DEFINITIONS": "1",
            "HERMES_TOOL_ARTIFACT_ROOT": artifact_root,
            "HERMES_TOOL_ARTIFACT_OWNER": owner_id,
            # Conversation ownership is supplied on session.create/resume so
            # this shared process never leaks artifacts across conversations.
            "HERMES_TOOL_ARTIFACT_CONVERSATION": "",
            "HERMES_ACCOUNT_GENERATION": account_generation,
            # Plain hosted mobile chat must reach gateway.ready without
            # starting every configured MCP server.  The session/build path
            # and the first explicitly tool-enabled turn start discovery on
            # demand through tui_gateway.server.
            "HERMES_HOSTED_GATEWAY_LAZY_MCP": "1",
        }
        inherited = str(env.get("PYTHONPATH") or "").strip()
        env["PYTHONPATH"] = os.pathsep.join(
            dict.fromkeys([import_root, *inherited.split(os.pathsep)])
        ).rstrip(os.pathsep)
        gateway = _GatewayProcess(env=env, cwd=import_root)
        _POOL[key] = gateway
        return gateway


def prewarm_hosted_gateway(
    *,
    runtime_home: str,
    owner_id: str,
    account_generation: str,
    conversation_id: str,
    profile: str,
    artifact_root: str,
    import_root: str,
    requested_session_id: str = "",
    allow_tools: bool = True,
    extra_env: Optional[dict[str, str]] = None,
) -> None:
    """Start the official gateway and build its Agent off the request path.

    ``session.create`` is intentionally lazy in Hermes 0.20: it returns the
    session id before the Agent (and its tool registry) is ready.  That is the
    right behavior for an idle session, but if we stop here the first user
    prompt pays the entire cold Agent build.  The dashboard calls this helper
    while the conversation is being opened/typed, so wait briefly for the
    already-scheduled build in this background thread.  The wait never blocks
    the API request and does not eagerly start MCP children; those remain
    lazy until a tool is actually invoked.
    """

    gateway = _gateway_for(
        runtime_home=runtime_home,
        owner_id=owner_id,
        account_generation=account_generation,
        conversation_id=conversation_id,
        profile=profile,
        artifact_root=artifact_root,
        import_root=import_root,
        extra_env=extra_env,
    )
    gateway.ensure_session(
        conversation_id,
        requested_session_id,
        artifact_context={
            "root": artifact_root,
            "owner_id": owner_id,
            "conversation_id": conversation_id,
            "account_generation": account_generation,
            "allow_tools": "1" if allow_tools else "0",
        },
    )
    # A bounded wait lets the prewarm overlap the user's typing without
    # turning a slow/unavailable model into a blocked request.  The prompt
    # path still waits for readiness when necessary, so this is only a latency
    # optimization for cold starts.
    gateway.wait_until_warm(conversation_id, timeout=8.0)


def run_hosted_gateway_turn(
    prompt: str,
    *,
    runtime_home: str,
    owner_id: str,
    account_generation: str,
    conversation_id: str,
    turn_id: str,
    profile: str,
    artifact_root: str,
    import_root: str,
    requested_session_id: str = "",
    allow_tools: bool = True,
    event_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    timeout: float = 600.0,
    extra_env: Optional[dict[str, str]] = None,
) -> str:
    gateway = _gateway_for(
        runtime_home=runtime_home,
        owner_id=owner_id,
        account_generation=account_generation,
        conversation_id=conversation_id,
        profile=profile,
        artifact_root=artifact_root,
        import_root=import_root,
        extra_env=extra_env,
    )
    return gateway.run_turn(
        prompt,
        requested_session_id=requested_session_id,
        turn_id=turn_id,
        event_callback=event_callback,
        cancel_check=cancel_check,
        timeout=timeout,
        conversation_id=conversation_id,
        artifact_context={
            "root": artifact_root,
            "owner_id": owner_id,
            "conversation_id": conversation_id,
            "account_generation": account_generation,
            "allow_tools": "1" if allow_tools else "0",
        },
    )


def release_hosted_gateway_conversation(
    conversation_id: str,
    *,
    owner_id: str,
    account_generation: str,
) -> int:
    """Close a deleted conversation in every matching account gateway."""

    normalized_conversation = str(conversation_id or "").strip()
    normalized_owner = str(owner_id or "").strip()
    normalized_generation = str(account_generation or "").strip()
    if not normalized_conversation or not normalized_owner or not normalized_generation:
        return 0
    with _POOL_LOCK:
        gateways = [
            gateway
            for key, gateway in _POOL.items()
            if key[1] == normalized_owner and key[2] == normalized_generation
        ]
    released = 0
    for gateway in gateways:
        try:
            released += int(gateway.close_session(normalized_conversation))
        except Exception:
            continue
    return released


def shutdown_hosted_gateways() -> None:
    with _POOL_LOCK:
        gateways = list(_POOL.values())
        _POOL.clear()
    for gateway in gateways:
        gateway.close()


atexit.register(shutdown_hosted_gateways)
