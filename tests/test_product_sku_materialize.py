import copy
from erp.product_sku_materialize import merge, variants
from erp.product_sku_groups import identity


def product(id, color):
    return dict(id=id, title=f'Comfortable cotton summer shirt {color}', source_type='pulled',
                source_item_id=f'MLB{id}', price=id*10, weight_g=100, currency_id='USD',
                main_image_url=f'https://images.test/{id}.jpg', package_length_cm=20, package_width_cm=10, package_height_cm=5,
                source_snapshot_json={'source': {'attributes': [
                    {'id':'COLOR', 'value_name':color}, {'id':'SELLER_SKU', 'value_name':f'123456789-{color}'}],
                    'available_quantity':id, 'pictures':[{'id':f'P{id}', 'url':f'https://images.test/{id}.jpg'}]},
                    'plugin_snapshot':{'token_id':1, 'site_id':'MLB'}})


def test_merge_preserves_each_sku_and_clean_title():
    rows = [product(1,'Red'), product(2,'Blue')]
    original = copy.deepcopy(rows)
    title, saved = merge(rows[1], rows, 'family')
    assert title == 'Comfortable cotton summer shirt'
    assert rows == original
    matrix = saved['source']['variations']
    assert [v['seller_sku'] for v in matrix] == ['123456789-Red', '123456789-Blue']
    assert [v['available_quantity'] for v in matrix] == [1,2]
    assert [v['price'] for v in matrix] == [10,20]
    assert matrix[0]['images'] == ['https://images.test/1.jpg']
    assert all(v['sku_images_customized'] for v in matrix)
    assert saved['source']['attributes'] == []
    parent = dict(rows[1], title=title, source_snapshot_json=saved)
    assert identity(parent) == ('family', title, '')
    assert merge(parent, rows, 'family') is None


def test_resync_keeps_edits_and_removed_skus_and_appends_new_member():
    rows = [product(1,'Red'), product(2,'Blue')]
    title, saved = merge(rows[1], rows, 'family')
    saved['source']['variations'].pop(0)
    saved['source']['variations'][0]['price'] = 99
    parent = dict(rows[1], title='Edited product', source_snapshot_json=saved)
    title, newer = merge(parent, [*rows, product(3,'Black')], 'family')
    assert title == 'Edited product'
    assert [v['source_product_item_id'] for v in newer['source']['variations']] == [2,3]
    assert newer['source']['variations'][0]['price'] == 99


def test_native_variants_keep_provenance_and_picture_selection():
    row = product(1,'Red')
    row['source_snapshot_json']['source']['variations'] = [
        {'id':123, 'seller_sku':'native', 'price':7, 'picture_ids':['P1'],
         'attribute_combinations':[{'id':'SIZE','value_name':'M'}]}]
    variant = variants(row)[0]
    assert 'id' not in variant
    assert variant['source_variation_id'] == 123
    assert variant['seller_sku'] == 'native'
    assert variant['price'] == 7


def test_persisted_merge_is_owner_scoped_and_editor_round_trips(monkeypatch):
    import json
    import re
    from test_purchase_net_proceeds_persistence import _Connection, _Cursor
    from erp import mercadolibre_collection_store as store
    from erp.product_sku_materialize import materialize, TABLE
    from erp.product_sku_groups import TABLE as GROUPS

    class Cursor(_Cursor):
        def execute(self, query, params=None):
            if 'ON DUPLICATE KEY UPDATE' in query and (TABLE in query or GROUPS in query):
                query = query.replace('ON DUPLICATE KEY UPDATE', 'ON CONFLICT(product_id) DO UPDATE SET')
                query = re.sub(r'VALUES\((\w+)\)', r'excluded.\1', query)
            super().execute(query, params)
    class Connection(_Connection):
        def cursor(self):
            return Cursor(self)
    db = Connection()
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    def insert(id, owner):
        row = product(id, 'Red' if id % 2 else 'Blue')
        row.update(owner_name=owner, organization_key='one', visibility='enterprise')
        row['source_snapshot_json'] = json.dumps(row['source_snapshot_json'])
        db.insert(store.PRODUCT_TABLE, **row)
        db.db.execute(f'INSERT INTO {GROUPS} VALUES (?, ?, ?, ?)', (id,'family','Shirt',''))
    for id, owner in [(1,'A'),(2,'A'),(3,'B'),(4,'B')]:
        insert(id,owner)
    assert materialize(db.cursor(), ['family']) == 2
    saved = json.loads(db.row(store.PRODUCT_TABLE,2)['source_snapshot_json'])
    assert saved['sku_family']['member_ids'] == [1,2]
    matrix = saved['source']['variations']
    matrix[0]['seller_sku'] = 'MY-EDIT'
    store.update_product_item(2, {'variations':matrix}, connection_factory=lambda:db)
    insert(5,'A')
    assert materialize(db.cursor(), ['family']) == 1
    saved = json.loads(db.row(store.PRODUCT_TABLE,2)['source_snapshot_json'])
    assert saved['sku_family']['member_ids'] == [1,2,5]
    assert saved['source']['variations'][0]['seller_sku'] == 'MY-EDIT'
    assert materialize(db.cursor(), ['family']) == 0
    store.upsert_pulled_store_links_to_products(
        {'id':1, 'store_name':'Test', 'organization_key':'one',
         'site_settings':[{'site_id':'MLB','salesperson':'A'}]},
        [{'id':'MLB2', 'title':'Remote title Blue', 'price':77, 'currency_id':'USD',
          'permalink':'https://example.test/2', 'category_id':'MLB1',
          'pictures':[{'secure_url':'https://images.test/new.jpg'}]}], connection_factory=lambda:db)
    after_sync = db.row(store.PRODUCT_TABLE,2)
    assert after_sync['title'] == 'Comfortable cotton summer shirt'
    assert json.loads(after_sync['source_snapshot_json'])['source']['variations'][0]['seller_sku'] == 'MY-EDIT'
    assert db.db.execute(f'SELECT COUNT(*) FROM {TABLE} WHERE parent_id=2').fetchone()[0] == 3
    assert db.db.execute(f'SELECT COUNT(*) FROM {TABLE} WHERE parent_id=4').fetchone()[0] == 2
    db.db.close()
