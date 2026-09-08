"""One-use, profile-scoped CLI processes prepared before a hosted assignment.

Only imports, tool registration and MCP connections are warmed. Every task
still runs the official CLI, receives its own PID/session/workspace, and exits.
No agent, prompt, conversation, or model request is created while idle.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading


def _signature(home: str) -> tuple:
    root = Path(home)
    paths = [root / name for name in ("config.yaml", ".env")]
    # Managed configuration can affect CLI_CONFIG at import time too.
    default_root = root.parents[1] if root.parent.name == "profiles" else root
    paths.extend(default_root / name for name in ("config.yaml", ".env"))
    result = []
    for path in paths:
        try:
            stat = path.stat()
            result.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            result.append((str(path), None, None))
    return tuple(result)


class WorkerPrewarmPool:
    """Bounded spare processes; never block dispatch waiting for a spare."""

    def __init__(self, *, limit: int = 1):
        self.limit = max(0, limit)
        self._spares: dict[str, dict] = {}
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
            previous = self._spares.get(home)
            if previous and previous["signature"] == signature and previous["proc"].poll() is None:
                return
            if previous:
                self._discard(self._spares.pop(home))
            if len(self._spares) >= self.limit:
                self._discard(self._spares.pop(next(iter(self._spares))))
            env = dict(os.environ)
            for key in _VAR_MAP:
                env.pop(key, None)
            for key in tuple(env):
                if key.startswith("HERMES_KANBAN_"):
                    env.pop(key, None)
            env.update(HERMES_HOME=home, HERMES_PROFILE=profile)
            # Warm the very board this profile will use, so the official
            # first-open integrity/schema migration is paid once while idle.
            if board:
                from hermes_cli import kanban_db
                env["HERMES_KANBAN_DB"] = str(kanban_db.kanban_db_path(board=board))
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", __name__, profile], env=env,
                    cwd=home, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )
            except OSError:
                return  # The ordinary Kanban launcher remains available.
            spare = {"proc": proc, "signature": signature, "ready": False,
                     "profile": profile, "board": board}
            self._spares[home] = spare

            def ready() -> None:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if line.strip() == b"HERMES_WORKER_READY":
                        spare["ready"] = True
                        break

            threading.Thread(target=ready, name="worker-prewarm-ready", daemon=True).start()

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
        with self._lock:
            spare = self._spares.pop(home, None)
        if spare:
            proc = spare["proc"]
            if (spare["ready"] and proc.poll() is None
                    and spare["signature"] == _signature(home)):
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
        result = subprocess.Popen(cmd, **kwargs)
        profile = env.get("HERMES_PROFILE", "")
        if profile:
            self._replenish(result, profile, env.get("HERMES_KANBAN_BOARD", ""))
        return result

    def _replenish(self, proc, profile: str, board: str) -> None:
        def replace():
            # Keep CPU-heavy imports off the current task's startup path.
            proc.wait()
            if proc.stdout:
                proc.stdout.close()
            self.prepare(profile, board=board)
        threading.Thread(target=replace, name="worker-prewarm-replace", daemon=True).start()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            spares, self._spares = self._spares, {}
        for spare in spares.values():
            self._discard(spare)


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
    from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build
    ensure_mcp_discovery_before_agent_build(logger=cli.logger, single_query=True)
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
    os.environ.update(request["env"])
    sys.argv = ["hermes", *request["argv"]]
    entry._apply_profile_override()
    if request.get("cwd"):
        os.chdir(request["cwd"])
    with open(request["log"], "ab", buffering=0) as log:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
    with open(os.devnull, "rb") as devnull:
        os.dup2(devnull.fileno(), 0)
    # Registration may depend on task context or the actual workspace (the
    # worker-stream observer, for example). Rebind through the official
    # teardown/discovery API after those values are installed.
    discover_plugins(force=True)
    entry.main()


if __name__ == "__main__":
    _main()
