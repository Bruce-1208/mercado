from unittest.mock import Mock

import pytest
from bit import bit_interface as web, bit_db_api


@pytest.mark.parametrize('user', [{}, {'permissions': ['*']},
    {'role_key': 'admin', 'permissions': ['*']}, {'role_key': 'member'},
    {'access_version': 1, 'is_platform_admin': True, 'permissions': ['*']}])
def test_only_explicit_super_admin_controls_capacity(user):
    assert not web.workbench_user_can_change_task_workers(user)


def test_shared_settings_permissions_and_roundtrip(monkeypatch):
    user = {'role_key': 'super_admin', 'permissions': ['*'], 'access_version': 1}
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: user)
    state = {'workers': 16}
    def setting(value=None):
        if value is not None:
            state['workers'] = value
        return state['workers']
    monkeypatch.setattr(bit_db_api, 'publish_workers', setting)
    client = web.app.test_client()
    assert client.put('/api/publish-concurrency', json={'worker_count': 7}).get_json()['data']['worker_count'] == 7
    assert client.get('/api/publish-concurrency').get_json()['data']['worker_count'] == 7
    user['role_key'] = 'admin'
    assert client.put('/api/publish-concurrency', json={'worker_count': 9}).status_code == 403
    assert client.get('/api/publish-concurrency').status_code == 403
    assert state['workers'] == 7


def test_ui_status_hides_capacity_without_mutating_task_commands(monkeypatch):
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: {'role_key': 'member'})
    payload = {'data': {'worker_count': 7, 'message': '准备使用 7 个线程上架 20 件产品',
        'logs': ['并发进程数：7', '上架成功'], 'payload': {'worker_count': 7}}}
    with web.app.test_request_context('/api/mercado-products/publish/status'):
        data = web.hide_task_capacity_from_non_admin(web.jsonify(payload)).get_json()['data']
    assert 'worker_count' not in data
    assert '线程' not in data['message']
    assert '20 件产品' in data['message']
    assert data['logs'][1] == '上架成功'
    assert data['payload']['worker_count'] == 7
    with web.app.test_request_context('/api/local-agents/poll'):
        assert web.hide_task_capacity_from_non_admin(web.jsonify(payload)).get_json() == payload
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: {'role_key': 'super_admin'})
    with web.app.test_request_context('/api/mercado-products/publish/status'):
        assert web.hide_task_capacity_from_non_admin(web.jsonify(payload)).get_json() == payload


def test_client_uses_central_settings(monkeypatch):
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'http')
    request = Mock(return_value={'worker_count': 7})
    monkeypatch.setattr(bit_db_api, '_request', request)
    assert bit_db_api.publish_workers() == 7
    request.assert_called_with('GET', '/api/db/publish-concurrency')
    assert bit_db_api.publish_workers(7) == 7
    request.assert_called_with('PUT', '/api/db/publish-concurrency', json={'worker_count': 7})


@pytest.mark.parametrize('role', ['super_admin', 'admin', 'member', 'custom'])
def test_all_roles_publish_with_shared_default(monkeypatch, role):
    import test_bit_interface_mercado_collection as existing
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: {
        'id': 9, 'username': 'tester', 'role_key': role, 'permissions': ['*'],
        'access_version': 1, 'is_platform_admin': True,
    })
    shared = Mock(return_value=7)
    monkeypatch.setattr(bit_db_api, 'publish_workers', shared)
    existing.test_group_publish_endpoint_applies_strategy_and_historical_account_ownership()
    shared.assert_called_once_with()


def test_setting_persists_across_connections(monkeypatch, tmp_path):
    import sqlite3
    from erp.publish_concurrency import publish_workers
    from bit.bit_mysql import pymysql
    path = tmp_path / 'settings.sqlite'
    class Connection:
        def __init__(self):
            self.db = sqlite3.connect(path)
        def cursor(self):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, sql, args=()):
            if 'CREATE TABLE' in sql:
                sql = 'CREATE TABLE IF NOT EXISTS workbench_publish_concurrency (id INTEGER PRIMARY KEY, workers INTEGER)'
            elif 'INSERT INTO' in sql:
                sql = 'INSERT OR REPLACE INTO workbench_publish_concurrency VALUES (1, ?)'
            self.result = self.db.execute(sql, args)
        def fetchone(self):
            row = self.result.fetchone()
            return {'workers': row[0]} if row else None
        def commit(self):
            self.db.commit()
        def close(self):
            self.db.close()
    monkeypatch.setattr(pymysql, 'connect', lambda **kwargs: Connection())
    assert publish_workers() == 16
    assert publish_workers(7) == 7
    assert publish_workers() == 7
    for invalid in [0, -1, True, 1.5, 'bad']:
        with pytest.raises((ValueError, TypeError)):
            publish_workers(invalid)
    assert publish_workers() == 7
