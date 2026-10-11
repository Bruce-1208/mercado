import json
from pathlib import Path
import pytest
from test_console_ui_browser import console_browser, console_page


@pytest.mark.parametrize('width', [1440, 390])
def test_order_analysis_rankings(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    row = dict(title='收纳盒 <测试>', sites='墨西哥', total_quantity=12,
               after_sale_quantity=2, after_sale_rate=16.67, after_sale_orders=2,
               total_income=1200, total_profit=300, profit_rate=25,
               comparable_orders=10, total_quoted_freight=100, total_actual_freight=120,
               freight_change=-12, max_freight_change=12)
    page.route('**/api/order-analysis/**', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': {'rows': [row]}})))
    page.evaluate('''async () => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-order-analysis').classList.add('active');
        await Promise.all([loadHighAfterSaleAlerts(), loadHighProfitProducts(), loadFreightChanges()]);
    }''')
    panel = page.locator('#tab-order-analysis')
    assert panel.locator('input[type=file]').count() == 0
    assert '产品编号' not in panel.inner_text()
    for body in ['after-sale-alert-body', 'high-profit-body', 'freight-change-body']:
        assert page.locator(f'#{body} tr:first-child td').count() == 8
        assert '收纳盒 <测试>' in page.locator(f'#{body}').inner_text()
    assert '-12.00' in page.locator('#freight-change-body').inner_text()
    page.locator('#freight-search').fill('收纳盒')
    page.locator('#freight-search').press('Enter')
    page.wait_for_function("!document.getElementById('load-freight-change-btn').disabled")
    assert not errors
    folder = Path('artifacts/order-analysis-20261009')
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f'analysis-{width}.png'), full_page=True)
