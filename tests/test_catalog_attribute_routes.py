import pytest
from flask import Flask
from bit import bit_db_api, product_access
from bit.catalog_attribute_routes import register_routes
from test_catalog_attributes import ADMIN, SCHEMA, row

@pytest.fixture
def api(monkeypatch):
    app=Flask(__name__)
    fixture={**row(), 'organization_key':'test','owner_user_id':10,'visibility':'enterprise'}
    monkeypatch.setattr(bit_db_api,'check_catalog_access',lambda *a,**kw:None)
    monkeypatch.setattr(bit_db_api,'get_mercado_collection_items_by_ids',lambda ids:[fixture])
    monkeypatch.setattr('erp.mercadolibre_follow_sell._category_attribute_schema',lambda *a:SCHEMA)
    monkeypatch.setattr('erp.mercadolibre_batch_publish.DatabaseMercadoLibreClient',lambda *a,**kw:object())
    calls=[]
    monkeypatch.setattr(bit_db_api,'update_catalog_attributes',lambda *args:calls.append(args) or {'changed':1,'results':[]})
    register_routes(app,login_required=lambda f:f,internal_required=lambda f:f,reject_client_mode=lambda:None,
                    current_user=lambda:ADMIN,has_permission=lambda *a:True,
                    filtered_tokens=lambda:{'rows':[{'id':7,'site_id':'MLM'}]},publishing=lambda:False)
    return app.test_client(),fixture,calls


def test_read_only_check_and_explicit_generic_write(api):
    client,fixture,calls=api
    response=client.get('/api/catalog/attributes/check?kind=collection&ids=1')
    assert response.status_code==200
    assert response.json['data']['results'][0]['status']=='missing'
    assert not calls
    response=client.patch('/api/catalog/attributes',json={'kind':'collection','ids':[1],'mode':'generic','schemas':{'MLM123':[]}})
    assert response.status_code==200
    assert calls[0][2]['MLM123'] != []  # Client cannot supply or weaken schema rules.
    assert calls[0][3] == {'mode':'generic'}


def test_foreign_read_and_write_blocked(api):
    client,fixture,calls=api
    fixture['organization_key']='foreign'
    assert client.get('/api/catalog/attributes/check?kind=collection&ids=1').status_code==403
    assert client.patch('/api/catalog/attributes',json={'kind':'collection','ids':[1],'mode':'generic'}).status_code==403
    assert not calls


@pytest.mark.parametrize('body',[{},[],{'kind':'bad','ids':[1]},{'kind':'collection','ids':[True],'mode':'generic'},
    {'kind':'collection','ids':[1],'mode':'set','only_missing':'false'}, {'kind':'collection','ids':list(range(1,102)),'mode':'generic'}])
def test_invalid_batch_never_writes(api,body):
    client,fixture,calls=api
    assert client.patch('/api/catalog/attributes',json=body).status_code==400
    assert not calls
