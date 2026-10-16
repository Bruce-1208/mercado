"""Offline quota persistence, account transitions and authorization."""
import pytest
from bit import bit_interface as app, bit_db_api
from test_enterprise_permission_isolation import roles  # noqa: F401


def add(name, **changes):
    return app.create_workbench_user_local(dict(username=name, password='safe-password',
        organization_key='company-a', role_key='basic_member', **changes))


def test_default_limit_and_account_transitions(roles):
    db, actor, _ = roles  # One existing viewer already occupies a seat.
    with app.app.test_request_context('/'):
        first = add('new-one')
        add('new-two')
        with pytest.raises(ValueError, match='上限 3'):
            add('new-three')
        disabled = add('inactive', is_active=False)
        with pytest.raises(ValueError, match='名额已用完'):
            app.update_workbench_user_local(disabled, {'organization_key': 'company-a', 'role_key': 'premium_member'})
        app.update_workbench_user_local(first, {'organization_key': 'company-a', 'role_key': 'premium_member'})
        app.update_workbench_user_local(first, {'organization_key': 'company-a', 'role_key': 'premium_member', 'is_active': False})
        app.update_workbench_user_local(disabled, {'organization_key': 'company-a', 'role_key': 'premium_member'})
        rows = app.list_workbench_organizations_local()
        assert next(r for r in rows if r['organization_key'] == 'company-a')['salesperson_count'] == 3
        assert next(r for r in rows if r['organization_key'] == 'company-b')['salesperson_count'] == 1


def test_quota_update_validation_and_exempt_roles(roles):
    db, actor, _ = roles
    with app.app.test_request_context('/'):
        with pytest.raises(ValueError, match='不能低于'):
            app.update_workbench_organization_local('company-a', {'salesperson_limit': 0})
        app.update_workbench_organization_local('company-a', {'salesperson_limit': 1})
        app.create_workbench_user_local({'username': 'manager', 'password': 'safe-password',
            'organization_key': 'company-a', 'role_key': 'enterprise_admin'})
        with pytest.raises(ValueError, match='名额已用完'):
            add('blocked')
        app.update_workbench_organization_local('company-a', {'salesperson_limit': 4})
        add('allowed')
        app.create_workbench_organization_local({'organization_key': 'company-c', 'organization_name': '企业丙'})
        assert db.execute("SELECT salesperson_limit FROM workbench_organizations WHERE organization_key='company-c'").fetchone()[0] == 3
        app.update_workbench_organization_local('company-c', {'salesperson_limit': 0})


@pytest.mark.parametrize('value', [-1, 1.5, True, None, '', 'abc', 1000000])
def test_reject_invalid_limit(value):
    with pytest.raises(ValueError):
        app._validate_salesperson_limit(value)


def test_enterprise_read_and_super_admin_write(roles, monkeypatch):
    db, actor, _ = roles
    monkeypatch.setattr(app, '_workbench_billing_blocked', lambda user: None)
    monkeypatch.setattr(bit_db_api, 'list_mercado_store_tokens', lambda: {'rows': []})
    client = app.app.test_client()
    result = client.get('/api/access/organizations?organization_key=company-b')
    assert result.status_code == 200
    assert [r['organization_key'] for r in result.json['data']] == ['company-a']
    assert result.json['data'][0]['salesperson_limit'] == 3
    assert client.put('/api/access/organizations/company-a', json={'salesperson_limit': 5}).status_code == 403
    assert client.post('/api/access/organizations', json={}).status_code == 403
    actor.update(role_key='super_admin', is_platform_admin=True, permissions=['*'])
    assert client.put('/api/access/organizations/company-a', json={'salesperson_limit': 5}).status_code == 200
    assert db.execute("SELECT salesperson_limit FROM workbench_organizations WHERE organization_key='company-a'").fetchone()[0] == 5


def test_remote_adapter(monkeypatch):
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    calls = []
    monkeypatch.setattr(bit_db_api, '_request', lambda *args, **kwargs: calls.append((args, kwargs)))
    bit_db_api.update_workbench_organization('company-a', {'salesperson_limit': 5})
    assert calls[0][0] == ('PUT', '/api/db/workbench/organizations/company-a')
    assert calls[0][1]['json'] == {'salesperson_limit': 5}


def test_old_schema_requires_quota_migration():
    class Cursor:
        def execute(self, sql):
            assert "SHOW COLUMNS" in sql and "salesperson_limit" in sql
        def fetchone(self):
            return None
    assert app._workbench_organizations_are_current(Cursor()) is False


def test_warehouse_is_exempt_and_custom_role_consumes_quota(roles):
    db, actor, cursor = roles
    with app.app.test_request_context('/'):
        app.update_workbench_organization_local('company-a', {'salesperson_limit': 1})
        app._check_salesperson_quota(cursor(), 'company-a', 'warehouse', True)
        custom = app.create_workbench_role_local({'role_name': '自定义业务', 'permissions': []})
        with pytest.raises(ValueError, match='名额已用完'):
            app.create_workbench_user_local({'username': 'custom-user', 'password': 'safe-password',
                'organization_key': 'company-a', 'role_key': custom})
