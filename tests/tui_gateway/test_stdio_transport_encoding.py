"""Regression tests: stdio JSON frames must survive non-UTF-8 locale codecs.

A POSIX gateway started under LC_ALL=C binds an ASCII stdout codec; before
the fix the first non-ASCII frame raised UnicodeEncodeError and killed the
process pre-ready. This mirrors hermes_bootstrap.py's Windows mitigation
(reconfigure to utf-8 with errors="replace" + per-frame degradation).
"""

import json
import threading

import pytest

from tui_gateway.transport import StdioTransport, _sanitize_line


class _AsciiStream:
    """Stand-in for stdout bound to an ASCII codec under LC_ALL=C."""

    encoding = "ascii"

    def __init__(self):
        self.chunks = []

    def write(self, s):
        for ch in s:
            if ord(ch) > 127:
                raise UnicodeEncodeError("ascii", ch, 0, 1, "ordinal not in range(128)")
        self.chunks.append(s)

    def flush(self):
        pass


def _transport(stream):
    return StdioTransport(lambda: stream, threading.Lock())


def test_non_ascii_frame_degrades_to_valid_single_frame():
    stream = _AsciiStream()
    ok = _transport(stream).write({"jsonrpc": "2.0", "id": 1, "result": "caf\u00e9\u2713"})
    assert ok is True  # NOT peer-gone: the gateway must keep running
    raw = "".join(stream.chunks)
    assert raw.endswith("\n")
    assert raw.count("\n") == 1  # newline-delimited framing preserved
    parsed = json.loads(raw)
    assert parsed["result"].startswith("caf")


def test_sanitize_line_replaces_only_unencodable_chars():
    out = _sanitize_line("h\u00e9llo\n", _AsciiStream())
    assert out == "h?llo\n"


def test_closed_file_valueerror_still_reports_peer_gone():
    class Closed:
        encoding = "utf-8"

        def write(self, s):
            raise ValueError("I/O operation on closed file")

        def flush(self):
            pass

    assert _transport(Closed()).write({"a": 1}) is False


def test_unrelated_valueerror_still_raises():
    class Boom:
        encoding = "utf-8"

        def write(self, s):
            raise ValueError("kaboom")

        def flush(self):
            pass

    with pytest.raises(ValueError):
        _transport(Boom()).write({"a": 1})


class _RecordingStream:
    def __init__(self):
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


def test_entry_pin_stdio_utf8_targets_all_three_streams(monkeypatch):
    from tui_gateway import entry

    streams = {name: _RecordingStream() for name in ("stdout", "stderr", "stdin")}
    for name, stream in streams.items():
        monkeypatch.setattr(entry.sys, name, stream)
    entry._pin_stdio_utf8()
    for stream in streams.values():
        assert stream.calls == [{"encoding": "utf-8", "errors": "replace"}]
