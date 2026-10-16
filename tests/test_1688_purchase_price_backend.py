from erp.ai_original_products import normalize_1688_product
import pytest


@pytest.mark.parametrize('field,value,expected', [
    ('salePrice', {'quantity': 200, 'price': '18.00'}, 18),
    ('priceValue', '26.00', 26),
    ('price', {'price': 1, 'amount': '26.00'}, 26),
])
def test_supported_sku_amount_fields_become_editable_prices(field, value, expected):
    product = normalize_1688_product({
        'source_url': 'https://detail.1688.com/offer/1075332656756.html',
        'title': '测试商品', 'price': None, 'scrape_status': 'ok',
        'variations': [{'id': '11', field: value}],
    })
    assert product['price'] == expected
    assert product['variations'][0]['price'] == expected
    assert product['variations'][0]['net_proceeds_usd'] is not None
    assert product['scrape_status'] == 'ok'


def test_old_collector_cannot_mark_missing_purchase_price_complete():
    product = normalize_1688_product({
        'source_url': 'https://detail.1688.com/offer/1075332656756.html',
        'title': '测试商品', 'price': None, 'scrape_status': 'ok',
        'error_message': '原有提示', 'variations': [{'id': '11', 'price': None}],
    })
    assert product['price'] is None
    assert product['scrape_status'] == 'partial'
    assert '原有提示' in product['error_message']
    assert '采购价' in product['error_message']


def test_saved_sku_price_remains_valid_offer_fallback():
    product = normalize_1688_product({
        'source_url': 'https://detail.1688.com/offer/1075332656756.html',
        'title': '测试商品', 'price': None, 'scrape_status': 'ok',
        'variations': [{'id': '11', 'price': 18.5}],
    })
    assert product['price'] == 18.5
    assert product['scrape_status'] == 'ok'
    assert product['error_message'] == ''


def test_old_collector_one_yuan_prices_are_cleared():
    product = normalize_1688_product({
        'source_url': 'https://detail.1688.com/offer/1069012635162.html',
        'title': '鲨鱼宝宝', 'price': 1, 'scrape_status': 'ok',
        'variations': [{'id': '11', 'price': 1, 'price_text': '¥1'}],
    })
    assert product['price'] is None
    assert product['variations'][0]['price'] is None
    assert product['variations'][0]['price_text'] == ''
    assert product['scrape_status'] == 'partial'
    assert '采购价' in product['error_message']


def test_quantity_in_price_object_is_not_a_cost():
    product = normalize_1688_product({
        'source_url': 'https://detail.1688.com/offer/1069012635162.html',
        'title': '鲨鱼宝宝', 'price': {'beginAmount': 1, 'amount': '26.00'},
    })
    assert product['price'] == 26


def test_old_offer_placeholder_falls_back_to_valid_sku():
    product = normalize_1688_product({
        'source_url': 'https://detail.1688.com/offer/1069012635162.html',
        'title': '鲨鱼宝宝', 'price': 1,
        'variations': [{'id': '11', 'price': 26}],
    })
    assert product['price'] == 26


def test_explicitly_missing_cost_does_not_restore_stale_listing_price():
    import json
    from erp.mercadolibre_collection_store import _mirror_zying_snapshot_fields
    product = _mirror_zying_snapshot_fields({
        'source_type': 'ai_original', 'price': 1,
        'source_snapshot_json': json.dumps({'original_1688': {'price': None, 'variations': []}}),
    })
    assert product['source_purchase_price_cny'] is None
    assert product['suggested_net_proceeds_usd'] is None
