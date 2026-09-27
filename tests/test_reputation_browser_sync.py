import inspect

import pytest

from bit import mercado_reputation as api
from bit import bit_reputation_info, reputation_browser_sync as browser


@pytest.mark.parametrize('active_index,missing,expected', [(99, False, 'active'), (None, False, 'inactive'),
                                                          (None, True, None), (0, True, 'active')])
def test_latest_hundred_statuses(monkeypatch, active_index, missing, expected):
    calls = []
    def fetch(_token, path, *, params, **kwargs):
        calls.append((path, dict(params)))
        if path.endswith('/search'):
            assert params['orders'] == 'start_time_desc'
            assert 'status' not in params
            return {'results': [f'MLM{i}' for i in range(params['offset'], params['offset'] + params['limit'])],
                    'paging': {'total': 500}}
        ids = params['ids'].split(',')
        assert len(ids) <= 20
        return [{'code': 200, 'body': {'id': value, 'status': 'active' if value == f'MLM{active_index}' else 'paused'}}
                for value in ids if not missing or value != 'MLM99']
    monkeypatch.setattr(api, '_fetch_json', fetch)
    result = api._sample_site_listing_status('token', 'site-seller', http=None, timeout=5)
    assert result['listing_sample_status'] == expected
    assert result['listing_sample_requested_count'] == 100
    assert [params['offset'] for path, params in calls if path.endswith('/search')] == [0, 50]
    assert all('site-seller' in path for path, _ in calls if path.endswith('/search'))


@pytest.mark.parametrize('total,expected', [(0, None), (3, 'inactive')])
def test_fewer_than_hundred_and_empty(monkeypatch, total, expected):
    def fetch(_token, path, **kwargs):
        if path.endswith('/search'):
            return {'results': [f'MLM{i}' for i in range(total)], 'paging': {'total': total}}
        return [{'id': f'MLM{i}', 'status': 'closed'} for i in range(total)]
    monkeypatch.setattr(api, '_fetch_json', fetch)
    result = api._sample_site_listing_status('token', 'seller', http=None, timeout=5)
    assert result['listing_sample_status'] == expected


def test_listing_status_clears_previous_ban_days():
    row = {'account_status': 'restricted', 'site_status_display': '永久封禁（已 10 天）',
           'suspension_days': 10, 'suspension_until': '2027-01-01'}
    api._apply_listing_sample_status(row, {'listing_sample_status': 'active'})
    assert row['account_status'] == 'normal'
    assert row['site_status_display'] == '正常'
    assert row['suspension_days'] is None
    assert row['suspension_until'] == ''
    api._apply_listing_sample_status(row, {'listing_sample_status': 'inactive'})
    assert row['account_status'] == 'restricted'
    assert '封禁天数未确认' in row['site_status_display']


def test_api_update_does_not_open_browser_by_default():
    assert inspect.signature(bit_reputation_info.get_reputation_info_all).parameters['collect_browser_auxiliary'].default is False
    row = bit_reputation_info._api_reputation_database_row('店铺', {}, 'now')
    assert row[9] is None  # Preserve browser-owned warnings.


def test_browser_persist_rejects_foreign_scope(monkeypatch):
    from bit import bit_mysql
    calls = []
    monkeypatch.setattr(bit_mysql, 'update_reputation_browser_fields', calls.append)
    with pytest.raises(ValueError):
        browser.persist({'rows': [{'store_name': '其他店铺', 'site': '巴西', 'visits': '[1]'}]},
                        [['window', '店铺', '', '墨西哥']])
    assert calls == []


@pytest.fixture
def console(monkeypatch, tmp_path):
    from bit import bit_interface as web, bit_config
    from bit.local_agent_hub import LocalAgentStore
    user = {'id': 17, 'username': 'operator', 'role_key': 'employee', 'permissions': ['reputation.view', 'reputation.execute'],
            'access_version': 1, 'is_active': True}
    monkeypatch.setattr(web, 'USE_DB_API', False)
    monkeypatch.setattr(web.app, 'testing', True)
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: user)
    monkeypatch.setattr(web, 'get_workbench_user', lambda **kwargs: user)
    monkeypatch.setattr(web, 'build_workbench_session_user', lambda row: dict(row))
    monkeypatch.setattr(web, '_parse_collection_request', lambda data, **kwargs: {
        'selected_shops': data['shops'], 'selected_sites': data.get('sites', []), 'max_workers': 1})
    monkeypatch.setattr(web, '_filter_mercado_tokens_for_user', lambda data: data)
    monkeypatch.setattr(web.bit_db_api, 'list_mercado_store_tokens', lambda: {'rows': [{'display_name': '店铺', 'site_settings': [{'site_id': 'MLM', 'reputation_update_enabled': True}]}]})
    monkeypatch.setattr(bit_config, 'list_config_rows', lambda **kwargs: [('w', '店铺', '', '墨西哥')])
    monkeypatch.setattr(web, 'current_local_agent_bundle', lambda: {'version': 'test'})
    store = LocalAgentStore(tmp_path / 'agent.sqlite3')
    monkeypatch.setattr(web, 'get_local_agent_store', lambda: store)
    store.heartbeat('test-agent', name='Agent', capabilities=['daily_task'], business_version='test', session_id='s')
    return user, store, web.app.test_client()


def test_server_requires_super_admin(console):
    user, store, client = console
    response = client.post('/api/reputation/browser-sync', json={'shops': ['店铺'], 'execution_target': 'server'})
    assert response.status_code == 403
    assert not store.list_jobs()


def test_agent_dispatch_and_status_ownership(console):
    user, store, client = console
    response = client.post('/api/reputation/browser-sync', json={'shops': ['店铺'], 'execution_target': 'agent', 'agent_id': 'test-agent'})
    assert response.status_code == 200, response.json
    task_id = response.json['data']['task_id']
    job = store.get_job(task_id)
    assert job['job_type'] == 'reputation_browser_sync'
    assert job['payload']['configs'] == [['', '店铺', '', '墨西哥', ['店铺']]]
    assert client.get(f'/api/reputation/browser-sync/{task_id}').json['data']['running']
    user['id'] = 18
    assert client.get(f'/api/reputation/browser-sync/{task_id}').status_code == 403


def test_browser_database_patch_only_updates_owned_columns(monkeypatch):
    from bit import bit_mysql
    class Cursor:
        rowcount = 1
        calls = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params): self.calls.append((sql, params))
    cursor = Cursor()
    class Connection:
        def cursor(self): return cursor
        def commit(self): pass
        def close(self): pass
        def rollback(self): pass
    monkeypatch.setattr(bit_mysql.pymysql, 'connect', lambda **kwargs: Connection())
    monkeypatch.setattr(bit_mysql, '_latest_reputation_snapshot_rows', lambda c: [
        {'店铺名': '店铺', '站点': '墨西哥', '提交时间': 'now'}])
    bit_mysql.update_reputation_browser_fields([{'store_name': '店铺', 'site': '墨西哥', 'system_warning': '正常',
                                               'account_status': 'normal', 'site_status_display': '正常'}])
    assert len(cursor.calls) == 1
    sql, params = cursor.calls[0]
    assert '`系统告警` = %s' in sql
    assert '站点状态' not in sql and '一周流量趋势' not in sql
    assert params == ['正常', '店铺', '墨西哥', 'now']


def test_agent_completion_persists_without_browser_poll(console, monkeypatch):
    from bit import bit_interface as web
    user, store, client = console
    response = client.post('/api/reputation/browser-sync', json={'shops': ['店铺'], 'execution_target': 'agent', 'agent_id': 'test-agent'})
    task_id = response.json['data']['task_id']
    store.claim_job('test-agent', session_id='s')
    calls = []
    monkeypatch.setattr(browser, 'persist', lambda result, configs: calls.append((result, configs)))
    credential = web.create_local_agent_credential('test-agent', user['id'])
    result = {'status': 'success', 'rows': [{'store_name': '店铺', 'site': '墨西哥', 'system_warning': '正常'}]}
    for _ in range(2):
        response = client.post(f'/api/local-agents/jobs/{task_id}/events',
                               headers={'X-Local-Agent-Token': credential},
                               json={'agent_id': 'test-agent', 'status': 'success', 'result': result})
        assert response.status_code == 200, response.json
    assert len(calls) == 1
    assert store.get_job(task_id)['status'] == 'success'


def test_server_super_admin_collects_and_persists(console, monkeypatch):
    from bit import bit_interface as web
    user, _, client = console
    user['role_key'] = 'super_admin'
    monkeypatch.setattr(web, '_reputation_browser_jobs', {})
    calls = []
    result = {'rows': [], 'status': 'partial', 'message': '一个站点失败'}
    monkeypatch.setattr(browser, 'collect', lambda configs: result)
    monkeypatch.setattr(browser, 'persist', lambda result, configs: calls.append(configs))
    class ImmediateThread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(web.threading, 'Thread', ImmediateThread)
    response = client.post('/api/reputation/browser-sync', json={'shops': ['店铺'], 'execution_target': 'server'})
    assert response.status_code == 200, response.json
    state = client.get('/api/reputation/browser-sync/' + response.json['data']['task_id']).json['data']
    assert state == {'running': False, 'status': 'partial', 'message': '一个站点失败'}
    assert len(calls) == 1


def test_warning_failure_preserves_traffic_and_does_not_return_account_status(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(browser, 'open_site', lambda *args, **kwargs: None)
    monkeypatch.setattr(bit_reputation_info, '_extract_visits_from_metrics_api', lambda *args: [{'visits': i} for i in range(8)])
    monkeypatch.setattr(bit_reputation_info, '_extract_account_risk_page_text', lambda *args: '')
    monkeypatch.setattr(bit_reputation_info, '_get_account_risk_links', lambda *args: [])
    row = browser.collect_site(SimpleNamespace(page=object()), '店铺', '墨西哥')
    assert row['visits'] == '[0, 1, 2, 3, 4, 5, 6, 7]'
    assert 'system_warning' not in row
    assert 'account_status' not in row and 'site_status_display' not in row
    assert '系统告警' in row['error']


def test_browser_window_is_resolved_on_execution_host(monkeypatch):
    from bit import bit_api
    opened = []
    monkeypatch.setattr(bit_api, 'listBrowsers', lambda: [{'name': '店铺', 'id': 'local-window'}])
    class Lease:
        def acquire(self, **kwargs): return True
        def release(self): pass
    monkeypatch.setattr(browser, 'create_window_lease', lambda window, **kwargs: Lease())
    class Session:
        def __init__(self, window, **kwargs): opened.append(window)
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(browser, 'BitPlaywrightSession', Session)
    monkeypatch.setattr(browser, 'collect_site', lambda session, name, site: {'store_name': name, 'site': site, 'error': ''})
    result = browser.collect([['', '店铺', '', '墨西哥', ['店铺']]])
    assert opened == ['local-window']
    assert result['status'] == 'success'
