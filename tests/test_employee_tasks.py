from datetime import datetime
from pathlib import Path
import sqlite3

import pytest
from flask import Flask

from bit import employee_tasks as module


@pytest.fixture
def harness(tmp_path, monkeypatch):
    path = tmp_path / 'tasks.sqlite'
    raw = sqlite3.connect(path)
    raw.executescript('''
        CREATE TABLE workbench_users (id INTEGER PRIMARY KEY, organization_key TEXT,
          is_active INTEGER, username TEXT, display_name TEXT);
        INSERT INTO workbench_users VALUES (1, 'alpha', 1, 'admin', '管理员'),
          (2, 'alpha', 1, 'sales', '业务员'), (3, 'beta', 1, 'other', '其他企业'),
          (4, 'alpha', 0, 'inactive', '停用'), (5, 'alpha', 1, 'colleague', '同事');
        CREATE TABLE workbench_employee_tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, organization_key TEXT, title TEXT, content TEXT,
          starts_at TIMESTAMP, due_at TIMESTAMP, assignee_id INTEGER, created_by INTEGER,
          is_completed INTEGER DEFAULT 0, completion_note TEXT, completed_at TIMESTAMP,
          created_at TIMESTAMP, updated_at TIMESTAMP);
    ''')
    raw.close()
    monkeypatch.setattr(module, 'ensure_tables', lambda cursor: None)

    class Cursor:
        def __init__(self, raw): self.raw = raw.cursor()
        def __enter__(self): return self
        def __exit__(self, *args): self.raw.close()
        def execute(self, sql, params=()):
            self.raw.execute(sql.replace('%s', '?').replace(' FOR UPDATE', ''),
                tuple(value.isoformat(' ') if isinstance(value, datetime) else value for value in params))
        def convert(self, row):
            if row is None:
                return None
            result = dict(row)
            for key in ('starts_at', 'due_at', 'completed_at', 'created_at', 'updated_at'):
                if result.get(key):
                    result[key] = datetime.fromisoformat(result[key])
            return result
        def fetchone(self): return self.convert(self.raw.fetchone())
        def fetchall(self): return [self.convert(row) for row in self.raw.fetchall()]
        @property
        def lastrowid(self): return self.raw.lastrowid

    class Connection:
        def __init__(self):
            self.raw = sqlite3.connect(path)
            self.raw.row_factory = sqlite3.Row
        def cursor(self): return Cursor(self.raw)
        def commit(self): self.raw.commit()
        def rollback(self): self.raw.rollback()
        def close(self): self.raw.close()

    user = {'id':1, 'role_key':'enterprise_admin', 'organization_key':'alpha', 'view':True}
    app = Flask(__name__, template_folder=str(Path(__file__).parents[1] / 'bit/templates'))
    app.register_blueprint(module.create_employee_tasks_blueprint(
        login_required=lambda f:f, current_user=lambda:user,
        has_permission=lambda u,p:u.get('view', False),
        selected_organization=lambda u: __import__('flask').request.args.get('organization_key', ''),
        storage=lambda payload:module.employee_tasks_local(payload, connect=Connection)))
    return app.test_client(), user


def fields(**changes):
    return dict(title='核对订单', content='核对今日订单并填写结果', starts_at='2020-01-01T09:00',
                due_at='2020-01-02T18:00', assignee_id=2) | changes


def test_create_feedback_and_reopen(harness):
    client, user = harness
    created = client.post('/api/employee-tasks', json=fields())
    assert created.status_code == 200
    task_id = created.json['data']['id']
    data = client.get('/api/employee-tasks').json['data']
    assert {u['id'] for u in data['assignees']} == {1, 2, 5}
    assert data['rows'][0]['overdue'] is True
    assert data['rows'][0]['starts_at'].endswith('+08:00')
    user.update(id=2, role_key='operator')
    assert client.get('/api/employee-tasks').json['data']['assignees'] == []
    url = f'/api/employee-tasks/{task_id}'
    assert client.patch(url, json={'is_completed':False, 'completion_note':'已核对一半'}).status_code == 200
    assert client.patch(url, json={'is_completed':True, 'completion_note':'全部核对完成'}).status_code == 200
    row = client.get('/api/employee-tasks').json['data']['rows'][0]
    assert row['is_completed'] and row['completed_at'] and not row['overdue']
    assert row['completion_note'] == '全部核对完成'
    assert client.patch(url, json={'is_completed':False, 'completion_note':'需要复核'}).status_code == 200
    assert client.get('/api/employee-tasks').json['data']['rows'][0]['completed_at'] is None


def test_tenant_and_assignee_boundaries(harness):
    client, user = harness
    assert client.post('/api/employee-tasks', json=fields(assignee_id=3)).status_code == 400
    assert client.post('/api/employee-tasks', json=fields(assignee_id=4)).status_code == 400
    task_id = client.post('/api/employee-tasks?organization_key=beta', json=fields()).json['data']['id']
    url = f'/api/employee-tasks/{task_id}'
    assert client.get('/api/employee-tasks').json['data']['rows'][0]['organization_key'] == 'alpha'
    assert client.patch(url, json={'is_completed':True}).status_code == 400
    user.update(id=3, organization_key='beta')
    assert client.get('/api/employee-tasks').json['data']['rows'] == []
    assert client.put(url, json=fields(assignee_id=3)).status_code == 400
    user.update(id=5, organization_key='alpha', role_key='operator')
    assert client.get('/api/employee-tasks').json['data']['rows'] == []
    assert client.patch(url, json={'is_completed':True, 'actor_id':2}).status_code == 400
    assert client.post('/api/employee-tasks', json=fields()).status_code == 403
    assert client.put(url, json=fields()).status_code == 403
    assert client.get('/employee-tasks').status_code == 403


def test_future_task_and_validation(harness):
    client, user = harness
    for body in (fields(title=' '), fields(content=''), fields(due_at='bad'),
                 fields(due_at='2019-01-01T00:00'), fields(starts_at='2020-01-01T09:00Z'),
                 fields(assignee_id=True), fields(title='a'*201)):
        assert client.post('/api/employee-tasks', json=body).status_code == 400
    assert client.post('/api/employee-tasks', json=['bad']).status_code == 400
    task_id = client.post('/api/employee-tasks', json=fields(starts_at='2099-01-01T09:00',due_at='2099-01-02T18:00')).json['data']['id']
    user.update(id=2, role_key='operator')
    assert client.get('/api/employee-tasks').json['data']['rows'][0]['not_started']
    assert client.patch(f'/api/employee-tasks/{task_id}', json={'is_completed':True}).status_code == 400
    user['view'] = False
    assert client.get('/api/employee-tasks').status_code == 403


def test_edit_reassignment_and_platform_scope(harness):
    client, user = harness
    task_id = client.post('/api/employee-tasks', json=fields()).json['data']['id']
    url = f'/api/employee-tasks/{task_id}'
    user.update(id=2, role_key='operator')
    for body in ({'is_completed':'false'}, {'is_completed':True,'completion_note':'x'*2001}):
        assert client.patch(url, json=body).status_code == 400
    client.patch(url, json={'is_completed':True,'completion_note':'完成'})
    user.update(id=1, role_key='enterprise_admin')
    assert client.put(url, json=fields(title='修改标题')).status_code == 200
    assert client.get('/api/employee-tasks').json['data']['rows'][0]['is_completed']
    assert client.put(url, json=fields(assignee_id=5)).status_code == 200
    row = client.get('/api/employee-tasks').json['data']['rows'][0]
    assert not row['is_completed'] and row['completion_note'] == '' and row['completed_at'] is None
    assert client.get('/api/employee-tasks?mine=1').json['data']['rows'] == []
    user['role_key'] = 'super_admin'
    assert client.post('/api/employee-tasks?organization_key=beta', json=fields(assignee_id=3)).status_code == 200
    assert len(client.get('/api/employee-tasks?organization_key=beta').json['data']['rows']) == 1


def test_management_page(harness):
    client, user = harness
    page = client.get('/employee-tasks')
    assert page.status_code == 200
    assert 'data-mode="manage"' in page.text
    assert 'employee-tasks.js' in page.text


def test_registered_app_login_and_rpc_scope(monkeypatch):
    from bit import bit_db_api, bit_interface
    monkeypatch.setattr(bit_interface.app, 'testing', True)
    monkeypatch.setattr(bit_interface, 'get_current_workbench_user', lambda: None)
    client = bit_interface.app.test_client()
    assert client.get('/api/employee-tasks').status_code == 401
    user = {'id': 2, 'role_key': 'operator', 'organization_key': 'alpha',
            'permissions': ['tasks.view'], 'access_version': 1}
    monkeypatch.setattr(bit_interface, 'get_current_workbench_user', lambda: user)
    calls = []
    def rpc(operation, payload):
        calls.append((operation, payload))
        return {'rows': [], 'assignees': [], 'actor_id': 2}
    monkeypatch.setattr(bit_db_api, '_mercado_action_center_call', rpc)
    result = client.patch('/api/employee-tasks/1?organization_key=beta',
        json={'is_completed': True, 'actor_id': 3, 'organization_key': 'beta', 'manager': True})
    assert result.status_code == 200
    operation, payload = calls[-1]
    assert operation == 'employee_tasks'
    assert payload['organization_key'] == 'alpha'
    assert payload['actor_id'] == 2 and payload['manager'] is False
    assert 'employee_tasks' in bit_interface.MERCADO_ACTION_CENTER_DB_OPERATIONS
