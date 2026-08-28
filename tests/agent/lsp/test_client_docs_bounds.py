"""Unit tests for LSPClient document-store bounding and dispatch refs.

Covers two resource-leak fixes:

1. Server→client request dispatch tasks must be strongly referenced —
   the event loop only holds weak refs, so a bare ``create_task`` in
   ``_reader_loop`` could be GC'd before answering (rust-analyzer /
   vtsls then block forever on e.g. ``workspace/configuration``).

2. ``_docs`` must stay bounded: opened documents are LRU-evicted with a
   paired ``textDocument/didClose`` once ``MAX_OPEN_DOCS`` is exceeded,
   and diagnostics-only spillover entries (publishDiagnostics /
   relatedDocuments for files never opened) are trimmed without one.
"""
from __future__ import annotations

import asyncio
import gc
import os
from types import SimpleNamespace

import pytest

from agent.lsp import client as client_mod
from agent.lsp.client import LSPClient, _DocState


def _client(tmp_path) -> LSPClient:
    return LSPClient(
        server_id="test",
        workspace_root=str(tmp_path),
        command=["true"],
    )


def _running(client: LSPClient) -> None:
    """Fake a running server so notifications are attempted."""
    client._state = "running"
    client._proc = SimpleNamespace(
        returncode=None,
        stdin=SimpleNamespace(is_closing=lambda: False),
        stdout=None,
    )
    client._reader_task = SimpleNamespace(done=lambda: False)


def _record_notifications(client: LSPClient) -> list:
    sent: list = []

    async def record(method, params):
        sent.append((method, params))

    client._send_notification = record  # type: ignore[method-assign]
    return sent


# ----------------------------------------------------------------------
# close_file
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_file_sends_didclose_and_drops_state(tmp_path):
    client = _client(tmp_path)
    _running(client)
    sent = _record_notifications(client)
    path = os.path.abspath(str(tmp_path / "a.py"))
    client._docs[path] = _DocState(version=3, text="x = 1\n")

    await client.close_file(path)

    assert path not in client._docs
    assert sent == [
        (
            "textDocument/didClose",
            {"textDocument": {"uri": client_mod.file_uri(path)}},
        )
    ]


@pytest.mark.asyncio
async def test_close_file_spillover_entry_skips_didclose(tmp_path):
    """Never-opened entries (version < 0) are dropped silently — the
    server never had them open, so didClose would be a protocol error."""
    client = _client(tmp_path)
    _running(client)
    sent = _record_notifications(client)
    path = os.path.abspath(str(tmp_path / "spill.py"))
    client._docs[path] = _DocState(version=-1)

    await client.close_file(path)

    assert path not in client._docs
    assert sent == []
    # Unknown path: no-op, no exception.
    await client.close_file(str(tmp_path / "missing.py"))


# ----------------------------------------------------------------------
# LRU eviction via open_file
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_file_evicts_lru_with_didclose(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_OPEN_DOCS", 2)
    client = _client(tmp_path)
    _running(client)
    sent = _record_notifications(client)

    paths = {}
    for name in ("a", "b", "c", "d"):
        p = tmp_path / f"{name}.py"
        p.write_text(f"{name} = 1\n", encoding="utf-8")
        paths[name] = os.path.abspath(str(p))

    await client.open_file(paths["a"])
    await client.open_file(paths["b"])
    # Third open exceeds the cap → oldest (a) is evicted with didClose.
    await client.open_file(paths["c"])
    # Re-open b: didChange path must move it to the MRU end...
    await client.open_file(paths["b"])
    # ...so this open evicts c, not b.
    await client.open_file(paths["d"])

    closed = [
        params["textDocument"]["uri"]
        for method, params in sent
        if method == "textDocument/didClose"
    ]
    assert closed == [
        client_mod.file_uri(paths["a"]),
        client_mod.file_uri(paths["c"]),
    ]
    assert set(client._docs) == {paths["b"], paths["d"]}
    assert len(client._docs) <= 2


# ----------------------------------------------------------------------
# spillover trimming
# ----------------------------------------------------------------------


def test_publish_diagnostics_spillover_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_OPEN_DOCS", 4)
    client = _client(tmp_path)

    # Three legitimately opened documents…
    opened = []
    for name in ("a", "b", "c"):
        p = os.path.abspath(str(tmp_path / f"{name}.py"))
        client._docs[p] = _DocState(version=0, text="")
        opened.append(p)

    # …then a chatty server pushes diagnostics for many never-opened files.
    spill = [os.path.abspath(str(tmp_path / f"spill{i}.py")) for i in range(6)]
    for p in spill:
        client._handle_publish_diagnostics(
            {"uri": client_mod.file_uri(p), "diagnostics": []}
        )
        assert len(client._docs) <= 4
        # The entry for the push we just handled must survive its own trim.
        assert p in client._docs

    # Opened docs are never sacrificed for spillover.
    for p in opened:
        assert p in client._docs


@pytest.mark.asyncio
async def test_related_documents_spillover_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_OPEN_DOCS", 4)
    client = _client(tmp_path)
    target = os.path.abspath(str(tmp_path / "main.py"))

    related = {
        client_mod.file_uri(os.path.abspath(str(tmp_path / f"rel{i}.py"))): {
            "kind": "full",
            "items": [],
        }
        for i in range(8)
    }

    async def fake_request(method, params, *, timeout):
        assert method == "textDocument/diagnostic"
        return {"kind": "full", "items": [], "relatedDocuments": related}

    client._send_request_with_retry = fake_request  # type: ignore[method-assign]
    await client._pull_document_diagnostics(target)

    assert len(client._docs) <= 4
    # The pulled document itself is protected from its own trim.
    assert target in client._docs


# ----------------------------------------------------------------------
# dispatch-task strong references
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_request_dispatch_holds_strong_ref(tmp_path, monkeypatch):
    client = _client(tmp_path)
    client._proc = SimpleNamespace(returncode=None, stdin=None, stdout=object())

    messages = [
        {"jsonrpc": "2.0", "id": 7, "method": "workspace/workspaceFolders"},
        None,  # server closes stdout
    ]

    async def fake_read_message(stream):
        await asyncio.sleep(0)
        return messages.pop(0)

    monkeypatch.setattr(client_mod, "read_message", fake_read_message)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_dispatch(req_id, msg):
        assert req_id == 7
        started.set()
        await release.wait()

    monkeypatch.setattr(client, "_dispatch_request", slow_dispatch)

    reader = asyncio.create_task(client._reader_loop())
    await asyncio.wait_for(started.wait(), timeout=2)

    # In-flight dispatch must be strongly referenced so GC can't drop it
    # (the event loop itself only keeps a weak reference).
    assert len(client._pending_dispatch) == 1
    task = next(iter(client._pending_dispatch))
    gc.collect()
    assert len(client._pending_dispatch) == 1 and not task.done()

    release.set()
    await asyncio.wait_for(task, timeout=2)
    await asyncio.wait_for(reader, timeout=2)
    await asyncio.sleep(0)  # let the done-callback discard run
    assert not client._pending_dispatch


@pytest.mark.asyncio
async def test_cleanup_process_cancels_pending_dispatch(tmp_path):
    client = _client(tmp_path)
    hung = asyncio.create_task(asyncio.sleep(3600))
    client._pending_dispatch.add(hung)

    await client._cleanup_process()

    assert hung.cancelled()
    assert not client._pending_dispatch
