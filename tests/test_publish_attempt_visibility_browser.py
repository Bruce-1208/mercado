"""Offline coverage for recorded infringement holds and detection failures."""
import json
import pytest
from test_console_ui_browser import console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 390])
def test_publish_risk_attempts_visible_without_manual_check(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    rows = [
        {'id': 101, 'title': '模拟侵权商品', 'status': 'pending_review',
         'failure_reason': '侵权检测为高风险，需人工复核'},
        {'id': 102, 'title': '模拟检测异常', 'status': 'failed',
         'failure_reason': '侵权补检未完成：百炼 Token 已失效'},
    ]
    page.route('**/api/mercado-publish-records?*', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': {
            'rows': rows, 'total': 2, 'counts': {'all': 2, 'pending_review': 1, 'failed': 1}}})))
    page.evaluate('''() => {
        currentWorkbenchUser.permissions = ['*'];
        document.querySelector('.tab-button[data-tab="mercado-publish-records"]').hidden = false;
        mercadoPublishStoresLoaded = true;
        switchTab('mercado-publish-records');
    }''')
    page.wait_for_function('mercadoPublishRecordRows.length === 2')
    body = page.locator('#publish-record-body')
    assert '待审核' in body.inner_text()
    assert '侵权检测为高风险，需人工复核' in body.inner_text()
    assert '百炼 Token 已失效' in body.inner_text()
    assert body.locator('input[value="101"]').is_enabled()
    checkbox = body.locator('input[value="102"]')
    checkbox.focus()
    page.keyboard.press('Space')
    assert checkbox.is_checked()
    assert page.locator('#publish-record-retry').is_enabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.evaluate('''() => {
        window.mercadoInfringementPublishIssues = () => ['侵权尚未检测'];
        return mercadoProductPublishIssues({source_type: 'collected', review_status: 'approved'}).length;
    }''') == 0
