from bit import bit_interface as web, product_access
from erp import mercadolibre_collection_store as store
from test_catalog_visibility import Connection, user
import pytest


@pytest.fixture
def console(monkeypatch):
    current = user()
    db = Connection()
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: current)
    monkeypatch.setattr(web, '_workbench_billing_blocked', lambda actor: None)
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    monkeypatch.setattr(store, '_connect', lambda: db)
    monkeypatch.setattr(store, '_connect_list', lambda: db)
    monkeypatch.setattr(web.bit_db_api, 'check_catalog_access', store.check_catalog_access)
    monkeypatch.setattr(web.bit_db_api, 'set_catalog_visibility', store.set_catalog_visibility)
    monkeypatch.setattr(web, 'db_list_mercado_product_items', store.list_product_items)
    monkeypatch.setattr(web, '_decorate_mercado_infringement', lambda data, *args: data)
    return current, db, web.app.test_client()


def test_member_sees_own_stores_assigned_to_username_or_display_name(console, monkeypatch):
    current, db, client = console
    monkeypatch.setattr(web.bit_db_api, 'list_mercado_store_tokens', lambda: {'rows': [
        {'id':1, 'organization_key':'acme', 'site_settings':[{'salesperson':'collector'}]},
        {'id':2, 'organization_key':'acme', 'site_settings':[{'salesperson':'采集员'}]},
        {'id':3, 'organization_key':'acme', 'site_settings':[{'salesperson':'同事'}]},
        {'id':4, 'organization_key':'other', 'site_settings':[{'salesperson':'collector'}]},
    ]})
    response = client.get('/api/mercado-tokens')
    assert response.status_code == 200
    assert [row['id'] for row in response.json['data']['rows']] == [1,2]


def test_stable_store_owner_id_does_not_fall_back_to_a_duplicate_name():
    token = {'site_settings':[{'owner_user_id':99, 'salesperson':'采集员'}]}
    assert not web._token_belongs_to_user(token, user())
    assert web._token_belongs_to_user({'site_settings':[{'owner_user_id':7}]}, user())


def test_member_shared_product_list_works_without_own_shop_tokens(console, monkeypatch):
    current, db, client = console
    monkeypatch.setattr(web, '_authorized_token_ids_for_user', lambda: pytest.fail('product visibility must not depend on own shops'))
    response = client.get('/api/mercado-products')
    assert response.status_code == 200
    assert [row['id'] for row in response.json['data']['rows']] == [5,3,1]


def test_id_endpoint_rejects_hidden_product_before_mutation(console, monkeypatch):
    current, db, client = console
    monkeypatch.setattr(web, 'db_update_mercado_product_item', lambda *args: pytest.fail('hidden product was mutated'))
    response = client.patch('/api/mercado-products/2', json={'title':'changed'})
    assert response.status_code == 403


def test_private_toggle_is_authorized_by_collector_and_admin(console):
    current, db, client = console
    assert client.patch('/api/catalog/visibility', json={'item_type':'products','item_ids':[1],'visibility':'enterprise'}).status_code == 200
    assert client.patch('/api/catalog/visibility', json={'item_type':'products','item_ids':[3],'visibility':'private'}).status_code == 403
    current['role_key'] = 'enterprise_admin'
    assert client.patch('/api/catalog/visibility', json={'item_type':'products','item_ids':[2],'visibility':'enterprise'}).status_code == 200
    assert client.patch('/api/catalog/visibility', json={'item_type':'products','item_ids':[4],'visibility':'private'}).status_code == 403


def test_public_caller_cannot_forge_product_actor_header(console):
    current, db, client = console
    import json
    response = client.get('/api/mercado-products', headers={product_access.HEADER: json.dumps(user('super_admin'))})
    assert [row['id'] for row in response.json['data']['rows']] == [5,3,1]


@pytest.mark.parametrize('path,method,payload', [
    ('/api/mercado-products/3', 'patch', {'title': 'changed'}),
    ('/api/1688-products/3', 'patch', {'title': 'changed'}),
    ('/api/ai-original-products/3/listing', 'patch', {}),
    ('/api/ai-original-products/process', 'post', {'product_item_ids': [1, 3]}),
    ('/api/mercado-products', 'delete', {'product_item_ids': [1, 3]}),
    ('/api/mercado-products/add', 'post', {'collection_item_ids': [11, 13]}),
    ('/api/mercado-collection/bulk-edit', 'patch', {'collection_item_ids': [13], 'changes': {'title': 'changed'}}),
    ('/api/mercado-products/publish', 'post', {'product_item_ids': [3]}),
    ('/api/mercado-management-categories/assign', 'post', {'item_type': 'collection', 'item_ids': [13]}),
])
def test_shared_products_cannot_be_operated_by_members(console, path, method, payload):
    current, db, client = console
    before = db.db.total_changes
    response = getattr(client, method)(path, json=payload)
    assert response.status_code == 403
    assert db.db.total_changes == before


@pytest.mark.parametrize('role', ['member', 'operator', 'viewer', 'enterprise_admin', 'super_admin'])
def test_operation_scope_allows_own_and_only_admins_others(console, role):
    current, db, client = console
    current['role_key'] = role
    with product_access.scope(current):
        store.check_catalog_access('products', [1, 5], operate=True)
        if role in {'enterprise_admin', 'super_admin'}:
            store.check_catalog_access('products', [1, 2, 3], operate=True)
        else:
            with pytest.raises(PermissionError):
                store.check_catalog_access('products', [1, 3], operate=True)
        with pytest.raises(PermissionError):
            store.check_catalog_access('products', [4], operate=True)
        assert store._json_safe_row({'organization_key': 'acme', 'owner_user_id': 8, 'visibility': 'enterprise'})['can_operate'] == (role in {'enterprise_admin', 'super_admin'})
