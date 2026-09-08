"""Incremental storage for conversation sidecars that outgrow a JSON file."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def active(json_path: Path) -> bool:
    return json_path.with_suffix('.sqlite').exists() or (
        json_path.exists() and json_path.stat().st_size >= 1024 * 1024)


def _open(json_path: Path):
    path = json_path.with_suffix('.sqlite')
    if path.is_symlink():
        raise ValueError('Conversation history database must not be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    path.chmod(0o600)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=FULL')
    conn.execute('PRAGMA secure_delete=ON')
    conn.execute('CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS items (position INTEGER PRIMARY KEY, kind TEXT NOT NULL, item_key TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(kind,item_key))')
    return conn


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _summary(conn, metadata):
    result = dict(metadata)
    for kind, field in (('messages', 'message_count'), ('session_entries', 'session_entry_count')):
        result[field] = conn.execute('SELECT COUNT(*) FROM items WHERE kind=?', (kind,)).fetchone()[0]
    last = conn.execute("SELECT value FROM items WHERE kind='messages' ORDER BY position DESC LIMIT 1").fetchone()
    result['last_message'] = json.loads(last[0]) if last else None
    return result


def merge(json_path: Path, incoming: dict, *, item_key, replace_messages=False) -> dict:
    """Commit changed rows and return a small index; never read old rows again.

    The legacy JSON remains a migration backup. Its rows are imported in the
    same transaction as the first update; a killed migration is safe to retry.
    Account boundaries are part of the database metadata, not inferred by ID.
    """
    conn = _open(json_path)
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            stored = conn.execute('SELECT value FROM meta WHERE id=1').fetchone()
            metadata = json.loads(stored[0]) if stored else {}
            identity = ('conversation_id', 'owner_id', 'account_generation')
            current = {key: str(incoming.get(key) or '') for key in identity}
            current.update(version=1, updated_at=incoming.get('updated_at', 0))
            sources = []
            if not stored and json_path.exists():
                legacy = json.loads(json_path.read_text(encoding='utf-8'))
                if all(legacy.get(key, '') == current[key] for key in identity):
                    sources.append(legacy)
            elif stored and any(metadata.get(key, '') != current[key] for key in identity):
                conn.execute('DELETE FROM items')
            if replace_messages:
                conn.execute("DELETE FROM items WHERE kind='messages'")
            sources.append(incoming)
            for source in sources:
                for kind in ('messages', 'session_entries'):
                    if replace_messages and source is not incoming and kind == 'messages':
                        continue
                    rows = ((kind, item_key(item, index), _encode(item))
                            for index, item in enumerate(source.get(kind) or []) if isinstance(item, dict))
                    conn.executemany('INSERT INTO items(kind,item_key,value) VALUES(?,?,?) '
                        'ON CONFLICT(kind,item_key) DO UPDATE SET value=excluded.value WHERE value<>excluded.value', rows)
            conn.execute('INSERT INTO meta(id,value) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value', (_encode(current),))
            return _summary(conn, current)
    finally:
        conn.close()


def read(json_path: Path) -> dict | None:
    path = json_path.with_suffix('.sqlite')
    if not path.exists():
        return None
    conn = _open(json_path)
    try:
        with conn:
            conn.execute('BEGIN')
            row = conn.execute('SELECT value FROM meta WHERE id=1').fetchone()
            if not row:  # interrupted first migration; the JSON is still authoritative
                return None
            result = _summary(conn, json.loads(row[0]))
            for kind in ('messages', 'session_entries'):
                result[kind] = [json.loads(row[0]) for row in conn.execute(
                    'SELECT value FROM items WHERE kind=? ORDER BY position', (kind,))]
            return result
    finally:
        conn.close()
