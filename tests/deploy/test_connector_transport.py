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
