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
