"""Product history uses record details without submitting any publish action."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


def open_products(page, can_operate=True):
    rows = [dict(id=i, title=f'模拟产品 {i}', source_item_id=f'SOURCE-{i}',
                 source_type='collected', last_publish_status='published', can_operate=can_operate) for i in (41, 42)]
    page.route('**/api/mercado-products?*', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': {
            'rows': rows, 'total': len(rows)}})))
    page.evaluate("""() => {
        currentWorkbenchUser.permissions = ['*'];
        switchTab('mercado-products');
    }""")
    page.wait_for_function('mercadoListRows.length === 2')


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_published_product_history_details_and_keyboard(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    requests = []
    record = dict(id=91, product_item_id=41, status='published', store_name='模拟店铺',
                  site_id='MLM', batch_id='BATCH-41', quantity=3, created_by='测试员',
                  started_at='2026-10-16 10:00:00', finished_at='2026-10-16 10:01:00',
                  result_json={'result': {'user_products': [{'site_items': [
                      {'item_id': 'MLM123'}, {'item_id': 'MLM456'}]}]},
                               'message': '<script>unsafe()</script>'})
    failed = dict(record, id=90, status='failed', failure_reason='模拟之前的失败原因')

    def history(route):
        requests.append(parse_qs(urlsplit(route.request.url).query))
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success',
                      'data': {'rows': [record, failed], 'total': 2}}))
    page.route('**/api/mercado-publish-records?*', history)
    open_products(page)
    for product_id in (41, 42):
        checkbox = page.locator(f'.mercado-item-select[value="{product_id}"]')
        checkbox.focus()
        checkbox.press('Space')
        assert checkbox.is_checked()
    button = page.locator('[data-product-id="41"] .mercado-product-publish-log')
    button.focus()
    button.press('Enter')
    dialog = page.locator('#product-publish-log-dialog')
    page.wait_for_function("document.getElementById('product-publish-log-message').textContent.includes('共 2 条')")
    assert requests == [{'product_item_id': ['41'], 'limit': ['100'], 'offset': ['0']}]
    text = dialog.inner_text()
    for expected in ('上架成功', '模拟店铺', 'MLM', 'BATCH-41', '测试员',
                     '2026-10-16 10:01:00', '模拟之前的失败原因'):
        assert expected in text
    summary = dialog.get_by_text('查看接口返回明细', exact=True).first
    summary.focus()
    summary.press('Enter')
    assert dialog.locator('pre').first.is_visible()
    assert '<script>unsafe()</script>' in dialog.locator('pre').first.inner_text()
    assert dialog.locator('script').count() == 0
    assert dialog.locator('pre').first.inner_text() == page.evaluate(
        'value => publishRecordResultText(value)', record['result_json'])
    ids = dialog.locator('.publish-record-items').first
    ids.locator('summary').focus()
    ids.locator('summary').press('Space')
    assert ids.locator('a').nth(1).is_visible()
    assert dialog.locator('.publish-record-review-one, .publish-record-item-select').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert dialog.evaluate('el => el.scrollWidth <= el.clientWidth'), dialog.evaluate('el => [...el.querySelectorAll("*")].filter(n => n.scrollWidth > n.clientWidth).map(n => [n.tagName,n.className,n.clientWidth,n.scrollWidth,getComputedStyle(n).whiteSpace,getComputedStyle(n).width])')
    artifacts = ROOT / 'artifacts/product-publish-logs-20261016'
    artifacts.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifacts / f'logs-{width}.png'))
    page.keyboard.press('Escape')
    assert not dialog.is_visible()
    assert button.evaluate('el => el === document.activeElement')
    assert page.evaluate('selectedMercadoCollectionIds.size') == 2


def test_product_log_pagination_empty_and_error_retry(console_page):
    page = console_page
    requests = []
    fail = False

    def history(route):
        query = parse_qs(urlsplit(route.request.url).query)
        requests.append(query)
        if fail:
            route.fulfill(status=500, content_type='application/json',
                          body='{"status":"error","message":"模拟读取异常"}')
            return
        product = int(query['product_item_id'][0])
        offset = int(query['offset'][0])
        rows = [{'id': 100 - offset, 'status': 'published', 'batch_id': f'PAGE-{offset}'}] if product == 41 else []
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success',
                      'data': {'rows': rows, 'total': 101 if product == 41 else 0}}))
    page.route('**/api/mercado-publish-records?*', history)
    open_products(page)
    page.locator('[data-product-id="41"] .mercado-product-publish-log').click()
    page.wait_for_function("document.getElementById('product-publish-log-message').textContent.includes('共 101 条')")
    page.locator('#product-publish-log-next').click()
    page.wait_for_function("document.getElementById('product-publish-log-entries').textContent.includes('PAGE-100')")
    assert requests[-1]['offset'] == ['100']
    assert page.locator('#product-publish-log-next').is_disabled()
    fail = True
    page.locator('#product-publish-log-refresh').click()
    page.wait_for_function("document.getElementById('product-publish-log-message').textContent.includes('模拟读取异常')")
    assert page.locator('#product-publish-log-refresh').is_enabled()
    fail = False
    page.locator('#product-publish-log-prev').click()
    page.wait_for_function("document.getElementById('product-publish-log-entries').textContent.includes('PAGE-0')")
    page.keyboard.press('Escape')
    page.locator('[data-product-id="42"] .mercado-product-publish-log').click()
    page.wait_for_function("document.getElementById('product-publish-log-message').textContent.includes('暂无可查看')")
    assert requests[-1]['product_item_id'] == ['42']
    assert requests[-1]['offset'] == ['0']
    assert page.locator('#product-publish-log-entries').inner_text() == ''


def test_read_only_product_can_view_logs_and_ignore_previous_response(console_page):
    page = console_page
    pending = []

    def history(route):
        product = parse_qs(urlsplit(route.request.url).query)['product_item_id'][0]
        if product == '41':
            pending.append(route)
        else:
            route.fulfill(content_type='application/json', body=json.dumps({'status': 'success',
                          'data': {'rows': [{'id': 2, 'status': 'published', 'batch_id': 'SECOND-PRODUCT'}], 'total': 1}}))
    page.route('**/api/mercado-publish-records?*', history)
    open_products(page, can_operate=False)
    first = page.locator('[data-product-id="41"] .mercado-product-publish-log')
    assert first.is_enabled()
    assert page.locator('[data-product-id="41"] .mercado-product-review-one').is_disabled()
    first.click()
    page.wait_for_timeout(50)
    assert pending
    page.keyboard.press('Escape')
    page.locator('[data-product-id="42"] .mercado-product-publish-log').click()
    page.wait_for_function("document.getElementById('product-publish-log-entries').textContent.includes('SECOND-PRODUCT')")
    pending[0].fulfill(content_type='application/json', body=json.dumps({'status': 'success',
                       'data': {'rows': [{'id': 1, 'batch_id': 'FIRST-PRODUCT'}], 'total': 1}}))
    page.wait_for_timeout(50)
    assert 'FIRST-PRODUCT' not in page.locator('#product-publish-log-entries').inner_text()
    assert 'SECOND-PRODUCT' in page.locator('#product-publish-log-entries').inner_text()
