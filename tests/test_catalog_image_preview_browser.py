"""Offline coverage for shared catalog image previews and compact cards."""
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_catalog_preview_and_selection(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400"><rect width="400" height="400" fill="#f8fafc"/><rect x="130" y="60" width="140" height="280" rx="28" fill="#527c9d"/><rect x="125" y="50" width="150" height="35" rx="10" fill="#233e57"/></svg>'
    page.route('**/catalog-test.svg', lambda route: route.fulfill(body=svg, content_type='image/svg+xml'))
    page.route('**/api/ai-original-products/source-image?*', lambda route: route.fulfill(body=svg, content_type='image/svg+xml'))
    for tab, selector in [
        ('mercado-products', '#mercado-list-body .market-card-image a'),
        ('mercado-collection', '#mercado-list-body .market-collection-image-link'),
        ('1688-products', '#products-1688-body .p1688-product-image-link'),
        ('ai-original-products', '#ai-original-grid .ai-original-image-pair img'),
    ]:
        page.evaluate('''tab => {
            document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
            document.getElementById('tab-' + tab).classList.add('active');
            const rows = Array.from({length: 4}, (_, i) => ({id:41+i, title:'便携不锈钢保温杯 · 多色款',
                source_item_id: '701168800041', source_url:'https://detail.1688.com/offer/701168800041.html',
                image_url:'https://images.test/catalog-test.svg', main_image_url:'https://images.test/catalog-test.svg',
                review_status:'approved', ai_status:'completed', source_weight_g:250, net_proceeds_usd:12,
                sale_price_usd:25, shipping_fee_usd:5,
                original_1688:{title:'便携不锈钢保温杯 · 多色款', main_image_url:'https://images.test/catalog-test.svg', price:20},
                title_es:'Termo de acero inoxidable', title_pt:'Garrafa térmica',
                description_es:'Conserva la temperatura.', ai_original:{status:'completed'}}));
            if (tab.startsWith('mercado-')) {
                document.getElementById('tab-' + tab).appendChild(document.getElementById('mercado-list-module'));
                mercadoListMode = tab === 'mercado-products' ? 'products' : 'collection';
                const module = document.getElementById('mercado-list-module');
                module.classList.toggle('product-mode', mercadoListMode === 'products');
                module.classList.toggle('collection-mode', mercadoListMode === 'collection');
                mercadoListRows = rows; renderMercadoListRows(rows);
            } else if (tab === '1688-products') { products1688Rows = rows; products1688RenderRows(); }
            else { aiOriginalRows = rows; renderAiOriginalProducts(); }
        }''', tab)
        target = page.locator(selector).first
        target.scroll_into_view_if_needed()
        page.wait_for_function('selector => { const el = document.querySelector(selector); const img = el.matches("img") ? el : el.querySelector("img"); return img.complete && img.naturalWidth > 0; }', arg=selector)
        target.hover()
        preview = page.locator('.order-image-preview.catalog-image-preview')
        assert preview.is_visible()
        bounds = preview.bounding_box()
        assert 0 <= bounds['x'] and bounds['x'] + bounds['width'] <= width
        assert 0 <= bounds['y'] and bounds['y'] + bounds['height'] <= 1000
        assert bounds['width'] > target.bounding_box()['width']
        output = ROOT / 'artifacts/catalog-refresh-20261014'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / f'{tab}-{width}.png'))
        page.mouse.move(0, 0)
        assert not preview.is_visible()
        target.focus()
        assert preview.is_visible()
        assert target.evaluate('el => getComputedStyle(el).outlineStyle') != 'none'
        page.keyboard.press('Escape')
        assert not preview.is_visible()
        # Re-rendering and focus movement must not change the selection or open a drawer.
        container = '#ai-original-grid' if tab == 'ai-original-products' else '#products-1688-body' if tab == '1688-products' else '#mercado-list-body'
        checkbox = page.locator(container + ' input[type=checkbox]').first
        checkbox.focus()
        page.keyboard.press('Space')
        assert checkbox.is_checked()
        page.keyboard.press('Space')
        assert not checkbox.is_checked()
        page.locator(container).evaluate('el => el.scrollIntoView({block: "start"})')
        page.mouse.move(0, 0)
        page.screenshot(path=str(output / f'{tab}-cards-{width}.png'))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors
