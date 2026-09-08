import json
from pathlib import Path

import pytest

from hermes_services import conversation_history as store


def key(item, index):
    return item['id']


def test_migration_upsert_preserves_order_history_and_original_backup(tmp_path, monkeypatch):
    path = tmp_path / 'history.json'
    base = dict(version=1, conversation_id='c', owner_id='owner', account_generation='g',
        messages=[{'id': str(i), 'content': 'x' * 1200} for i in range(1000)], session_entries=[])
    path.write_text(json.dumps(base), encoding='utf-8')
    original = path.read_bytes()
    assert store.active(path)
    tail = dict(base, messages=[{'id': '999', 'content': 'changed'}, {'id': '1000', 'content': 'new'}])
    assert store.merge(path, tail, item_key=key)['message_count'] == 1001
    assert path.read_bytes() == original
    real_read = Path.read_text
    def reject_old_json(self, *args, **kwargs):
        if self == path:
            pytest.fail('A subsequent append reread the complete historical JSON')
        return real_read(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', reject_old_json)
    assert store.merge(path, tail, item_key=key)['message_count'] == 1001
    result = store.read(path)
    assert result['messages'][:-2] == base['messages'][:-1]
    assert result['messages'][-2:] == tail['messages']


def test_generation_change_and_explicit_rewrite_cannot_resurrect_rows(tmp_path):
    path = tmp_path / 'history.json'
    base = dict(conversation_id='c', owner_id='owner', account_generation='g',
        messages=[{'id': 'a'}, {'id': 'b'}], session_entries=[{'id': 'entry'}])
    store.merge(path, base, item_key=key)
    store.merge(path, dict(base, messages=[{'id': 'b'}]), item_key=key, replace_messages=True)
    assert store.read(path)['messages'] == [{'id': 'b'}]
    assert store.read(path)['session_entries'] == [{'id': 'entry'}]
    store.merge(path, dict(base, account_generation='new', messages=[{'id': 'new'}], session_entries=[]), item_key=key)
    result = store.read(path)
    assert result['account_generation'] == 'new'
    assert result['messages'] == [{'id': 'new'}] and result['session_entries'] == []


def test_failed_migration_is_atomic_and_retryable(tmp_path):
    path = tmp_path / 'history.json'
    base = dict(conversation_id='c', owner_id='owner', account_generation='g',
        messages=[{'id': 'old'}], session_entries=[])
    path.write_text(json.dumps(base), encoding='utf-8')
    def fail(item, index):
        if item['id'] == 'new':
            raise RuntimeError('interrupted')
        return item['id']
    with pytest.raises(RuntimeError):
        store.merge(path, dict(base, messages=[{'id': 'new'}]), item_key=fail)
    assert store.read(path) is None
    assert store.merge(path, dict(base, messages=[{'id': 'new'}]), item_key=key)['message_count'] == 2
