"""Offline checks for on-demand order detail and progressive product rendering."""
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from test_console_ui_browser import console_browser, console_page


def open_detail(page, order_id="100"):
    page.evaluate("""id => {
        orderRows = [{id, pack_id:'PACK-100', country:'墨西哥', status:'找货-没汇总',
            shop_name:'测试店铺', salesperson:'测试业务员', amount:20, fee:3, freight:4,
            balance:13, income:91, cost:50, profit:41, purchase_order:'采购-100',
            ordered_at:'2026-10-04 08:00', shipping_deadline:'2026-10-05 18:00',
            title:'Camisa', product_id:'MLM123', seller_sku:'SKU-123', quantity:2}];
        showOrderDetail(0);
    }""", order_id)


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
    assert "汇率" not in detail.inner_text()
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


def test_product_is_readable_while_translation_pending_and_keeps_image_nodes(console_page):
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
            route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))

    page.route("**/api/orders/product-detail?**", product)
    open_detail(page)
    page.get_by_role("button", name="产品详情", exact=True).click()
    page.locator("#order-product-detail h2").wait_for()
    assert page.locator("#order-product-detail h2").inner_text() == "Camisa"
    assert page.locator("#order-product-detail .order-product-description").inner_text() == "Ropa cómoda"
    page.wait_for_function("document.querySelector('.order-product-translation').textContent.includes('加载中')")
    assert translations
    page.evaluate("window.originalProductImage = document.querySelector('#order-product-detail img')")
    translated = {**data, "translations_zh": {"Camisa": "衬衫", "Ropa cómoda": "舒适服装", "Azul": "蓝色"}, "translation_error": ""}
    translations[0].fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": translated}))
    page.wait_for_function("document.querySelector('#order-product-detail h2').textContent.includes('衬衫')")
    assert page.evaluate("window.originalProductImage === document.querySelector('#order-product-detail img')")
    assert [entry["translate"] for entry in calls] == [["0"], ["1"]]
