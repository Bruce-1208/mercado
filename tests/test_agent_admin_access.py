import threading

import pytest

from bit import bit_interface as web
from bit.local_agent_hub import LocalAgentStore
import local_agent_worker

pytestmark = pytest.mark.usefixtures('subscribed_workbench_account')

@pytest.fixture
def console(monkeypatch, tmp_path):
    user = {'id': 12, 'username': 'support', 'display_name': '售后管理员', 'role_key': 'enterprise_admin',
            'is_active': True, 'organization_key': 'company-a', 'access_version': 1, 'permissions': ['*'], 'permissions_json': '["*"]'}
    store = LocalAgentStore(tmp_path / 'hub.sqlite3')
    store.heartbeat('office', name='办公室电脑', owner_user_id=12, capabilities=['daily_task', 'appeal'])
    monkeypatch.setattr(web.app, 'testing', True)
    monkeypatch.setattr(web, 'USE_DB_API', False)
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: user)
    monkeypatch.setattr(web, 'get_workbench_user', lambda **kw: user)
    monkeypatch.setattr(web, 'get_local_agent_store', lambda: store)
    monkeypatch.setattr(web, 'current_local_agent_bundle', lambda: {'version': 'test'})
    monkeypatch.setattr(web.bit_db_api, 'list_mercado_store_tokens', lambda: {'rows': [
        {'id': 9, 'display_name': '授权店铺', 'nickname': 'shop', 'organization_key': 'company-a'},
        {'id': 10, 'display_name': '其他企业店铺', 'organization_key': 'company-b'}]})
    # The web process must never open a local browser for this action.
    monkeypatch.setattr(web, 'openBrowser', lambda *args, **kw: pytest.fail('服务器打开了浏览器'))
    return user, store, web.app.test_client()

@pytest.mark.parametrize('role', ['member', 'operator', 'viewer', 'warehouse'])
@pytest.mark.parametrize('method,path,body', [
    ('GET', '/api/execution-agents', None),
    ('GET', '/api/local-agents/download', None),
    ('POST', '/api/mercado-claims/9/open-browser', {'agent_id': 'office'}),
    ('POST', '/api/reputation/9/open-browser', {}),
    ('GET', '/api/mercado-claims/open-store/task', None),
    ('POST', '/api/mercado/today/rights-holder/9/open-browser', {}),
    ('POST', '/api/tasks/daily/start', {'execution_target': 'agent', 'agent_id': 'office'}),
    ('GET', '/api/tasks/daily/status?execution_target=agent', None),
    ('POST', '/api/ai-weight-price/stop', {'execution_target': 'agent'}),
    ('GET', '/api/orders/zying-sync/status', None),
    ('POST', '/api/orders/zying-sync/stop', {}),
    ('POST', '/api/orders/zying-sync/confirm-login', {}),
    ('DELETE', '/api/mercado-collection/ai-weight-price', None),
])
def test_ordinary_roles_cannot_use_agents_even_with_all_permissions(console, role, method, path, body):
    user, store, client = console
    user.update(role_key=role, is_platform_admin=True)
    response = client.open(path, method=method, json=body)
    assert response.status_code == 403
    assert not store.list_jobs()

@pytest.mark.parametrize('role', ['enterprise_admin', 'super_admin'])
def test_claim_launch_queues_then_reports_agent_result(console, role):
    user, store, client = console
    user['role_key'] = role
    user['is_platform_admin'] = role == 'super_admin'
    response = client.post('/api/mercado-claims/9/open-browser?organization_key=company-a', json={
        'execution_target': 'agent', 'agent_id': 'office', 'claim_id': 'claim-123', 'shop_name': 'shop'})
    assert response.status_code == 202
    job = store.get_job(response.json['data']['task_id'])
    assert job['job_type'] == 'open_store'
    assert job['agent_id'] == 'office'
    assert job['created_by_id'] == 12
    assert job['payload']['destination'] == 'claims'
    assert job['payload']['claim_id'] == 'claim-123'
    assert job['payload']['token_record'] == {'id': 9, 'display_name': '授权店铺', 'nickname': 'shop'}
    endpoint = '/api/mercado-claims/open-store/' + job['job_id'] + '?organization_key=company-a'
    assert client.get(endpoint).json['data']['running']
    store.claim_job('office')
    store.append_event(job['job_id'], 'office', status='success', result={'status': 'success', 'message': '店铺已打开'})
    data = client.get(endpoint).json['data']
    assert not data['running']
    assert data['status'] == 'success'
    assert data['message'] == '店铺已打开'

@pytest.mark.parametrize('body,status', [
    ({'execution_target': 'server'}, 403),
    ({'agent_id': 'office'}, 202),
    ({'agent_id': 'missing'}, 403),
    ({}, 400),
])
def test_claim_never_falls_back_to_server(console, body, status):
    _, _, client = console
    assert client.post('/api/mercado-claims/9/open-browser', json=body).status_code == status


def test_claim_cannot_open_foreign_company(console):
    _, store, client = console
    assert client.post('/api/mercado-claims/10/open-browser', json={'agent_id': 'office'}).status_code in {403, 404}
    assert not store.list_jobs()


def test_role_demotion_revokes_enrollment_and_worker_credentials(console):
    user, _, client = console
    enrollment = web.create_local_agent_enrollment_token(user)
    credential = web.create_local_agent_credential('office', user['id'])
    with web.app.test_request_context():
        assert web._local_agent_enrollment_user(enrollment)
        assert web._verify_local_agent_credential(credential)
    user['role_key'] = 'member'
    with web.app.test_request_context():
        assert web._local_agent_enrollment_user(enrollment) is None
        assert web._verify_local_agent_credential(credential) is None
        with pytest.raises(ValueError):
            web.create_local_agent_enrollment_token(user)
    assert client.post('/api/local-agents/enroll', headers={'Authorization': 'Bearer ' + enrollment},
                       json={'agent_id': 'new'}).status_code == 401
    assert client.post('/api/local-agents/heartbeat', headers={'X-Local-Agent-Token': credential},
                       json={'agent_id': 'office'}).status_code == 401


def test_claim_worker_opens_local_bitbrowser_without_reputation_redirect(monkeypatch):
    calls = []
    monkeypatch.setattr(web, '_open_mercado_claim_browser', lambda token_id, **kw:
                        calls.append((token_id, kw)) or {'shop_name': '授权店铺', 'window_id': 'local-window'})
    result = local_agent_worker.run_open_store({'destination': 'claims', 'token_id': 9,
        'token_record': {'id': 9, 'display_name': '授权店铺'}, 'claim_id': 'claim-123'}, threading.Event())
    assert calls[0][1]['target_url'] == ''
    assert calls[0][1]['token_record']['display_name'] == '授权店铺'
    assert result['claim_id'] == 'claim-123'
    assert result['status'] == 'success'


def test_member_cannot_cancel_agent_through_business_task_id(console, monkeypatch):
    user, store, client = console
    user['role_key'] = 'member'
    store.enqueue_job('old-job', 'office', 'ai_weight_price', {}, created_by_id=user['id'])
    def stop_execute(*args, **kwargs):
        kwargs['agent_cancel']('old-job')
    monkeypatch.setattr(web.weight_dimensions_records, 'stop_execute', stop_execute)
    response = client.post('/api/weight-dimensions-records/business-task/stop')
    assert response.status_code == 403
    assert not store.get_job('old-job')['cancel_requested']


@pytest.mark.parametrize('role,allowed', [('member', False), ('operator', False), ('enterprise_admin', True), ('super_admin', True)])
def test_weight_price_page_only_offers_agent_to_admins(role, allowed):
    from flask import Flask
    from types import SimpleNamespace
    from erp.ai_weight_price.web import create_blueprint
    app = Flask(__name__)
    app.secret_key = 'test'
    app.register_blueprint(create_blueprint(SimpleNamespace(bind_actor=lambda *args, **kwargs: None),
        authorize=lambda permission: None, agent_dispatch=lambda *args: {}, server_execution=True))
    client = app.test_client()
    with client.session_transaction(base_url='https://workbench.test') as session:
        session['workbench_user'] = {'id': 12, 'role_key': role}
    response = client.get('/ai-weight-price', base_url='https://workbench.test')
    assert response.status_code == 200
    assert ('id="execution-agent"' in response.text) == allowed
    if not allowed:
        denied = client.post('/api/ai-weight-price/login/open', base_url='https://workbench.test',
                             json={'execution_target': 'agent', 'agent_id': 'office'}, headers={'X-AWP-Request': '1'})
        assert denied.status_code == 403
