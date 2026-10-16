import pytest
from flask import Flask, jsonify
from bit import service_split


@pytest.mark.parametrize('path', [
    '/api/mercado-products', '/api/db/mercado-products',
    '/api/mercado-collection/items', '/api/db/mercado-collection/items',
    '/api/tasks/daily/options', '/api/size-charts/templates',
    '/api/size-charts/options', '/api/size-charts/records',
    '/api/risk-check/categories', '/api/risk-check/options', '/api/risk-check/results',
])
def test_catalog_read_never_contacts_busy_worker(monkeypatch, path):
    monkeypatch.setenv('BIT_SERVICE_MODE', 'web')
    def unexpected_worker():
        raise AssertionError('catalog read contacted worker')
    monkeypatch.setattr(service_split.requests, 'Session', unexpected_worker)
    app = Flask(__name__)
    service_split.install_service_routing(app)
    app.add_url_rule(path, view_func=lambda: jsonify(rows=[{'id': 1}]))
    response = app.test_client().get(path)
    assert response.status_code == 200
    assert response.json == {'rows': [{'id': 1}]}
    assert not service_split.is_worker_path(path, 'HEAD')


@pytest.mark.parametrize('path,method', [
    ('/api/mercado-products', 'DELETE'),
    ('/api/mercado-collection/items', 'DELETE'),
    ('/api/db/mercado-collection/items', 'POST'),
    ('/api/mercado-products/publish', 'POST'),
    ('/api/mercado-products/publish/status', 'GET'),
    ('/api/mercado-publish-records', 'GET'),
    ('/api/mercado-publish-records/retry', 'POST'),
    ('/api/mercado/infringement/tasks', 'GET'),
])
def test_catalog_mutations_and_tasks_still_use_worker(path, method):
    assert service_split.is_worker_path(path, method)


@pytest.mark.parametrize('isolated', ['0', '1'])
def test_publish_record_refresh_reads_worker_results_in_split_mode(monkeypatch, isolated):
    import json
    from unittest.mock import Mock

    monkeypatch.setenv('BIT_SERVICE_MODE', 'web')
    monkeypatch.setenv('BIT_WORKER_ISOLATION', isolated)
    monkeypatch.setenv('BIT_WORKER_PORT', '5001')
    app = Flask(__name__)
    app.secret_key = 'offline-test'
    service_split.install_service_routing(app)
    path = '/api/mercado-publish-records'
    app.add_url_rule(path, view_func=lambda: pytest.fail('read empty web process state'))
    state = {'running': False, 'published_count': 1, 'failed_count': 1,
             'logs': ['成功 · SKU-1 · MLB900', '失败 · SKU-2 · 图片不合规']}
    upstream = Mock(status_code=200, headers={'Content-Type': 'application/json'})
    upstream.raw.headers.getlist.return_value = []
    upstream.iter_content.return_value = [json.dumps({
        'status': 'success', 'data': {'publish_state': state},
    }).encode()]
    transport = Mock()
    transport.request.return_value = upstream
    monkeypatch.setattr(service_split.requests, 'Session', lambda: transport)

    response = app.test_client().get(path + '?search=retry-test')
    assert response.json['data']['publish_state'] == state
    assert transport.request.call_args.args[:2] == (
        'GET', 'http://127.0.0.1:5001/api/mercado-publish-records?search=retry-test',
    )
    assert transport.trust_env is False
