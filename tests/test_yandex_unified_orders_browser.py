import json
import pytest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from test_console_ui_browser import console_browser, console_page


@pytest.mark.parametrize("width", [1440, 390])
def test_yandex_is_in_default_list_and_platform_filter_preserves_negative_store(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    calls = []
    row = {'id': 'yandex:33:123', 'order_number': '123', 'pack_id': '123', 'store_id': -2,
           'source': 'Yandex Market', 'data_origin': 'yandex', 'country': '俄罗斯', 'shop_name': 'Yandex测试店',
           'title': '测试商品', 'product_id': 'SKU-1', 'quantity': 2, 'ordered_at': '2026-10-05 13:00:00',
           'status': '找货-没汇总', 'amount': None, 'amount_local': 20, 'currency_id': 'CNY',
           'fee': None, 'balance': None, 'profit': None, 'freight_missing': 1}
    def orders(route):
        calls.append(parse_qs(urlsplit(route.request.url).query, keep_blank_values=True))
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': {
            'rows': [row], 'total': 1, 'pages': 1, 'origin_counts': {'yandex': 1},
            'store_counts': [{'id': -2, 'name': 'Yandex测试店', 'count': 1}], 'summary': {'settlement_missing_count': 1}}}))
    page.route('**/api/orders?**', orders)
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-orders').classList.add('active');
        loadOrders(1);
    }""")
    page.wait_for_function('orderRows.length === 1')
    assert calls[-1]['origin'] == ['']
    assert 'Yandex Market' in page.locator('#order-table-body').inner_text()
    assert 'CNY 20' in page.locator('#order-table-body').inner_text()
    assert '待结算核实' in page.locator('#order-table-body').inner_text()
    assert page.locator('#order-table-body button[title="调用美客多接口打印发货面单"]').is_disabled()
    page.locator('.order-origin-button[data-origin="yandex"]').click()
    page.wait_for_function("orderOrigin === 'yandex'")
    page.wait_for_timeout(200)
    assert calls[-1]['origin'] == ['yandex']
    page.evaluate("""() => {
        document.getElementById('order-store-filter').value = '-2';
        loadOrders(1);
    }""")
    page.wait_for_timeout(200)
    assert calls[-1]['store_id'] == ['-2']
    page.evaluate('showOrderDetail(0)')
    assert 'CNY 20' in page.locator('#order-detail-content').inner_text()
    assert page.get_by_role('button', name='产品详情', exact=True).is_disabled()
    page.evaluate('orderDetailDialog.close()')
    output = Path(__file__).resolve().parents[1] / 'artifacts/yandex-unified-orders'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output/f'orders-{width}.png'))
