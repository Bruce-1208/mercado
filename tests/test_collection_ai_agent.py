from contextlib import nullcontext
import threading

import pytest

from bit import collection_ai_workflow as workflow
from erp import collection_ai_check as repository
from erp.ai_weight_price import collection_runner
from erp.ai_weight_price.models import Models
from erp.ai_weight_price.service import Service


@pytest.fixture
def collection_api(monkeypatch, tmp_path):
    from bit import bit_interface as web
    from bit.local_agent_hub import LocalAgentStore
    user = {'id': 7, 'username': 'tester', 'role_key': 'employee', 'is_active': True,
            'permissions': ['ai_weight_price.view', 'ai_weight_price.execute'], 'access_version': 1}
    hub = LocalAgentStore(tmp_path / 'hub.sqlite3')
    hub.heartbeat('agent-office', name='Office', capabilities=['ai_weight_price'])
    service = Service(tmp_path / 'awp')
    service.bind_actor(user)
    service.exchange_rate = lambda _: {'cny_per_usd': '7', 'date': '2026-09-27', 'source': 'test'}
    monkeypatch.setattr(web, 'USE_DB_API', False)
    monkeypatch.setattr(web.app, 'testing', True)
    monkeypatch.setattr(web.app, 'secret_key', 'collection-test')
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: user)
    monkeypatch.setattr(web, 'get_workbench_user', lambda **_: user)
    monkeypatch.setattr(web, 'build_workbench_session_user', lambda row: dict(row))
    monkeypatch.setattr(web, 'get_local_agent_store', lambda: hub)
    monkeypatch.setattr(web, 'ai_weight_price_service', service)
    monkeypatch.setattr(web, 'current_local_agent_bundle', lambda: {'version': 'test'})
    monkeypatch.setattr(web.browser_extension_models, 'get_api_key', lambda *_: 'secret-key')
    monkeypatch.setattr(web, '_verify_local_agent_credential', lambda value: {'agent_id': value})
    monkeypatch.setattr(workflow, 'queue_lock', lambda _: nullcontext())
    monkeypatch.setattr(repository, 'products', lambda ids: [
        {'erp_goods_id': f'collection:{i}', 'collection_item_id': i, 'title': 'Product',
         'source_type': 'mercado_collection', 'main_image_url': 'https://http2.mlstatic.com/test.jpg'} for i in ids])
    saved = []
    def save(task, status, reason, changes=None, evidence=None):
        saved.append((task, status, changes, evidence))
        return {'status': status, 'reason': reason}
    monkeypatch.setattr(repository, 'save_result', save)
    return web.app.test_client(), service, hub, user, saved


URL = '/api/mercado-collection/ai-weight-price'


def launch(client, **extra):
    return client.post(URL, json={'collection_item_ids': [1, 2], 'agent_id': 'agent-office', **extra})


def test_default_dispatches_real_agent_job_and_rejects_duplicate(collection_api):
    client, service, hub, user, saved = collection_api
    response = launch(client)
    assert response.status_code == 200, response.get_json()
    assert response.json['data']['execution_target'] == 'agent'
    jobs = hub.list_jobs(job_type='ai_weight_price')
    assert len(jobs) == 1
    assert jobs[0]['payload']['action'] == 'collection-check'
    assert 'secret-key' not in str(jobs[0])
    assert service.store.state('collection_pending') is None
    assert launch(client).status_code == 400
    assert len(hub.list_jobs()) == 1


def test_server_permission_and_admin_dispatch(collection_api, monkeypatch):
    client, service, hub, user, saved = collection_api
    starts = []
    monkeypatch.setattr(workflow, 'start_server', lambda *args: starts.append(args))
    assert launch(client, execution_target='server').status_code == 403
    assert not saved
    user['role_key'] = 'super_admin'
    assert launch(client, execution_target='server').status_code == 200
    assert len(starts) == 1
    assert not hub.list_jobs()
    assert service.store.state('run')['execution_target'] == 'server'


def test_offline_agent_returns_error_and_releases_batch(collection_api):
    client, service, hub, user, saved = collection_api
    assert launch(client, agent_id='agent-missing').status_code == 409
    assert service.store.state('run')['outcome'] == 'stopped'
    assert launch(client).status_code == 200


def test_claim_callback_identity_and_completion(collection_api, monkeypatch):
    client, service, hub, user, saved = collection_api
    assert launch(client).status_code == 200
    job = hub.claim_job('agent-office')
    endpoint = f"/api/local-agents/jobs/{job['job_id']}/collection-check"
    def step(action, payload=None, agent='agent-office'):
        return client.post(endpoint, headers={'X-Internal-Token': agent}, json={'action': action, 'payload': payload or {}})
    assert step('next', agent='agent-other').status_code == 404
    first = step('next').json['data']
    key = first['task']['erp_goods_id']
    assert first['action'] == 'search'
    assert step('detail', {'task_id': 'stale', 'detail': {}}).status_code == 400
    monkeypatch.setattr(Models, 'match_images', lambda *_: ([{'url': 'https://detail.1688.com/offer/1.html'}], []))
    assert step('search', {'task_id': key, 'candidates': [{'url': 'https://detail.1688.com/offer/1.html'}]}).json['data']['action'] == 'detail'
    detail = {'skus': [{'id': 'sku', 'price': 7, 'raw_weight': '80g', 'raw_dimensions': '10x5x2cm'}]}
    second = step('detail', {'task_id': key, 'detail': detail}).json['data']
    assert second['action'] == 'search'
    assert any(row[1] == 'checked' and row[2]['weight_g'] == '80' for row in saved)
    assert client.delete(URL).status_code == 200
    assert hub.get_job(job['job_id'])['cancel_requested']
    assert step('detail', {'task_id': second['task']['erp_goods_id'], 'detail': detail}).json['data']['action'] == 'done'
    assert saved[-1][1] == 'stopped'


def test_dead_agent_is_reported_and_can_restart(collection_api):
    client, service, hub, user, saved = collection_api
    launch(client)
    job = hub.claim_job('agent-office')
    hub.append_event(job['job_id'], 'agent-office', status='error', message='Edge connection failed')
    response = client.get(URL)
    assert response.json['data']['run']['outcome'] == 'failed'
    assert saved[-1][1] == 'failed'
    assert launch(client).status_code == 200


def test_browser_runner_executes_search_detail_without_zying_login(tmp_path, monkeypatch):
    events = []
    class Browser:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def search_images(self, task):
            events.append(('search', task))
            return [{'url': 'https://detail.1688.com/offer/1.html'}]
        def read_offer(self, task, candidate):
            events.append(('detail', task))
            return {'skus': [{'price': 7}]}
        def release_search(self, task): pass
    service = Service(tmp_path, browser_factory=lambda *_: Browser())
    monkeypatch.setattr(collection_runner, 'open_edge', lambda *args: events.append(('open', args)))
    def step(action, payload):
        events.append((action, payload))
        return {'next': {'action': 'search', 'task': {'erp_goods_id': 'collection:1'}},
                'search': {'action': 'detail', 'candidate': {'url': 'offer'}},
                'detail': {'action': 'done'}}[action]
    assert collection_runner.run(service, step, threading.Event()) == {'action': 'done'}
    assert [event[0] for event in events] == ['next', 'open', 'search', 'search', 'detail', 'detail']


def test_browser_launch_failure_records_failure(tmp_path, monkeypatch):
    service = Service(tmp_path)
    calls = []
    def step(action, payload):
        calls.append((action, payload))
        return {'action': 'search', 'task': {'erp_goods_id': 'collection:1'}}
    def broken(*args): raise ValueError('Edge missing')
    monkeypatch.setattr(collection_runner, 'open_edge', broken)
    with pytest.raises(ValueError, match='Edge missing'):
        collection_runner.run(service, step, threading.Event())
    assert calls[-1] == ('fail', {'task_id': 'collection:1', 'action': 'search', 'error': 'Edge missing'})


def test_server_edge_runs_whole_batch_with_actor_scope(collection_api, monkeypatch):
    client, service, hub, user, saved = collection_api
    user['role_key'] = 'super_admin'
    class Browser:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def search_images(self, task):
            return [{'url': 'https://detail.1688.com/offer/1.html'}]
        def read_offer(self, task, candidate):
            return {'skus': [{'id': 'sku', 'price': 7, 'raw_weight': '80g'}]}
        def release_search(self, task): pass
    service.browser_factory = lambda *_: Browser()
    monkeypatch.setattr(collection_runner, 'open_edge', lambda *_: None)
    monkeypatch.setattr(Models, 'match_images', lambda _, task, candidates: (candidates, []))
    assert launch(client, execution_target='server').status_code == 200
    service.thread.join(timeout=5)
    assert not service.thread.is_alive()
    status = client.get(URL).json['data']
    assert status['run']['outcome'] == 'completed'
    assert status['run']['processed_items'] == 2
    assert status['run']['success_items'] == 2
    assert status['running'] is False
    assert len([row for row in saved if row[1] == 'checked']) == 2
    with service.store.actor_scope({'id': 8}):
        assert not service.store.state('run')


def test_dispatch_function_also_restores_existing_browser_actions(collection_api):
    from bit import bit_interface as web
    client, service, hub, user, saved = collection_api
    with web.app.test_request_context():
        response = web.app.make_response(web.enqueue_local_agent_ai_weight_price('login/open', {'agent_id': 'agent-office'}))
    assert response.status_code == 200
    assert hub.list_jobs()[0]['payload']['action'] == 'login/open'


def awp_control(action, **data):
    from bit import bit_interface as web
    with web.app.test_request_context():
        return web.app.make_response(web.enqueue_local_agent_ai_weight_price(
            action, {'agent_id': 'agent-office', **data}))


@pytest.mark.parametrize('outcome', ['running', 'blocked', 'stopped', 'failed', 'completed'])
def test_terminate_orphan_batch_unblocks_login_and_preserves_history(collection_api, outcome):
    client, service, hub, user, saved = collection_api
    store = service.store
    store.add({'erp_goods_id': 'old-product', 'title': 'Old product'})
    store.save_run({'run_id': 'old-run', 'outcome': outcome})
    store.record_run_item('old-run', 'old-product', execution_result='pending')
    store.set_state('run', {'run_id': 'old-run', 'outcome': outcome, 'execution_target': 'agent'})
    store.set_state('login', {'confirmed': True})
    store.set_state('circuit', {'reason': 'login required'})
    store.set_state('pipeline_current', {'erp_goods_id': 'old-product'})
    response = awp_control('terminate')
    assert response.status_code == 200, response.json
    assert response.json['data']['cleared'] is True
    assert store.state('run') == {}
    assert store.state('circuit') is None
    assert store.state('pipeline_current') is None
    assert not store.has_run_item('old-run', 'old-product')
    with pytest.raises(ValueError, match='执行批次不存在'):
        store.run_items('old-run')
    assert store.get('old-product')['title'] == 'Old product'
    assert store.state('login')['confirmed'] is True
    assert service.status()['running'] is False
    assert awp_control('terminate').status_code == 200  # repeat is harmless
    assert awp_control('login/open').status_code == 200
    assert hub.list_jobs()[0]['payload']['action'] == 'login/open'


def test_terminate_orphan_works_when_agent_offline(collection_api):
    client, service, hub, user, saved = collection_api
    hub.heartbeat('agent-office', name='Office', capabilities=['ai_weight_price'], now=1)
    service.store.set_state('run', {'run_id': 'orphan', 'outcome': 'running', 'execution_target': 'agent'})
    assert awp_control('terminate').status_code == 200
    assert service.status()['running'] is False
    assert awp_control('login/open').status_code == 409


def test_terminate_controls_users_job_on_previous_agent(collection_api):
    client, service, hub, user, saved = collection_api
    hub.heartbeat('agent-previous', name='Previous', capabilities=['ai_weight_price'])
    job = hub.enqueue_job('previous-job', 'agent-previous', 'ai_weight_price',
                          {'actor': user, 'action': 'start'}, created_by_id=user['id'])
    service.store.set_state('run', {'run_id': 'previous-run', 'outcome': 'running', 'execution_target': 'agent'})
    assert awp_control('terminate').status_code == 200
    assert hub.get_job(job['job_id'])['cancel_requested']
    assert service.store.state('agent_terminate_requested') == job['job_id']
    assert service.store.state('run') == {}


def test_admin_termination_cleans_job_owner_state_only(collection_api):
    client, service, hub, user, saved = collection_api
    user['role_key'] = 'super_admin'
    service.store.set_state('run', {'run_id': 'admin-batch', 'outcome': 'completed'})
    with service.store.actor_scope({'id': 8}):
        service.store.set_state('run', {'run_id': 'employee-batch', 'outcome': 'blocked'})
    job = hub.enqueue_job('employee-job', 'agent-office', 'ai_weight_price',
                          {'actor': {'id': 8}, 'action': 'start'}, created_by_id=8)
    assert awp_control('terminate').status_code == 200
    assert hub.get_job(job['job_id'])['cancel_requested']
    assert service.store.state('run')['run_id'] == 'admin-batch'
    with service.store.actor_scope({'id': 8}):
        assert service.store.state('run') == {}
        assert service.store.state('agent_terminate_requested') == job['job_id']


def test_employee_cannot_terminate_another_owners_job(collection_api):
    client, service, hub, user, saved = collection_api
    job = hub.enqueue_job('foreign-job', 'agent-office', 'ai_weight_price',
                          {'actor': {'id': 8}, 'action': 'start'}, created_by_id=8)
    assert awp_control('terminate').status_code == 409
    assert not hub.get_job(job['job_id'])['cancel_requested']


def test_terminate_endpoint_clears_stale_running_status(collection_api):
    from flask import Flask
    from bit import bit_interface as web
    from erp.ai_weight_price.web import create_blueprint
    _, service, hub, user, _ = collection_api
    app = Flask(__name__)
    app.secret_key = 'isolated-awp-test'
    app.register_blueprint(create_blueprint(service, authorize=lambda _: None,
                                            agent_dispatch=web.enqueue_local_agent_ai_weight_price))
    client = app.test_client()
    with client.session_transaction() as session:
        session['workbench_user'] = user
    service.store.set_state('run', {'run_id': 'orphan', 'outcome': 'running', 'execution_target': 'agent'})
    assert client.get('/api/ai-weight-price/status').json['running'] is True
    response = client.post('/api/ai-weight-price/terminate',
                           headers={'X-AWP-Request': '1'},
                           json={'execution_target': 'agent', 'agent_id': 'agent-office'})
    assert response.status_code == 200, response.json
    status = client.get('/api/ai-weight-price/status').json
    assert status['running'] is False
    assert status['run'] == {}
    response = client.post('/api/ai-weight-price/login/open',
                           headers={'X-AWP-Request': '1'},
                           json={'execution_target': 'agent', 'agent_id': 'agent-office'})
    assert response.status_code == 200, response.json


def test_terminate_orphan_without_selected_agent(collection_api):
    _, service, _, _, _ = collection_api
    service.store.set_state('run', {'run_id': 'orphan', 'outcome': 'running', 'execution_target': 'agent'})
    assert awp_control('terminate', agent_id='').status_code == 200
    assert service.store.state('run') == {}


@pytest.mark.parametrize('cancelled', [False, True])
def test_start_reaps_expired_worker_without_visiting_agent_list(collection_api, cancelled):
    import time
    _, service, hub, user, _ = collection_api
    old = time.time() - 1800
    hub.enqueue_job('expired-job', 'agent-office', 'ai_weight_price',
                    {'actor': user, 'action': 'start'}, created_by_id=user['id'], now=old)
    hub.claim_job('agent-office', session_id='old-session', now=old)
    if cancelled:
        hub.request_cancel('expired-job', now=old + 1)
    response = awp_control('start', mode='pipeline')
    assert response.status_code == 200, response.json
    assert hub.get_job('expired-job')['status'] == ('stopped' if cancelled else 'error')
    assert response.json['data']['task_id'] != 'expired-job'


def test_start_preserves_live_worker_and_exposes_blocking_job(collection_api):
    _, _, hub, user, _ = collection_api
    hub.enqueue_job('live-job', 'agent-office', 'ai_weight_price',
                    {'actor': user, 'action': 'login/open'}, created_by_id=user['id'])
    hub.claim_job('agent-office', session_id='live-session')
    response = awp_control('start', mode='pipeline')
    assert response.status_code == 409
    assert response.json['data']['blocking_job']['task_id'] == 'live-job'
    assert 'login/open' in response.json['message']
    assert hub.get_job('live-job')['status'] == 'running'


@pytest.mark.parametrize('claimed', [False, True])
def test_terminate_login_job_without_batch(collection_api, claimed):
    _, service, hub, user, _ = collection_api
    hub.enqueue_job('login-only', 'agent-office', 'ai_weight_price',
                    {'actor': user, 'action': 'login/open'}, created_by_id=user['id'])
    if claimed:
        hub.claim_job('agent-office', session_id='login-session')
    assert not service.store.state('run')
    response = awp_control('terminate')
    assert response.status_code == 200, response.json
    job = hub.get_job('login-only')
    assert job['cancel_requested']
    assert job['status'] == ('stopping' if claimed else 'stopped')
