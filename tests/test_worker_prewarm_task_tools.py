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
