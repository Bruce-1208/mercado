"""SKU image identity, per-SKU economics, persistence and catalog count contracts."""
from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import patch

import pytest
from erp import sku_matrix as sku
from erp import mercadolibre_collection_store as store
from erp.mercadolibre_follow_sell import build_user_product_family_payload
from test_erp_mercadolibre_follow_sell import FamilyClient, family_source
from test_1688_product_management_backend import ProductConnection


@pytest.mark.parametrize('price,expected', [(12.5,3), (18,4), (8.4,2), (15.1,3), (15.11,4), (None,None), (0,None)])
def test_each_sku_pays_five_yuan_before_usd_ceiling(price, expected):
    assert sku.supplier_net_proceeds(price) == expected


def test_family_uses_own_net_and_own_first_image():
    source = family_source()
    source['variations'][0].update(net_proceeds_usd=3, publish_net_proceeds_usd=3.6,
                                  image_url='https://img.test/selected-red.jpg', images=['https://img.test/red-detail.jpg'])
    source['variations'][0].pop('picture_ids', None)
    source['variations'][1]['net_proceeds_usd'] = 7
    payload = build_user_product_family_payload(FamilyClient(), source, {}, net_proceeds=99)
    assert [p['global_net_proceeds'] for p in payload] == [3.6, 7]
    assert payload[0]['pictures'][0]['source'] == 'https://img.test/selected-red.jpg'
    assert payload[0]['pictures'][1]['source'] == 'https://img.test/red-detail.jpg'
    assert len(payload) == 2 and all('variations' not in p for p in payload)


def test_source_edit_persists_order_and_updates_only_matching_generated_sku(monkeypatch):
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    rows = [{'sku_id': 'red', 'price': 12.5, 'image_url': 'https://img.test/red.jpg'},
            {'sku_id': 'blue', 'price': 18, 'image_url': 'https://img.test/blue.jpg'}]
    draft_rows = deepcopy(rows[::-1])
    draft_rows[1].update(image_url='/images/red-ai-white.jpg', white_image_url='/images/red-ai-white.jpg',
                         source_image_url=rows[0]['image_url'], image_generation_method='local_background_removal')
    snapshot = {'original_1688': {'variations': rows}, 'ai_original': {'status': 'completed', 'title_es':'Manual title', 'variations': draft_rows}}
    db = ProductConnection({'id': 7, 'source_type':'ai_original', 'currency_id':'CNY', 'source_snapshot_json':json.dumps(snapshot)})
    edited = deepcopy(rows)
    edited[0].update(price=25, images=['https://img.test/new-red.jpg', 'https://img.test/detail.jpg'], image_url='https://img.test/new-red.jpg')
    store.update_1688_product(7, {'variations': edited}, connection_factory=lambda: db)
    saved = json.loads(db.cursor_object.row['source_snapshot_json'])
    assert saved['original_1688']['variations'][0]['net_proceeds_usd'] == 5
    assert saved['original_1688']['variations'][1]['net_proceeds_usd'] == 4
    assert saved['ai_original']['title_es'] == 'Manual title'
    assert saved['ai_original']['variations'][1]['image_url'].endswith('new-red.jpg')
    assert 'white_image_url' not in saved['ai_original']['variations'][1]
    assert saved['ai_original']['failed_step'] == 'sku_images'
    assert saved['source_1688_overrides']['variations'][0]['images'] == edited[0]['images']
    assert db.committed


def test_supplier_sku_edit_rejects_missing_duplicate_identity_and_invalid_data():
    for rows in [[{}], [{'sku_id':'1'}, {'sku_id':'1'}], [{'sku_id':'1','price':-2}],
                 [{'sku_id':'1','available_quantity':1.5}], [{'sku_id':'1','images':['javascript:alert(1)']}],
                 [{'sku_id':'1','images':12}], [{'sku_id':'1','seller_sku':{}}],
                 [{'sku_id':'1','price':'1e1000'}]]:
        with pytest.raises(ValueError):
            sku.supplier_skus(rows, validate=True)


def test_repeated_sku_cost_edits_refresh_summary_and_do_not_become_manual_net(monkeypatch):
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    rows = [{'sku_id': 'red', 'price': 12.5, 'image_url': 'https://img.test/red.jpg'}]
    db = ProductConnection({'id': 7, 'source_type': 'ai_original', 'currency_id': 'USD', 'price': 99,
                            'source_snapshot_json': json.dumps({'original_1688': {'price': 12.5, 'variations': rows}})})
    for cost, net in [(25, 5), (18, 4)]:
        edited = [{**rows[0], 'price': cost}]
        saved = store.update_1688_product(7, {'variations': edited}, connection_factory=lambda: db)
        assert saved['source_purchase_price_cny'] == cost
        assert saved['source_net_proceeds_usd'] == net
        assert saved['net_proceeds_usd'] == net
        assert saved['price'] == 99
        assert 'net_proceeds_usd' not in saved['source_1688_overrides']


@pytest.mark.parametrize('changed_photo', [False, True])
def test_legacy_single_sku_edit_keeps_generated_cover_or_requests_image_repair(monkeypatch, changed_photo):
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    raw = 'https://img.test/raw.jpg'
    white = '/api/ai-original-products/images/white.jpg'
    snapshot = {
        'original_1688': {'price': 12.5,
                          'main_image_url': raw, 'images': [raw], 'variations': []},
        'ai_original': {'status': 'completed', 'title_es': 'Keep title', 'main_image_url': white,
                        'images': [white], 'image_generation_method': 'local_background_removal'},
        'source': {'id': 'CBT907173727091', 'main_image_url': white, 'pictures': [{'source': white}]},
    }
    db = ProductConnection({'id': 7, 'source_type': 'ai_original', 'currency_id': 'USD',
                            'source_item_id': '1688_907173727091', 'source_snapshot_json': json.dumps(snapshot)})
    selected = 'https://img.test/new.jpg' if changed_photo else raw
    edited = [{'sku_id': '907173727091', 'seller_sku': 'CUSTOM-NEW', 'price': 18,
               'available_quantity': 10, 'image_url': selected, 'images': [selected]}]
    saved = store.update_1688_product(7, {'variations': edited}, connection_factory=lambda: db)
    after = json.loads(saved['source_snapshot_json'])
    draft = after['ai_original']
    assert draft['title_es'] == 'Keep title'
    assert draft['variations'][0]['seller_sku'] == 'CUSTOM-NEW'
    assert draft['variations'][0]['available_quantity'] == 10
    assert draft['source_variations'][0]['source_image_url'] == selected
    if changed_photo:
        assert draft['status'] == 'failed' and draft['failed_step'] == 'sku_images'
        assert draft['main_image_url'] == selected
    else:
        assert draft['status'] == 'completed'
        assert draft['main_image_url'] == white
        assert draft['variations'][0]['image_generation_method'] == 'local_background_removal'
    assert after['original_1688']['price'] == 18


def test_sku_edit_rejects_changed_identity_and_other_owners_before_writes(monkeypatch):
    from bit import product_access
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    row = {'id': 7, 'source_type': 'ai_original', 'source_item_id': '1688_907173727091',
           'organization_key': 'enterprise', 'visibility': 'enterprise', 'owner_user_id': 2,
           'source_snapshot_json': json.dumps({'original_1688': {'variations': []}})}
    db = ProductConnection(row)
    with pytest.raises(ValueError, match='SKU 编号已变化'):
        store.update_1688_product(7, {'variations': [{'sku_id': 'foreign'}]}, connection_factory=lambda: db)
    assert db.rolled_back and not any(q.startswith('UPDATE') for q, _ in db.cursor_object.queries)
    db.cursor_object.fetchall = lambda: [row]
    with product_access.scope({'id': 1, 'role_key': 'member', 'organization_key': 'enterprise'}):
        with pytest.raises(PermissionError, match='只能操作自己的产品'):
            store.update_1688_product(7, {'variations': [{'sku_id': '1688_907173727091'}]}, connection_factory=lambda: db)
    assert not any(q.startswith('UPDATE') for q, _ in db.cursor_object.queries)


def test_count_uses_complete_source_matrix_and_single_sku_default():
    assert sku.sku_count({}) == 1
    assert sku.sku_count({'variations':[{},{}]}) == 2
    assert sku.sku_count({'original_1688':{'variations':[{}, {}, {}]}, 'ai_original':{'variations':[{}]}}) == 3
    assert store._json_safe_row({'source_json':json.dumps({'variations':[{},{}]})})['sku_count'] == 2
    assert 'JSON_TYPE' in sku.sku_count_sql('`source_json`')


@pytest.mark.parametrize('minimum,maximum', [(-1,None), (1.5,None), (5,2), ('bad',None)])
def test_count_filter_rejects_invalid_range_before_query(minimum,maximum):
    with pytest.raises(ValueError, match='SKU'):
        store.list_product_items(sku_count_min=minimum, sku_count_max=maximum,
                                 connection_factory=lambda: pytest.fail('must validate before DB'))


@pytest.mark.parametrize('kind,column', [('collection','source_json'), ('product','source_snapshot_json')])
def test_count_range_applies_in_sql_before_pagination(kind,column):
    from test_erp_mercadolibre_collection_store import _FakeConnection
    db = _FakeConnection()
    function = store.list_collection_items if kind == 'collection' else store.list_product_items
    function(sku_count_min=2, sku_count_max=9, limit=10, offset=20, connection_factory=lambda: db)
    queries = [(q,p) for q,p in db.fake_cursor.queries if 'GREATEST(1,' in q]
    assert len(queries) >= 2
    assert all(f'JSON_VALID(`{column}`)' in q and ') >= %s' in q and ') <= %s' in q for q,p in queries)
    assert all(2 in p and 9 in p for q,p in queries)


def test_recollection_preserves_reviewed_sku_photos_and_costs(monkeypatch):
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    reviewed = sku.supplier_skus([
        {'sku_id': 'red', 'price': 25, 'image_url': 'https://img.test/manual-red.jpg',
         'images': ['https://img.test/manual-red.jpg', 'https://img.test/detail.jpg']},
        {'sku_id': 'blue', 'price': 18, 'image_url': 'https://img.test/blue.jpg'},
    ])
    snapshot = {'original_1688': {'variations': reviewed}, 'ai_original': {'status': 'pending'},
                'source_1688_overrides': {'variations': reviewed}}
    db = ProductConnection({'id': 7, 'source_type': 'ai_original', 'currency_id': 'CNY',
                            'source_item_id': '1688_907173727091', 'source_snapshot_json': json.dumps(snapshot)})
    saved = store.upsert_ai_original_product({
        'source_url': 'https://detail.1688.com/offer/907173727091.html', 'title': 'Recollected', 'price': 10,
        'images': ['https://img.test/cover.jpg'],
        'variations': [{'sku_id': 'blue', 'price': 19, 'image_url': 'https://img.test/raw-blue.jpg'},
                       {'sku_id': 'red', 'price': 11, 'image_url': 'https://img.test/raw-red.jpg'}],
    }, connection_factory=lambda: db)
    actual = {v['sku_id']: v for v in saved['original_1688']['variations']}
    assert actual['red']['images'] == reviewed[0]['images']
    assert actual['red']['price'] == 25 and actual['red']['net_proceeds_usd'] == 5
    assert actual['blue']['image_url'] == reviewed[1]['image_url']
    assert db.committed


def test_recollection_keeps_reviewed_legacy_single_sku(monkeypatch):
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    reviewed = sku.supplier_skus([{'sku_id': '1688_907173727091', 'seller_sku': 'MY-SKU',
                                  'price': 25, 'image_url': 'https://img.test/reviewed.jpg'}])
    snapshot = {'original_1688': {'variations': reviewed}, 'ai_original': {'status': 'pending'},
                'source_1688_overrides': {'variations': reviewed}}
    db = ProductConnection({'id': 7, 'source_type': 'ai_original', 'currency_id': 'CNY',
                            'source_item_id': '1688_907173727091', 'source_snapshot_json': json.dumps(snapshot)})
    saved = store.upsert_ai_original_product({
        'source_url': 'https://detail.1688.com/offer/907173727091.html', 'title': 'Recollected', 'price': 12,
        'images': ['https://img.test/raw.jpg'], 'variations': [],
    }, connection_factory=lambda: db)
    actual = saved['original_1688']['variations']
    assert len(actual) == 1 and actual[0]['seller_sku'] == 'MY-SKU'
    assert actual[0]['price'] == saved['source_purchase_price_cny'] == 25
    assert actual[0]['image_url'] == 'https://img.test/reviewed.jpg'
    assert saved['source_1688_overrides']['variations'] == reviewed
