"""ZYing-style full editing, using only offline template and mock products."""
import json
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_full_product_editor_roundtrip_gallery_and_keyboard(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('https://images.test/**', lambda route: route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"><rect width="160" height="160" fill="#e0edff"/><text x="35" y="85" font-size="20">产品图片</text></svg>'))
    page.evaluate('''() => {
        window.fullEditorRow = {id: 42, source_type: 'zying', source_item_id: 'ZY42',
            title: '智赢格式模拟产品 · 不锈钢杯', price: 30, currency_id: 'USD', net_proceeds_usd: 12,
            weight_g: 250, package_length_cm: 20, package_width_cm: 10, package_height_cm: 8,
            main_image_url: 'https://images.test/0.svg', source_url: 'https://example.test/item',
            description_text: '完整产品描述', source_snapshot_json: {source: {
                pictures: Array.from({length: 7}, (_, i) => ({id: 'P' + i, source: 'https://images.test/' + i + '.svg'})),
                available_quantity: 15, condition: 'new',
                attributes: [{id: 'MATERIAL', name: '材质', value_name: '钢', value_id: 'old-id'}],
                sale_terms: [{id: 'WARRANTY_TIME', name: '保修期', value_name: '12 个月'}],
                variations: [{seller_sku: 'CUP-BLUE', available_quantity: 10, price: 30,
                    attribute_combinations: [{id: 'COLOR', name: '颜色', value_name: '蓝色', value_id: 'blue-id'}],
                    picture_ids: Array.from({length: 7}, (_, i) => 'P' + i)}]
            }}};
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-mercado-products').classList.add('active');
        document.getElementById('tab-mercado-products').appendChild(document.getElementById('mercado-list-module'));
        mercadoListRows = [fullEditorRow]; mercadoListMode = 'products'; renderMercadoListRows(mercadoListRows);
    }''')
    page.locator('.market-row-select[value="42"]').check()
    page.locator('.market-product-edit-button').click()
    dialog = page.locator('#mercado-product-edit-dialog')
    assert dialog.is_visible()
    assert page.locator('#mercado-product-edit-title').evaluate('el => el === document.activeElement')
    assert page.locator('#mercado-product-edit-gallery img').count() == 7
    assert page.locator('#mercado-product-edit-variations .zs-sku-selected img').count() == 7
    assert page.evaluate('collectMercadoFullEditorChanges()') == {}
    page.locator('#mercado-product-edit-net').fill('19.5')
    page.locator('#mercado-product-edit-weight').fill('450')
    page.locator('#mercado-product-edit-length').fill('25')
    page.locator('#mercado-product-edit-attributes [data-field="value_name"]').fill('不锈钢')
    page.locator('[data-variant-field="sku"]').fill('EDITED-SKU')
    page.get_by_role('button', name='＋ 添加变体', exact=True).click()
    assert page.locator('[data-variant-field="sku"]').first.input_value() == 'EDITED-SKU'
    page.locator('#mercado-product-edit-variations tr').last.get_by_role('button', name='删除').click()
    assert page.locator('[data-variant-field="sku"]').input_value() == 'EDITED-SKU'
    page.locator('.mercado-editor-nav').get_by_role('button', name='产品图片').click()
    page.locator('.mercado-editor-picture').nth(1).get_by_role('button', name='设为主图').click()
    assert page.locator('#mercado-product-edit-image').input_value() == 'https://images.test/1.svg'
    artifact = ROOT / 'artifacts/product-full-editor-20261013'
    artifact.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact / f'images-{width}.png'))
    page.locator('.mercado-editor-nav').get_by_role('button', name='产品属性').click()
    assert page.locator('#mercado-editor-attributes').evaluate('el => el === document.activeElement')
    page.keyboard.press('Tab')
    assert page.locator('#mercado-product-edit-attributes input').first.evaluate('el => el === document.activeElement')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert dialog.evaluate('el => el.scrollWidth <= el.clientWidth')
    page.screenshot(path=str(artifact / f'attributes-{width}.png'))
    saved = []
    def save(route):
        payload = route.request.post_data_json
        saved.append(payload)
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': {}}))
    page.route('**/api/mercado-products/42', save)
    page.locator('#mercado-product-edit-save').click()
    page.wait_for_function('!document.getElementById("mercado-product-edit-dialog").open && !mercadoProductEditSaving')
    assert len(saved) == 1
    assert saved[0]['net_proceeds_usd'] == 19.5
    assert saved[0]['weight_g'] == 450
    assert saved[0]['package_length_cm'] == 25
    assert saved[0]['attributes'][0]['value_name'] == '不锈钢'
    assert 'value_id' not in saved[0]['attributes'][0]
    assert saved[0]['variations'][0]['seller_sku'] == 'EDITED-SKU'
    assert saved[0]['variations'][0]['attribute_combinations'][0]['value_id'] == 'blue-id'
    assert 'price' not in saved[0]  # Do not invalidate unchanged pricing.
    page.evaluate('''changes => {
        for (const [key, value] of Object.entries(changes)) {
            if (['pictures', 'attributes', 'variations', 'sale_terms', 'variation_dimensions'].includes(key)) fullEditorRow.source_snapshot_json.source[key] = value;
            else fullEditorRow[key] = value;
        }
        mercadoListRows = [fullEditorRow]; selectedMercadoCollectionIds.add(42); renderMercadoListRows(mercadoListRows); openMercadoProductEditor(42);
    }''', saved[0])
    assert page.locator('#mercado-product-edit-net').input_value() == '19.5'
    assert page.locator('#mercado-product-edit-gallery img').count() == 7
    # Editing then cancelling must leave the cached row and selection intact.
    before = page.evaluate('JSON.stringify(fullEditorRow)')
    page.locator('.mercado-editor-nav').get_by_role('button', name='产品图片').click()
    page.locator('.mercado-editor-picture').last.get_by_role('button', name='删除').click()
    assert page.locator('#mercado-product-edit-gallery img').count() == 6
    assert page.locator('#mercado-product-edit-variations .zs-sku-selected img').count() == 6
    page.keyboard.press('Escape')
    assert not dialog.is_visible()
    assert page.evaluate('JSON.stringify(fullEditorRow)') == before
    assert page.locator('.market-row-select[value="42"]').is_checked()
    assert not errors
