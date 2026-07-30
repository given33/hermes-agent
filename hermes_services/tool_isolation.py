"""Hard-deadline process isolation for bounded, side-effect-free tools."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import multiprocessing
import os
import pickle
from pathlib import Path
import signal
import sys
import time
from typing import Any, Callable


_PROCESS_START_TIMEOUT_SECONDS = 10.0
_DEFAULT_TOOL_TIMEOUT_SECONDS = 420.0
_PR_SET_CHILD_SUBREAPER = 36
logger = logging.getLogger(__name__)


def _enable_linux_child_subreaper() -> None:
    """Make an isolated worker adopt descendants of short-lived helpers.

    ``execute_code`` and third-party tools may create a new session or
    double-fork.  A process-group kill alone cannot reach those descendants.
    Linux's child-subreaper flag lets the worker retain ownership when an
    intermediate launcher exits; teardown then enumerates and kills the whole
    owned tree.  Failure is deliberately non-fatal on kernels that do not
    expose ``prctl``; the parent still applies process-group teardown.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong)
        prctl.restype = ctypes.c_int
        if prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            logger.debug("PR_SET_CHILD_SUBREAPER unavailable", exc_info=True)
    except Exception:
        pass


def _linux_descendant_pids(root_pid: int) -> set[int]:
    """Return live descendants of ``root_pid`` using the proc parent map."""
    if not sys.platform.startswith("linux") or not root_pid:
        return set()
    parent_map: dict[int, int] = {}
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                stat = Path(entry.path) / "stat"
                raw = stat.read_text(encoding="ascii")
                # comm may contain spaces/parentheses; the final ')' before
                # the state field is the stable delimiter.
                close = raw.rfind(")")
                fields = raw[close + 2 :].split()
                if len(fields) >= 2:
                    parent_map[pid] = int(fields[1])
            except (OSError, ValueError):
                continue
    except OSError:
        return set()

    descendants: set[int] = set()
    frontier = [int(root_pid)]
    while frontier:
        parent = frontier.pop()
        for pid, ppid in parent_map.items():
            if ppid == parent and pid not in descendants:
                descendants.add(pid)
                frontier.append(pid)
    return descendants


def _signal_posix_tree(root_pid: int, sig: int) -> None:
    """Signal descendants before the root group, tolerating races."""
    if os.name == "nt" or not root_pid:
        return
    # Repeat once after the first pass: a launcher can fork between scans.
    for _ in range(2):
        for pid in sorted(_linux_descendant_pids(root_pid), reverse=True):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            os.killpg(root_pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        if sig == signal.SIGKILL:
            break


def _signal_posix_pids(pids: set[int], sig: int) -> None:
    """Signal a captured ownership set after its root may have exited."""
    if os.name == "nt":
        return
    for pid in sorted({int(pid) for pid in pids if int(pid) > 0}, reverse=True):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def default_tool_timeout_seconds() -> float:
    """Return the hard deadline for handlers without an explicit contract.

    Registry handlers always execute in a disposable process.  The explicit
    contract timeout remains authoritative, while this value closes the old
    unbounded path for ordinary tools.  The concurrent-batch setting is also
    considered so a batch deadline cannot leave its registry child running
    after the parent worker has returned a timeout terminal.
    """

    values = [_DEFAULT_TOOL_TIMEOUT_SECONDS]
    for variable in ("HERMES_TOOL_TIMEOUT_S", "HERMES_CONCURRENT_TOOL_TIMEOUT_S"):
        raw = os.getenv(variable, "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return min(values)


class ToolIsolationError(RuntimeError):
    """Raised when an isolated tool cannot start or return a usable result."""


class ToolIsolationResolutionError(ToolIsolationError):
    """Raised when a tool identity cannot be reconstructed in the child."""


class ToolIsolationCancelled(ToolIsolationError):
    """Raised after an interrupt terminates the isolated tool process."""


class _WindowsJob:
    """A kill-on-close Windows Job Object that owns one worker's descendants."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows Job Objects are only available on Windows")

        from ctypes import wintypes

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        )
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, pid: int) -> None:
        process_handle = self._kernel32.OpenProcess(
            self._PROCESS_TERMINATE
            | self._PROCESS_SET_QUOTA
            | self._PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._kernel32.CloseHandle(process_handle)

    def terminate(self) -> None:
        if self._handle:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _resolve_isolated_handler(identity: dict) -> tuple[Callable[..., Any], bool]:
    """Resolve a registry handler by name inside the fresh worker process."""

    try:
        from tools.registry import registry
        from tools.registry import ToolRegistryResolutionError

        return registry.resolve_handler_for_isolation(identity)
    except ToolRegistryResolutionError as exc:
        raise ToolIsolationResolutionError(str(exc)) from exc
    except ToolIsolationResolutionError:
        raise
    except Exception as exc:
        raise ToolIsolationResolutionError(
            f"isolated registry resolver failed: {type(exc).__name__}: {exc}"
        ) from exc


def _isolated_handler_main(send_connection, start_connection, payload: bytes) -> None:
    """Deserialize identity/arguments and execute inside a disposable child."""

    try:
        # Establish ownership before unpickling or invoking handler code.
        if os.name != "nt":
            os.setsid()
            _enable_linux_child_subreaper()
        send_connection.send(("bootstrap",))
        start_connection.recv()
        # execute_code normally creates a new POSIX session. Keep its script in
        # this worker's group so an outer isolation deadline owns that tree.
        os.environ["HERMES_TOOL_ISOLATION"] = "1"
        identity, args, kwargs = pickle.loads(payload)
        handler, is_async = _resolve_isolated_handler(identity)
        send_connection.send(("ready",))
        result = handler(args, **kwargs)
        if is_async:
            result = asyncio.run(result)
        response = (True, result)
        try:
            pickle.dumps(response, protocol=pickle.HIGHEST_PROTOCOL)
        except BaseException as exc:
            response = (
                False,
                "ToolIsolationError",
                f"isolated tool returned an unserializable result: {exc}",
            )
    except BaseException as exc:
        response = (False, type(exc).__name__, str(exc)[:2000])
    try:
        send_connection.send(response)
    except BaseException:
        pass
    finally:
        start_connection.close()
        send_connection.close()


def _stop_process(
    process: multiprocessing.Process,
    *,
    windows_job: _WindowsJob | None = None,
    join_seconds: float = 0.25,
) -> None:
    """Bounded teardown of an owned worker and every ordinary descendant."""

    if windows_job is not None:
        windows_job.terminate()
    elif os.name != "nt" and process.pid:
        # The child calls setsid before the parent releases its start gate.
        # Signal descendants first as handlers may create their own session.
        captured_descendants = _linux_descendant_pids(process.pid)
        _signal_posix_tree(process.pid, signal.SIGTERM)
    elif process.is_alive():
        process.terminate()
    process.join(timeout=join_seconds)
    if windows_job is not None:
        windows_job.terminate()
    elif os.name != "nt" and process.pid:
        # Preserve the first ownership snapshot: once the worker exits,
        # stubborn descendants can be reparented to init and disappear from
        # the live /proc parent map. Kill captured descendants before the
        # final group kill, then rescan for any late forks.
        captured_descendants.update(_linux_descendant_pids(process.pid))
        _signal_posix_pids(captured_descendants, signal.SIGKILL)
        _signal_posix_tree(process.pid, signal.SIGKILL)
    elif process.is_alive():
        kill = getattr(process, "kill", None)
        if callable(kill):
            kill()
    process.join(timeout=join_seconds)


def _poll_interruptibly(
    receive_connection,
    process: multiprocessing.Process,
    *,
    timeout_seconds: float,
    tool_name: str,
    phase: str,
    windows_job: _WindowsJob | None = None,
) -> bool:
    """Wait for one pipe envelope while honoring the caller thread's cancel bit."""

    from tools.interrupt import is_interrupted

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        if is_interrupted():
            _stop_process(process, windows_job=windows_job)
            raise ToolIsolationCancelled(
                f"Tool '{tool_name}' was cancelled during isolated {phase}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if receive_connection.poll(min(0.05, remaining)):
            return True
        if not process.is_alive():
            # Let recv() below surface EOF or the final envelope already queued
            # by a process that exited immediately after sending it.
            return receive_connection.poll(0)


def run_tool_handler_isolated(
    identity: dict,
    args: dict[str, Any],
    kwargs: dict[str, Any],
    *,
    timeout_seconds: float,
    tool_name: str,
) -> Any:
    """Run a registry handler by stable identity in a killable child process.

    The spawn context avoids forking the multithreaded gateway. Only the tool
    identity and plain argument data cross the boundary; the child imports the
    defining module and resolves the handler from its own registry. This keeps
    lambdas registered at module import safe while making dynamic closures and
    live MCP handlers fail explicitly instead of falling back to a thread.
    """

    if not isinstance(identity, dict) or not identity.get("tool_name"):
        raise ToolIsolationResolutionError("missing process-safe tool identity")
    tool_name = str(identity.get("tool_name"))
    timeout_seconds = max(0.0, float(timeout_seconds))
    deadline = time.monotonic() + timeout_seconds

    def _remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    def _deadline_error(phase: str) -> TimeoutError:
        return TimeoutError(
            f"Tool '{tool_name}' exceeded its hard {timeout_seconds:g}s "
            f"deadline during isolated {phase}"
        )

    try:
        payload = pickle.dumps(
            (dict(identity), dict(args), dict(kwargs)),
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    except BaseException as exc:
        raise ToolIsolationError(
            f"Tool '{tool_name}' has a hard deadline but its identity or "
            f"argument context is not process-isolatable: {exc}"
        ) from exc

    if _remaining() <= 0:
        raise _deadline_error("serialization")

    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    start_receive, start_send = context.Pipe(duplex=False)
    windows_job = _WindowsJob() if os.name == "nt" else None
    process = context.Process(
        target=_isolated_handler_main,
        args=(send_connection, start_receive, payload),
        name=f"hermes-tool-{tool_name[:32]}",
        daemon=True,
    )
    try:
        process.start()
    except BaseException as exc:
        receive_connection.close()
        send_connection.close()
        start_receive.close()
        start_send.close()
        if windows_job is not None:
            windows_job.close()
        raise ToolIsolationError(
            f"Tool '{tool_name}' isolated worker could not start: {exc}"
        ) from exc
    finally:
        send_connection.close()
        start_receive.close()

    try:
        if _remaining() <= 0:
            _stop_process(process, windows_job=windows_job)
            raise _deadline_error("startup")
        if not _poll_interruptibly(
            receive_connection,
            process,
            timeout_seconds=min(_PROCESS_START_TIMEOUT_SECONDS, _remaining()),
            tool_name=tool_name,
            phase="startup",
            windows_job=windows_job,
        ):
            _stop_process(process, windows_job=windows_job)
            if _remaining() <= 0:
                raise _deadline_error("startup")
            raise ToolIsolationError(
                f"Tool '{tool_name}' isolated worker did not bootstrap"
            )
        try:
            bootstrap = receive_connection.recv()
        except EOFError as exc:
            _stop_process(process, windows_job=windows_job)
            raise ToolIsolationError(
                f"Tool '{tool_name}' isolated worker exited during startup"
            ) from exc
        if bootstrap != ("bootstrap",):
            _stop_process(process, windows_job=windows_job)
            raise ToolIsolationError(
                f"Tool '{tool_name}' isolated worker could not establish process-tree ownership"
            )
        try:
            if windows_job is not None:
                windows_job.assign(process.pid)
            start_send.send(("start",))
        except BaseException as exc:
            _stop_process(process, windows_job=windows_job)
            raise ToolIsolationError(
                f"Tool '{tool_name}' isolated worker could not establish process-tree ownership: {exc}"
            ) from exc
        if not _poll_interruptibly(
            receive_connection,
            process,
            timeout_seconds=min(_PROCESS_START_TIMEOUT_SECONDS, _remaining()),
            tool_name=tool_name,
            phase="startup",
            windows_job=windows_job,
        ):
            _stop_process(process, windows_job=windows_job)
            if _remaining() <= 0:
                raise _deadline_error("startup")
            raise ToolIsolationError(
                f"Tool '{tool_name}' isolated worker did not become ready"
            )
        try:
            ready = receive_connection.recv()
        except EOFError as exc:
            _stop_process(process, windows_job=windows_job)
            raise ToolIsolationError(
                f"Tool '{tool_name}' isolated worker exited during startup"
            ) from exc
        if ready != ("ready",):
            _stop_process(process, windows_job=windows_job)
            if isinstance(ready, tuple) and ready and ready[0] is False:
                error_type = str(ready[1] if len(ready) > 1 else "ToolIsolationError")
                error_message = str(ready[2] if len(ready) > 2 else "startup failed")
                error_cls = (
                    ToolIsolationResolutionError
                    if error_type == "ToolIsolationResolutionError"
                    else ToolIsolationError
                )
                raise error_cls(
                    f"Tool '{tool_name}' isolated handler failed: "
                    f"{error_type}: {error_message}"
                )
            raise ToolIsolationError(
                f"Tool '{tool_name}' isolated worker returned an invalid startup envelope"
            )
        if not _poll_interruptibly(
            receive_connection,
            process,
            timeout_seconds=_remaining(),
            tool_name=tool_name,
            phase="execution",
            windows_job=windows_job,
        ):
            _stop_process(process, windows_job=windows_job)
            raise _deadline_error("execution")
        try:
            response = receive_connection.recv()
        except EOFError as exc:
            _stop_process(process, windows_job=windows_job)
            raise ToolIsolationError(
                f"Tool '{tool_name}' isolated worker exited without a result"
            ) from exc
        process.join(timeout=0.25)
        # A completed root can still leave a descendant behind. Clean the
        # owned tree before publishing the tool's terminal result.
        _stop_process(process, windows_job=windows_job)
        if not isinstance(response, tuple) or not response:
            raise ToolIsolationError(
                f"Tool '{tool_name}' isolated worker returned an invalid envelope"
            )
        if response[0] is True and len(response) == 2:
            return response[1]
        error_type = str(response[1] if len(response) > 1 else "ToolIsolationError")
        error_message = str(response[2] if len(response) > 2 else "isolated tool failed")
        error_cls = (
            ToolIsolationResolutionError
            if error_type == "ToolIsolationResolutionError"
            else ToolIsolationError
        )
        raise error_cls(
            f"Tool '{tool_name}' isolated handler failed: {error_type}: {error_message}"
        )
    finally:
        receive_connection.close()
        start_send.close()
        _stop_process(process, windows_job=windows_job)
        if windows_job is not None:
            windows_job.close()
