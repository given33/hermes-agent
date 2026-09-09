from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

import pytest

from deploy.dbb3 import dbb3_cloud_connector as connector_module


def test_rest_reuses_connection_and_rewinds_upload_after_token_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    token = tmp_path / 'token'
    token.write_text('old')
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            calls.append((self.client_address, self.headers['Authorization'], body,
                          self.request_version, self.headers.get('Connection')))
            status = 200
            if self.path == '/rotate' and self.headers['Authorization'] == 'Bearer old':
                token.write_text('new')
                status = 401
            self.send_response(status)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = connector_module.CloudRelayClient(f'http://127.0.0.1:{server.server_port}', 'old', token_path=token)
    upload = tmp_path / 'upload.bin'
    upload.write_bytes(b'upload-body' * 10000)
    try:
        client._request('/first', method='POST', payload={'check': 'pool'})
        client._request('/rotate', method='POST', body_path=upload)
        client._request('/last', method='POST', payload={'check': 'done'})
        assert len(calls) == 4
        assert len({call[0] for call in calls}) == 1, [(*call[:2], *call[3:]) for call in calls]
        assert calls[1][2] == calls[2][2] == upload.read_bytes()
        assert calls[2][1] == calls[3][1] == 'Bearer new'
        assert json.loads(calls[0][2]) == {'check': 'pool'}
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dropped_post_is_not_silently_retried():
    import httpx
    calls = []
    client = connector_module.CloudRelayClient('https://example.test', 'test')
    client._http.close()

    def disconnect(request):
        calls.append(request)
        raise httpx.ReadError('connection lost', request=request)

    client._http = httpx.Client(transport=httpx.MockTransport(disconnect))
    try:
        with pytest.raises(connector_module.urllib.error.URLError):
            client._request('/mutate', method='POST', payload={'claim': 'one'})
        assert len(calls) == 1
    finally:
        client.close()


def test_completed_artifact_failures_back_off_without_blocking_new_pulls(tmp_path, monkeypatch):
    pulls = []
    checks = []
    cloud = SimpleNamespace(pull_runs=lambda **kw: pulls.append('pull') or [],
                            pull_cancellations=lambda **kw: [])
    connector = connector_module.DBB3CloudConnector(cloud, state_file=tmp_path / 'state.json')
    connector.checkpoints.save({'runs': {'old': {'remote_run_id': 'old', 'acked': True,
        'root_task_id': 'task-old', 'status': 'completed', 'artifact_paths': ['/missing/output.md']}}})
    monkeypatch.setattr(connector, '_upload_artifacts',
        lambda *args: checks.append('check') or (0, False, ['missing'], False))
    try:
        connector.sync_once()
        connector.sync_once()
        assert pulls == ['pull', 'pull']
        assert checks == ['check']
        state = connector.checkpoints.load()['runs']['old']
        assert state['artifact_errors'] == ['missing']
        assert not state.get('artifacts_synced')
    finally:
        connector.close()


def test_missing_artifacts_do_not_make_a_network_request(tmp_path):
    cloud = SimpleNamespace(list_run_attachments=lambda _: pytest.fail('Missing files cannot be uploaded'))
    connector = connector_module.DBB3CloudConnector(cloud, state_file=tmp_path / 'state.json')
    try:
        count, complete, errors, transient = connector._upload_artifacts('old', {}, ['/missing/output.md'], {})
        assert count == 0 and not complete and errors and not transient
    finally:
        connector.close()


@pytest.mark.parametrize('once', [True, False])
def test_terminal_handoff_has_no_fixed_wait_and_honors_once(monkeypatch, capsys, once):
    calls = []
    closed = []

    class Connector:
        _wake_event = SimpleNamespace(wait=lambda **kw: pytest.fail('Handoff must poll immediately'))

        def __init__(self, *args, **kwargs):
            pass

        def sync_once(self):
            calls.append('sync')
            if len(calls) == 1:
                return {'terminal_pushed': 1}
            if once:
                pytest.fail('--once must stop after a terminal checkpoint')
            raise connector_module.ConnectorAuthError(401, 'stop')

        def close(self):
            closed.append(True)

    monkeypatch.setattr(connector_module, '_load_token', lambda path: 'test')
    monkeypatch.setattr(connector_module, 'CloudRelayClient', lambda *a, **kw: object())
    monkeypatch.setattr(connector_module, 'DBB3CloudConnector', Connector)
    monkeypatch.setattr(connector_module.time, 'sleep', lambda *a: pytest.fail('Fixed sleep delays handoff'))
    args = ['--cloud-url', 'https://example.test', '--quiet'] + (['--once'] if once else [])
    assert connector_module.main(args) == (0 if once else 78)
    assert len(calls) == (1 if once else 2)
    assert closed == [True]
    assert json.loads(capsys.readouterr().out.splitlines()[0])['terminal_pushed'] == 1


def test_terminal_on_pulled_run_wakes_next_stage_without_counting_other_statuses(tmp_path, monkeypatch):
    cloud = SimpleNamespace(pull_runs=lambda **kw: [{'remote_run_id': 'new'}],
                            pull_cancellations=lambda **kw: [])
    connector = connector_module.DBB3CloudConnector(cloud, state_file=tmp_path / 'state.json')
    connector.checkpoints.save({'runs': {'old': {'acked': True, 'root_task_id': 'old-task', 'status': 'running'}}})

    def accept(payload, state):
        state['runs']['new'] = {'acked': True, 'root_task_id': 'new-task', 'status': 'running'}

    def sync(remote_id, *_args):
        connector._last_reported_terminal = remote_id == 'new'
        return 1, 0

    monkeypatch.setattr(connector, '_accept_run', accept)
    monkeypatch.setattr(connector, '_sync_local_run', sync)
    try:
        result = connector.sync_once()
        assert result['terminal_pushed'] == 1
        assert result['statuses'] == 2
    finally:
        connector.close()


def test_batch_dispatch_precedes_status_and_upload_work(tmp_path, monkeypatch):
    order = []
    cloud = SimpleNamespace(pull_runs=lambda **kw: [{'remote_run_id': 'one'}, {'remote_run_id': 'two'}],
                            pull_cancellations=lambda **kw: [])
    connector = connector_module.DBB3CloudConnector(cloud, state_file=tmp_path / 'state.json')

    def accept(payload, state):
        remote_id = payload['remote_run_id']
        order.append(('dispatch', remote_id))
        state['runs'][remote_id] = {'acked': True, 'root_task_id': remote_id, 'status': 'running'}

    def sync(remote_id, *_args):
        order.append(('sync', remote_id))
        return 1, 0

    connector.checkpoints.save({'runs': {}})
    monkeypatch.setattr(connector, '_accept_run', accept)
    monkeypatch.setattr(connector, '_sync_local_run', sync)
    try:
        connector.sync_once()
        assert order == [('dispatch', 'one'), ('dispatch', 'two'), ('sync', 'one'), ('sync', 'two')]
    finally:
        connector.close()


def test_pull_consumes_old_wake_but_preserves_new_notification(tmp_path):
    def pull(**kwargs):
        assert not connector._wake_event.is_set()
        connector._wake_event.set()
        return []

    cloud = SimpleNamespace(pull_runs=pull, pull_cancellations=lambda **kw: [])
    connector = connector_module.DBB3CloudConnector(cloud, state_file=tmp_path / 'state.json')
    try:
        connector._wake_event.set()
        connector.sync_once()
        assert connector._wake_event.is_set()
    finally:
        connector.close()


def test_new_assignment_interrupts_artifact_batch_and_retry_keeps_uploaded_files(tmp_path):
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    first.write_text('first')
    second.write_text('second')
    uploads = []
    lists = []

    def upload(remote_id, **kwargs):
        uploads.append(kwargs['path'])
        connector._wake_event.set()

    cloud = SimpleNamespace(list_run_attachments=lambda _: lists.append('list') or [], upload_artifact=upload)
    connector = connector_module.DBB3CloudConnector(cloud, state_file=tmp_path / 'state.json', artifact_roots=[tmp_path])
    local = {'remote_run_id': 'old', 'root_task_id': 'old-task'}
    state = {'runs': {'old': local}}
    paths = [str(first), str(second)]
    try:
        connector._wake_event.set()
        assert connector._upload_artifacts('old', local, paths, state) == (0, False, [], True)
        assert lists == uploads == []
        connector._wake_event.clear()
        assert connector._upload_artifacts('old', local, paths, state) == (1, False, [], True)
        assert uploads == [first]
        connector._wake_event.clear()
        assert connector._upload_artifacts('old', local, paths, state) == (1, True, [], False)
        assert uploads == [first, second]
    finally:
        connector.close()
