from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from flask import Flask

from bit.local_agent_hub import LocalAgentStore
from erp.ai_weight_price.service import Service
from erp.ai_weight_price.web import create_blueprint


def test_same_account_cannot_enqueue_concurrently_on_two_agents(tmp_path):
    path = tmp_path / 'hub.sqlite3'
    LocalAgentStore(path)
    barrier = Barrier(2)
    def enqueue(index):
        store = LocalAgentStore(path)
        barrier.wait()
        try:
            store.enqueue_job(f'job-{index}', f'agent-{index}', 'ai_weight_price', {}, created_by_id=7)
            return 'ok'
        except ValueError:
            return 'blocked'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(enqueue, range(2))) == ['blocked', 'ok']


def test_different_accounts_can_run_and_finished_account_can_restart(tmp_path):
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    for user in (7, 8):
        store.enqueue_job(f'job-{user}', f'agent-{user}', 'ai_weight_price', {}, created_by_id=user)
    store.request_cancel('job-7')
    assert store.enqueue_job('job-new', 'agent-9', 'ai_weight_price', {}, created_by_id=7)


@pytest.mark.parametrize('role,total', [('enterprise_admin', 2), ('super_admin', 2), ('member', 1)])
def test_admin_reads_all_tasks_employee_reads_own(tmp_path, role, total):
    service = Service(tmp_path)
    for user in (7, 8):
        with service.store.actor_scope({'id': user}):
            service.store.add({'erp_goods_id': f'g-{user}', 'title': f'Product {user}'})
            service.store.log(f'log-{user}')
    app = Flask(__name__)
    app.secret_key = 'test'
    app.register_blueprint(create_blueprint(service, authorize=lambda _: None))
    client = app.test_client()
    with client.session_transaction() as session:
        session['workbench_user'] = {'id': 7, 'role_key': role}
    assert client.get('/api/ai-weight-price/tasks').json['total'] == total
    page = client.get('/ai-weight-price').get_data(as_text=True)
    assert ('所有业务员商品' in page) == (total == 2)
    assert ('id="owner-filter"' in page) == (total == 2)
    assert len(client.get('/api/ai-weight-price/owners').json['options']) == total
    for endpoint in ('tasks', 'logs'):
        selected = client.get(f'/api/ai-weight-price/{endpoint}?owner_user_id=8')
        if total == 1:
            assert selected.status_code == 403
        else:
            assert selected.status_code == 200
            rows = selected.json['rows'] if endpoint == 'tasks' else selected.json
            assert len(rows) == 1
            assert rows[0]['owner_user_id'] == 8
        assert client.get(f'/api/ai-weight-price/{endpoint}?owner_user_id=invalid').status_code == 400
    own = client.get('/api/ai-weight-price/tasks?owner_user_id=7').json
    assert own['total'] == 1
    assert client.get('/api/ai-weight-price/tasks').json['total'] == total



def test_stopping_still_occupies_account_until_worker_exits(tmp_path):
    store = LocalAgentStore(tmp_path / 'hub.sqlite3')
    store.enqueue_job('active', 'agent-a', 'ai_weight_price', {}, created_by_id=7)
    store.claim_job('agent-a', session_id='session')
    store.request_cancel('active')
    with pytest.raises(ValueError, match='每个账号'):
        store.enqueue_job('duplicate', 'agent-b', 'ai_weight_price', {}, created_by_id=7)


def test_shared_agent_cannot_run_two_accounts_at_once(tmp_path):
    store = LocalAgentStore(tmp_path / 'hub.sqlite3')
    store.enqueue_job('first', 'agent-a', 'ai_weight_price', {}, created_by_id=7)
    with pytest.raises(ValueError, match='所选 Agent'):
        store.enqueue_job('second', 'agent-a', 'ai_weight_price', {}, created_by_id=8)
