from pathlib import Path

import pytest
from test_console_ui_browser import console_browser, console_page


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_release_display_advanced_filter_reset_focus_and_selection(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""() => {
        window.releaseQueries = [];
        loadOrders = page => releaseQueries.push({page, query: orderQueryParams(page).toString()});
        const base = {store_id:7,shop_name:'模拟店铺',country:'墨西哥',status:'交付',
            amount:25,balance:18,profit:50,cost:70,income:175,ordered_at:'2026-10-10 10:00:00',
            sku_items:[{title:'模拟商品',quantity:1}],payment_release_checked_at:'2026-10-13 12:00:00'};
        renderOrderRows([
            {...base,id:'101',pack_id:'900',payment_release_state:'released',payment_release_date:'2026-10-12'},
            {...base,id:'102',pack_id:'900',payment_release_state:'unreleased',payment_release_date:'2030-01-01'},
            {...base,id:'103',payment_release_state:'unknown'},
            {...base,id:'104',payment_release_state:'partial',payment_release_date:'2026-10-11'},
            {...base,id:'105',payment_release_state:'released',payment_release_date:null},
        ]);
    }""")
    cells = page.locator('#order-table-body tr td:last-child')
    assert cells.count() == 4
    assert '子订单 101' in cells.nth(0).inner_text()
    assert '已释放' in cells.nth(0).inner_text() and '未释放' in cells.nth(0).inner_text()
    assert '2026-10-12' in cells.nth(0).inner_text()
    assert '2030-01-01' not in cells.nth(0).inner_text()
    assert '待核实' in cells.nth(1).inner_text()
    assert '部分释放' in cells.nth(2).inner_text() and '2026-10-11' in cells.nth(2).inner_text()
    assert '日期待核实' in cells.nth(3).inner_text()
    assert page.locator('.order-table thead th').count() == 16
    assert page.locator('#order-table-body tr:first-child td').count() == 16
    toggle = page.locator('#order-list-focus-toggle')
    if toggle.get_attribute('aria-expanded') == 'false':
        toggle.click()
    summary = page.locator('#tab-orders .order-filters .zs-advanced-filters > summary')
    summary.focus()
    page.keyboard.press('Enter')
    select = page.locator('#order-payment-release-filter')
    assert select.is_visible()
    assert select.evaluate("el => Boolean(el.closest('.zs-advanced-filters'))")
    select.focus()
    assert select.evaluate('el => el === document.activeElement')
    select.select_option('unreleased')
    query = page.evaluate('releaseQueries.at(-1)')
    assert query['page'] == 1 and 'payment_release=unreleased' in query['query']
    output = Path(__file__).resolve().parents[1] / 'artifacts/order-payment-release-20261013'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'filter-{width}.png'))
    page.locator('#order-table-body .order-row-checkbox').first.check()
    assert page.evaluate("[...selectedOrderIds]") == ['101', '102']
    assert select.input_value() == 'unreleased'
    page.locator('#order-store-filter-trigger').click()
    page.locator('#order-store-filter-picker input[type="search"]').fill('巴西')
    assert page.locator('#order-store-filter-picker .market-multi-option:visible').count() == 1
    page.keyboard.press('Escape')
    page.evaluate('resetOrderFilters()')
    assert select.input_value() == ''
    assert page.evaluate('releaseQueries.at(-1).query').find('payment_release=&') >= 0
    page.evaluate('showOrderDetail(0)')
    assert '101：已释放；102：未释放' in page.locator('#order-detail-content').inner_text()
    page.keyboard.press('Escape')
    page.locator('#order-table-body .order-row-checkbox').first.uncheck()
    if toggle.get_attribute('aria-expanded') == 'true':
        toggle.click()
    page.locator('.order-table-wrap').evaluate('el => el.scrollLeft = el.scrollWidth')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(output / f'release-{width}.png'))
