"""Subprocess lifecycle manager for the google_meet bot.

Single active meeting at a time. Stores the running pid + out_dir in a
session-scoped state file under ``$HERMES_HOME/workspace/meetings/.active.json``
so tool calls across turns can find the bot, and ``on_session_end`` can clean
it up.

The bot runs as a detached subprocess — we don't hold file descriptors open,
so the parent agent loop can't block on it. We communicate via files only.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

# File + directory layout (under $HERMES_HOME):
#
#   workspace/meetings/
#       .active.json                # pointer to current session's bot
#       <meeting-id>/
#           status.json             # live bot state (written by bot each tick)
#           transcript.txt          # scraped captions
#
# .active.json holds:
#   {"pid": 12345, "meeting_id": "abc-defg-hij", "out_dir": "...",
#    "url": "https://meet.google.com/...", "started_at": 1714159200.0,
#    "session_id": "optional", "start_time": 123456789}


def _root() -> Path:
    return Path(get_hermes_home()) / "workspace" / "meetings"


def _active_file() -> Path:
    return _root() / ".active.json"


def _read_active() -> Optional[Dict[str, Any]]:
    p = _active_file()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_active(data: Dict[str, Any]) -> None:
    p = _active_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def _clear_active() -> None:
    try:
        _active_file().unlink()
    except FileNotFoundError:
        pass


def _path_within(path: Path, root: Path) -> bool:
    """Return whether *path* is a strict descendant of *root*."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _prepare_out_dir(out_dir: Optional[Path], meeting_id: str) -> Path:
    """Create and return a meeting directory contained by the managed root.

    ``out_dir`` remains an internal/testing override, but it is never allowed
    to escape ``workspace/meetings``.  Resolving both before and after mkdir
    rejects existing symlinks/junctions that cross the boundary.
    """
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    root_real = root.resolve(strict=True)
    candidate = Path(out_dir) if out_dir is not None else root / meeting_id
    if not candidate.is_absolute():
        candidate = root / candidate
    before = candidate.resolve(strict=False)
    if not _path_within(before, root_real):
        raise ValueError("meeting out_dir must remain under the managed meetings root")
    candidate.mkdir(parents=True, exist_ok=True)
    after = candidate.resolve(strict=True)
    if not _path_within(after, root_real):
        raise ValueError("meeting out_dir escaped the managed meetings root")
    return after


def _active_out_dir(
    active: Optional[Dict[str, Any]], *, require_exists: bool = False
) -> Optional[Path]:
    """Resolve a persisted output directory only while it remains managed."""
    if not isinstance(active, dict):
        return None
    raw = active.get("out_dir")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        root_real = _root().resolve(strict=False)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = _root() / candidate
        resolved = candidate.resolve(strict=require_exists)
    except (OSError, RuntimeError):
        return None
    if not _path_within(resolved, root_real):
        return None
    if require_exists and not resolved.is_dir():
        return None
    return resolved


def _pid_alive(pid: int) -> bool:
    # ``os.kill(pid, 0)`` is NOT a no-op on Windows (bpo-14484) — it
    # routes through GenerateConsoleCtrlEvent and can kill the target.
    # Use the cross-platform existence check.
    from gateway.status import _pid_exists
    return _pid_exists(pid)


def _pid_start_time(pid: int) -> Optional[int]:
    """Return the host's stable process-identity fingerprint for *pid*.

    The value is deliberately obtained through the shared gateway helper so
    Meet uses the same ``/proc``/psutil semantics as the rest of Hermes.  A
    missing value means the identity cannot be proven and callers must fail
    closed rather than signalling a potentially recycled PID.
    """
    try:
        from gateway.status import get_process_start_time

        return get_process_start_time(int(pid))
    except Exception:
        return None


def _recorded_pid(active: Optional[Dict[str, Any]]) -> int:
    """Parse a PID from an active record without allowing malformed state to raise."""
    if not isinstance(active, dict):
        return 0
    try:
        pid = int(active.get("pid", 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    return pid if pid > 0 else 0


def _active_identity_matches(active: Optional[Dict[str, Any]]) -> bool:
    """Whether an active pointer still names the process it created.

    Legacy pointers without ``start_time`` are intentionally not considered
    safe to signal.  This prevents a stale ``.active.json`` from killing an
    unrelated process after the original bot exited and its PID was reused.
    """
    if not isinstance(active, dict):
        return False
    pid = _recorded_pid(active)
    recorded = active.get("start_time")
    if not pid or recorded is None:
        return False
    try:
        recorded_int = int(recorded)
    except (TypeError, ValueError):
        return False
    if not _pid_alive(pid):
        return False
    current = _pid_start_time(pid)
    return current is not None and current == recorded_int


# ---------------------------------------------------------------------------
# Public API — used by tool handlers + CLI
# ---------------------------------------------------------------------------

def start(
    url: str,
    *,
    out_dir: Optional[Path] = None,
    headed: bool = False,
    auth_state: Optional[str] = None,
    guest_name: str = "Hermes Agent",
    duration: Optional[str] = None,
    session_id: Optional[str] = None,
    mode: str = "transcribe",
    realtime_model: Optional[str] = None,
    realtime_voice: Optional[str] = None,
    realtime_instructions: Optional[str] = None,
    realtime_api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Spawn the meet_bot subprocess for *url*.

    If a bot is already running for this hermes install, leave it first —
    we enforce single-active-meeting semantics.

    Returns a dict summarizing the started bot.
    """
    from plugins.google_meet.meet_bot import _is_safe_meet_url, _meeting_id_from_url

    if not _is_safe_meet_url(url):
        return {
            "ok": False,
            "error": (
                "refusing: only https://meet.google.com/ URLs are allowed. "
                "got: " + repr(url)
            ),
        }

    existing = _read_active()
    if existing:
        # Only signal an existing process after proving both PID liveness and
        # its recorded start-time identity.  A stale/legacy pointer is simply
        # discarded; it must never be used as a kill authority.
        if _active_identity_matches(existing):
            stop(reason="replaced by new meet_join")
        else:
            # Whether the stale PID is dead, reused, or malformed, discard
            # only our pointer and leave the unrelated process untouched.
            _clear_active()

    meeting_id = _meeting_id_from_url(url)
    try:
        out = _prepare_out_dir(out_dir, meeting_id)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    # Wipe any stale transcript/status files from a previous run of this
    # meeting id so polling isn't confused.
    for name in ("transcript.txt", "status.json"):
        f = out / name
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass

    env = os.environ.copy()
    env["HERMES_MEET_URL"] = url
    env["HERMES_MEET_OUT_DIR"] = str(out)
    env["HERMES_MEET_GUEST_NAME"] = guest_name
    if headed:
        env["HERMES_MEET_HEADED"] = "1"
    if auth_state:
        env["HERMES_MEET_AUTH_STATE"] = auth_state
    if duration:
        env["HERMES_MEET_DURATION"] = duration
    # v2: realtime mode + passthroughs. The bot defaults to transcribe
    # mode if HERMES_MEET_MODE isn't set, matching v1 behavior.
    if mode:
        env["HERMES_MEET_MODE"] = mode
    if realtime_model:
        env["HERMES_MEET_REALTIME_MODEL"] = realtime_model
    if realtime_voice:
        env["HERMES_MEET_REALTIME_VOICE"] = realtime_voice
    if realtime_instructions:
        env["HERMES_MEET_REALTIME_INSTRUCTIONS"] = realtime_instructions
    # Resolve the realtime key at SPAWN time, in the parent, where the
    # profile secret scope (a contextvar) is still installed. The detached
    # child inherits the process environment — NOT the scope — so under a
    # multiplexed gateway an in-child os.environ read would see another
    # profile's OPENAI_API_KEY (or nothing). Pass it explicitly instead;
    # meet_bot checks HERMES_MEET_REALTIME_KEY before OPENAI_API_KEY.
    if not realtime_api_key:
        try:
            from agent.secret_scope import get_secret

            realtime_api_key = (
                get_secret("HERMES_MEET_REALTIME_KEY")
                or get_secret("OPENAI_API_KEY")
            )
        except ImportError:  # pragma: no cover — secret_scope is in-repo
            pass
    if realtime_api_key:
        env["HERMES_MEET_REALTIME_KEY"] = realtime_api_key

    log_path = out / "bot.log"
    # Detach: stdin=devnull, stdout/stderr → log file, new session so parent
    # signals don't propagate.
    log_fh = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "plugins.google_meet.meet_bot"],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        # The subprocess now owns the log fd; we can close ours.
        log_fh.close()

    record = {
        "pid": proc.pid,
        "meeting_id": meeting_id,
        "out_dir": str(out),
        "url": url,
        "started_at": time.time(),
        "session_id": session_id,
        "log_path": str(log_path),
        "mode": mode,
        "start_time": _pid_start_time(proc.pid),
    }
    _write_active(record)
    return {"ok": True, **record}


def status() -> Dict[str, Any]:
    """Return the current meeting state, or ``{"ok": False, "reason": ...}``."""
    active = _read_active()
    if not active:
        return {"ok": False, "reason": "no active meeting"}

    pid = _recorded_pid(active)
    alive = _active_identity_matches(active)

    out_dir = _active_out_dir(active, require_exists=True)
    status_path = out_dir / "status.json" if out_dir is not None else None
    bot_status: Dict[str, Any] = {}
    if status_path is not None and status_path.is_file():
        try:
            bot_status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "ok": True,
        "alive": alive,
        "pid": pid,
        "meetingId": active.get("meeting_id"),
        "url": active.get("url"),
        "startedAt": active.get("started_at"),
        "outDir": str(out_dir) if out_dir is not None else None,
        **bot_status,
    }


def transcript(last: Optional[int] = None) -> Dict[str, Any]:
    """Read the current transcript file. Returns ok=False if none exists."""
    active = _read_active()
    if not active:
        return {"ok": False, "reason": "no active meeting"}

    out_dir = _active_out_dir(active, require_exists=True)
    if out_dir is None:
        return {"ok": False, "reason": "active meeting out_dir is not managed"}
    tp = out_dir / "transcript.txt"
    if not tp.is_file():
        return {
            "ok": True,
            "meetingId": active.get("meeting_id"),
            "lines": [],
            "total": 0,
            "path": str(tp),
        }
    text = tp.read_text(encoding="utf-8", errors="replace")
    all_lines = [ln for ln in text.splitlines() if ln.strip()]
    lines = all_lines[-last:] if last else all_lines
    return {
        "ok": True,
        "meetingId": active.get("meeting_id"),
        "lines": lines,
        "total": len(all_lines),
        "path": str(tp),
    }


def enqueue_say(text: str) -> Dict[str, Any]:
    """Append a ``say`` request to the active bot's JSONL queue.

    Returns ``{"ok": False, "reason": ...}`` when no meeting is active or
    the active bot is in transcribe-only mode. Otherwise writes a line to
    ``<out_dir>/say_queue.jsonl`` that the bot's realtime speaker thread
    will consume.
    """
    import uuid

    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "text is required"}

    active = _read_active()
    if not active:
        return {"ok": False, "reason": "no active meeting"}
    if active.get("mode") != "realtime":
        return {
            "ok": False,
            "reason": (
                "active meeting is in transcribe mode — pass mode='realtime' "
                "to meet_join to enable agent speech"
            ),
        }

    out_dir = _active_out_dir(active, require_exists=True)
    if out_dir is None:
        return {"ok": False, "reason": "active meeting out_dir is not managed"}

    queue_path = out_dir / "say_queue.jsonl"
    entry = {"id": uuid.uuid4().hex[:12], "text": text}
    with queue_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return {
        "ok": True,
        "meetingId": active.get("meeting_id"),
        "enqueued_id": entry["id"],
        "queue_path": str(queue_path),
    }


def stop(*, reason: str = "requested") -> Dict[str, Any]:
    """Signal the active bot to leave cleanly, then clear the active pointer.

    Sends SIGTERM and waits up to 10s for the bot to exit. Falls back to
    SIGKILL if the bot doesn't respond.
    """
    active = _read_active()
    if not active:
        return {"ok": False, "reason": "no active meeting"}

    pid = _recorded_pid(active)
    out_dir = _active_out_dir(active, require_exists=False)
    transcript_path = out_dir / "transcript.txt" if out_dir is not None else None

    live = bool(pid and _pid_alive(pid))
    identity_verified = bool(live and _active_identity_matches(active))
    if live and not identity_verified:
        # The PID is live but its identity cannot be proven (including legacy
        # pointers without a start-time fingerprint).  Never signal it.
        _clear_active()
        return {
            "ok": False,
            "reason": "active meeting process identity could not be verified; not signalled",
            "meetingId": active.get("meeting_id"),
            "transcriptPath": str(transcript_path) if transcript_path else None,
        }

    if identity_verified:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(20):
            if not _active_identity_matches(active):
                break
            time.sleep(0.5)
        if _active_identity_matches(active):
            try:
                os.kill(pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only plugin (google_meet registers no-op on Windows; see __init__.py)
            except ProcessLookupError:
                pass

    _clear_active()
    return {
        "ok": True,
        "reason": reason,
        "meetingId": active.get("meeting_id"),
        "transcriptPath": str(transcript_path) if transcript_path else None,
    }
