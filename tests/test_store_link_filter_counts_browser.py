"""Real template + real filter SQL over mock listings; no business API calls."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from test_console_ui_browser import console_browser, console_page
from test_store_link_filter_counts import listings


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_filters_update_counts_without_losing_selection(console_page, listings, width):
    page = console_page
    load, _ = listings
    requests = []

    def route(request):
        query = parse_qs(urlsplit(request.request.url).query)
        requests.append(query)
        tokens = [int(value) for value in query.get('token_id', [])]
        kwargs = {name: query[name][0] for name in ('site_id', 'status', 'salesperson', 'group_name') if name in query}
        if tokens:
            kwargs['filter_token_ids'] = tokens
        data = load(**kwargs, page_size=int(query.get('page_size', ['200'])[0]))
        data['stores'] = [{'token_id': 1, 'store_name': '模拟店铺甲'}, {'token_id': 2, 'store_name': '模拟店铺乙'}]
        data['groups'] = []
        for row in data['rows']:
            row.update(title='模拟商品', store_name='模拟店铺', price=10)
        request.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': data}))

    page.route('**/api/store-links?*', route)
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('''async () => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-store-links').classList.add('active');
        await loadStoreLinks(1);
        Array.from(storeLinkStoreFilter.options).forEach(option => option.selected = option.value === '1');
        syncMercadoPublishPicker(storeLinkStoreFilter);
        await loadStoreLinks(1);
    }''')
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    if not page.locator('#store-link-site-filter').is_visible():
        advanced = page.locator('#tab-store-links .zs-advanced-filters > summary')
        advanced.focus()
        advanced.press('Enter')
    page.locator('#store-link-site-filter').select_option('MLM')
    page.wait_for_function("!storeLinkListLoading && storeLinkCurrentCount.textContent === '2'")
    assert page.locator('#store-link-all-count').inner_text() == '3'
    assert page.locator('#store-link-site-filter option:checked').inner_text() == 'MLM · 2 条'
    assert page.locator('#store-link-status-filter option[value="active"]').inner_text() == '在售 · 1 条'
    assert '共 2 条' in page.locator('#store-link-result-summary').inner_text()
    checkbox = page.locator('.store-link-row-select').first
    checkbox.check()
    assert checkbox.is_checked()
    page.locator('#store-link-status-filter').select_option('active')
    page.wait_for_function("!storeLinkListLoading && storeLinkCurrentCount.textContent === '1'")
    assert page.locator('#store-link-all-count').inner_text() == '1'
    assert page.locator('#store-link-site-filter option:checked').inner_text() == 'MLM · 1 条'
    assert page.evaluate('selectedStoreLinkIds.size') == 0
    page.locator('#store-link-status-filter').select_option('closed')
    page.wait_for_function("!storeLinkListLoading && storeLinkCurrentCount.textContent === '0'")
    assert page.locator('#store-link-site-filter').input_value() == 'MLM'
    assert page.locator('#store-link-all-count').inner_text() == '1'
    assert page.locator('#store-link-site-filter option:checked').inner_text() == 'MLM · 0 条'
    page.locator('#store-link-status-filter').select_option('')
    page.wait_for_function("!storeLinkListLoading && storeLinkCurrentCount.textContent === '2'")
    # Keyboard navigation of the native filter and multi-shop picker survives updates.
    page.locator('#store-link-status-filter').focus()
    page.locator('#store-link-status-filter').press('Tab')
    assert page.evaluate('document.activeElement !== document.body')
    page.locator('#store-link-store-filter-trigger').click()
    picker = page.locator('#store-link-store-filter-picker')
    picker.locator('input[type="checkbox"]').nth(1).check()
    page.wait_for_function("!storeLinkListLoading && storeLinkCurrentCount.textContent === '4'")
    assert page.evaluate('mercadoSelectedValues(storeLinkStoreFilter)') == ['1', '2']
    page.keyboard.press('Escape')
    assert page.locator('#store-link-store-filter-trigger').get_attribute('aria-expanded') == 'false'
    assert page.locator('#store-link-store-count').inner_text() == '2'
    assert page.locator('#store-link-all-count').inner_text() == '5'
    assert page.locator('#store-link-site-filter option:checked').inner_text() == 'MLM · 4 条'
    assert requests[-1]['token_id'] == ['1', '2']
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    output = Path('artifacts/store-link-filter-counts-20261015')
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'filters-{width}.png'), full_page=True)
