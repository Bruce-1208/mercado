"""Previous executions stay in opt-in logs; current task progress stays visible."""
import json

import pytest
from test_console_ui_browser import console_browser, console_page


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_previous_execution_is_not_shown_on_entry(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    previous = {'running': False, 'status': 'completed', 'message': '上一次模拟执行完成',
                'logs': ['上一次模拟执行日志'], 'total_stores': 2, 'processed_stores': 2,
                'total_links': 20, 'processed_links': 20, 'success_count': 20}
    for endpoint in ('sync/status', 'bulk-update/status'):
        page.route('**/api/store-links/' + endpoint, lambda route: route.fulfill(
            content_type='application/json', body=json.dumps({'status': 'success', 'data': previous})))
    page.route('**/api/store-links?*', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': {
            'rows': [], 'total': 0, 'summary': {}, 'page': 1, 'pages': 1}})))
    page.evaluate("switchTab('store-links')")
    page.wait_for_function("!storeLinkListLoading && !storeLinkSyncStatusLoading && !storeLinkRemoteUpdateStatusLoading")
    assert '上一次' not in page.locator('#store-link-sync-state').inner_text()
    assert '上一次' not in page.locator('#store-link-message').inner_text()
    assert '自动同步已开启' in page.locator('#store-link-sync-state').inner_text()
    for log_id in ('store-link-sync-log', 'store-link-remote-update-log'):
        log = page.locator('#' + log_id)
        assert not log.is_visible()
        summary = log.locator('..').locator('summary')
        summary.focus()
        summary.press('Enter')
        assert log.is_visible()
        assert '上一次模拟执行日志' in log.inner_text()
    page.evaluate("switchTab('overview'); switchTab('store-links')")
    page.wait_for_function("!storeLinkListLoading && !storeLinkSyncStatusLoading && !storeLinkRemoteUpdateStatusLoading")
    assert not page.locator('#store-link-sync-log').is_visible()
    assert not page.locator('#store-link-remote-update-log').is_visible()
    assert '上一次' not in page.locator('#store-link-message').inner_text()
    page.evaluate('''() => {
        const task = {running: true, status: 'running', message: '本次模拟任务执行中', logs: ['本次模拟日志']};
        renderStoreLinkSyncStatus(task);
        renderStoreLinkRemoteUpdateStatus(task);
    }''')
    assert '本次模拟任务执行中' in page.locator('#store-link-sync-state').inner_text()
    assert '本次模拟任务执行中' in page.locator('#store-link-message').inner_text()
    page.evaluate('''() => {
        const task = {running: false, status: 'completed', message: '本次模拟任务完成', logs: ['本次完成日志']};
        renderStoreLinkSyncStatus(task);
        renderStoreLinkRemoteUpdateStatus(task);
    }''')
    assert '本次模拟任务完成' in page.locator('#store-link-sync-state').inner_text()
    assert '本次模拟任务完成' in page.locator('#store-link-message').inner_text()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
