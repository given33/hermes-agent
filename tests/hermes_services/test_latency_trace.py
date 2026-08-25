"""Tests for hermes_services.latency_trace."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from hermes_services import latency_trace as lt


@pytest.fixture()
def tracer(tmp_path: Path):
    lt.flush(timeout_seconds=30.0)  # drain any stragglers from a prior test
    lt.reset_for_tests()
    sink = tmp_path / "traces"
    lt.configure(enabled=True, sink_dir=sink, max_bytes=200_000, retain=2)
    yield sink
    lt.reset_for_tests()


def _records(sink: Path) -> list[dict]:
    assert lt.flush(timeout_seconds=60.0), "trace writer did not drain"
    path = sink / "traces.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_disabled_tracer_writes_nothing(tmp_path: Path, monkeypatch):
    lt.reset_for_tests()
    lt._read_config_enabled = lambda: False  # type: ignore[method-assign]
    with lt.span("noop.op"):
        pass
    lt.instant("noop.point")
    assert not (tmp_path / "traces" / "traces.jsonl").exists()


def test_span_records_duration_status_and_attrs(tracer: Path):
    with lt.span("demo.work", request_id="req-1", route="hosted") as current:
        current.attr("items", 3)
        time.sleep(0.005)
    records = _records(tracer)
    assert len(records) == 1
    record = records[0]
    assert record["name"] == "demo.work"
    assert record["req"] == "req-1"
    assert record["status"] == "ok"
    assert record["attrs"] == {"route": "hosted", "items": 3}
    assert record["dur_ms"] >= 5.0


def test_span_error_status_preserves_exception(tracer: Path):
    with pytest.raises(ValueError), lt.span("demo.fail"):
        raise ValueError("boom")
    records = _records(tracer)
    assert records[0]["status"] == "error:ValueError"


def test_request_id_inherits_into_nested_spans(tracer: Path):
    with lt.span("outer", request_id="req-parent"):
        with lt.span("inner"):
            pass
    names = {r["name"]: r["req"] for r in _records(tracer)}
    assert names["outer"] == "req-parent"
    assert names["inner"] == "req-parent"


def test_instant_and_lock_wait(tracer: Path):
    lt.instant("point.a", request_id="r")
    lock = threading.Lock()
    with lt.lock_wait("state"), lock:
        pass
    names = sorted(r["name"] for r in _records(tracer))
    assert names == ["lock.wait.state", "point.a"]


def test_rotation_keeps_retained_files(tracer: Path):
    lt._MAX_BYTES = 400
    for i in range(12):
        with lt.span(f"rotate.{i}"):
            time.sleep(0.001)
    assert lt.flush(timeout_seconds=60.0)
    files = list(tracer.glob("traces.jsonl*"))
    assert any(str(f).endswith(".1") for f in files)
    # Retention keeps the most recent windows; the exact split depends on
    # record size vs the tiny max_bytes, so require the newest span survived.
    all_records: list[dict] = []
    for f in files:
        all_records.extend(
            json.loads(line)
            for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    assert len(all_records) >= 6
    surviving_names = {r["name"] for r in all_records}
    assert "rotate.11" in surviving_names


def test_summarize_percentiles_known_values(tracer: Path):
    values = [10.0, 20.0, 30.0, 40.0]
    for v in values:
        record = {
            "ts": "2026-08-24T00:00:00.000+00:00",
            "req": "r",
            "name": "known.dist",
            "dur_ms": v,
            "status": "ok",
            "thread": "t",
            "attrs": {},
        }
        (tracer / "traces.jsonl").parent.mkdir(parents=True, exist_ok=True)
        with (tracer / "traces.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    summary = lt.summarize([tracer / "traces.jsonl"])
    row = summary["known.dist"]
    assert row["count"] == 4
    assert row["p50"] == pytest.approx(25.0)
    assert row["p95"] == pytest.approx(38.5)
    assert row["max"] == pytest.approx(40.0)


def test_summarize_since_seconds_filters_old_rows(tracer: Path):
    old = {
        "ts": "2000-01-01T00:00:00.000+00:00",
        "req": "",
        "name": "old.span",
        "dur_ms": 5,
        "status": "ok",
        "thread": "t",
        "attrs": {},
    }
    new = dict(old, name="new.span", ts="2100-01-01T00:00:00.000+00:00")
    path = tracer / "traces.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8")
    summary = lt.summarize([path], since_seconds=60)
    assert set(summary) == {"new.span"}


def test_configure_reads_config_gate(monkeypatch, tmp_path: Path):
    lt.reset_for_tests()
    monkeypatch.setattr(
        lt, "_read_config_enabled", lambda: True, raising=False
    )
    lt.configure(sink_dir=tmp_path / "cfg-sink")
    assert lt.enabled() is True
    lt.reset_for_tests()