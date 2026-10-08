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
    ('/api/mercado/infringement/tasks', 'GET'),
])
def test_catalog_mutations_and_tasks_still_use_worker(path, method):
    assert service_split.is_worker_path(path, method)
