import importlib.util
from pathlib import Path
from contextlib import nullcontext
from copy import deepcopy


def test_live_role_wakes_do_not_rewrite_history_but_expiring_lease_renews(monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'plugins/collaboration/dashboard/plugin_api.py'
    spec = importlib.util.spec_from_file_location('collaboration_lease_latency', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    role = {'status': 'running', 'execution_owner': 'owner', 'lease_expires_at': 220000}
    state = {'conversations': [{'id': 'conversation', 'hosted_turns': {
        'turn': {'status': 'running', 'active_roles': {'worker': role}},
    }}]}
    saves = []
    reads = []
    monkeypatch.setattr(module, '_STATE_LOCK', nullcontext())
    monkeypatch.setattr(module.time, 'time', lambda: 100)
    monkeypatch.setattr(module, '_load_single_state_for_event_stream', lambda: state)
    def writable():
        reads.append(1)
        return deepcopy(state)
    monkeypatch.setattr(module, 'load_single_state', writable)
    monkeypatch.setattr(module, 'save_single_state', saves.append)
    for _ in range(100):
        assert module._renew_hosted_active_role('conversation', 'turn', role_stage='worker', execution_owner='owner')
    assert not saves and not reads
    role['lease_expires_at'] = 130000
    assert module._renew_hosted_active_role('conversation', 'turn', role_stage='worker', execution_owner='owner')
    assert len(saves) == len(reads) == 1
    assert saves[0]['conversations'][0]['hosted_turns']['turn']['active_roles']['worker']['lease_expires_at'] == 220000
    assert role['lease_expires_at'] == 130000  # immutable cached read was not changed
    assert not module._renew_hosted_active_role('conversation', 'turn', role_stage='worker', execution_owner='other')
