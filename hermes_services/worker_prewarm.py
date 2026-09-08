"""One-use, profile-scoped CLI processes prepared before a hosted assignment.

Only imports, tool registration and MCP connections are warmed. Every task
still runs the official CLI, receives its own PID/session/workspace, and exits.
No agent, prompt, conversation, or model request is created while idle.
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


def _signature(home: str) -> tuple:
    root = Path(home)
    paths = [root / name for name in ("config.yaml", ".env")]
    # Managed configuration can affect CLI_CONFIG at import time too.
    default_root = root.parents[1] if root.parent.name == "profiles" else root
    paths.extend(default_root / name for name in ("config.yaml", ".env"))
    result = []
    for path in paths:
        try:
            result.append((str(path), hashlib.sha256(path.read_bytes()).digest()))
        except OSError:
            result.append((str(path), None, None))
    return tuple(result)


class WorkerPrewarmPool:
    """Bounded spare processes; never block dispatch waiting for a spare."""

    def __init__(self, *, limit: int = 1, depth: int = 2):
        self.limit = max(0, limit)
        self.depth = max(1, min(4, depth))
        self._spares: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._closed = False

    def prepare(self, profile: str, *, board: str = "") -> None:
        if not self.limit or os.name == "nt":
            return
        from hermes_cli.profiles import resolve_profile_env
        from gateway.session_context import _VAR_MAP
        try:
            home = resolve_profile_env(profile)
        except (ValueError, FileNotFoundError):
            return
        signature = _signature(home)
        with self._lock:
            if self._closed:
                return
            previous = self._spares.get(home, [])
            usable = []
            for spare in previous:
                if spare["signature"] == signature and spare["proc"].poll() is None:
                    usable.append(spare)
                else:
                    self._discard(spare)
            if home in self._spares:
                self._spares[home] = usable
            if len(usable) >= self.depth:
                return
            if home not in self._spares and len(self._spares) >= self.limit:
                for spare in self._spares.pop(next(iter(self._spares))):
                    self._discard(spare)
            env = dict(os.environ)
            for key in _VAR_MAP:
                env.pop(key, None)
            for key in tuple(env):
                if key.startswith("HERMES_KANBAN_"):
                    env.pop(key, None)
            env.update(HERMES_HOME=home, HERMES_PROFILE=profile)
            code_root = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(
                [code_root, *filter(None, env.get("PYTHONPATH", "").split(os.pathsep))]))
            # Warm the very board this profile will use, so the official
            # first-open integrity/schema migration is paid once while idle.
            if board:
                from hermes_cli import kanban_db
                env["HERMES_KANBAN_DB"] = str(kanban_db.kanban_db_path(board=board))
            def ready(proc, spare) -> None:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if line.strip() == b"HERMES_WORKER_READY":
                        spare["ready"] = True
                        break
                if not spare["ready"]:
                    logging.getLogger(__name__).warning(
                        "Worker prewarm exited before readiness for %s (code %s)",
                        profile, proc.poll())

            self._spares[home] = usable
            for _ in range(self.depth - len(usable)):
                try:
                    proc = subprocess.Popen(
                        [sys.executable, "-m", __name__, profile], env=env,
                        cwd=home, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                    )
                except OSError:
                    return  # The ordinary Kanban launcher remains available.
                spare = {"proc": proc, "signature": signature, "ready": False,
                         "created_at": time.monotonic(), "profile": profile, "board": board}
                usable.append(spare)
                threading.Thread(target=ready, args=(proc, spare),
                                 name="worker-prewarm-ready", daemon=True).start()

    @staticmethod
    def _discard(spare: dict) -> None:
        proc = spare["proc"]
        if proc.stdin:
            proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
        # Reap outside dispatch without a blocking join. Spares have no work.
        def reap():
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
        threading.Thread(target=reap, name="worker-prewarm-reap", daemon=True).start()

    def launch(self, cmd: list[str], **kwargs):
        env = kwargs["env"]
        home = env.get("HERMES_HOME", "")
        signature = _signature(home)
        with self._lock:
            candidates = self._spares.get(home, [])
            spare = next((item for item in candidates if item["ready"]
                          and item["signature"] == signature and item["proc"].poll() is None), None)
            if spare:
                candidates.remove(spare)
            pending = bool(candidates)
        diagnostic = {"event": "worker.startup", "timestamp_ms": int(time.time() * 1000),
                      "prewarmed": False, "reason": "spare-preparing" if pending else "no-spare"}
        if spare:
            proc = spare["proc"]
            diagnostic.update(pid=proc.pid, ready=spare["ready"],
                              spare_age_ms=round((time.monotonic()-spare["created_at"])*1000),
                              signature_matches=spare["signature"] == signature)
            if (spare["ready"] and proc.poll() is None
                    and spare["signature"] == signature):
                diagnostic.update(prewarmed=True, reason="ready")
                kwargs["stdout"].write((json.dumps(diagnostic)+"\n").encode())
                kwargs["stdout"].flush()
                # The command is built by Kanban, not by a remote payload.
                # Preserve all official CLI flags after its -p profile pair.
                try:
                    args = cmd[cmd.index("-p"):]
                    packet = {"argv": args, "env": env, "cwd": kwargs.get("cwd"),
                              "log": str(kwargs["stdout"].name)}
                    proc.stdin.write((json.dumps(packet) + "\n").encode())
                    proc.stdin.flush()
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    # A partial handoff is ambiguous; do not launch a duplicate
                    # worker. Kanban's PID/crash reconciliation owns recovery.
                    return proc
                self._replenish(proc, spare["profile"], spare["board"])
                return proc
            self._discard(spare)
            diagnostic["reason"] = "unready-or-stale"
        kwargs["stdout"].write((json.dumps(diagnostic)+"\n").encode())
        kwargs["stdout"].flush()
        result = subprocess.Popen(cmd, **kwargs)
        profile = env.get("HERMES_PROFILE", "")
        if profile:
            self._replenish(result, profile, env.get("HERMES_KANBAN_BOARD", ""))
        return result

    def _replenish(self, proc, profile: str, board: str) -> None:
        def replace():
            # Start the successor immediately while the current task is
            # waiting on provider/tool work. This makes the next assignment
            # consume a genuinely ready spare instead of paying the full
            # import cost after the previous worker exits.
            self.prepare(profile, board=board)
            proc.wait()
            if proc.stdout:
                proc.stdout.close()
        threading.Thread(target=replace, name="worker-prewarm-replace", daemon=True).start()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            spares, self._spares = self._spares, {}
        for candidates in spares.values():
            for spare in candidates:
                self._discard(spare)


def _bind_task_environment(env: dict[str, str]) -> None:
    os.environ.update(env)
    # Requirement probes ran without an assignment. In particular Kanban's
    # task tools must not inherit the idle process's cached negative result.
    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()


def _main() -> None:
    profile = sys.argv[1]
    # CLI import-time configuration belongs to this one profile for the
    # process's entire lifetime. Task context is bound only after assignment.
    sys.argv = ["hermes", "-p", profile, "--cli", "--accept-hooks", "chat", "-Q"]
    from hermes_cli import main as entry
    import cli
    import run_agent
    import model_tools
    from tools.registry import discover_builtin_tools
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    discover_builtin_tools()
    # SDK model construction otherwise spends seconds importing pydantic's
    # generated response classes on the first real OpenAI request.
    from openai import OpenAI
    from openai.resources.chat import Completions
    import hermes_cli.models
    from agent.models_dev import fetch_models_dev
    fetch_models_dev(allow_network=False)
    from agent.ssl_verify import default_httpx_context
    default_httpx_context()
    from agent.ssl_guard import verify_ca_bundle
    verify_ca_bundle()
    import hermes_cli.model_data_policy_guard
    import hermes_cli.active_sessions
    import hermes_cli.observability.relay_shared_metrics
    try:
        import aiohttp
    except ImportError:
        pass
    from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build
    ensure_mcp_discovery_before_agent_build(logger=cli.logger, single_query=True)
    # Resolve lazy requirement imports while idle. Actual task toolsets and
    # permissions are still recomputed after its context has been installed.
    model_tools.get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True)
    # Relay's first-turn settings currently import gateway.run and its
    # adapters; resolve those static settings before an assignment arrives.
    from agent.relay_runtime import _segments_config
    _segments_config()
    from tools.env_probe import get_environment_probe_line
    get_environment_probe_line()
    if os.environ.get("HERMES_KANBAN_DB"):
        from hermes_cli import kanban_db
        with kanban_db.connect_closing():
            pass
    print("HERMES_WORKER_READY", flush=True)
    packet = sys.stdin.buffer.readline(2 * 1024 * 1024)
    if not packet:
        return
    request = json.loads(packet)
    if request["env"].get("HERMES_HOME") != os.environ.get("HERMES_HOME"):
        raise RuntimeError("Prewarmed worker profile changed")
    _bind_task_environment(request["env"])
    sys.argv = ["hermes", *request["argv"]]
    entry._apply_profile_override()
    if request.get("cwd"):
        os.chdir(request["cwd"])
    with open(request["log"], "ab", buffering=0) as log:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
    with open(os.devnull, "rb") as devnull:
        os.dup2(devnull.fileno(), 0)
    # Bundled observers bind task IDs at invocation. Third-party or project
    # plugins may instead capture task/workspace state during registration;
    # retain the official reload for those installations.
    from hermes_cli.plugins import get_plugin_manager
    from utils import env_var_enabled
    manager = get_plugin_manager()
    reload_plugins = env_var_enabled("HERMES_ENABLE_PROJECT_PLUGINS") or any(
        getattr(loaded.manifest, "source", "") != "bundled"
        and getattr(loaded.manifest, "name", "") != "collaboration-worker-stream"
        for loaded in manager._plugins.values()
    )
    discover_plugins(force=reload_plugins)
    entry.main()


if __name__ == "__main__":
    _main()
