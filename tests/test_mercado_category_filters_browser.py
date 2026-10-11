"""Both real console filters use the same Chinese, sequential picker."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page


@pytest.mark.parametrize('kind', ['products', 'links'])
@pytest.mark.parametrize('width', [1440, 390])
def test_chinese_levels_parent_filter_reset_and_deep_path(console_page, kind, width):
    page = console_page
    page.set_default_timeout(5000)
    page.set_viewport_size({'width': width, 'height': 1000})
    paths = {
        'CBT8': [{'id': f'CBT{i}', 'name': name} for i, name in enumerate(
            ['家电', '厨房电器', '冰箱', '商用冰箱', '冷藏设备', '立式冷柜', '单门冷柜', '节能冷柜'], 1)],
        'CBT21': [{'id': 'CBT20', 'name': '工具'}, {'id': 'CBT21', 'name': '锤子'}],
        'MLB8': [{'id': f'MLB{i}', 'name': name} for i, name in enumerate(
            ['家电', '厨房电器', '冰箱', '商用冰箱', '冷藏设备', '立式冷柜', '单门冷柜', '节能冷柜'], 1)],
    }
    rows = [{'category_id': key} for key in paths]
    def route(request):
        path = urlsplit(request.request.url).path
        data = paths if path.endswith('/category-paths') else {'rows': rows, 'mercado_categories': rows}
        request.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': data}))
    page.route('**/api/store-links**', route)
    page.route('**/api/store-links/category-paths**', route)
    page.route('**/api/mercado-products/mercado-categories**', route)
    page.evaluate('''kind => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        const field = document.getElementById(kind === 'links' ? 'store-link-category-trigger' : 'mercado-product-category-trigger').closest(kind === 'links' ? '.order-filter-field' : '.market-filter-field');
        document.getElementById(kind === 'links' ? 'tab-store-links' : 'tab-mercado-products').classList.add('active');
        if (kind === 'products') mountMercadoListModule('products');
        field.closest('details').open = true;
        window.categoryChanges = [];
        const picker = kind === 'links' ? storeLinkCategoryPicker : mercadoProductCategoryPicker;
        picker.onChange = () => window.categoryChanges.push(picker.input.value);
    }''', kind)
    prefix = 'store-link-category' if kind == 'links' else 'mercado-product-category'
    trigger = page.locator(f'#{prefix}-trigger')
    panel = page.locator(f'#{prefix}-panel')
    trigger.click()
    selects = panel.locator('select')
    selects.first.wait_for()
    assert selects.count() == 1
    assert 'Refrigerator' not in panel.inner_text()
    selects.nth(0).select_option(label='家电')
    assert selects.count() == 2
    selects.nth(1).select_option(label='厨房电器')
    assert selects.count() == 3
    selects.nth(0).select_option(label='工具')
    assert selects.count() == 2
    assert selects.nth(1).input_value() == ''
    assert '厨房电器' not in selects.nth(1).inner_text()
    panel.locator('[data-category-apply]').click()
    assert page.evaluate('window.categoryChanges') == ['tree:CBT20']
    assert trigger.inner_text().strip().startswith('工具')
    trigger.click()
    selects.nth(0).select_option(label='家电')
    for depth, name in enumerate(['厨房电器', '冰箱', '商用冰箱', '冷藏设备', '立式冷柜', '单门冷柜', '节能冷柜'], 1):
        selects.nth(depth).select_option(label=name)
    assert selects.count() == 8
    panel.locator('[data-category-apply]').click()
    assert page.evaluate('window.categoryChanges.at(-1)') == 'tree:CBT8,MLB8'
    trigger.click()
    assert selects.nth(7).input_value() == '节能冷柜'
    bounds = panel.bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
    output = ROOT / 'artifacts/mercado-category-filters'
    output.mkdir(exist_ok=True)
    page.screenshot(path=str(output / f'{kind}-{width}.png'))
    panel.locator('[data-category-clear]').click()
    assert page.evaluate('window.categoryChanges.at(-1)') == ''
    assert trigger.inner_text().strip().startswith('全部美客多分类')


def test_untranslated_official_hierarchy_stays_selectable(console_page):
    page = console_page
    page.set_default_timeout(5000)
    page.route('**/api/mercado-products/mercado-categories**', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': {'rows': [{'category_id': 'CBT2'}]}})))
    page.route('**/api/store-links/category-paths**', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': {'CBT2': [
            {'id': 'CBT1', 'name': 'Tools'}, {'id': 'CBT2', 'name': 'Hammers'},
        ]}})))
    page.evaluate("() => { currentWorkbenchUser.permissions = ['*']; switchTab('mercado-products'); document.querySelector('#mercado-product-filters details').open = true; window.categoryChanges = []; mercadoProductCategoryPicker.onChange = () => window.categoryChanges.push(mercadoPlatformCategoryFilter.value); }")
    page.locator('#mercado-product-category-trigger').click()
    panel = page.locator('#mercado-product-category-panel')
    panel.locator('select').first.select_option(label='Tools')
    panel.locator('select').nth(1).select_option(label='Hammers')
    panel.locator('[data-category-apply]').click()
    assert page.evaluate('window.categoryChanges') == ['tree:CBT2']
