from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_statistics_ui_filters_empty_error_and_safe_names():
    api = pytest.importorskip('playwright.sync_api')
    source = (ROOT / 'bit/templates/index.html').read_text()
    section = source.split('<div class="tab-page" id="tab-salesperson-stats">', 1)[1].split('<div class="tab-page" id="tab-store-analysis">', 1)[0]
    with api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        state = {'mode': 'success', 'urls': []}
        def route(r):
            state['urls'].append(r.request.url)
            if state['mode'] == 'error':
                r.fulfill(status=502, json={'status': 'error', 'message': '服务不可用'})
            else:
                r.fulfill(json={'status': 'success', 'data': {'rows': [] if state['mode'] == 'empty' else [
                    {'operator_id': 1, 'salesperson': '<img src=x onerror=alert(1)>', 'print_count': 2, 'purchase_count': 1}],
                    'print_count': 2, 'purchase_count': 1}})
        page.route('**/api/salesperson-stats?*', route)
        page.route('http://stats.test/', lambda r: r.fulfill(content_type='text/html', body='<div>'+section))
        page.goto('http://stats.test/')
        page.add_script_tag(path=str(ROOT / 'bit/static/analysis-request.js'))
        page.add_script_tag(path=str(ROOT / 'bit/static/salesperson-stats.js'))
        page.evaluate('loadSalespersonStats()')
        assert page.locator('#salesperson-stats-rows tr').count() == 1
        assert page.locator('#salesperson-stats-rows img').count() == 0
        page.locator('#salesperson-stats-start').fill('2026-10-01')
        page.locator('#salesperson-stats-end').fill('2026-10-02')
        page.locator('button[type=submit]').click()
        page.wait_for_function("document.getElementById('salesperson-stats-message').textContent.includes('共 1')")
        assert 'start_date=2026-10-01' in state['urls'][-1]
        state['mode'] = 'empty'
        page.locator('#salesperson-stats-all').click()
        page.wait_for_function("document.getElementById('salesperson-stats-message').textContent.includes('暂无')")
        assert page.locator('#salesperson-stats-start').input_value() == ''
        state['mode'] = 'error'
        page.evaluate('loadSalespersonStats(true)')
        assert '服务不可用' in page.locator('#salesperson-stats-message').inner_text()
        assert page.locator('#salesperson-stats-rows tr').count() == 0
        browser.close()
