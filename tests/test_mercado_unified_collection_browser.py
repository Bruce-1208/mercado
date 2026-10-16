import json

import pytest
from jinja2 import Environment, FileSystemLoader
from bit.mercado_collection_detail import build_collection_detail
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_unified_details_and_sku_save(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    source = {'category_id': 'MLM123', 'variations': [{'id': 11, 'seller_sku': 'ORIGINAL', 'available_quantity': 2,
              'attribute_combinations': [{'id': 'COLOR', 'name': 'Color', 'value_name': 'Blue'}]}]}
    row = {'id': 91, 'task_id': 1, 'source_item_id': 'MLM123456', 'title': '模拟采集商品', 'category_id': 'MLM123',
           'currency_id': 'MXN', 'source_json': json.dumps(source), 'can_operate': True}
    env = Environment(loader=FileSystemLoader(str(ROOT / 'bit/templates')), autoescape=True)
    detail = env.get_template('mercado_collection_detail.html').render(detail=build_collection_detail(row), error='', url_for=lambda *a, **k: '/static/' + k['filename'])
    page.route('**/mercado-collection/items/91', lambda route: route.fulfill(content_type='text/html', body=detail))
    page.route('**/api/mercado-collection/items?*', lambda route: route.fulfill(json={'status': 'success', 'data': {'rows': [row], 'total': 1}}))
    page.route('**/api/catalog/attributes/check?*', lambda route: route.fulfill(json={'status': 'success', 'data': {'results': []}}))
    saved = []
    def save(route):
        saved.append(route.request.post_data_json)
        route.fulfill(json={'status': 'success', 'data': {'changed': 1}})
    page.route('**/api/mercado-collection/bulk-edit', save)
    page.evaluate("switchTab('mercado-collection')")
    button = page.get_by_role('button', name='详情与 SKU', exact=True)
    button.wait_for()
    page.locator('.mercado-item-select[value="91"]').check()
    button.click()
    dialog = page.locator('#mercado-product-edit-dialog')
    dialog.locator('.detail-title').wait_for()
    assert '模拟采集商品' in dialog.inner_text()
    dialog.get_by_role('button', name='变体 SKU', exact=True).click()
    assert page.evaluate("document.activeElement.id") == 'mercado-editor-variants'
    sku = dialog.locator('input[value="ORIGINAL"]')
    sku.fill('CHANGED')
    sku.dispatch_event('change')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    artifact = ROOT / 'artifacts/mercado-unified-collection-20261015'
    artifact.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact / f'sku-{width}.png'))
    dialog.get_by_role('button', name='保存修改', exact=True).click()
    page.wait_for_function('!document.getElementById("mercado-product-edit-dialog").open')
    assert saved[0]['collection_item_ids'] == [91]
    assert saved[0]['changes']['variations'][0]['seller_sku'] == 'CHANGED'
    button.click()
    dialog.locator('.detail-title').wait_for()
    page.keyboard.press('Escape')
    assert not dialog.is_visible()
    assert button.evaluate('el => document.activeElement === el')
