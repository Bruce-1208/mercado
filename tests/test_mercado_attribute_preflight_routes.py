import pytest

from test_browser_extension_management_categories import workbench, HEADERS


@pytest.fixture(autouse=True)
def offline_catalog_access(workbench, monkeypatch, isolated_legacy_console_user,
                           subscribed_workbench_account):
    # Keep the shared catalog permission lookup off the live database.
    monkeypatch.setattr(workbench.bit_db_api, "check_catalog_access", lambda *a, **kw: None)


def logged_client(workbench):
    workbench.app.config.update(SECRET_KEY="test-secret")
    client = workbench.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = {"id": 7, "username": "tester", "permissions": ["*"]}
    return client


def test_virtual_ean_internal_route_requires_auth_and_uses_shared_storage(workbench, monkeypatch):
    from erp import mercadolibre_virtual_ean
    monkeypatch.setenv('BIT_DB_API_TOKEN', 'internal-test')
    monkeypatch.setattr(workbench, 'reject_db_api_client_mode', lambda: None)
    calls = []
    monkeypatch.setattr(mercadolibre_virtual_ean, 'allocate_shared',
                        lambda identity: calls.append(identity) or '2000000000015')
    client = workbench.app.test_client()
    path = '/api/db/mercado-products/virtual-ean'
    response = client.post(path, json={'identity': 'product'}, environ_base={'REMOTE_ADDR': '203.0.113.1'})
    assert response.status_code == 403 and not calls
    response = client.post(path, json={'identity': 'product'}, headers={'X-Internal-Token': 'internal-test'},
                           environ_base={'REMOTE_ADDR': '203.0.113.1'})
    assert response.status_code == 200
    assert response.json['data'] == {'ean': '2000000000015'}
    assert calls == ['product']



def test_preview_checks_account_access_before_reading_products(workbench, monkeypatch):
    monkeypatch.setattr(workbench.bit_db_api, "list_mercado_store_tokens", lambda: {"rows": []})
    monkeypatch.setattr(workbench, "_filter_mercado_tokens_for_user", lambda data, *args: data)
    monkeypatch.setattr(workbench, "db_get_mercado_product_items_by_ids", lambda ids: (_ for _ in ()).throw(AssertionError("must not read")))
    client = workbench.app.test_client()
    assert client.post('/api/mercado-products/attribute-preflight', json={}).status_code == 401
    response = logged_client(workbench).post('/api/mercado-products/attribute-preflight', headers=HEADERS,
                           json={"product_item_ids": [1], "token_id": 99})
    assert response.status_code == 403


@pytest.mark.parametrize("count", [1, 51, 100])
def test_preview_persists_server_generated_draft_and_returns_only_report_and_revision(workbench, monkeypatch, count):
    from erp import mercadolibre_batch_publish as batch
    from erp import mercadolibre_follow_sell as publisher
    from erp import mercadolibre_publication_drafts as storage
    monkeypatch.setattr(workbench.bit_db_api, "list_mercado_store_tokens", lambda: {"rows": [{"id": 7}]})
    monkeypatch.setattr(workbench, "_filter_mercado_tokens_for_user", lambda data, *args: data)
    monkeypatch.setattr(workbench, "db_get_mercado_product_items_by_ids", lambda ids: [{"id": id, "source_item_id": "MLM123", "owner_user_id": 7} for id in ids])
    monkeypatch.setattr(workbench, "db_get_existing_mercado_user_product_ids", lambda *a, **k: {})
    monkeypatch.setattr(batch, "DatabaseMercadoLibreClient", lambda token: object())
    monkeypatch.setattr(batch, "_prepared_listing_from_product_row", lambda *a: ({"id": "MLM123"}, {}))
    monkeypatch.setattr(batch, "discounted_net_proceeds_usd", lambda *a: 10)
    monkeypatch.setattr(batch, "site_discount_rate", lambda *a: 100)
    calls = []
    def fake_follow(*args, **kwargs):
        assert not kwargs.get("publish")
        return {"preflight": {"status": "ready", "issues": [], "mappings": []},
                "draft": {"revision": "rev-1", "source": {"attributes": []}}}
    monkeypatch.setattr(publisher, "follow_sell", fake_follow)
    monkeypatch.setattr(storage, "shared_draft_storage", lambda *a, **kw: calls.append((a, kw)))
    response = logged_client(workbench).post('/api/mercado-products/attribute-preflight', headers=HEADERS,
                 json={"product_item_ids": list(range(1, count + 1)), "token_id": 7, "site_id": "MLM"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert len(response.json["data"]["results"]) == count
    assert len(calls) == count
    item = response.json["data"]["results"][0]
    assert item["revision"] == "rev-1"
    assert "draft" not in item
    assert calls[0][1]["product_id"] == 1
    assert calls[0][1]["token_id"] == 7


@pytest.mark.parametrize("ids", [[], None, "1"])
def test_preview_rejects_empty_or_invalid_selection(workbench, ids):
    response = logged_client(workbench).post('/api/mercado-products/attribute-preflight', headers=HEADERS,
                   json={"product_item_ids": ids})
    assert response.status_code == 400


def test_publish_loads_owned_server_draft_and_passes_it_to_worker(workbench, monkeypatch):
    from test_erp_mercadolibre_batch_publish import _rows
    from erp import mercadolibre_publication_drafts as storage
    monkeypatch.setattr(workbench.bit_db_api, 'list_mercado_store_tokens', lambda: {'rows':[{'id':7, 'site_id':'CBT', 'display_name':'测试店铺'}]})
    monkeypatch.setattr(workbench.bit_db_api, 'publish_workers', lambda value=None: value or 16)
    monkeypatch.setattr(workbench, '_filter_mercado_tokens_for_user', lambda data, *args: data)
    monkeypatch.setattr(workbench, 'db_get_mercado_product_items_by_ids', lambda ids: [_rows()[0]])
    monkeypatch.setattr(workbench, 'db_create_mercado_product_publish_records',
                        lambda rows, **kwargs: {int(row['id']): 100 + int(row['id']) for row in rows})
    monkeypatch.setattr(workbench, 'db_update_mercado_product_publish_record', lambda *args, **kwargs: None)
    loaded = []
    def load(action, **kwargs):
        loaded.append((action, kwargs))
        return {'revision':'owned-draft'}
    monkeypatch.setattr(storage, 'shared_draft_storage', load)
    workers = []
    class Worker:
        def __init__(self, **kwargs):
            workers.append(kwargs)
        def start(self):
            pass
    monkeypatch.setattr(workbench.threading, 'Thread', Worker)
    monkeypatch.setattr(workbench, '_mercado_publish_state', {'running':False})
    response = logged_client(workbench).post('/api/mercado-products/publish', json={
        'product_item_ids':[11], 'selection_mode':'accounts', 'token_ids':[7], 'site_ids':['MLM'],
        'quantity':1, 'net_proceeds_ratio':100, 'draft_revisions':{'11:7:MLM':'owned-draft'},
    })
    assert response.status_code == 200, response.json
    assert loaded == [('get', {'revision':'owned-draft','product_id':11,'token_id':7,'site_id':'MLM','owner_id':7})]
    assert workers
    targets = workers[0]['args'][1]
    assert targets[0]['prepared_drafts'] == {'11':{'revision':'owned-draft'}}
