"""Offline listing identifiers and keyboard-accessible variant expansion."""
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_listing_identifiers_expand_with_keyboard(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.route('**/api/mercado-publish-records?*', lambda route: route.fulfill(
        content_type='application/json',
        body='{"status":"success","data":{"rows":[],"total":0,"counts":{"all":0}}}'))
    page.evaluate('''() => {
        currentWorkbenchUser.permissions = ['*'];
        mercadoPublishStoresLoaded = true;
        switchTab('mercado-publish-records');
    }''')
    page.wait_for_function('mercadoPublishRecordsLoaded')
    page.evaluate('''() => {
        renderMercadoPublishRecords({rows: [
            {id: 3, title: '模拟多变体商品', status: 'published', site_id: 'MLM',
                result_json: {result: {user_products: [
                    {site_items: [{item_id: 'MLM123'}]},
                    {site_items: [{item_id: 'MLM456'}]}]}}},
            {id: 2, title: '模拟全局刊登', status: 'published',
                result_json: {result: [{id: 'CBT123'}, {id: 'CBT456'}]}},
            {id: 1, title: '模拟待复核', status: 'failed'},
            {id: 4, title: '模拟待复核二', status: 'failed'}
        ], total: 4, counts: {all: 4, published: 2, failed: 2}, publish_running: false});
    }''')
    body = page.locator('#publish-record-body')
    details = body.locator('.publish-record-items').filter(has_text='MLM123')
    summary = details.locator('summary')
    assert summary.inner_text() == 'MLM123 · 共 2 个编号'
    assert not details.locator('a').first.is_visible()
    summary.focus()
    assert summary.evaluate('(el) => el === document.activeElement')
    page.keyboard.press('Enter')
    assert details.locator('a').count() == 2
    assert details.locator('a').nth(1).is_visible()
    assert details.locator('a').nth(1).get_attribute('href').endswith('/MLM-456')
    page.keyboard.press('Tab')
    assert details.locator('a').first.evaluate('(el) => el === document.activeElement')
    summary.focus()
    page.keyboard.press('Space')
    assert not details.locator('a').first.is_visible()
    for record_id in (1, 4):
        checkbox = body.locator(f'input[value="{record_id}"]')
        checkbox.focus()
        page.keyboard.press('Space')
        assert checkbox.is_checked()
    assert body.locator('input[value="3"]').is_disabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    summary.focus()
    page.keyboard.press('Enter')
    artifacts = ROOT / 'artifacts/publish-record-items-20261015'
    artifacts.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifacts / f'records-{width}.png'))
