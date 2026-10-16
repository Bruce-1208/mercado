"""Product filters remain usable at desktop and mobile sizes."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from test_console_ui_browser import ROOT, console_browser, console_page


@pytest.mark.parametrize('width', [1440, 390])
def test_source_first_developer_advanced_and_salesperson_filter(console_page, width):
    page = console_page
    page.set_default_timeout(5000)
    page.set_viewport_size({'width': width, 'height': 1000})
    requests = []
    def products(route):
        query = parse_qs(urlsplit(route.request.url).query)
        requests.append(query)
        person = query.get('salesperson', [''])[0]
        rows = [{'id': i + 1, 'source_type': 'zying', 'title': name + '的产品', 'salesperson': name}
                for i, name in enumerate(['业务甲', '业务乙']) if not person or person == name]
        route.fulfill(content_type='application/json', body=json.dumps({
            'status': 'success', 'data': {'rows': rows, 'total': len(rows), 'salespeople': ['业务甲', '业务乙']},
        }))
    page.route('**/api/mercado-products/salespeople', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': {'salespeople': ['业务甲', '业务乙']}})))
    page.route('**/api/mercado-products?*', products)
    page.evaluate("() => { currentWorkbenchUser.permissions = ['*']; switchTab('mercado-products'); }")
    page.wait_for_function('mercadoListRows.length === 2')
    source = page.locator('#mercado-product-sources')
    assert source.bounding_box()['y'] < page.locator('#mercado-list-module .market-list-toolbar').bounding_box()['y']
    assert page.locator('#mercado-source-all').bounding_box()['height'] >= (44 if width > 760 else 70)
    developer = page.locator('#mercado-product-developer-filter')
    assert not developer.is_visible()
    assert developer.locator('..').locator('label').text_content() == '产品开发（智赢）'
    selector = page.locator('#mercado-salesperson-filter')
    from test_common_date_filters_browser import reveal_filter
    reveal_filter(selector)
    page.evaluate('mercadoListPage = 3; selectedMercadoCollectionIds.add(1)')
    selector.select_option('业务乙')
    page.wait_for_function("mercadoListRows.length === 1 && mercadoListRows[0].salesperson === '业务乙'")
    assert requests[-1]['salesperson'] == ['业务乙']
    assert requests[-1]['offset'] == ['0']
    assert page.evaluate('selectedMercadoCollectionIds.size') == 0
    assert selector.locator('option').all_text_contents() == ['全部业务员', '业务甲', '业务乙']
    page.locator('#mercado-source-zying').click()
    page.wait_for_function("mercadoListRows.length === 1 && mercadoListLoadController === null")
    assert requests[-1]['source_type'] == ['zying'] and requests[-1]['salesperson'] == ['业务乙']
    assert page.locator('#mercado-source-zying').get_attribute('aria-pressed') == 'true'
    assert page.locator('#mercado-source-all').get_attribute('aria-pressed') == 'false'
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts/product-filter-refresh-20261010'
    output.mkdir(exist_ok=True)
    page.screenshot(path=str(output / f'products-{width}.png'), full_page=True)
    if not developer.is_visible():
        page.locator('#mercado-product-filters .zs-advanced-filters > summary').click()
    assert developer.is_visible()
    page.evaluate('resetMercadoProductFilters()')
    page.wait_for_function('mercadoListRows.length === 2')
    assert selector.input_value() == ''


def test_product_rows_do_not_wait_for_salesperson_directory(console_page):
    page = console_page
    page.set_default_timeout(5000)
    pending, requests = [], []
    page.route('**/api/mercado-products/salespeople', lambda route: pending.append(route))
    def products(route):
        requests.append(parse_qs(urlsplit(route.request.url).query))
        route.fulfill(content_type='application/json', body=json.dumps({
            'status': 'success', 'data': {'rows': [{'id': 1, 'title': '先显示产品'}], 'total': 1},
        }))
    page.route('**/api/mercado-products?*', products)
    page.evaluate("() => { currentWorkbenchUser.permissions = ['*']; switchTab('mercado-products'); }")
    page.wait_for_function('mercadoListRows.length === 1')
    assert len(pending) == 1
    assert 'include_salesperson_options' not in requests[0]
    assert '先显示产品' in page.locator('#mercado-list-body').inner_text()
    page.evaluate('loadMercadoList()')
    page.wait_for_function('mercadoListRows.length === 1 && mercadoListLoadController === null')
    assert len(pending) == 1
    pending.pop().fulfill(content_type='application/json', body=json.dumps({
        'status': 'success', 'data': {'salespeople': ['业务甲']},
    }))
    page.wait_for_function("document.getElementById('mercado-salesperson-filter').options.length === 2")
    assert len(requests) == 2


def test_failed_salesperson_directory_can_retry_without_reloading_products(console_page):
    page = console_page
    page.set_default_timeout(5000)
    requests, directories = [], []
    def directory(route):
        directories.append(route.request.url)
        failure = len(directories) == 1
        route.fulfill(status=503 if failure else 200, content_type='application/json', body=json.dumps({
            'status': 'error' if failure else 'success', 'data': {'salespeople': ['业务甲']},
        }))
    def products(route):
        requests.append(route.request.url)
        route.fulfill(content_type='application/json', body=json.dumps({
            'status': 'success', 'data': {'rows': [{'id': 1, 'title': '产品正常显示'}], 'total': 1},
        }))
    page.route('**/api/mercado-products/salespeople', directory)
    page.route('**/api/mercado-products?*', products)
    page.evaluate("() => { currentWorkbenchUser.permissions = ['*']; switchTab('mercado-products'); }")
    page.wait_for_function('mercadoListRows.length === 1')
    retry = page.locator('#mercado-salesperson-retry')
    retry.click()
    page.wait_for_function("document.getElementById('mercado-salesperson-filter').options.length === 2")
    assert retry.is_hidden()
    assert len(requests) == 1 and len(directories) == 2
