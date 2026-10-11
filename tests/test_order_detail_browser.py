"""Offline checks for on-demand order detail and progressive product rendering."""
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from test_console_ui_browser import console_browser, console_page


def open_detail(page, order_id="100", **fields):
    page.evaluate("""({id, fields}) => {
        orderRows = [{id, pack_id:'PACK-100', country:'墨西哥', status:'已发-在途中',
            shop_name:'测试店铺', salesperson:'测试业务员', amount:20, fee:3, freight:4,
            actual_freight_usd:4, quoted_freight_usd:3.5, freight_pending_shipment:false,
            balance:13, income:91, cost:50, profit:41, purchase_order:'采购-100',
            ordered_at:'2026-10-04 08:00', shipping_deadline:'2026-10-05 18:00',
            title:'Camisa', product_id:'MLM123', seller_sku:'SKU-123', quantity:2, ...fields}];
        showOrderDetail(0);
    }""", {"id": order_id, "fields": fields})


@pytest.mark.parametrize("width", [1440, 390])
def test_order_freight_displays_api_quote_and_actual_only_after_shipment(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    open_detail(page, status="待发", quoted_freight_usd=7.25,
                actual_freight_usd=9.5, freight=7.25, freight_pending_shipment=True, balance=None)
    page.evaluate("renderOrderRows(orderRows)")
    freight_cell = page.locator("#order-table-body tr td").nth(5)
    assert freight_cell.locator("strong").inner_text() == "标价 $7.25"
    assert freight_cell.locator("span").all_inner_texts() == ["实际 —", "差额 —"]
    assert "美客多 API" in freight_cell.locator("div").get_attribute("title")
    assert "发货后同步" in freight_cell.locator("div").get_attribute("title")
    detail = page.locator("#order-detail-dialog")
    settlement_freight = detail.locator('.order-detail-finance-row[data-operator="−"]').nth(1).locator("strong")
    balance = detail.get_by_text("结余", exact=True).locator("..").locator("strong")
    balance_cell = page.locator("#order-table-body tr td").nth(6)
    assert settlement_freight.inner_text() == "$7.25"
    assert detail.get_by_text("标价运费", exact=True).count() == 1
    assert balance.inner_text() == "$9.75"
    assert balance_cell.locator(".order-money").inner_text() == "$9.75"
    assert "发货后结算" not in detail.inner_text()

    # A quote or the legacy freight alias must not fill an unavailable actual charge.
    page.evaluate("""() => {
        Object.assign(orderRows[0], {status:'已发-在途中', freight_pending_shipment:false,
            actual_freight_usd:null, freight:7.25});
        renderOrderRows(orderRows);
        showOrderDetail(0);
    }""")
    assert freight_cell.locator("span").all_inner_texts() == ["实际 —", "差额 —"]
    assert settlement_freight.inner_text() == "$7.25"
    assert balance.inner_text() == "$9.75"

    page.evaluate("""() => {
        Object.assign(orderRows[0], {actual_freight_usd:9.5, freight:7.25, balance:7.5});
        renderOrderRows(orderRows);
        showOrderDetail(0);
    }""")
    assert freight_cell.locator("strong").inner_text() == "标价 $7.25"
    assert freight_cell.locator("span").all_inner_texts() == ["实际 $9.50", "差额 +$2.25"]
    assert settlement_freight.inner_text() == "$9.50"
    assert detail.get_by_text("实际运费", exact=True).count() == 1
    assert balance.inner_text() == "$7.50"
    assert balance_cell.locator(".order-money").inner_text() == "$7.50"

    page.evaluate("orderRows[0].quoted_freight_usd = null; renderOrderRows(orderRows)")
    assert freight_cell.locator("strong").inner_text() == "标价 待同步"
    assert "待计算" not in freight_cell.inner_text()
    assert page.locator('#order-freight-variance-filter option[value="pending_quote"]').text_content() == "标价运费待同步"


@pytest.mark.parametrize("width", [1440, 390])
def test_order_detail_defers_optional_requests_and_resets_for_next_order(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    calls = []

    def optional(route):
        calls.append(urlsplit(route.request.url).path)
        data = {"rows": []} if route.request.method == "GET" else {
            "weight_complete": True, "actual_weight_g": 300, "shipping_amount_usd": 4,
        }
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))

    page.route("**/api/orders/*/logs", optional)
    page.route("**/api/orders/weight-quote", optional)
    open_detail(page)
    page.wait_for_timeout(100)
    assert not calls
    detail = page.locator("#order-detail-dialog")
    assert detail.is_visible()
    assert "人民币汇率待获取" in detail.inner_text()
    assert "来源" not in detail.inner_text()
    assert detail.locator(".order-detail-item > span", has_text="采购成本").count() == 1
    assert "Camisa" in detail.inner_text()
    assert detail.locator(".order-detail-footer").evaluate("el => el.scrollWidth <= el.clientWidth")
    screenshot_dir = os.environ.get("ORDER_DETAIL_SCREENSHOT_DIR")
    if screenshot_dir:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        detail.screenshot(path=str(Path(screenshot_dir) / f"order-detail-{width}.png"))
    detail.locator(".order-detail-weight-quote > summary").click()
    page.wait_for_function("document.getElementById('order-detail-weight-quote').textContent.includes('300 g')")
    assert calls == ["/api/orders/weight-quote"]
    detail.locator(".order-detail-weight-quote > summary").click()
    detail.locator(".order-detail-weight-quote > summary").click()
    detail.locator("#order-detail-logs > summary").click()
    page.wait_for_function("document.getElementById('order-detail-log-list').textContent.includes('暂无操作日志')")
    assert calls == ["/api/orders/weight-quote", "/api/orders/100/logs"]
    open_detail(page, "101")
    assert not page.locator("#order-detail-logs").evaluate("el => el.open")
    detail.locator("#order-detail-logs > summary").click()
    page.wait_for_function("document.getElementById('order-detail-log-list').textContent.includes('暂无操作日志')")
    assert calls[-1] == "/api/orders/101/logs"


@pytest.mark.parametrize("progressive", [False, True])
def test_product_is_readable_while_translation_pending_and_keeps_image_nodes(console_page, progressive):
    page = console_page
    translations = []
    calls = []
    data = {"item": {"id": "MLM123", "title": "Camisa", "price": 20,
                     "pictures": [{"id": "P1", "secure_url": "https://images.test/one.png"}],
                     "attributes": [{"id": "COLOR", "name": "Color", "value_name": "Azul"}]},
            "description": {"plain_text": "Ropa cómoda"}, "warnings": [], "translations_zh": {}}

    def product(route):
        params = parse_qs(urlsplit(route.request.url).query)
        calls.append(params)
        if params.get("translate") == ["1"]:
            translations.append(route)
        else:
            route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": {**data, "description": {}, "description_pending": True} if progressive else data}))

    page.route("**/api/orders/product-detail?**", product)
    open_detail(page)
    page.get_by_role("button", name="产品详情", exact=True).click()
    page.locator("#order-product-detail h2").wait_for()
    assert page.locator("#order-product-detail h2").inner_text() == "Camisa"
    assert page.locator("#order-product-detail .order-product-description").inner_text() == ("商品描述加载中…" if progressive else "Ropa cómoda")
    page.wait_for_function("document.querySelector('.order-product-translation').textContent.includes('加载中')")
    assert translations
    page.evaluate("window.originalProductImage = document.querySelector('#order-product-detail img')")
    translated = {**data, "translations_zh": {"Camisa": "衬衫", "Ropa cómoda": "舒适服装", "Azul": "蓝色"}, "translation_error": ""}
    translations[0].fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": translated}))
    page.wait_for_function("document.querySelector('#order-product-detail h2').textContent.includes('衬衫')")
    assert page.evaluate("window.originalProductImage === document.querySelector('#order-product-detail img')")
    assert "舒适服装" in page.locator("#order-product-detail .order-product-description").inner_text()
    assert [entry["translate"] for entry in calls] == [["0"], ["1"]]
    page.locator("#order-product-detail .order-picture-zoom").click()
    assert page.locator("#order-image-viewer").is_visible()
    assert page.locator("#order-image-viewer img").get_attribute("src") == "https://images.test/one.png"
    page.keyboard.press("Escape")
    assert not page.locator("#order-image-viewer").is_visible()
    assert page.locator("#order-product-detail").is_visible()


def test_profit_panel_uses_rmb_balance_and_platform_filter_is_in_advanced(console_page):
    page = console_page
    open_detail(page, amount=5.01, fee=0.78, actual_freight_usd=2.25,
                quoted_freight_usd=2.25, balance=1.98, balance_cny=13.25,
                income=33.53, cost=9.88, profit=3.37)
    panel = page.locator('[aria-label="人民币利润"]')
    assert panel.locator('.order-detail-finance-formula').inner_text() == '结余人民币 − 成本 = 利润'
    assert panel.get_by_text('￥13.25', exact=True).count() == 1
    assert panel.get_by_text('￥3.37', exact=True).count() == 1
    assert panel.get_by_text('￥33.53', exact=True).count() == 0
    page.locator('#order-detail-dialog .order-detail-close').click()
    assert page.locator('.zs-advanced-filters #order-purchase-platform-filter').count() == 1
    page.locator('#order-purchase-platform-filter').evaluate("el => el.value = 'pdd'")
    assert page.evaluate("orderQueryParams().get('purchase_platform')") == 'pdd'
    page.evaluate('resetOrderFilters()')
    assert page.locator('#order-purchase-platform-filter').input_value() == ''


@pytest.mark.parametrize('width',[1440,390])
def test_advanced_freight_difference_range_supports_negative_zero_and_reset(console_page,width):
    page=console_page
    page.set_viewport_size({'width':width,'height':950})
    page.evaluate("document.querySelectorAll('.tab-page').forEach(el=>el.classList.remove('active'));document.getElementById('tab-orders').classList.add('active')")
    advanced=page.locator('#tab-orders .zs-advanced-filters')
    advanced.locator('summary').click()
    low=advanced.locator('#order-freight-difference-min')
    high=advanced.locator('#order-freight-difference-max')
    low.fill('-2.50');high.fill('0')
    assert page.evaluate("orderQueryParams().get('freight_difference_min')")=='-2.50'
    assert page.evaluate("orderQueryParams().get('freight_difference_max')")=='0'
    assert low.is_visible() and high.is_visible()
    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
    page.screenshot(path=str(Path(__file__).resolve().parents[1]/f'artifacts/order-freight-range-20261010/range-{width}.png'))
    page.evaluate('resetOrderFilters()')
    assert low.input_value()=='' and high.input_value()==''


@pytest.mark.parametrize('cost',[None,0])
def test_missing_or_zero_cost_does_not_hide_numeric_profit_and_margin(console_page,cost):
    page=console_page
    open_detail(page,amount=7.54,fee=0.94,actual_freight_usd=None,quoted_freight_usd=2.60,
                status='找货-没汇总',balance=4,balance_cny=26.77,income=50.46,cost=cost,profit=26.77)
    page.evaluate("renderOrderRows(orderRows);renderOrderMetrics({total:1,summary:{amount:7.54,income:50.46,balance_cny:26.77,cost:0,profit:26.77,cost_missing_count:orderRows[0].cost==null?1:0}})")
    panel=page.locator('[aria-label="人民币利润"]')
    assert panel.get_by_text('￥26.77',exact=True).count()==2
    assert '利润率 53.1%' in panel.inner_text()
    assert '待核算' not in panel.inner_text()
    cost_note=panel.get_by_text('成本未填，暂按0',exact=True)
    assert cost_note.count()==(1 if cost is None else 0)
    cells=page.locator('#order-table-body tr td')
    assert cells.nth(8).inner_text()=='￥26.77'
    assert cells.nth(9).inner_text()=='53.1%'
    summary=page.locator('#order-metric-profit').inner_text()
    assert '￥26.77 / 53.1%' in summary
    assert '待核算' not in summary
    if cost is None:
        page.screenshot(path=str(Path(__file__).resolve().parents[1]/'artifacts/order-profit-display-20261011/profit-detail.png'))
