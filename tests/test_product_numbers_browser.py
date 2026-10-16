"""Number display, keyboard selection and navigation with offline product data."""
import json

import pytest

from erp.mercadolibre_collection_store import _json_safe_row
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_product_numbers_remain_stable(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    rows = [_json_safe_row(dict(id=i, collection_item_id=None, source_type=source,
                               title='离线测试产品', source_item_id=f'SOURCE-{i}'))
            for i, source in enumerate(['collected', 'pulled', 'zying', 'ai_original'], 1)]
    page.route('**/api/mercado-products?*', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({
            'status': 'success', 'data': {'rows': rows, 'total': len(rows)}})))
    page.evaluate("switchTab('mercado-products')")
    page.wait_for_function('mercadoListRows.length === 4')
    numbers = page.locator('#mercado-list-body .market-product-number')
    assert numbers.all_text_contents() == [f'10000000{i}' for i in range(1, 5)]
    image = page.locator('#mercado-list-body .market-card-image').first
    assert numbers.first.bounding_box()['y'] >= image.bounding_box()['y'] + image.bounding_box()['height']
    assert 'SOURCE-' not in page.locator('#mercado-list-body').inner_text()
    assert page.locator('.market-card-detail-trigger, .market-card-detail-content').count() == 0
    checkboxes = page.locator('#mercado-list-body .mercado-item-select')
    checkboxes.first.focus()
    assert checkboxes.first.evaluate('el => el === document.activeElement')
    page.keyboard.press('Space')
    checkboxes.nth(1).check()
    assert page.evaluate('Array.from(selectedMercadoCollectionIds)') == [1, 2]
    page.evaluate('renderMercadoListRows([...mercadoListRows].reverse())')
    assert numbers.all_text_contents() == [f'10000000{i}' for i in range(4, 0, -1)]
    assert page.locator('#mercado-list-body .mercado-item-select:checked').count() == 2
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    artifacts = ROOT / 'artifacts/product-numbers'
    artifacts.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifacts / f'products-{width}.png'), full_page=True)
    page.evaluate("switchTab('mercado-collection'); switchTab('mercado-products')")
    page.wait_for_function('mercadoListRows.length === 4')
    assert numbers.first.inner_text() == '100000001'


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_collection_number_below_image_survives_add(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    rows = [_json_safe_row(dict(id=i, task_id=27, title='离线跟卖产品',
                               source_item_id=f'MLM{i}', weight_g=100)) for i in [17, 18]]
    for row in rows:
        row['can_claim'] = True
    products = [_json_safe_row(dict(row, id=854+i, collection_item_id=row['id'],
                                    source_type='collected')) for i, row in enumerate(rows)]
    added = []

    def add(route):
        added.extend(route.request.post_data_json['collection_item_ids'])
        route.fulfill(content_type='application/json', body=json.dumps({
            'status': 'success', 'data': {'count': len(added)}}))

    page.route('**/api/mercado-collection/items?*', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({
            'status': 'success', 'data': {'rows': [] if added else rows,
                                        'total': 0 if added else len(rows)}})))
    page.route('**/api/mercado-products?*', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({
            'status': 'success', 'data': {'rows': products, 'total': len(products)}})))
    page.route('**/api/mercado-products/add', add)
    page.evaluate("switchTab('mercado-collection')")
    page.wait_for_function('mercadoListRows.length === 2')
    numbers = page.locator('#mercado-list-body .market-product-number')
    assert numbers.all_text_contents() == ['900000017', '900000018']
    image = page.locator('#mercado-list-body [data-label="主图"] .market-product-image').first
    assert numbers.first.bounding_box()['y'] >= image.bounding_box()['y'] + image.bounding_box()['height']
    checkboxes = page.locator('#mercado-list-body .mercado-item-select')
    checkboxes.first.focus()
    assert checkboxes.first.evaluate('el => el === document.activeElement')
    page.keyboard.press('Space')
    checkboxes.nth(1).check()
    assert page.evaluate('Array.from(selectedMercadoCollectionIds)') == [17, 18]
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    artifacts = ROOT / 'artifacts/product-numbers'
    artifacts.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifacts / f'collection-{width}.png'), full_page=True)
    page.locator('#mercado-add-selected').click()
    page.wait_for_function('mercadoListRows.length === 0')
    assert added == [17, 18]
    page.evaluate("switchTab('mercado-products')")
    page.wait_for_function('mercadoListRows.length === 2')
    assert numbers.all_text_contents() == ['900000017', '900000018']
