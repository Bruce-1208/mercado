import threading
import time

from flask import Flask, jsonify, session

from bit.session_security import install_session_security
from bit.analysis_jobs import background_analysis, _JOBS
from bit import order_read_cache
from bit.bit_ad_analysis import _filtered_snapshot
from bit.mercado_action_center import order_task_entry_url


def security_app(tmp_path):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['WORKBENCH_HTTPS_ORIGIN'] = ''
    app.config['WORKBENCH_SESSION_DB'] = str(tmp_path / 'sessions.sqlite3')
    install_session_security(app)
    @app.post('/api/login')
    def login():
        session.clear()
        session['workbench_user'] = {'id': 1}
        return jsonify(ok=True)
    @app.get('/api/account/profile')
    def profile():
        return jsonify(ok=True), 200 if session.get('workbench_user') else 401
    @app.post('/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)
    return app


def test_logout_revokes_replayed_cookie_across_instances(tmp_path):
    app = security_app(tmp_path)
    client = app.test_client()
    response = client.post('/api/login', base_url='https://example.test')
    cookie = client.get_cookie('session', domain='example.test').value
    assert 'Secure;' in response.headers['Set-Cookie']
    assert 'HttpOnly;' in response.headers['Set-Cookie']
    assert 'SameSite=Lax' in response.headers['Set-Cookie']
    assert client.get('/api/account/profile', base_url='https://example.test').status_code == 200
    client.post('/logout', base_url='https://example.test')
    replay = security_app(tmp_path).test_client()
    replay.set_cookie('session', cookie, domain='example.test')
    assert replay.get('/api/account/profile', base_url='https://example.test').status_code == 401


def test_public_http_redirect_and_local_exception(tmp_path):
    client = security_app(tmp_path).test_client()
    response = client.post('/api/login', base_url='http://example.test', headers={'X-Forwarded-Proto': 'https'})
    assert response.status_code == 308
    assert response.headers['Location'] == 'https://example.test/api/login'
    assert client.post('/api/login', base_url='http://localhost').status_code == 200


def test_legacy_signed_cookie_cannot_skip_registry(tmp_path):
    app = security_app(tmp_path)
    signed = app.session_interface.get_signing_serializer(app).dumps({'workbench_user': {'id': 1}})
    client = app.test_client()
    client.set_cookie('session', signed, domain='example.test')
    assert client.get('/api/account/profile', base_url='https://example.test').status_code == 401


def test_async_job_deduplicates_and_keeps_user_scope():
    _JOBS.clear()
    app = Flask(__name__)
    app.secret_key = 'jobs'
    gate = threading.Event()
    calls = []
    @app.get('/analysis')
    @background_analysis
    def analysis():
        calls.append(session['workbench_user']['id'])
        gate.wait(2)
        return jsonify(status='success', data=calls[-1])
    client = app.test_client()
    with client.session_transaction() as state:
        state['workbench_user'] = {'id': 7}
    headers = {'X-Analysis-Async': '1'}
    assert client.get('/analysis', headers=headers).status_code == 202
    assert client.get('/analysis', headers=headers).status_code == 202
    gate.set()
    for _ in range(100):
        response = client.get('/analysis', headers=headers)
        if response.status_code != 202:
            break
        time.sleep(.01)
    assert response.json['data'] == 7
    assert calls == [7]
    with client.session_transaction() as state:
        state['workbench_user'] = {'id': 8}
    client.get('/analysis', headers=headers)
    for job in list(_JOBS.values()):
        job[1].result(timeout=2)
    assert calls == [7, 8]


def test_ads_paginate_before_enriching_and_keep_total(monkeypatch):
    images = []
    monkeypatch.setattr('bit.ad_images.enrich', lambda links: images.extend(links))
    monkeypatch.setattr('bit.ad_profit.enrich', lambda data: data)
    snapshot = {'accounts': [{'token_id': 1}, {'token_id': 2}], 'links': [
        {'token_id': store, 'currency_id': 'USD', 'item_id': str(i), 'metrics': {'cost': 1}} for store in (1, 2) for i in range(250)
    ]}
    result = _filtered_snapshot(snapshot, [1], page=2, page_size=100)
    assert len(images) == 100
    assert result['total'] == 250 and result['pages'] == 3
    assert result['summary']['cost'] == 250
    assert result['links'][0]['item_id'] == '100'
    assert all(row['token_id'] == 1 for row in result['links'])
    images.clear()
    result = _filtered_snapshot(snapshot, [1], page=1, summary_only=True)
    assert not images and not result['links'] and result['total'] == 250
    assert len(snapshot['links']) == 500


def test_order_cache_scope_invalidation_and_copy():
    order_read_cache.invalidate()
    calls = []
    def loader(**params):
        calls.append(params)
        return {'rows': [{'id': len(calls)}]}
    first = order_read_cache.read(loader, {'allowed_store_ids': [1]})
    first['rows'].clear()
    assert order_read_cache.read(loader, {'allowed_store_ids': [1]})['rows']
    order_read_cache.read(loader, {'allowed_store_ids': [2]})
    assert len(calls) == 2
    order_read_cache.invalidate()
    order_read_cache.read(loader, {'allowed_store_ids': [1]})
    assert len(calls) == 3


def test_task_link_carries_order_and_store():
    assert order_task_entry_url({'entry_url': '/?tab=orders', 'resource': '/orders/123', 'token_id': 9}) == '/?tab=orders&order_id=123&store_id=9'
