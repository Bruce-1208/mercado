import json
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401
from test_product_sku_materialize import product
from erp.product_sku_materialize import merge


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_synced_product_uses_standard_card_and_edits_sku_children(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    rows = [product(11, 'Red'), product(12, 'Blue'), product(13, 'Black')]
    title, saved = merge(rows[-1], rows, 'family')
    parent = dict(rows[-1], title=title, source_snapshot_json=saved, sku_count=3, can_operate=True)
    other = dict(product(14, 'White'), can_operate=False, sku_count=1)
    page.evaluate('''rows => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-mercado-products').classList.add('active');
        document.getElementById('mercado-list-products-host').append(document.getElementById('mercado-list-module'));
        mercadoListMode='products';
        document.getElementById('mercado-list-module').classList.remove('collection-mode');
        document.getElementById('mercado-list-module').classList.add('product-mode');
        document.getElementById('mercado-product-actions').style.display='flex';
        window.skuTestRows=rows;
        renderMercadoListRows(rows);
        mercadoListTotal=2;mercadoListPage=1;mercadoListPageSize=20;updateMercadoListPagination();
    }''', [parent, other])
    body = page.locator('#mercado-list-body')
    assert body.locator(':scope > tr').count() == 2
    assert body.locator('.market-sku-group, details').count() == 0
    assert '多 SKU 产品' not in body.inner_text()
    assert body.inner_text().count('店铺同步') == 2
    assert '本页 2 件' in page.locator('#mercado-page-range').inner_text()
    checkbox = page.locator('.market-row-select[value="13"]')
    checkbox.focus(); page.keyboard.press('Space')
    assert page.evaluate('Array.from(selectedMercadoCollectionIds)') == [13]
    assert body.locator('.market-product-edit-button').nth(1).is_disabled()
    output = ROOT / 'artifacts/product-sku-groups'; output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'products-{width}.png'))
    body.locator('.market-product-edit-button').first.click()
    assert page.locator('#mercado-product-edit-title').input_value() == title
    assert page.locator('#mercado-product-edit-title').evaluate('el => el === document.activeElement')
    skus = page.locator('[data-variant-field="sku"]')
    assert skus.count() == 3
    assert skus.first.input_value() == '123456789-Red'
    page.locator('.mercado-editor-nav').get_by_role('button', name='变体 SKU', exact=False).click()
    skus.first.fill('EDITED-RED')
    captured = []
    def save(route):
        captured.append(route.request.post_data_json)
        route.fulfill(content_type='application/json', body=json.dumps({'status':'success','data':{}}))
    page.route('**/api/mercado-products/13', save)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(output / f'editor-{width}.png'))
    page.locator('#mercado-product-edit-save').click()
    page.wait_for_function('!document.getElementById("mercado-product-edit-dialog").open && !mercadoProductEditSaving')
    assert len(captured) == 1
    assert captured[0]['variations'][0]['seller_sku'] == 'EDITED-RED'
    assert captured[0]['variations'][0]['source_product_item_id'] == 11
    page.evaluate('''changes => {
        skuTestRows[0].source_snapshot_json.source.variations=changes.variations;
        renderMercadoListRows(skuTestRows);openMercadoProductEditor(13);
    }''', captured[0])
    assert skus.first.input_value() == 'EDITED-RED'
    page.keyboard.press('Escape')
    page.locator('#mercado-select-all').check()
    assert page.evaluate('Array.from(selectedMercadoCollectionIds)') == [13]
    assert not errors
