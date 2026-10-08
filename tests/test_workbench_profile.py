import sqlite3
from datetime import datetime

import pytest

from bit import bit_db_api, bit_interface as workbench


@pytest.fixture
def profile_store(tmp_path, monkeypatch):
    path = tmp_path / "users.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE workbench_users (id INTEGER PRIMARY KEY,
            username TEXT, display_name TEXT, personal_name TEXT, email TEXT,
            department TEXT, wechat_id TEXT, phone TEXT, password_hash TEXT,
            is_active INTEGER, role_key TEXT, organization_key TEXT, updated_at TEXT)""")
        for user_id, name in [(7, '业务员甲'), (8, '业务员乙')]:
            db.execute("INSERT INTO workbench_users VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (user_id, f'sales{user_id}', name, None, f'sales{user_id}@example.com',
                        '运营部', 'wx-old', '13800138000', workbench.make_password_hash('old-password'),
                        1, 'member', 'wuhan-zeshun', None))

    class Cursor:
        def __init__(self, db): self.raw = db.cursor()
        def __enter__(self): return self
        def __exit__(self, *args): self.raw.close()
        def execute(self, sql, params=()):
            self.raw.execute(sql.replace('%s', '?').replace(' FOR UPDATE', ''),
                             tuple(value.isoformat(' ') if isinstance(value, datetime) else value for value in params))
        def fetchone(self):
            row = self.raw.fetchone()
            return dict(row) if row else None

    class Connection:
        def __init__(self):
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
        def cursor(self): return Cursor(self.db)
        def commit(self): self.db.commit()
        def rollback(self): self.db.rollback()
        def close(self): self.db.close()

    def read(user_id=7):
        connection = Connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT * FROM workbench_users WHERE id = %s', (user_id,))
                row = cursor.fetchone()
            if row:
                row.update(permissions_json='[]', own_store_only=True, role_name='业务员')
            return row
        finally:
            connection.close()

    monkeypatch.setattr(workbench, 'USE_DB_API', False)
    monkeypatch.setattr(workbench.pymysql, 'connect', lambda **kwargs: Connection())
    monkeypatch.setattr(workbench, 'get_workbench_user', lambda username='', user_id=None: read(user_id))
    monkeypatch.setitem(workbench.app.config, 'TESTING', True)
    monkeypatch.setitem(workbench.app.config, 'SECRET_KEY', 'profile-test-secret')
    # Self-service must remain usable without calling billing at all.
    monkeypatch.setattr(bit_db_api, 'account_billing', lambda _: pytest.fail('profile requested billing'))
    return read


def signed_client(profile_store):
    client = workbench.app.test_client()
    with client.session_transaction() as session:
        session['workbench_user'] = workbench.build_workbench_session_user(profile_store())
    response = client.get('/api/account/profile')
    assert response.status_code == 200
    return client, {'X-CSRF-Token': response.json['csrf_token']}


def test_profile_load_update_and_scope_are_account_bound(profile_store):
    client, headers = signed_client(profile_store)
    loaded = client.get('/api/account/profile')
    assert loaded.json['data']['personal_name'] == '业务员甲'
    assert 'password_hash' not in loaded.get_data(as_text=True)
    assert loaded.headers['Cache-Control'] == 'no-store'
    assert client.get('/settings/profile').status_code == 200
    original = profile_store()
    response = client.put('/api/account/profile', headers=headers, json={
        'personal_name': '新姓名', 'email': ' NEW@example.com ',
        'department': '销售部', 'wechat_id': 'wx-new', 'phone': '+86 13900139000',
    })
    assert response.status_code == 200
    row = profile_store()
    assert row['personal_name'] == '新姓名'
    assert row['email'] == 'new@example.com'
    assert row['phone'] == '+86 13900139000'
    for field in ('display_name', 'username', 'role_key', 'organization_key', 'password_hash', 'is_active'):
        assert row[field] == original[field]
    assert profile_store(8)['personal_name'] is None
    assert client.get('/api/account/profile').json['data']['personal_name'] == '新姓名'
    with client.session_transaction() as session:
        assert session['workbench_user']['personal_name'] == '新姓名'
        assert workbench.workbench_user_salesperson(session['workbench_user']) == '业务员甲'


@pytest.mark.parametrize('payload', [
    {'id': 8}, {'actor_id': 8}, {'user_id': 8}, {'role_key': 'super_admin'},
    {'display_name': '业务员乙'}, {'username': 'sales8'}, {'organization_key': 'other'},
    {'is_active': False}, {'permissions': ['*']}, {'password_hash': 'fake'},
    {'personal_name': ''}, {'personal_name': 'a' * 65}, {'email': 'invalid'},
    {'email': 'sales8@example.com'}, {'phone': 'abc'}, {'wechat_id': []},
    {'department': 'foo\nbar'}, {}, [],
])
def test_profile_rejects_escalation_invalid_values_and_duplicate_email(profile_store, payload):
    client, headers = signed_client(profile_store)
    original = profile_store()
    response = client.put('/api/account/profile', json=payload, headers=headers)
    assert response.status_code == 400
    assert profile_store() == original


def test_partial_updates_and_empty_optional_values_preserve_other_fields(profile_store):
    client, headers = signed_client(profile_store)
    assert client.put('/api/account/profile', headers=headers, json={'phone': ''}).status_code == 200
    row = profile_store()
    assert row['phone'] == ''
    assert row['email'] == 'sales7@example.com'
    assert row['wechat_id'] == 'wx-old'


@pytest.mark.parametrize('path,method', [('/api/account/profile', 'put'), ('/api/account/password', 'post')])
def test_profile_changes_require_csrf(profile_store, path, method):
    client, headers = signed_client(profile_store)
    original = profile_store()
    assert getattr(client, method)(path, json={}).status_code == 403
    assert getattr(client, method)(path, json={}, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert profile_store() == original


@pytest.mark.parametrize('payload', [
    {'current_password': 'wrong-password', 'new_password': 'new-password', 'confirm_password': 'new-password'},
    {'current_password': 'old-password', 'new_password': 'short', 'confirm_password': 'short'},
    {'current_password': 'old-password', 'new_password': 'new-password', 'confirm_password': 'different'},
    {'current_password': 'old-password', 'new_password': 'old-password', 'confirm_password': 'old-password'},
    {'current_password': [], 'new_password': 'new-password', 'confirm_password': 'new-password'},
    {'current_password': 'old-password', 'new_password': 'x' * 129, 'confirm_password': 'x' * 129},
    {'user_id': 8, 'current_password': 'old-password', 'new_password': 'new-password', 'confirm_password': 'new-password'},
])
def test_password_validation_does_not_write(profile_store, payload):
    client, headers = signed_client(profile_store)
    original = profile_store()['password_hash']
    response = client.post('/api/account/password', json=payload, headers=headers)
    assert response.status_code == 400
    assert profile_store()['password_hash'] == original
    assert client.get('/api/account/profile').status_code == 200


def test_password_is_hashed_and_logs_out_current_session(profile_store):
    client, headers = signed_client(profile_store)
    other_hash = profile_store(8)['password_hash']
    response = client.post('/api/account/password', headers=headers, json={
        'current_password': 'old-password', 'new_password': 'new-password', 'confirm_password': 'new-password',
    })
    assert response.status_code == 200
    row = profile_store()
    assert row['password_hash'] != 'new-password'
    assert workbench.verify_password('new-password', row['password_hash'])
    assert not workbench.verify_password('old-password', row['password_hash'])
    assert profile_store(8)['password_hash'] == other_hash
    with client.session_transaction() as session:
        assert not session.get('workbench_user')
        assert not session.get('profile_csrf_token')
    assert client.get('/api/account/profile').status_code == 401


def test_profile_authentication_and_internal_endpoint_protection(profile_store, monkeypatch):
    client = workbench.app.test_client()
    assert client.get('/api/account/profile').status_code == 401
    assert client.put('/api/account/profile', json={}).status_code == 401
    assert client.post('/api/account/password', json={}).status_code == 401
    assert client.get('/settings/profile').status_code == 302
    assert client.post('/api/db/workbench/profile', json={'actor_id': 8, 'action': 'get'}).status_code == 403
    monkeypatch.setenv('BIT_DB_API_TOKEN', 'internal-secret')
    result = client.post('/api/db/workbench/profile', json={'actor_id': 8, 'action': 'get'},
                         headers={'X-Internal-Token': 'internal-secret'})
    assert result.status_code == 200
    assert result.json['data']['username'] == 'sales8'


def test_client_mode_profile_forwarding_uses_session_actor(profile_store, monkeypatch):
    client, headers = signed_client(profile_store)
    monkeypatch.setattr(workbench, 'USE_DB_API', True)
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    monkeypatch.setattr(bit_db_api, 'get_workbench_session_user',
                        lambda user_id: workbench.build_workbench_session_user(profile_store(user_id)))
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {'personal_name': '新姓名', 'email': '', 'department': '', 'wechat_id': '', 'phone': ''}
    monkeypatch.setattr(bit_db_api, '_request', request)
    assert client.put('/api/account/profile', headers=headers, json={'personal_name': '新姓名'}).status_code == 200
    assert calls == [('POST', '/api/db/workbench/profile', {
        'json': {'actor_id': 7, 'action': 'update', 'data': {'personal_name': '新姓名'}}, 'timeout': 15,
    })]
    monkeypatch.setattr(bit_db_api, '_request', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('原密码不正确')))
    response = client.post('/api/account/password', headers=headers, json={
        'current_password': 'wrong', 'new_password': 'new-password', 'confirm_password': 'new-password',
    })
    assert response.status_code == 400
    assert response.json['message'] == '原密码不正确'


def test_profile_name_column_is_added_to_existing_schema(monkeypatch):
    statements = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=()): statements.append(sql)
        def fetchone(self): return {'total': 1}
    class Connection:
        def cursor(self): return Cursor()
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass
    monkeypatch.setattr(workbench.pymysql, 'connect', lambda **kwargs: Connection())
    monkeypatch.setattr(workbench, '_workbench_schema_state', lambda _: (
        {'workbench_roles', 'workbench_users', 'workbench_organizations'},
        set(workbench._WORKBENCH_ROLE_REQUIRED_COLUMNS),
        set(workbench._WORKBENCH_USER_REQUIRED_COLUMNS) - {'personal_name'},
    ))
    assert workbench.ensure_workbench_user_table() is True
    assert any('ADD COLUMN `personal_name`' in sql for sql in statements)
