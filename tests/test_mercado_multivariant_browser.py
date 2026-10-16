"""Real workbench multi-SKU editing with mocked products and requests only."""
import json

import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401
from test_mercado_multivariant import collected_source


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_multivariant_editor_preserves_other_skus_and_selection(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.route('https://images.test/**', lambda route: route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="#cbd5e1"/></svg>'))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.evaluate('''source => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-mercado-products').classList.add('active');
        document.getElementById('tab-mercado-products').appendChild(document.getElementById('mercado-list-module'));
        window.matrixRow = {id: 43, source_type: 'collected', source_item_id: source.id, title: source.title,
            main_image_url: 'https://images.test/Red.jpg', source_url: 'https://example.test/item',
            weight_g: 250, package_length_cm: 20, package_width_cm: 10, package_height_cm: 8,
            net_proceeds_usd: 20, description_text: '模拟多变体商品',
            price: 120, currency_id: 'MXN', review_status: 'approved', source_snapshot_json: {source}};
        mercadoListRows = [matrixRow, {...matrixRow, id: 44}]; mercadoListMode = 'products'; renderMercadoListRows(mercadoListRows);
    }''', collected_source())
    page.locator('.market-row-select[value="43"]').check()
    page.locator('.market-row-select[value="44"]').check()
    page.locator('.market-product-edit-button').first.click()
    assert page.locator('#mercado-product-edit-title').evaluate('el => el === document.activeElement')
    page.locator('.mercado-editor-nav').get_by_role('button', name='变体 SKU', exact=True).click()
    assert page.locator('[data-variant-field="sku"]').count() == 4
    assert page.get_by_role('spinbutton', name='变体 1 库存', exact=True).input_value() == '0'
    assert page.locator('#mercado-product-edit-variations .zs-sku-selected .is-main img').count() == 4
    page.get_by_role('spinbutton', name='变体 2 库存', exact=True).fill('12')
    page.get_by_role('textbox', name='变体 2 SKU', exact=True).fill('RED-M-EDITED')
    page.keyboard.press('Tab')
    assert page.get_by_role('spinbutton', name='变体 2 库存', exact=True).evaluate('el => el === document.activeElement')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('#mercado-product-edit-dialog').evaluate('el => el.scrollWidth <= el.clientWidth')
    artifact = ROOT / 'artifacts/mercado-multivariant-20261013'
    artifact.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact / f'editor-{width}.png'))
    saved = []
    def save(route):
        saved.append(route.request.post_data_json)
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': {}}))
    page.route('**/api/mercado-products/43', save)
    assert page.evaluate('Array.from(mercadoProductEditForm.querySelectorAll(":invalid")).map(el => [el.id, el.value, el.validationMessage])') == []
    assert page.locator('.market-row-select[value="43"]').is_checked()
    assert page.locator('.market-row-select[value="44"]').is_checked()
    page.locator('#mercado-product-edit-save').click()
    page.wait_for_function('!document.getElementById("mercado-product-edit-dialog").open && !mercadoProductEditSaving')
    variants = saved[0]['variations']
    assert [v.get('available_quantity') for v in variants] == [0, 12, 7, None]
    assert [v['seller_sku'] for v in variants] == ['Red-S', 'RED-M-EDITED', 'Blue-S', 'Blue-M']
    assert all(len(v['attribute_combinations']) == 2 for v in variants)
    assert not errors
