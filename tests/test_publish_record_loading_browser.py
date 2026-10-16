"""Offline list reads only: paging, source filters, request races and failures."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from test_console_ui_browser import console_browser, console_page  # noqa: F401


def open_records(page):
    page.evaluate('''() => {
        currentWorkbenchUser.permissions = ['*'];
        document.querySelector('.tab-button[data-tab="mercado-publish-records"]').hidden = false;
        mercadoPublishStoresLoaded = true;
        switchTab('mercado-publish-records');
    }''')


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_sources_paging_focus_and_selection(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    requests = []
    sources = {'collected': '美客多采集', 'pulled': '店铺同步', 'zying': '智赢采集', 'ai_original': 'AI 原创'}

    def records(route):
        query = parse_qs(urlsplit(route.request.url).query)
        requests.append(query)
        source = query.get('source_type', [''])[0]
        rows = [{'id': i + 1, 'title': '模拟商品', 'status': 'failed', 'source_type': key}
                for i, key in enumerate(sources) if not source or key == source]
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': {
            'rows': rows, 'total': 201, 'counts': {'all': 201, 'failed': 201}}}))

    page.route('**/api/mercado-publish-records?*', records)
    open_records(page)
    page.wait_for_function('mercadoPublishRecordsLoaded && !mercadoPublishRecordRequest')
    body = page.locator('#publish-record-body')
    for label in sources.values():
        assert label in body.inner_text()
    checkbox = body.locator('input').first
    checkbox.focus()
    page.keyboard.press('Space')
    assert checkbox.is_checked()
    page.evaluate('loadMercadoPublishRecords({background: true})')
    assert checkbox.is_checked()
    page.locator('#publish-record-next').click()
    page.wait_for_function('mercadoPublishRecordOffset === 100')
    assert requests[-1]['offset'] == ['100']
    if not page.locator('#publish-record-source').is_visible():
        page.locator('#tab-mercado-publish-records .zs-advanced-filters > summary').click()
    for key, label in sources.items():
        page.locator('#publish-record-source').select_option(key)
        page.wait_for_function('!mercadoPublishRecordRequest')
        assert requests[-1]['source_type'] == [key]
        assert requests[-1]['offset'] == ['0']
        assert requests[-1]['limit'] == ['100']
        assert label in body.inner_text()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    out = Path('artifacts/publish-record-loading')
    out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / f'sources-{width}.png'))
    page.locator('#publish-record-body').scroll_into_view_if_needed()
    page.screenshot(path=str(out / f'records-{width}.png'))


def test_duplicate_requests_failure_and_manual_recovery(console_page):
    page = console_page
    calls = []
    def failed(route):
        calls.append(route.request.url)
        route.fulfill(status=503, content_type='application/json',
                      body='{"status":"error","message":"模拟服务繁忙"}')
    page.route('**/api/mercado-publish-records?*', failed)
    page.evaluate('mercadoPublishRunning = true')
    open_records(page)
    page.wait_for_function('!mercadoPublishRecordRequest')
    assert '已停止自动刷新' in page.locator('#publish-record-message').inner_text()
    page.clock.install()
    page.clock.fast_forward(30000)
    assert len(calls) == 1
    page.evaluate('''async () => {
        window.pendingRecordFetch = [];
        window.fetch = (url, options) => new Promise(resolve => pendingRecordFetch.push({url, resolve}));
        loadMercadoPublishRecords();
        loadMercadoPublishRecords();
    }''')
    assert page.evaluate('pendingRecordFetch.length') == 1
    page.evaluate('''() => {
        document.getElementById('publish-record-source').value = 'zying';
        loadMercadoPublishRecords();
        const reply = source => new Response(JSON.stringify({status:'success', data:{
            rows:[{id:9,title:'模拟商品',status:'failed',source_type:source}], total:1, counts:{all:1}}}),
            {status:200,headers:{'Content-Type':'application/json'}});
        pendingRecordFetch[1].resolve(reply('zying'));
        pendingRecordFetch[0].resolve(reply('collected'));
    }''')
    page.wait_for_function('mercadoPublishRecordsLoaded && !mercadoPublishRecordRequest')
    assert '智赢采集' in page.locator('#publish-record-body').inner_text()
    assert '美客多采集' not in page.locator('#publish-record-body').inner_text()


def test_timeout_stops_and_preserves_loaded_rows(console_page):
    page = console_page
    page.clock.install()
    page.evaluate('''() => {
        mercadoPublishRecordsLoaded = true;
        mercadoPublishRunning = true;
        publishRecordBody.innerHTML = '<tr><td>已加载的模拟记录</td></tr>';
        window.recordReadCalls = 0;
        window.fetch = (url, options) => {
            recordReadCalls++;
            return new Promise((resolve, reject) => options.signal.addEventListener('abort',
                () => reject(new DOMException('aborted', 'AbortError'))));
        };
        loadMercadoPublishRecords({background: true});
    }''')
    page.clock.fast_forward(21000)
    page.wait_for_function('!mercadoPublishRecordRequest')
    assert '请求超时' in page.locator('#publish-record-message').inner_text()
    assert '已加载的模拟记录' in page.locator('#publish-record-body').inner_text()
    page.clock.fast_forward(30000)
    assert page.evaluate('recordReadCalls') == 1
