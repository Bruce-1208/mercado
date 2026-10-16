import json
import pytest
from test_console_ui_browser import console_browser, console_page


@pytest.mark.parametrize('width', [1440, 390])
def test_return_to_links_loads_new_publications(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    requests = []
    def listing(route):
        requests.append(route.request.url)
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': {
            'rows': [{'id': 42, 'item_id': 'MLM123', 'title': '模拟刚上架商品', 'status': 'active'}],
            'total': 1, 'page': 1, 'page_size': 200}}))
    page.route('**/api/store-links?*', listing)
    page.evaluate('''() => {
        currentWorkbenchUser.permissions = ['*'];
        storeLinksLoaded = true;
        switchTab('store-links');
    }''')
    page.wait_for_function("!storeLinkListLoading && storeLinkRows.some(r => r.item_id === 'MLM123')")
    assert requests
    checkbox = page.locator('.store-link-row-select[value="42"]')
    checkbox.focus()
    checkbox.press('Space')
    assert checkbox.is_checked()
    assert checkbox.evaluate('el => el === document.activeElement')
