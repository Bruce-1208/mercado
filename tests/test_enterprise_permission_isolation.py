"""Offline relational tests for enterprise role boundaries and account settings."""
import json
import sqlite3

import pytest

from bit import bit_interface as app, bit_ai_video as video, bit_db_api


@pytest.fixture
def roles(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.create_function('FIELD', -1, lambda *values: values[1:].index(values[0]) if values[0] in values[1:] else 99)
    db.executescript('''
        CREATE TABLE workbench_organizations (
            organization_key TEXT PRIMARY KEY, organization_name TEXT UNIQUE,
            salesperson_limit INTEGER NOT NULL DEFAULT 3, is_active INTEGER DEFAULT 1,
            created_at TEXT, updated_at TEXT);
        INSERT INTO workbench_organizations (organization_key, organization_name)
            VALUES ('company-a', '企业甲'), ('company-b', '企业乙');
        CREATE TABLE workbench_roles (
            organization_key TEXT, role_key TEXT, role_name TEXT, description TEXT,
            permissions_json TEXT, own_store_only INTEGER, is_system INTEGER,
            created_at TEXT, updated_at TEXT,
            PRIMARY KEY (organization_key, role_key), UNIQUE (organization_key, role_name));
        CREATE TABLE workbench_users (
            id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, display_name TEXT,
            personal_name TEXT, email TEXT, department TEXT, wechat_id TEXT, phone TEXT,
            organization_key TEXT, role_key TEXT, warehouse_key TEXT, is_active INTEGER,
            created_at TEXT, updated_at TEXT);
    ''')
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=()):
            sql = sql.replace('%s', '?').replace('INSERT IGNORE', 'INSERT OR IGNORE').replace(' FOR UPDATE', '')
            self.cursor = db.execute(sql, params)
            self.lastrowid = self.cursor.lastrowid
            self.rowcount = self.cursor.rowcount
        def executemany(self, sql, rows):
            for params in rows: self.execute(sql, params)
        def fetchone(self):
            row = self.cursor.fetchone()
            return dict(row) if row is not None else None
        def fetchall(self): return [dict(row) for row in self.cursor.fetchall()]
    class Connection:
        def cursor(self): return Cursor()
        def commit(self): db.commit()
        def rollback(self): db.rollback()
        def close(self): pass
    monkeypatch.setattr(app.pymysql, 'connect', lambda **kwargs: Connection())
    monkeypatch.setattr(app, 'USE_DB_API', False)
    for org, user in [('company-a', 11), ('company-b', 22)]:
        app._seed_workbench_roles(Cursor(), org, '2026-10-12 00:00:00')
        db.execute('INSERT INTO workbench_users (id, username, organization_key, role_key, is_active) VALUES (?,?,?,?,1)',
                   (user, org, org, 'viewer'))
    actor = {'id': 99, 'username': 'manager', 'role_key': 'enterprise_admin',
             'organization_key': 'company-a', 'access_version': 1,
             'permissions': ['access.view', 'access.manage']}
    monkeypatch.setattr(app, 'get_current_workbench_user', lambda: actor)
    db.commit()
    yield db, actor, Cursor
    db.close()


def test_same_role_changes_only_current_enterprise_and_survives_seeding(roles):
    db, actor, cursor = roles
    with app.app.test_request_context('/api/access/roles/viewer?organization_key=company-b'):
        app.update_workbench_role_local('viewer', {'role_name': '本企业只读', 'permissions': ['appeal.view']})
        listed = app.list_workbench_roles_local()
        assert next(r for r in listed if r['role_key'] == 'viewer')['user_count'] == 1
    app._seed_workbench_roles(cursor(), 'company-a', '2026-10-12 00:00:00')
    first = app.get_workbench_user(user_id=11)
    second = app.get_workbench_user(user_id=22)
    assert json.loads(first['permissions_json']) == ['appeal.view']
    assert first['role_name'] == '本企业只读'
    assert second['role_name'] != first['role_name']
    assert json.loads(second['permissions_json']) != ['appeal.view']


def test_foreign_custom_role_cannot_be_seen_changed_deleted_or_assigned(roles):
    db, actor, cursor = roles
    with app.app.test_request_context('/api/access/roles'):
        key = app.create_workbench_role_local({'role_name': 'custom', 'permissions': []})
        actor['organization_key'] = 'company-b'
        assert key not in {r['role_key'] for r in app.list_workbench_roles_local()}
        with pytest.raises(ValueError, match='角色不存在'):
            app.update_workbench_role_local(key, {'role_name': 'stolen', 'permissions': []})
        with pytest.raises(ValueError, match='角色不存在'):
            app.delete_workbench_role_local(key)
        with pytest.raises(ValueError, match='角色不存在'):
            app.create_workbench_user_local({'username': 'new-user', 'password': 'safe-test-pass',
                'organization_key': 'company-b', 'role_key': key})
        other = app.create_workbench_role_local({'role_name': 'custom', 'permissions': []})
        app.delete_workbench_role_local(other)
    assert db.execute('SELECT COUNT(*) FROM workbench_roles WHERE role_key=?', (key,)).fetchone()[0] == 1


def test_platform_role_edit_is_scoped_to_explicit_enterprise(roles):
    db, actor, cursor = roles
    actor.update(role_key='super_admin', is_platform_admin=True)
    with app.app.test_request_context('/api/access/roles?organization_key=company-b'):
        app.update_workbench_role_local('viewer', {'role_name': 'B only', 'permissions': []})
    assert app.get_workbench_user(user_id=22)['role_name'] == 'B only'
    assert app.get_workbench_user(user_id=11)['role_name'] != 'B only'


def test_internal_role_scope_requires_forwarded_boundary(roles):
    with app.app.test_request_context('/api/db/workbench/roles'):
        with pytest.raises(ValueError): app.list_workbench_roles_local()
    with app.app.test_request_context('/api/db/workbench/roles', headers={'X-Knowledge-Organization': 'company-b'}):
        assert app.workbench_role_organization() == 'company-b'


@pytest.mark.parametrize('operation', ['update', 'password'])
def test_access_manager_cannot_take_over_admin(roles, monkeypatch, operation):
    db, actor, cursor = roles
    db.execute("UPDATE workbench_users SET role_key='super_admin' WHERE id=11")
    with app.app.test_request_context('/api/access/users/11', method='PUT', json={'role_key': 'viewer'}):
        result = app.api_access_user_detail.__wrapped__(11) if operation == 'update' else app.api_access_user_password.__wrapped__(11)
        assert result[1] == 403
    assert app.get_workbench_user(user_id=11)['role_key'] == 'super_admin'


def test_video_settings_and_credentials_never_fall_back_across_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_VIDEO_STORAGE_PATH', str(tmp_path))
    monkeypatch.setenv('AI_VIDEO_SEEDANCE_API_KEY', 'legacy-server-secret')
    monkeypatch.setenv('AI_VIDEO_WAN_API_KEY', 'legacy-server-wan-secret')
    monkeypatch.setattr(video, '_account_credential_resolver', lambda uid: {})
    video.save_provider_settings({'public_base_url': 'https://a.example', 'seedance_api_key': 'a-secret'}, user_id=11)
    video.save_provider_settings({'public_base_url': 'https://b.example'}, user_id=22)
    assert video._common_config(11)['public_base_url'] == 'https://a.example'
    assert video._common_config(22)['public_base_url'] == 'https://b.example'
    assert video._provider_config(video.SEEDANCE_PROVIDER, 11)['api_key'] == 'a-secret'
    assert video._provider_config(video.SEEDANCE_PROVIDER, 22)['api_key'] == ''
    assert video._provider_config(video.WAN_PROVIDER, 22)['api_key'] == ''
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'mysql')
    assert bit_db_api.update_ai_video_settings({'public_base_url': 'https://new.example'}, user_id=22)
    assert video._common_config(11)['public_base_url'] == 'https://a.example'


def test_appeal_templates_are_private_to_each_enterprise(roles, monkeypatch):
    from bit import bit_mysql
    from bit.knowledge_organization import scope
    db, actor, cursor = roles
    db.execute("""CREATE TABLE appeal_phrases (
        id INTEGER PRIMARY KEY AUTOINCREMENT, organization_key TEXT, source_key TEXT,
        appeal_type TEXT, content TEXT, content_hash TEXT, is_active INTEGER,
        created_at TEXT, updated_at TEXT, deleted_at TEXT,
        UNIQUE(organization_key, source_key), UNIQUE(organization_key, appeal_type, content_hash))""")
    monkeypatch.setattr(bit_mysql, 'initialize_appeal_storage', lambda: None)
    monkeypatch.setattr(bit_mysql, '_appeal_connection', lambda: app.pymysql.connect())
    with scope('company-a'):
        created = bit_mysql.create_appeal_phrase({'appeal_type': '投诉', 'content': 'Private A', 'is_active': True})
        first = created['id']
        own = bit_mysql.list_appeal_phrases()
        assert any(r['id'] == first for r in own['rows'])
    with scope('company-b'):
        assert first not in {r['id'] for r in bit_mysql.list_appeal_phrases()['rows']}
        with pytest.raises(ValueError, match='不存在'):
            bit_mysql.update_appeal_phrase(first, {'appeal_type': '投诉', 'content': 'Hacked', 'is_active': True})
        with pytest.raises(ValueError, match='不存在'):
            bit_mysql.delete_appeal_phrase(first)
        assert bit_mysql.create_appeal_phrase({'appeal_type': '投诉', 'content': 'Private A', 'is_active': True})['id'] != first
        for _ in range(10):
            assert bit_mysql.get_random_appeal_phrase('投诉')['id'] != first
    with scope('company-a'):
        bit_mysql.delete_appeal_phrase(first)
        assert first not in {r['id'] for r in bit_mysql.list_appeal_phrases()['rows']}


def test_legacy_permissions_do_not_grant_wildcard_access():
    assert not app.workbench_user_has_permission({'id': 1, 'permissions': ['*']}, 'access.manage')


def test_legacy_session_without_account_is_rejected():
    from flask import session
    with app.app.test_request_context('/'):
        session['workbench_user'] = {'username': 'legacy', '_legacy_compat': True, 'permissions': ['*']}
        assert app.get_current_workbench_user() is None
        assert 'workbench_user' not in session


@pytest.mark.parametrize('kind', ['infraction', 'prohibited'])
def test_auto_sync_switch_filters_owners_before_batch_limit(roles, monkeypatch, kind):
    import importlib
    from erp import mercadolibre_overview_sync_settings as settings
    from bit.knowledge_organization import scope
    db, actor, cursor = roles
    module = importlib.import_module(f'erp.mercadolibre_{kind}_store')
    scope_name = 'official_infractions' if kind == 'infraction' else 'prohibited_listings'
    state_table = getattr(module, f'{kind.upper()}_SYNC_STATE_TABLE')
    db.executescript(f'''
        CREATE TABLE mercado_store_tokens (id INTEGER, organization_key TEXT, enabled INTEGER);
        CREATE TABLE {state_table} (token_id INTEGER, requested_at TEXT, last_completed_at TEXT,
                                   last_started_at TEXT, last_status TEXT);
        CREATE TABLE {settings.SETTINGS_TABLE} (organization_key TEXT, scope TEXT, enabled INTEGER,
                                               updated_at TEXT, PRIMARY KEY (organization_key, scope));
        INSERT INTO mercado_store_tokens VALUES (1, 'company-a', 1), (2, 'company-b', 1);
    ''')
    db.execute(f'INSERT INTO {settings.SETTINGS_TABLE} VALUES (?, ?, 0, NULL)', ('company-a', scope_name))
    monkeypatch.setattr(settings, '_schema_ready', True)
    monkeypatch.setattr(module, f'ensure_{kind}_tables', lambda cursor: None)
    due = getattr(module, f'list_due_{kind}_token_ids')
    assert due(limit=1, connection_factory=lambda: app.pymysql.connect()) == [2]
    with app.app.test_request_context('/api/overview-auto-sync'):
        with scope('company-a'):
            assert due(connection_factory=lambda: app.pymysql.connect()) == []
            assert settings.get_overview_sync_settings(scope_name, connection_factory=lambda: app.pymysql.connect())['enabled'] is False
        with scope('company-b'):
            assert due(connection_factory=lambda: app.pymysql.connect()) == [2]
            assert settings.get_overview_sync_settings(scope_name, connection_factory=lambda: app.pymysql.connect())['enabled'] is True


@pytest.mark.parametrize('path', ['/api/db/workbench/roles', '/api/db/appeal-phrases', '/api/db/overview-auto-sync',
    '/api/db/official-infractions/sync/status', '/api/db/prohibited-listings/sync/status'])
def test_client_forwards_trusted_enterprise_not_query_override(roles, monkeypatch, path):
    from unittest.mock import Mock
    response = Mock(status_code=200, ok=True)
    response.json.return_value = {'status': 'success', 'data': []}
    request = Mock(return_value=response)
    monkeypatch.setattr(bit_db_api.DB_API_SESSION, 'request', request)
    with app.app.test_request_context('/api/access/roles?organization_key=company-b'):
        bit_db_api._request('GET', path)
    assert request.call_args.kwargs['headers']['X-Knowledge-Organization'] == 'company-a'


@pytest.mark.parametrize('operation', ['update', 'password'])
def test_platform_selected_enterprise_blocks_foreign_account_mutations(roles, operation):
    db, actor, cursor = roles
    actor.update(role_key='super_admin', is_platform_admin=True)
    with app.app.test_request_context('/api/access/users/22?organization_key=company-a', method='PUT',
                                     json={'role_key': 'viewer', 'password': 'test-password'}):
        result = app.api_access_user_detail.__wrapped__(22) if operation == 'update' else app.api_access_user_password.__wrapped__(22)
        assert result[1] == 403


def test_background_settings_use_captured_enterprise():
    from erp.ai_usage_billing import usage_context, bind_usage
    from bit.knowledge_organization import organization
    with usage_context(organization_key='company-b'):
        callback = bind_usage(organization)
    assert callback() == 'company-b'


@pytest.mark.parametrize('identity', ['browser_extension_user', 'local_executor_user', 'internal_agent_user'])
def test_worker_settings_ignore_spoofed_organization(identity):
    from flask import g
    from bit.knowledge_organization import organization
    with app.app.test_request_context('/api/appeal-phrases?organization_key=company-a',
                                     headers={'X-Knowledge-Organization': 'company-a'}):
        setattr(g, identity, {'organization_key': 'company-b'})
        assert organization() == 'company-b'


def test_legacy_session_reloads_enterprise_and_permissions(monkeypatch):
    from flask import session
    monkeypatch.setattr(app, 'USE_DB_API', False)
    monkeypatch.setattr(app, 'get_workbench_user', lambda **_: {
        'id': 11, 'username': 'member-a', 'organization_key': 'company-a', 'is_active': True,
        'role_key': 'viewer', 'permissions_json': '["appeal.view"]'})
    with app.app.test_request_context('/'):
        session['workbench_user'] = {'id': 11, 'organization_key': 'company-b', 'permissions': ['*']}
        user = app.get_current_workbench_user()
        assert user['organization_key'] == 'company-a'
        assert user['permissions'] == ['appeal.view']
        assert not app.workbench_user_has_permission(user, 'access.manage')


def test_regular_appeal_threads_retain_enterprise(monkeypatch):
    from bit import bit_appeal
    from bit.knowledge_organization import scope, organization
    results = []
    monkeypatch.setattr(bit_appeal, 'use_one_browser_run_task', lambda _: results.append(organization()))
    with scope('company-b'):
        bit_appeal.use_all_browser_run_task_with_thread_pool([{}, {}], max_threads=2)
    assert results == ['company-b', 'company-b']
