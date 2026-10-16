import pytest
from test_console_ui_browser import ROOT, console_browser, console_page
from test_product_developer_filter_browser import prepare


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_status_filter_query_reset_and_focus(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    requests, _ = prepare(page)
    page.evaluate("switchTab('mercado-products')")
    page.wait_for_function('mercadoListRows.length === 2')
    page.locator('#mercado-product-filters .zs-advanced-filters > summary').click()
    selector = page.locator('#mercado-zying-status-filter')
    assert selector.is_visible()
    selector.focus()
    assert selector.evaluate('(el) => document.activeElement === el')
    with page.expect_response(lambda response: '/api/mercado-products?' in response.url):
        selector.select_option('疑似')
    assert requests[-1]['zying_status'] == ['疑似']
    bounds = selector.bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
    with page.expect_response(lambda response: '/api/mercado-products?' in response.url):
        page.evaluate('resetMercadoProductFilters()')
    assert selector.input_value() == ''
    assert 'zying_status' not in requests[-1]
    artifacts = ROOT / 'artifacts/zying-product-status'
    artifacts.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifacts / f'products-{width}.png'), full_page=True)
