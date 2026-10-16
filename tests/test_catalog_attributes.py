import copy
import json
import pytest
from erp.catalog_attributes import inspect, apply
from erp import mercadolibre_collection_store as store
from bit import product_access
from test_purchase_net_proceeds_persistence import database, _row  # noqa: F401

SCHEMA = [
    {'id':'BRAND','name':'品牌','tags':{'required':True}},
    {'id':'MODEL','name':'型号','tags':{'required':True}},
    {'id':'COLOR','name':'颜色','tags':{'required':True},'values':[{'id':'red','name':'Red'},{'id':'blue','name':'Blue'}]},
    {'id':'MATERIAL','name':'材质'},
    {'id':'UNUSED','name':'未使用选填属性'},
    {'id':'SYSTEM','tags':{'required':True,'read_only':True}},
]
ADMIN = {'id':10, 'role_key':'enterprise_admin', 'organization_key':'test'}

def row(kind='collection', attrs=None, variants=None):
    source = {'category_id':'MLM123','attributes':attrs or [], 'variations':variants or []}
    result = {'id':1,'category_id':'MLM123', 'source_type':'ai_original' if kind in {'supplier','original'} else 'collected'}
    if kind == 'collection': result['source_json'] = source
    else: result['source_snapshot_json'] = { {'supplier':'original_1688','original':'ai_original'}.get(kind,'source'):source }
    return result

@pytest.mark.parametrize('kind', ['collection','products','supplier','original'])
def test_missing_required_and_optional_empty_generic_preserves_choices_and_existing(kind):
    product=row(kind,[{'id':'BRAND','value_name':'Acme'},{'id':'MATERIAL','value_name':' '}])
    before=copy.deepcopy(product)
    report=inspect(product,kind,SCHEMA)
    assert {m['attribute_id'] for m in report['missing']} == {'MODEL','COLOR','MATERIAL'}
    source,count,after=apply(product,kind,SCHEMA,{'mode':'generic'})
    assert count == 2
    assert {a['id']:a.get('value_name') for a in source['attributes']} == {'BRAND':'Acme','MODEL':'generic','MATERIAL':'generic'}
    assert [m['attribute_id'] for m in after['missing']] == ['COLOR']
    assert after['missing'][0]['is_choice']
    assert product == before


def test_sku_empty_override_filled_without_overwriting_other_skus():
    product=row(attrs=[{'id':'BRAND','value_name':'Acme'},{'id':'COLOR','value_id':'red'}],variants=[
      {'seller_sku':'A','attributes':[{'id':'MODEL','value_name':'Known'}]},
      {'seller_sku':'B','attribute_combinations':[{'id':'MODEL','value_name':''}]}])
    source,count,after=apply(product,'collection',SCHEMA,{'mode':'generic'})
    assert count == 1 and after['status'] == 'complete'
    assert source['variations'][0]['attributes'][0]['value_name'] == 'Known'
    assert source['variations'][1]['attribute_combinations'][0]['value_name'] == 'generic'


def test_shared_omission_goes_on_parent_and_zero_false_preserved():
    product=row(attrs=[{'id':'BRAND','value_name':0},{'id':'COLOR','value_name':False}],variants=[{'seller_sku':'A'},{'seller_sku':'B'}])
    source,count,after=apply(product,'collection',SCHEMA,{'mode':'generic'})
    assert count == 1 and after['status'] == 'complete'
    assert source['attributes'][-1] == {'id':'MODEL','name':'型号','value_name':'generic'}


def test_manual_choice_validated_per_category_and_no_optional_defaults():
    product=row()
    with pytest.raises(ValueError,match='有效选项'):
        apply(product,'collection',SCHEMA,{'mode':'set','category_id':'MLM123','attribute_id':'COLOR','value':'generic'})
    source,count,after=apply(product,'collection',SCHEMA,{'mode':'set','category_id':'MLM123','attribute_id':'COLOR','value':'blue'})
    assert source['attributes'] == [{'id':'COLOR','name':'颜色','value_name':'Blue','value_id':'blue'}]
    assert count == 1
    assert apply(product,'collection',SCHEMA,{'mode':'set','category_id':'MLB999','attribute_id':'COLOR','value':'blue'})[1] == 0


def test_no_schema_no_category_and_conditional_do_not_fill():
    assert apply(row(),'collection',None,{'mode':'generic'})[1] == 0
    product=row();product['category_id']='';product['source_json']['category_id']=''
    assert inspect(product,'collection',SCHEMA)['status'] == 'unavailable'
    schema=[{'id':'CONDITIONAL','tags':{'conditional_required':True}}]
    assert apply(row(),'collection',schema,{'mode':'generic'})[1] == 0


@pytest.mark.parametrize('kind', ['collection','products','supplier','original'])
def test_real_attribute_transaction_persists_only_attributes_and_review(database,kind):
    seeded=_row(source_type='ai_original' if kind in {'supplier','original'} else 'collected',purchased=False)
    source=row(kind)
    for field in ['source_json','source_snapshot_json']:
        if field in source:seeded[field]=json.dumps(source[field])
    seeded.update(organization_key='test',owner_user_id=10,owner_name='主管',visibility='enterprise',category_id='MLM123')
    table=store.COLLECTION_TABLE if kind=='collection' else store.PRODUCT_TABLE
    database.insert(table,**seeded);database.commit()
    before=database.row(table)
    with product_access.scope(ADMIN):
        result=store.update_catalog_attributes(kind,[1],{'MLM123':SCHEMA},{'mode':'generic'},connection_factory=lambda:database)
    assert result['changed']==1
    saved=database.row(table)
    for field in ['title','price','weight_g','main_image_url','net_proceeds_usd']:
        assert saved[field]==before[field]
    assert inspect(saved,kind,SCHEMA)['missing_count']==1
    if kind != 'collection': assert saved['review_status']=='unreviewed'
    with product_access.scope(ADMIN):
        result=store.update_catalog_attributes(kind,[1],{'MLM123':SCHEMA},{'mode':'generic'},connection_factory=lambda:database)
    assert result['changed']==0


def test_whole_batch_rejected_for_foreign_product_and_rollback(database):
    for owner in [10,99]:
        seeded=_row(source_type='collected',purchased=False)
        seeded.update(id=1 if owner==10 else 2,source_item_id=f'item{owner}',organization_key='test' if owner==10 else 'foreign',owner_user_id=owner,source_json=json.dumps(row()['source_json']))
        database.insert(store.COLLECTION_TABLE,**seeded)
    database.commit()
    before=database.row(store.COLLECTION_TABLE)
    with product_access.scope(ADMIN),pytest.raises(PermissionError):
        store.update_catalog_attributes('collection',[1,2],{'MLM123':SCHEMA},{'mode':'generic'},connection_factory=lambda:database)
    assert database.row(store.COLLECTION_TABLE)==before


def test_publish_records_sorted_by_start_before_pagination(database):
    database.db.execute(f'CREATE TABLE {store.PUBLISH_RECORD_TABLE} (id INTEGER, status TEXT, token_id INTEGER, site_id TEXT, product_item_id INTEGER, started_at TEXT, created_at TEXT)')
    database.db.execute('ALTER TABLE mercado_store_site_settings ADD COLUMN group_name TEXT')
    for record in [(99,'2026-10-01 10:00:00','2026-10-14 12:00:00'),
                   (1,'2026-10-14 13:00:00','2026-10-01 10:00:00'),
                   (2,None,'2026-10-14 14:00:00'),
                   (3,'2026-10-14 13:00:00','2026-10-01 10:00:00')]:
        database.db.execute(f'INSERT INTO {store.PUBLISH_RECORD_TABLE} VALUES (?,"published",1,"MLM",NULL,?,?)',record)
    database.commit()
    first=store.list_product_publish_records(limit=2,connection_factory=lambda:database)
    second=store.list_product_publish_records(limit=2,offset=2,connection_factory=lambda:database)
    assert [r['id'] for r in first['rows']] == [2,3]
    assert [r['id'] for r in second['rows']] == [1,99]


def test_manual_batch_can_fill_absent_optional_without_changing_known_values():
    product=row(attrs=[{'id':'BRAND','value_name':'Acme'}])
    source,count,_=apply(product,'collection',SCHEMA,{'mode':'set','category_id':'MLM123','attribute_id':'MATERIAL','value':'Steel'})
    assert count==1 and source['attributes'][-1]['value_name']=='Steel'
    product['source_json']=source
    assert apply(product,'collection',SCHEMA,{'mode':'set','category_id':'MLM123','attribute_id':'MATERIAL','value':'Wood'})[1]==0
    updated,count,_=apply(product,'collection',SCHEMA,{'mode':'set','category_id':'MLM123','attribute_id':'MATERIAL','value':'Wood','only_missing':False})
    assert count==1 and updated['attributes'][-1]['value_name']=='Wood'


@pytest.mark.parametrize('kind',['collection','products'])
def test_linked_copies_fill_their_own_blanks_without_overwriting_known_values(database,kind):
    for table,scope in [(store.COLLECTION_TABLE,'collection'),(store.PRODUCT_TABLE,'products')]:
        seeded=_row(source_type='collected',purchased=False)
        facts=row(scope,attrs=[{'id':'BRAND','value_name':'Own brand'}] if scope=='products' else [])
        seeded.update(organization_key='test',owner_user_id=10,category_id='MLM123',**{
            k:json.dumps(v) for k,v in facts.items() if k in {'source_json','source_snapshot_json'}})
        database.insert(table,**seeded)
    database.commit()
    with product_access.scope(ADMIN):
        result=store.update_catalog_attributes(kind,[1],{'MLM123':SCHEMA},{'mode':'generic'},connection_factory=lambda:database)
    assert result['changed']==1
    for table,scope in [(store.COLLECTION_TABLE,'collection'),(store.PRODUCT_TABLE,'products')]:
        assert [m['attribute_id'] for m in inspect(database.row(table),scope,SCHEMA)['missing']] == ['COLOR']
    saved=json.loads(database.row(store.PRODUCT_TABLE)['source_snapshot_json'])['source']
    assert saved['attributes'][0]['value_name']=='Own brand'
