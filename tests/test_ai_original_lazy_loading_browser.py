"""Offline pagination and historical-task regression checks."""
import json
from urllib.parse import urlsplit, parse_qs
import pytest
from test_console_ui_browser import console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 390])
def test_load_more_only_on_navigation(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    batches = []

    def products(route):
        query = parse_qs(urlsplit(route.request.url).query)
        offset = int(query.get('offset', ['0'])[0])
        limit = int(query.get('limit', ['50'])[0])
        if limit == 50:
            batches.append(offset)
        rows = [{'id': i, 'ai_status': 'failed', 'ai_error': '历史错误',
                 'original_1688': {'title': f'模拟商品 {i}'}}
                for i in range(offset + 1, min(offset + limit, 75) + 1)]
        route.fulfill(content_type='application/json', body=json.dumps({
            'status': 'success', 'data': {'rows': rows, 'has_more': offset == 0,
                                         'generator_salespeople': []}}))

    page.route('**/api/ai-original-products?*', products)
    page.route('**/api/ai-original-products/process/status', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': {
            'running': False, 'status': 'error', 'message': '历史错误'}})))
    page.evaluate('''async () => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        await loadAiOriginalProducts(); await loadAiOriginalTaskStatus();
    }''')
    assert batches == [0]
    assert page.locator('#ai-original-grid > .ai-original-card').count() == 50
    assert page.locator('#ai-original-task-log').is_hidden()
    assert '历史错误' not in page.locator('#ai-original-task-status').inner_text()
    page.locator('#ai-original-page-next').focus()
    page.keyboard.press('Enter')
    page.wait_for_function('aiOriginalPage === 2')
    assert batches == [0, 50]
    assert page.locator('#ai-original-grid > .ai-original-card').count() == 25
    page.evaluate('toggleAllAiOriginalProducts(true)')
    assert page.evaluate('aiOriginalSelected.size') == 25
    assert page.locator('#ai-original-page-next').is_disabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
