"""Offline catalog consistency: real template, mock rows, no business writes."""
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401

WORKSPACES = [
    ('1688-products', '#products-1688-select-all', '#products-1688-body input[type=checkbox]'),
    ('mercado-collection', '#mercado-select-all', '#mercado-list-body input[type=checkbox]'),
    ('ai-original-products', '#ai-original-select-all', '#ai-original-grid input[type=checkbox]'),
    ('mercado-products', '#mercado-select-all', '#mercado-list-body input[type=checkbox]'),
]


def show_workspace(page, tab):
    page.route('**/catalog-test-product.svg', lambda r: r.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"><rect width="160" height="160" rx="12" fill="#f8fafc"/><rect x="54" y="25" width="52" height="103" rx="12" fill="#547c98"/><rect x="52" y="22" width="56" height="14" rx="5" fill="#203e56"/><text x="80" y="149" text-anchor="middle" font-size="12" fill="#64748b">模拟商品</text></svg>'))
    page.evaluate('''tab => {
        document.querySelectorAll('.tab-page').forEach(n => n.classList.toggle('active', n.id === 'tab-' + tab));
        const rows = ['便携不锈钢保温杯 · 多色款', '桌面收纳盒 · 大容量'].map((title, i) => ({
            id: 41 + i, title, source_item_id: '70116880004' + i, can_operate: true,
            main_image_url: '/catalog-test-product.svg',
            source_type: 'zying', product_number: 'ZS2026101500' + i,
            visibility: 'enterprise', review_status: 'approved', ai_status: 'pending',
            net_proceeds_usd: 12, source_weight_g: 250, weight_g: 250,
            package_length_cm: 12, package_width_cm: 8, package_height_cm: 20,
            source_purchase_price_cny: 20, collector_salesperson: '模拟业务员',
            original_1688: {title, price: 20, main_image_url: '/catalog-test-product.svg'}, ai_original: {},
        }));
        if (tab === '1688-products') {
            document.getElementById('products-1688-total-value').dataset.total = rows.length;
            document.getElementById('products-1688-total-value').textContent = rows.length;
            products1688Rows = rows; products1688Selected.clear(); products1688RenderRows();
        } else if (tab === 'ai-original-products') {
            aiOriginalRows = rows; aiOriginalSelected.clear(); renderAiOriginalProducts();
        } else {
            const productMode = tab === 'mercado-products';
            mercadoListMode = productMode ? 'products' : 'collection';
            document.getElementById(productMode ? 'mercado-list-products-host' : 'mercado-list-collection-host').append(mercadoListModule);
            mercadoListModule.classList.toggle('product-mode', productMode);
            mercadoListModule.classList.toggle('collection-mode', !productMode);
            mercadoListRows = rows; selectedMercadoCollectionIds.clear(); renderMercadoListRows(rows);
            syncConsoleWorkflow();
        }
        window.scrollTo(0, 0);
    }''', tab)
    page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')


@pytest.mark.parametrize('width', [1440, 1024, 390, 320])
def test_catalog_layout_selection_and_keyboard(console_page, width):
    page = console_page
    page.emulate_media(reduced_motion='reduce')
    page.set_viewport_size({'width': width, 'height': 1000})
    writes, errors, styles = [], [], []
    page.on('request', lambda r: writes.append(r.url) if r.method not in ('GET', 'HEAD') else None)
    page.on('pageerror', lambda e: errors.append(str(e)))
    out = ROOT / 'artifacts/catalog-consistency-20261015'
    out.mkdir(parents=True, exist_ok=True)
    for tab, select_all, row_checks in WORKSPACES:
        show_workspace(page, tab)
        root = page.locator('#tab-' + tab)
        filters = root.locator('.zs-catalog-filters')
        selection = root.locator('.zs-catalog-selection')
        assert filters.bounding_box()['y'] < selection.bounding_box()['y']
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate('''() => [...document.querySelectorAll('.tab-page.active *')].filter(n => n.getBoundingClientRect().right > innerWidth + 1).map(n => [n.tagName, n.id, n.className, n.getBoundingClientRect().right]).slice(0, 20)''')
        styles.append(root.locator('.zs-catalog-heading').evaluate('''n => {
            const s = getComputedStyle(n), t = getComputedStyle(n.querySelector('h2'));
            return [s.padding, s.borderBottom, t.fontSize, t.color];
        }'''))
        assert filters.get_by_role('button', name='查询', exact=True).is_visible()
        page.locator(select_all).check()
        assert all(check.is_checked() for check in page.locator(row_checks).all())
        # A partial selection remains visible; native checkbox keyboard action works.
        page.locator(row_checks).first.uncheck()
        assert page.locator(select_all).evaluate('n => n.indeterminate')
        page.locator(select_all).focus()
        page.keyboard.press('Space')
        assert all(check.is_checked() for check in page.locator(row_checks).all())
        summary = selection.locator('.zs-bulk-menu > summary')
        summary.focus()
        assert summary.evaluate('n => getComputedStyle(n).outlineStyle') != 'none'
        page.keyboard.press('Enter')
        assert summary.evaluate('n => n.parentElement.open')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.keyboard.press('Escape')
        assert summary.evaluate('n => !n.parentElement.open && n === document.activeElement')
        # Resizing must preserve the selected products and the existing input nodes.
        search = filters.locator('input[type=search]').first
        search.evaluate("n => { n.value = '保温杯'; n.dataset.catalogProbe = 'kept'; }")
        page.set_viewport_size({'width': 390 if width > 760 else 1440, 'height': 1000})
        page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
        assert search.input_value() == '保温杯'
        assert search.get_attribute('data-catalog-probe') == 'kept'
        assert all(check.is_checked() for check in page.locator(row_checks).all())
        page.set_viewport_size({'width': width, 'height': 1000})
        page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
        page.evaluate('window.scrollTo(0, 0)')
        page.screenshot(path=str(out / f'{tab}-{width}.png'))
        page.screenshot(path=str(out / f'{tab}-{width}-workspace.png'), full_page=True)
    assert all(style == styles[0] for style in styles), styles
    assert not writes
    assert not errors


def test_collection_help_keyboard_and_download(console_page):
    page = console_page
    show_workspace(page, 'mercado-collection')
    help_panel = page.locator('.zs-catalog-help')
    assert not help_panel.evaluate('n => n.open')
    link = page.locator('#tab-mercado-collection .zs-catalog-heading a[download]')
    assert link.is_visible()
    assert link.get_attribute('href') == '/api/browser-extension/download'
    summary = help_panel.locator(':scope > summary')
    summary.focus()
    page.keyboard.press('Enter')
    assert help_panel.get_by_text('加载已解压扩展', exact=True).is_visible()
    page.keyboard.press('Escape')
    assert summary.evaluate('n => n === document.activeElement && !n.parentElement.open')


@pytest.mark.parametrize('width', [1440, 390])
def test_catalog_navigation_search_and_keyboard(console_page, width):
    page = console_page
    page.emulate_media(reduced_motion='reduce')
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("switchTab('1688-products')")
    for tab, label in [('mercado-collection', '美客多跟卖采集'),
                       ('ai-original-products', 'AI原创产品'),
                       ('mercado-products', '产品列表'), ('1688-products', '1688产品区')]:
        if width < 760:
            page.locator('#sidebar-toggle').click()
        search = page.get_by_role('searchbox', name='搜索功能')
        search.fill('没有这个模块')
        assert page.locator('.nav-empty').is_visible()
        search.fill(label)
        page.locator(f'.sidebar [data-tab="{tab}"]').click()
        assert page.locator('#tab-' + tab).is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if width < 760:
            assert page.locator('#sidebar-toggle').get_attribute('aria-expanded') == 'false'
    if width < 760:
        toggle = page.locator('#sidebar-toggle')
        toggle.click()
        page.keyboard.press('Escape')
        assert toggle.evaluate('n => n === document.activeElement')
        assert toggle.get_attribute('aria-expanded') == 'false'
