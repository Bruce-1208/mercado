"""Offline category selection uses official IDs and compact image previews."""
import json
from urllib.parse import parse_qs, urlsplit
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_editor_chinese_category_leaf_and_retry(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.route('**/api/store-links/category-paths', lambda r: r.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': {'MLM9': [{'id': 'MLM1', 'name': '家电'}, {'id': 'MLM9', 'name': '冰箱'}]}})))
    failed = []
    def children(route):
        parent = parse_qs(urlsplit(route.request.url).query).get('parent', [''])[0]
        if parent == 'MLM2' and not failed:
            failed.append(True)
            route.fulfill(status=502, content_type='application/json', body=json.dumps({'status': 'error', 'message': '暂不可用'}))
            return
        rows = {'': [{'id': 'MLM1', 'name_zh': '家电'}], 'MLM1': [{'id': 'MLM2', 'name_zh': '洗衣机'}], 'MLM2': []}[parent]
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': rows}))
    page.route('**/api/mercado-products/category-children?*', children)
    page.evaluate('''() => {
        mercadoListMode = 'products';
        mercadoListRows = [{id: 42, title: '模拟商品', source_type: 'pulled', category_id: 'MLM9', currency_id: 'USD', main_image_url: 'https://images.test/a.png', source_snapshot_json: {source: {category_id: 'MLM9'}}}];
        openMercadoProductEditor(42);
    }''')
    from playwright.sync_api import expect
    expect(page.locator('#mercado-product-edit-category-name')).to_have_text('家电 / 冰箱')
    assert page.evaluate('collectMercadoFullEditorChanges()') == {}
    page.get_by_role('button', name='按中文逐级修改分类').click()
    page.get_by_label('第 1 级产品分类', exact=True).select_option(label='家电')
    assert page.locator('#mercado-product-edit-category').input_value() == 'MLM9'
    page.get_by_label('第 2 级产品分类', exact=True).select_option(label='洗衣机')
    expect(page.locator('#mercado-product-edit-category-status')).to_contain_text('暂不可用')
    assert page.locator('#mercado-product-edit-category').input_value() == 'MLM9'
    page.get_by_role('button', name='重试加载', exact=True).click()
    expect(page.locator('#mercado-product-edit-category')).to_have_value('MLM2')
    expect(page.locator('#mercado-product-edit-category-name')).to_have_text('家电 / 洗衣机')
    assert page.evaluate('collectMercadoFullEditorChanges().category_id') == 'MLM2'
    assert page.locator('#mercado-product-edit-gallery img').first.bounding_box()['height'] <= 120
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts/product-category-editor'
    output.mkdir(exist_ok=True)
    page.screenshot(path=str(output / f'editor-{width}.png'))
    page.keyboard.press('Escape')
    assert not page.locator('#mercado-product-edit-dialog').is_visible()
    page.evaluate('openMercadoProductEditor(42)')
    assert page.locator('#mercado-product-edit-category').input_value() == 'MLM9'
