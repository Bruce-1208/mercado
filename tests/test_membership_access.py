"""Offline role ceilings and tenant/owner boundaries for the membership tiers."""
import json
import pytest
from bit import bit_interface as web, membership_access as policy, product_access
from test_enterprise_permission_isolation import roles  # noqa: F401


def member(role='basic_member'):
    default = next(item for item in web.WORKBENCH_DEFAULT_ROLES if item['role_key'] == role)
    return web.build_workbench_session_user({
        'id': 31, 'username': 'alice', 'display_name': 'Alice',
        'organization_key': 'company-a', 'role_key': role, 'own_store_only': False,
        'permissions_json': json.dumps(default['permissions']),
    })


@pytest.mark.parametrize('role', sorted(policy.MEMBER_ROLES))
def test_scope_cannot_be_disabled_and_grants_cannot_elevate(role):
    user = member(role)
    assert user['own_store_only']
    assert web.workbench_user_own_store_only({**user, 'own_store_only': False})
    assert not web.workbench_user_has_permission({**user, 'permissions': ['*']}, 'access.manage')
    assert not web.workbench_user_has_permission({**user, 'permissions': ['*']}, 'shop_status.view')
    assert not policy.limit_permissions(role, ['*', 'access.manage'])


@pytest.mark.parametrize('path,method', [
    ('/api/mercado-products', 'get'), ('/api/mercado-products/publish', 'post'),
    ('/api/ai-original-products', 'get'), ('/api/ai-videos/jobs', 'post'),
    ('/api/1688-products', 'get'), ('/api/zying-collection/start', 'post'),
    ('/api/store-links', 'get'), ('/api/access/users', 'get'),
    ('/api/access/roles', 'post'), ('/api/ai-weight-price/tasks', 'post'),
    ('/mercado-collection/items/123', 'get'), ('/ai-videos/123/details', 'get'),
    ('/api/orders/bulk-update', 'post'), ('/api/new-module', 'post'),
])
def test_basic_member_is_denied_before_business_handler(monkeypatch, path, method):
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: member())
    response = getattr(web.app.test_client(), method)(path)
    assert response.status_code == 403
    assert response.json['code'] == 'membership_permission_required'


@pytest.mark.parametrize('role', sorted(policy.MEMBER_ROLES))
def test_foreign_tokens_and_other_salesperson_denied(monkeypatch, role):
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: member(role))
    monkeypatch.setattr(web, '_authorized_token_ids_for_user', lambda user: {1})
    monkeypatch.setattr(web, '_workbench_billing_blocked', lambda user: None)
    client = web.app.test_client()
    assert client.get('/api/orders?token_id=2').status_code == 403
    assert client.post('/api/orders/print', json={'token_id': 2}).status_code == 403
    assert client.post('/api/orders/print', json={'salesperson': 'Bob'}).status_code == 403


@pytest.mark.parametrize('role', ['basic_member', 'premium_member', 'enterprise_admin'])
def test_token_lists_remain_scoped_by_company_and_owner(role):
    user = member(role)
    rows = [
        {'id': 1, 'organization_key': 'company-a', 'site_settings': [{'salesperson': 'Alice'}]},
        {'id': 2, 'organization_key': 'company-a', 'site_settings': [{'salesperson': 'Bob'}]},
        {'id': 3, 'organization_key': 'company-b', 'site_settings': [{'salesperson': 'Alice'}]},
    ]
    result = web._filter_mercado_tokens_for_user({'rows': rows}, user)
    assert [row['id'] for row in result['rows']] == ([1, 2] if role == 'enterprise_admin' else [1])


def test_premium_catalog_grant_and_product_ownership():
    user = member('premium_member')
    assert web.workbench_user_has_permission(user, 'catalog.execute')
    assert web.workbench_user_has_permission(user, 'ai_content.execute')
    own = {'organization_key': 'company-a', 'owner_user_id': 31, 'visibility': 'private'}
    shared = {**own, 'owner_user_id': 32, 'visibility': 'enterprise'}
    assert product_access.can_operate(own, user)
    assert product_access.can_view(shared, user)
    assert not product_access.can_operate(shared, user)
    assert not product_access.can_view({**shared, 'visibility': 'private'}, user)
    assert not product_access.can_view({**own, 'organization_key': 'company-b'}, user)


def test_basic_order_access_and_extension_direct_urls(monkeypatch):
    user = member()
    with web.app.test_request_context('/api/orders'):
        assert web._membership_request_blocked(user) is None
    with web.app.test_request_context('/api/browser-extension/1688/products', method='POST'):
        assert web._membership_request_blocked(user)[1] == 403
    premium = member('premium_member')
    with web.app.test_request_context('/api/ai-original-products'):
        assert web._membership_request_blocked(premium) is None
        premium['permissions'] = []
        assert web._membership_request_blocked(premium)[1] == 403


def test_member_role_edits_reject_upgrade_and_force_own_scope(roles):
    db, actor, cursor = roles
    with web.app.test_request_context('/api/access/roles'):
        with pytest.raises(ValueError, match='会员等级'):
            web.update_workbench_role_local('basic_member', {'role_name': '基础会员', 'permissions': ['catalog.view']})
        web.update_workbench_role_local('premium_member', {'role_name': '高级会员', 'permissions': ['catalog.view'], 'own_store_only': False})
    row = db.execute("SELECT own_store_only FROM workbench_roles WHERE organization_key='company-a' AND role_key='premium_member'").fetchone()
    assert row[0] == 1


def test_enterprise_admin_cannot_rewrite_administrator_roles(roles, monkeypatch):
    monkeypatch.setattr(web, '_workbench_billing_blocked', lambda user: None)
    client = web.app.test_client()
    for role in ('super_admin', 'enterprise_admin'):
        assert client.put('/api/access/roles/' + role, json={'role_name': 'elevated'}).status_code == 403
        assert client.delete('/api/access/roles/' + role).status_code == 403


@pytest.mark.parametrize('role', ['premium_member', 'enterprise_admin'])
def test_revoked_catalog_grant_denies_direct_api(monkeypatch, role):
    user = member(role)
    user['permissions'] = []
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: user)
    response = web.app.test_client().get('/api/mercado-products')
    assert response.status_code == 403


@pytest.mark.parametrize('role', ['basic_member', 'premium_member'])
def test_extension_collect_cannot_bypass_membership(monkeypatch, role):
    user = member(role)
    user['permissions'] = []
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: None)
    monkeypatch.setattr(web, '_browser_extension_user_from_token', lambda token: user)
    response = web.app.test_client().post('/api/browser-extension/collect',
        headers={'Authorization': 'Bearer offline-test'}, json={'source_platform': '1688'})
    assert response.status_code == 403


def test_seed_upgrades_only_unchanged_defaults_in_each_enterprise(roles):
    db, actor, cursor = roles
    defaults = {item['role_key']: item for item in web.WORKBENCH_DEFAULT_ROLES}
    for role in ('enterprise_admin', 'member', 'operator', 'viewer'):
        old = [key for key in defaults[role]['permissions']
               if not key.startswith(('catalog.', 'ai_content.'))
               and not (role == 'enterprise_admin' and key.startswith('access.'))]
        db.execute("UPDATE workbench_roles SET permissions_json=? WHERE organization_key='company-a' AND role_key=?",
                   (json.dumps(old, ensure_ascii=False), role))
    db.execute("UPDATE workbench_roles SET permissions_json='[]' WHERE organization_key='company-b' AND role_key='enterprise_admin'")
    for org in ('company-a', 'company-b'):
        web._seed_workbench_roles(cursor(), org, '2026-10-13 00:00:00')
    for role in ('enterprise_admin', 'member', 'operator', 'viewer'):
        row = db.execute("SELECT permissions_json FROM workbench_roles WHERE organization_key='company-a' AND role_key=?", (role,)).fetchone()
        assert json.loads(row[0]) == list(defaults[role]['permissions'])
    assert db.execute("SELECT permissions_json FROM workbench_roles WHERE organization_key='company-b' AND role_key='enterprise_admin'").fetchone()[0] == '[]'
