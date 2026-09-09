def test_assignment_rechecks_idle_tool_permissions(monkeypatch, tmp_path):
    import model_tools
    from hermes_services.worker_prewarm import _bind_task_environment
    from tools.registry import invalidate_check_fn_cache
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    (tmp_path / 'config.yaml').write_text('toolsets: []\n')
    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()
    idle = model_tools.get_tool_definitions(enabled_toolsets=['kanban'], quiet_mode=True)
    assert not any(t['function']['name'] == 'kanban_complete' for t in idle)
    # monkeypatch owns cleanup of the environment changed by the real handoff.
    monkeypatch.setenv('HERMES_KANBAN_TASK', '')
    _bind_task_environment({'HERMES_KANBAN_TASK': 't_assigned'})
    assigned = model_tools.get_tool_definitions(enabled_toolsets=['kanban'], quiet_mode=True)
    names = {t['function']['name'] for t in assigned}
    assert {'kanban_show', 'kanban_complete', 'kanban_block'} <= names
    assert 'kanban_unblock' not in names
    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()


def test_prewarm_signature_uses_content_hash_not_mtime(tmp_path):
    import os
    from hermes_services.worker_prewarm import _signature
    config = tmp_path / "config.yaml"
    config.write_text("model: original\n")
    before = _signature(str(tmp_path))
    os.utime(config, ns=(config.stat().st_atime_ns, config.stat().st_mtime_ns + 1_000_000))
    assert _signature(str(tmp_path)) == before
    config.write_text("model: modified\n")
    assert _signature(str(tmp_path)) != before


def test_cold_fallback_preserves_inflight_prewarm(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from hermes_services import worker_prewarm as runtime
    pool = runtime.WorkerPrewarmPool()
    proc = SimpleNamespace(poll=lambda: None)
    spare = {'proc': proc, 'ready': False, 'signature': runtime._signature(str(tmp_path))}
    pool._spares[str(tmp_path)] = [spare]
    child = object()
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *a, **k: child)
    monkeypatch.setattr(pool, '_replenish', lambda *a: None)
    with (tmp_path/'worker.log').open('ab') as log:
        assert pool.launch(['hermes', '-p', 'test'], env={'HERMES_HOME': str(tmp_path)}, stdout=log) is child
    assert pool._spares[str(tmp_path)] == [spare]


def test_ready_reserve_is_used_while_other_spare_is_preparing(monkeypatch, tmp_path):
    import io
    import time
    from types import SimpleNamespace
    from hermes_services import worker_prewarm as runtime
    pool = runtime.WorkerPrewarmPool()
    proc = SimpleNamespace(poll=lambda: None, pid=123, stdin=io.BytesIO())
    pending = {'proc': proc, 'ready': False, 'signature': runtime._signature(str(tmp_path))}
    ready = {**pending, 'ready': True, 'created_at': time.monotonic(), 'profile': 'test', 'board': 'test'}
    pool._spares[str(tmp_path)] = [pending, ready]
    monkeypatch.setattr(pool, '_replenish', lambda *a: None)
    with (tmp_path/'worker.log').open('ab') as log:
        assert pool.launch(['hermes', '-p', 'test'], env={'HERMES_HOME': str(tmp_path)}, stdout=log) is proc
    assert pool._spares[str(tmp_path)] == [pending]


def test_failed_handoff_replenishes_reserve_without_duplicate_task(monkeypatch, tmp_path):
    import time
    from types import SimpleNamespace
    from hermes_services import worker_prewarm as runtime

    def broken_write(packet):
        raise BrokenPipeError('spare exited')

    proc = SimpleNamespace(poll=lambda: None, pid=123, stdin=SimpleNamespace(write=broken_write))
    pool = runtime.WorkerPrewarmPool()
    spare = {'proc': proc, 'ready': True, 'signature': runtime._signature(str(tmp_path)),
             'created_at': time.monotonic(), 'profile': 'test', 'board': 'test'}
    pool._spares[str(tmp_path)] = [spare]
    replacements = []
    monkeypatch.setattr(pool, '_replenish', lambda *args: replacements.append(args))
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *a, **kw: __import__('pytest').fail('Duplicate task'))
    with (tmp_path / 'worker.log').open('ab') as log:
        assert pool.launch(['hermes', '-p', 'test'], env={'HERMES_HOME': str(tmp_path)}, stdout=log) is proc
    assert replacements == [(proc, 'test', 'test')]
    assert pool._spares[str(tmp_path)] == []
