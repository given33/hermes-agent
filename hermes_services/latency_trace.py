"""Opt-in local latency tracing for the Hermes chat worker chain.

One request/turn produces spans along the chain (server accept/enqueue, state
save, workflow routing, provider first token, tool segments, terminal append,
SSE delivery, worker visible/claim/spawn/exit/upload).  Spans are appended as
JSONL to <HERMES_HOME>/logs/traces/traces.jsonl by a single background writer
thread so the hot path only pays one queue put.

Policy:

* Local files only -- nothing is ever sent over the network.
* Disabled by default; enabled via collaboration.latency_tracing.enabled
  in config.yaml (repo policy: behavioral settings live in config.yaml,
  never in new environment variables).
* When disabled, every call site reduces to one module-global truthiness
  check and an immediate return.
* The writer never raises into the caller: sink errors degrade the tracer to
  a counting no-op instead of blocking or crashing the serving path.

Percentiles: use summarize() (or python -m hermes_services.latency_trace
summary) to reduce the JSONL log to per-span p50/p95/max tables.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import queue
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "configure",
    "enabled",
    "new_request_id",
    "current_request_id",
    "span",
    "instant",
    "lock_wait",
    "flush",
    "summarize",
    "degraded_count",
    "reset_for_tests",
]

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_ENABLED = False
_SINK_DIR: Path | None = None
_MAX_BYTES = 32 * 1024 * 1024
_RETAIN = 2

_QUEUE: "queue.SimpleQueue[dict[str, Any] | None]" = queue.SimpleQueue()
_QUEUE_MAX = 100_000
_DROPPED = 0
_ENQUEUED = 0
_WRITTEN = 0
_WRITER_THREAD: threading.Thread | None = None
_WRITER_LOCK = threading.Lock()
_DEGRADED_REASON: str | None = None

_CURRENT_REQUEST: ContextVar[str] = ContextVar("hermes_latency_request", default="")
_SINK_FILENAME = "traces.jsonl"


def _default_sink_dir() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "logs" / "traces"


def _read_config_enabled() -> bool:
    """Best-effort config read; any failure keeps tracing disabled."""

    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        section = cfg_get(
            load_config_readonly(), "collaboration", "latency_tracing", default={}
        )
        if isinstance(section, dict):
            return bool(section.get("enabled", False))
        return bool(section)
    except Exception:
        return False


def configure(
    *,
    enabled: bool | None = None,
    sink_dir: "str | Path | None" = None,
    max_bytes: int | None = None,
    retain: int | None = None,
) -> None:
    """Enable/disable the tracer explicitly (tests, embedders) or from config.

    Safe to call repeatedly; the writer thread starts lazily on first enable.
    """

    global _ENABLED, _SINK_DIR, _MAX_BYTES, _RETAIN
    if enabled is None:
        enabled = _read_config_enabled()
    if sink_dir is not None:
        _SINK_DIR = Path(sink_dir)
    if max_bytes is not None:
        _MAX_BYTES = int(max_bytes)
    if retain is not None:
        _RETAIN = max(1, int(retain))
    was_enabled = _ENABLED
    _ENABLED = bool(enabled)
    if _ENABLED and not was_enabled:
        _ensure_writer()


# ---------------------------------------------------------------------------
# Writer thread
# ---------------------------------------------------------------------------


def _sink_path() -> Path:
    directory = _SINK_DIR if _SINK_DIR is not None else _default_sink_dir()
    return directory / _SINK_FILENAME


def _rotate_if_needed(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < _MAX_BYTES:
            return
        oldest = str(path) + "." + str(_RETAIN)
        try:
            if os.path.exists(oldest):
                os.unlink(oldest)
        except OSError:
            pass
        for i in range(_RETAIN - 1, 0, -1):
            src = str(path) + "." + str(i)
            dst = str(path) + "." + str(i + 1)
            try:
                if os.path.exists(src):
                    os.replace(src, dst)
            except OSError:
                pass
        os.replace(path, str(path) + ".1")
    except OSError:
        # Rotation failure must not kill the writer.
        pass


def _writer_loop() -> None:
    global _DEGRADED_REASON, _WRITTEN
    while True:
        record = _QUEUE.get()
        if record is None:  # shutdown sentinel
            return
        path = _sink_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(path)
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            _WRITTEN += 1
        except Exception as exc:  # pragma: no cover - defensive
            # Count failed records as written so flush() cannot hang forever.
            _WRITTEN += 1
            _DEGRADED_REASON = "sink write failed: " + type(exc).__name__


def _ensure_writer() -> None:
    global _WRITER_THREAD
    with _WRITER_LOCK:
        if _WRITER_THREAD is not None and _WRITER_THREAD.is_alive():
            return
        _WRITER_THREAD = threading.Thread(
            target=_writer_loop,
            name="hermes-latency-trace",
            daemon=True,
        )
        _WRITER_THREAD.start()


def degraded_count() -> int:
    """Number of spans dropped because the queue cap was hit."""

    return _DROPPED


def flush(timeout_seconds: float = 5.0) -> bool:
    """Block until every enqueued span has been written or dropped.

    Uses monotonic enqueued/written counters rather than queue emptiness so a
    record mid-write cannot race the caller. Returns True when fully drained.
    """

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while _WRITTEN < _ENQUEUED:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.002)
    return True


def _enqueue(record: dict[str, Any]) -> None:
    global _DROPPED, _ENQUEUED
    # Bounded drop policy: when the writer cannot keep up (disk stall), shed
    # traces rather than grow memory without limit.
    if _QUEUE.qsize() > _QUEUE_MAX:
        _DROPPED += 1
        # Dropped records never reach the writer; keep the counters consistent
        # so flush()'s drained condition stays reachable.
        _ENQUEUED += 1
        _WRITTEN += 1
        return
    _ENQUEUED += 1
    _QUEUE.put(record)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """True when tracing is active. Hot-path call sites check this first."""

    return _ENABLED


def new_request_id(prefix: str = "req") -> str:
    return prefix + "-" + uuid.uuid4().hex[:16]


def current_request_id() -> str:
    return _CURRENT_REQUEST.get()


class _Span:
    __slots__ = ("name", "request_id", "attrs", "status", "start")

    def __init__(self, name: str, request_id: str, attrs: dict[str, Any]) -> None:
        self.name = name
        self.request_id = request_id
        self.attrs = attrs
        self.status = "ok"
        self.start = time.perf_counter()

    def set_status(self, status: str) -> None:
        self.status = status

    def attr(self, key: str, value: Any) -> None:
        self.attrs[key] = value


@contextlib.contextmanager
def span(
    name: str,
    *,
    request_id: str | None = None,
    **attrs: Any,
) -> Iterator[_Span]:
    """Record one timed span. With tracing disabled this is a no-op context."""

    if not _ENABLED:
        yield _Span(name, "", attrs)  # inert handle; attrs dict is discarded
        return
    rid = request_id if request_id is not None else _CURRENT_REQUEST.get()
    current = _Span(name, rid, dict(attrs))
    token = _CURRENT_REQUEST.set(rid) if rid else None
    try:
        yield current
    except BaseException as exc:
        current.status = "error:" + type(exc).__name__
        raise
    finally:
        if token is not None:
            _CURRENT_REQUEST.reset(token)
        duration_ms = round((time.perf_counter() - current.start) * 1000.0, 3)
        _enqueue(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "req": current.request_id,
                "name": current.name,
                "dur_ms": duration_ms,
                "status": current.status,
                "thread": threading.current_thread().name,
                "attrs": current.attrs,
            }
        )


def instant(name: str, *, request_id: str | None = None, **attrs: Any) -> None:
    """Record a zero-duration point event."""

    if not _ENABLED:
        return
    with span(name, request_id=request_id, **attrs):
        pass


@contextlib.contextmanager
def lock_wait(lock_name: str, *, request_id: str | None = None) -> Iterator[None]:
    """Measure pure lock-acquire wait time (call immediately before .acquire())."""

    if not _ENABLED:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        waited_ms = round((time.perf_counter() - started) * 1000.0, 3)
        rid = request_id if request_id is not None else _CURRENT_REQUEST.get()
        _enqueue(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "req": rid,
                "name": "lock.wait." + lock_name,
                "dur_ms": waited_ms,
                "status": "ok",
                "thread": threading.current_thread().name,
                "attrs": {"kind": "lock-wait"},
            }
        )


# ---------------------------------------------------------------------------
# Reduction / reporting
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    rank = (len(sorted_values) - 1) * pct
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[lo]
    weight = rank - lo
    return sorted_values[lo] * (1.0 - weight) + sorted_values[hi] * weight


def summarize(
    paths: "list[str | Path] | None" = None,
    *,
    since_seconds: float | None = None,
) -> dict[str, dict[str, float]]:
    """Reduce JSONL span files to per-span-name p50/p95/max in milliseconds."""

    if paths is None:
        base = _sink_path()
        candidates = [base]
        for i in range(1, _RETAIN + 1):
            rotated = Path(str(base) + "." + str(i))
            if rotated.exists():
                candidates.append(rotated)
        paths = [p for p in candidates if p.exists()]

    cutoff = None
    if since_seconds is not None:
        cutoff = time.time() - since_seconds

    buckets: dict[str, list[float]] = {}
    for raw_path in paths:
        path = Path(raw_path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = record.get("name")
            dur = record.get("dur_ms")
            if not isinstance(name, str) or not isinstance(dur, (int, float)):
                continue
            if cutoff is not None and not _record_after(record, cutoff):
                continue
            buckets.setdefault(name, []).append(float(dur))

    summary: dict[str, dict[str, float]] = {}
    for name, values in buckets.items():
        values.sort()
        summary[name] = {
            "count": len(values),
            "p50": round(_percentile(values, 0.50), 3),
            "p95": round(_percentile(values, 0.95), 3),
            "max": round(values[-1], 3),
        }
    return summary


def _record_after(record: dict[str, Any], cutoff_epoch: float) -> bool:
    ts = record.get("ts")
    if not isinstance(ts, str):
        return True
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return True
    epoch = (
        parsed.timestamp()
        if parsed.tzinfo
        else parsed.replace(tzinfo=timezone.utc).timestamp()
    )
    return epoch >= cutoff_epoch


def reset_for_tests() -> None:
    """Reset module state between tests (tests pass their own sink_dir)."""

    global _ENABLED, _SINK_DIR, _DROPPED, _ENQUEUED, _WRITTEN
    _ENABLED = False
    _SINK_DIR = None
    _DROPPED = 0
    _ENQUEUED = 0
    _WRITTEN = 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="latency_trace")
    sub = parser.add_subparsers(dest="command", required=True)
    sum_p = sub.add_parser("summary", help="print per-span p50/p95/max table")
    sum_p.add_argument("--since-seconds", type=float, default=None)
    sum_p.add_argument("--paths", nargs="*", default=None)
    args = parser.parse_args(argv)

    if args.command == "summary":
        rows = summarize(args.paths, since_seconds=args.since_seconds)
        header = "%-44s %7s %10s %10s %10s" % ("span", "count", "p50ms", "p95ms", "maxms")
        print(header)
        for name in sorted(rows, key=lambda n: -rows[n]["p95"]):
            row = rows[name]
            print(
                "%-44s %7d %10.2f %10.2f %10.2f"
                % (name, row["count"], row["p50"], row["p95"], row["max"])
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
