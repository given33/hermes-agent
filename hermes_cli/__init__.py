"""
Hermes CLI - Unified command-line interface for Hermes Agent.

Provides subcommands for:
- hermes chat          - Interactive chat (same as ./hermes)
- hermes gateway       - Run gateway in foreground
- hermes gateway start - Start gateway service
- hermes gateway stop  - Stop gateway service
- hermes setup         - Interactive setup wizard
- hermes status        - Show status of all components
- hermes cron          - Manage cron jobs

Importing this package is side-effect free. The UTF-8 stdio repair that
used to run here at import time is now an explicit entry-point call —
see :func:`ensure_utf8_stdio` below for why and for who must call it.
"""

import os
import sys

from hermes_runtime.version import HERMES_RELEASE_DATE, HERMES_VERSION

__version__ = HERMES_VERSION
__release_date__ = HERMES_RELEASE_DATE


def _ensure_utf8():
    """Force UTF-8 stdout/stderr to prevent UnicodeEncodeError crashes.

    Several environments select a legacy, non-UTF-8 encoding for the standard
    streams:

    - Windows services and terminals default to cp1252.
    - Linux hosts with a latin-1 / C / POSIX locale (common on minimal Debian
      installs and Raspberry Pi) select latin-1 or ASCII.

    The CLI prints box-drawing characters (┌│├└─) and the ⚕ glyph in the setup
    wizard, doctor, and status banners. Encoding those under a non-UTF-8 codec
    raises an unhandled UnicodeEncodeError that crashes the command before it
    can even start — e.g. `hermes setup` on a fresh Pi.

    This is the raw repair worker; entry points should call
    :func:`ensure_utf8_stdio` (the guarded wrapper) instead. It re-wraps
    stdout/stderr as UTF-8 when their encoding is not already UTF-8,
    preferring TextIOWrapper.reconfigure() so the existing stream object is
    fixed in place (cached `sys.stdout` references keep working) and falling
    back to reopening the file descriptor with closefd=False (the
    CPython-recommended safe variant).

    No-op when the streams are already UTF-8: a healthy UTF-8 system sees no
    stream change and no environment mutation.

    Note: this is intentionally the earliest, platform-agnostic guard.
    hermes_cli/stdio.py::configure_windows_stdio() runs later from the entry
    points and layers on the Windows-only extras (console code-page flip,
    EDITOR default, PATH augmentation); its stream reconfiguration is a
    harmless idempotent no-op once we have already repaired the streams here.
    """
    repaired = False

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if encoding == "utf8":
                continue

            # Preferred: reconfigure the existing TextIOWrapper in place. This
            # preserves object identity so any code already holding a reference
            # to the old sys.stdout benefits from the repair too.
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(encoding="utf-8", errors="replace")
                repaired = True
                continue

            # Fallback: reopen the underlying file descriptor as UTF-8. Used
            # for streams that don't expose reconfigure() (e.g. some wrapped
            # or replaced streams). closefd=False keeps the original fd open.
            new_stream = open(
                stream.fileno(), "w", encoding="utf-8",
                errors="replace", buffering=1, closefd=False,
            )
            setattr(sys, stream_name, new_stream)
            repaired = True
        except (AttributeError, OSError, ValueError):
            pass

    # Only nudge child processes toward UTF-8 when we actually detected a
    # non-UTF-8 locale. On a healthy UTF-8 host children inherit UTF-8 from the
    # locale already, so leave the environment untouched (minimal footprint).
    if repaired:
        os.environ.setdefault("PYTHONUTF8", "1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")


_UTF8_STDIO_DONE = False


def ensure_utf8_stdio(force: bool = False) -> None:
    """Explicit entry-point hook for the UTF-8 stdio repair.

    Historically :func:`_ensure_utf8` ran at package import time, which made
    ``import hermes_cli`` (the most-imported package in the repo) mutate
    process-global state: it could replace ``sys.stdout``/``sys.stderr`` and
    write ``PYTHONUTF8``/``PYTHONIOENCODING`` into the environment inherited
    by every later subprocess — and it fought pytest's stream capture. The
    repair is real and still needed (cp1252 Windows consoles, latin-1/C
    locales on minimal Debian/Raspberry Pi crash on the banner glyphs), but
    it belongs to *process entry points*, not to library imports.

    Call this once, early, from actual console entry points before anything
    prints. Current callers / required wiring:

    - ``cli.py`` module init (classic REPL bootstrap — covers ``hermes chat``
      and any legacy ``python cli.py`` launch).
    - ``hermes_cli/doctor.py::run_doctor`` (banner-heavy subcommand).
    - ``hermes_cli/main.py::main`` should call this right before its
      ``configure_windows_stdio()`` call so every ``hermes`` subcommand is
      covered on every platform (configure_windows_stdio no-ops off-Windows;
      this repair is the one that catches legacy POSIX locales).
    - ``gateway/run.py`` startup, next to its ``configure_windows_stdio()``
      call, for the long-lived gateway service.
    - ``run_agent.py::main`` — the ``hermes-agent`` console script (pyproject
      ``[project.scripts]``) lands there directly, bypassing every other
      entry point above, and prints emoji banners immediately.

    Guards (all skipped with ``force=True``):
    - Idempotent per process — repeated calls are free.
    - No-op under pytest (``PYTEST_CURRENT_TEST`` set or ``pytest`` already
      imported): pytest owns the streams during capture, and reopening fd 1
      behind its back corrupts captured output. Direct tests of the repair
      call ``_ensure_utf8()`` itself.

    The per-stream "already UTF-8 → untouched" no-op lives in the worker, so
    a healthy UTF-8 host sees no stream or environment change either way.
    """
    global _UTF8_STDIO_DONE
    if not force:
        if _UTF8_STDIO_DONE:
            return
        if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
            return
    _UTF8_STDIO_DONE = True
    _ensure_utf8()
