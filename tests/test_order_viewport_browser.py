import json
from pathlib import Path
import pytest
from test_console_ui_browser import console_browser, console_page


@pytest.mark.parametrize('width,height', [(1440,900), (1280,720), (390,844)])
def test_order_summary_stays_visible_when_list_scrolls(console_page, width, height):
    page = console_page
    page.set_viewport_size({'width':width, 'height':height})
    page.evaluate('''() => {
        renderOrderRows(Array.from({length:50}, (_,i) => ({id:String(i), title:'测试商品',
            amount:20, fee:3, actual_freight_usd:4, balance:13, income:91, cost:50})));
    }''')
    page.wait_for_timeout(100)
    footer = page.locator('.order-footer-metrics')
    before = footer.bounding_box()
    assert before['y'] + before['height'] <= height, before
    table = page.locator('.order-table-wrap')
    assert table.evaluate('el => el.scrollHeight > el.clientHeight')
    table.evaluate('el => el.scrollTop = el.scrollHeight')
    assert footer.bounding_box() == before
    assert page.evaluate('window.scrollY') == 0
    # A late global notification must not push the fixed footer out of view.
    page.locator('#model-balance-alert').evaluate("el => el.style.paddingBottom = '30px'")
    page.wait_for_timeout(100)
    after = footer.bounding_box()
    assert after['y'] + after['height'] <= height
    folder = Path('artifacts/order-viewport-20261009')
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f'orders-{width}-{height}.png'))


def test_latest_rate_conversion_and_failure(console_page):
    page = console_page
    page.route('**/api/orders/display-exchange-rate', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status':'success', 'data': {
            'cny_per_usd':7.12, 'rate_date':'2026-10-08', 'source':'欧洲央行参考汇率'}})))
    page.evaluate('''async () => {
        renderOrderRows([{id:'1', amount:20, fee:3, actual_freight_usd:4, balance:13},
            {id:'2', amount:0, fee:3, actual_freight_usd:4, balance:-7},
            {id:'3', amount:20, fee:3, actual_freight_usd:null, quoted_freight_usd:null}]);
        await loadOrderDisplayRate();
    }''')
    amounts = page.locator('#order-table-body .order-balance-cny')
    assert amounts.all_inner_texts() == ['≈ ￥92.56', '≈ ￥-49.84']
    assert '2026-10-08' in amounts.first.get_attribute('title')
    page.route('**/api/orders/display-exchange-rate', lambda route: route.fulfill(status=502, body='{}'))
    page.evaluate('async () => {orderDisplayRateCheckedAt = 0; await loadOrderDisplayRate();}')
    assert amounts.all_inner_texts() == ['人民币汇率待获取', '人民币汇率待获取']
