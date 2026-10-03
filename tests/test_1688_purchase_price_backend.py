from erp.ai_original_products import normalize_1688_product


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
