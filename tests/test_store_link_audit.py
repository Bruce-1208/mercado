from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask, jsonify, session
from erp import store_link_audit as audit


def test_durable_results_failures_redaction_and_pagination():
    @audit.audited
    def operation(token, row):
        if row == 'bad':
            raise ValueError('invalid row')
        return {'status': 'partial', 'errors': ['remote rejected price']}

    operation({'access_token': 'SECRET', 'refresh_token': 'SECRET'}, {'remote_json': '{"access_token":"SECRET"}'})
    with pytest.raises(ValueError):
        operation({}, 'bad')
    rows = audit.history()
    assert len(rows) == 4
    assert rows[0]['phase'] == 'failed'
    assert rows[2]['phase'] == 'partial'
    assert 'SECRET' not in str(rows)
    assert rows[0]['operation_id'] == rows[1]['operation_id']
    assert len(audit.history(before_id=rows[1]['id'])) == 2
    assert audit.history(limit=1)[0] == rows[0]


def test_concurrent_workers_keep_request_identity():
    @audit.audited
    def worker(link_id):
        return {'link_id': link_id}
    token = audit._context.set({'trace_id': 'trace-1', 'actor': {'id': 7, 'username': 'operator'}})
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(audit.contextual_target(worker), n) for n in range(20)]
            for future in futures:
                future.result()
    finally:
        audit._context.reset(token)
    rows = audit.history(trace_id='trace-1')
    assert len(rows) == 40
    assert all(row['details']['context']['actor']['id'] == 7 for row in rows)
    assert audit._context.get() is None


def test_http_failures_and_upload_metadata_without_file_contents():
    import io
    app = Flask(__name__)
    app.secret_key = 'test'
    audit.install_request_audit(app)
    @app.post('/api/store-links/1/video')
    def upload():
        return jsonify(status='error', message='rejected'), 422
    client = app.test_client()
    with client.session_transaction() as state:
        state['workbench_user'] = {'id': 9, 'username': 'tester', 'password': 'SECRET'}
    response = client.post('/api/store-links/1/video', data={'video': (io.BytesIO(b'PRIVATE_CONTENT'), 'demo.mp4')})
    assert response.status_code == 422
    rows = audit.history(trace_id=response.headers['X-Store-Link-Trace-Id'])
    assert [r['phase'] for r in rows] == ['failed', 'started']
    assert rows[1]['details']['files']['video'][0]['filename'] == 'demo.mp4'
    assert 'PRIVATE_CONTENT' not in str(rows)
    assert 'SECRET' not in str(rows)
    assert audit._context.get() is None


def test_no_side_effect_if_start_record_cannot_be_written(monkeypatch):
    calls = []
    @audit.audited
    def operation():
        calls.append(True)
    def broken(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(audit, 'record', broken)
    with pytest.raises(OSError):
        operation()
    assert calls == []
    assert audit._context.get() is None


def test_forwarded_identity_requires_internal_authentication(monkeypatch):
    app = Flask(__name__)
    monkeypatch.setenv('BIT_DB_API_TOKEN', 'shared-secret')
    token = audit._context.set({'trace_id': 'a' * 32, 'actor': {'id': 42, 'username': 'operator'}})
    try:
        headers = audit.forwarding_headers()
    finally:
        audit._context.reset(token)
    with app.test_request_context('/api/db/store-links/delete', headers=headers):
        from flask import request
        assert audit.trusted_forwarded_context(request) is None
    headers['X-Internal-Token'] = 'shared-secret'
    with app.test_request_context('/api/db/store-links/delete', headers=headers):
        assert audit.trusted_forwarded_context(request)['actor']['id'] == 42
    with app.test_request_context('/api/store-links/delete', headers=headers):
        assert audit.trusted_forwarded_context(request) is None


def test_history_endpoint_requires_platform_admin(monkeypatch):
    from bit import bit_interface as workbench
    app = workbench.app
    app.config.update(TESTING=True, SECRET_KEY='test')
    client = app.test_client()
    user = {'id': 1, 'username': 'member', 'permissions': ['*'], 'is_platform_admin': False}
    monkeypatch.setattr(workbench, 'get_current_workbench_user', lambda: user)
    with client.session_transaction() as state:
        state['workbench_user'] = user
    assert client.get('/api/store-links/operation-logs').status_code == 403
    user['is_platform_admin'] = True
    audit.record('test', 'completed', {'result': 'ok'})
    response = client.get('/api/store-links/operation-logs')
    assert response.status_code == 200
    assert response.json['data'][0]['action'] == 'test'
    assert client.get('/api/store-links/operation-logs?limit=bad').status_code == 400
    audit.record('delete_store_links', 'completed', {})
    filtered = client.get('/api/store-links/operation-logs?operation_type=delete')
    assert filtered.status_code == 200
    assert [r['action'] for r in filtered.json['data']] == ['delete_store_links']
    assert client.get('/api/store-links/operation-logs?operation_type=invalid').status_code == 400


@pytest.mark.parametrize('action,path,method,category', [
    ('replace_store_snapshot', None, None, 'sync'),
    ('_update_one_link', None, None, 'update'),
    ('delete_store_links', None, None, 'delete'),
    ('mark_advertising_status', None, None, 'advertising'),
    ('upload_store_link_video', None, None, 'video'),
    ('http_request', '/api/db/store-links/sync/start', 'POST', 'sync'),
    ('http_request', '/api/store-links/bulk-update', 'POST', 'update'),
    ('http_request', '/api/store-links/delete', 'POST', 'delete'),
    ('http_request', '/api/store-links/1/advertise', 'POST', 'advertising'),
    ('http_request', '/api/store-links/bulk-advertise', 'POST', 'advertising'),
    ('http_request', '/api/db/store-links/7/video', 'POST', 'video'),
    ('http_request', '/api/store-links/bulk-update/status', 'GET', 'query'),
    ('http_request', '/api/store-links/category-paths', 'POST', 'query'),
    ('unknown_future_operation', None, None, 'other'),
])
def test_operation_type_includes_existing_http_and_worker_records(action, path, method, category):
    audit.record(action, 'completed', {'context': {'path': path, 'method': method}})
    rows = audit.history(operation_type=category)
    assert len(rows) == 1
    assert rows[0]['operation_type'] == category
    assert rows[0]['action'] == action
    for other_type in audit.OPERATION_TYPES:
        if other_type != category:
            assert audit.history(operation_type=other_type) == []


def test_operation_filter_precedes_pagination():
    for n in range(8):
        audit.record('delete_store_links', 'completed', {'index': n})
        audit.record('upload_store_link_video', 'completed', {})
    first = audit.history(operation_type='delete', limit=3)
    second = audit.history(operation_type='delete', limit=3, before_id=first[-1]['id'])
    assert [row['details']['index'] for row in first + second] == [7, 6, 5, 4, 3, 2]
    assert len(audit.history(limit=500)) == 16
    with pytest.raises(ValueError):
        audit.history(operation_type="delete' OR 1=1 --")
