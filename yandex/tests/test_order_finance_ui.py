"""Offline browser checks: real template/assets, fixture APIs, no server or database.

Run with ``python -m pytest yandex/tests/test_order_finance_ui.py``. Windows uses
installed Chrome; set YANDEX_TEST_BROWSER_CHANNEL to select another channel.
"""

from __future__ import annotations

import base64
import json
import os
import unittest
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import expect, sync_playwright


APP_DIR = Path(__file__).resolve().parents[1] / "app"
ORIGIN = "https://yandex-finance.test"
PRODUCT_URL = "https://market.yandex.ru/product--test-camera/123456789?sku=987654321"
PRODUCT_IMAGE = "https://avatars.mds.yandex.net/get-mpic/offline-test/orig"
IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5xkAAAAASUVORK5CYII="
)


def money(value, currency="RUB"):
    return {"value": value, "currency": currency}


def order_fixture(order_id=101, *, finance=None):
    return {
        "orderId": order_id,
        "creationDate": "2026-09-06T09:00:00Z",
        "status": "PROCESSING",
        "programType": "FBS",
        "items": [{"offerId": "SKU-1", "offerName": "测试商品", "count": 1}],
        # Deliberately conflicting legacy values must never fill finance fields.
        "prices": {"total": 987654.32, "buyerTotal": 876543.21, "currency": "RUB"},
        "finance": finance or {},
    }


def media_order_fixture(order_id=101, *, image_url=PRODUCT_IMAGE, product_url=PRODUCT_URL, title="高清行车记录仪 Pro"):
    item = {
        "name": title,
        "offer_id": f"SKU-{order_id}",
        "count": 1,
        "image_url": image_url,
        "product_url": product_url,
        "listing_unit": money(199, "CNY"),
    }
    order = order_fixture(order_id, finance={"items": [item]})
    order["items"] = [{
        "offerId": item["offer_id"],
        "offerName": item["name"],
        "count": item["count"],
        "image_url": image_url,
        "product_url": product_url,
    }]
    return order


class OrderFinanceBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        channel = os.environ.get("YANDEX_TEST_BROWSER_CHANNEL")
        if channel is None and os.name == "nt":
            channel = "chrome"
        cls.browser = cls.playwright.chromium.launch(headless=True, channel=channel)
        cls.addClassCleanup(cls.browser.close)
        template_env = Environment(
            loader=FileSystemLoader(APP_DIR / "templates"),
            autoescape=select_autoescape(["html"]),
        )
        cls.html = template_env.get_template("index.html").render(
            url_for=lambda _name, path: f"/static{path}",
            embedded=True,
            max_products=200,
        )
        cls.assets = {
            "/static/app.js": ("application/javascript", (APP_DIR / "static/app.js").read_text(encoding="utf-8")),
            "/static/styles.css": ("text/css", (APP_DIR / "static/styles.css").read_text(encoding="utf-8")),
        }

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors = []
        self.unexpected_requests = []
        self.resource_requests = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.assertEqual(self.errors, [], "Browser JavaScript errors")
        self.assertEqual(self.unexpected_requests, [], "All browser requests must remain mocked/offline")

    def open_orders(self, orders, *, resources=None):
        resources = resources or {}
        responses = {
            "/api/health": {"status": "ok"},
            "/api/stores": {"stores": [{"id": 1, "alias": "离线测试店铺", "store_name": "Fixture", "placement_type": "FBS"}]},
            "/api/zeshun-stores": {"stores": []},
            "/api/exchange-rate": {"exchange_rate": {"rate": 0.08, "effective_date": "2026-09-06"}},
            "/api/orders": {"orders": orders, "paging": {}},
        }

        def route_request(route):
            request = route.request
            parsed = urlparse(request.url)
            if request.url in resources:
                self.resource_requests.append(request.url)
                return route.fulfill(**resources[request.url])
            if f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
                self.unexpected_requests.append(request.url)
                return route.abort()
            if parsed.path == "/":
                return route.fulfill(content_type="text/html", body=self.html)
            if parsed.path in self.assets:
                content_type, body = self.assets[parsed.path]
                return route.fulfill(content_type=content_type, body=body)
            if parsed.path in responses:
                expected_method = "POST" if parsed.path == "/api/orders" else "GET"
                if request.method != expected_method:
                    self.unexpected_requests.append(f"{request.method} {request.url}")
                    return route.abort()
                return route.fulfill(content_type="application/json", body=json.dumps(responses[parsed.path]))
            self.unexpected_requests.append(request.url)
            return route.abort()

        # Context routing includes the very first navigation in a new product tab.
        self.context.route("**/*", route_request)
        self.page.goto(ORIGIN, wait_until="networkidle")
        self.page.locator("#ordersContent").wait_for(state="visible")

    def test_zero_is_a_known_amount_and_missing_data_stays_unknown(self):
        self.open_orders([order_fixture(finance={
            "listing_total": money(None),
            "buyer_payment": money(0),
            "seller_net": money(-12.5),
            "buyer_shipping": money(0),
            "seller_shipping": money(None),
        })])
        row = self.page.locator(".order-main-row")
        self.assertEqual(row.locator(".money-cell").all_text_contents(), ["—", "0.00 RUB", "-12.50 RUB"])
        self.assertIn("0.00 RUB", row.locator("td").nth(5).inner_text())
        self.assertEqual(self.page.locator("#orderListingTotal").inner_text(), "—")
        self.assertEqual(self.page.locator("#orderRevenue").inner_text(), "0.00 RUB")
        self.assertEqual(self.page.locator("#orderShippingTotal").inner_text(), "—")
        self.assertIn("0/1 单有数据", self.page.locator("#orderListingCoverage").inner_text())
        self.assertIn("1/1 单有数据", self.page.locator("#orderPaymentCoverage").inner_text())

    def test_each_summary_groups_currency_and_reports_coverage(self):
        fields = ("listing_total", "buyer_payment", "seller_net", "seller_shipping")
        orders = [
            order_fixture(101, finance={field: money(10, "RUB") for field in fields}),
            order_fixture(102, finance={field: money(20, "RUR") for field in fields}),
            order_fixture(103, finance={field: money(7, "CNY") for field in fields}),
            order_fixture(104, finance={field: money(100, None) for field in fields}),
        ]
        self.open_orders(orders)
        for selector in ("orderListingTotal", "orderRevenue", "orderSellerNet", "orderShippingTotal"):
            with self.subTest(summary=selector):
                self.assertEqual(set(self.page.locator(f"#{selector}").inner_text().split(" / ")), {"30.00 RUB", "7.00 CNY"})
        for selector in ("orderListingCoverage", "orderPaymentCoverage", "orderNetCoverage", "orderShippingCoverage"):
            self.assertIn("3/4 单有数据", self.page.locator(f"#{selector}").inner_text())

    def test_absent_finance_never_uses_legacy_prices(self):
        order = order_fixture()
        del order["finance"]
        self.open_orders([order])
        self.assertEqual(self.page.locator(".money-cell").all_text_contents(), ["—", "—", "—"])
        for selector in ("orderListingTotal", "orderRevenue", "orderSellerNet", "orderShippingTotal"):
            self.assertEqual(self.page.locator(f"#{selector}").inner_text(), "—")
        self.assertNotIn("987,654", self.page.locator("#ordersView").inner_text())
        self.assertNotIn("876,543", self.page.locator("#ordersView").inner_text())

    def test_expansion_includes_every_sku_and_escapes_api_text(self):
        hostile = '<img src=x onerror="window.financeInjected=true"> & "SKU"'
        items = [{
            "name": hostile if index == 0 else f"商品 {index + 1}",
            "offer_id": f"SKU-{index + 1}",
            "count": index + 1,
            "listing_unit": money(100),
            "listing_total": money(100 * (index + 1)),
            "buyer_payment": money(80 * (index + 1)),
            "cashback": money(0),
            "seller_subsidy": money(None),
        } for index in range(4)]
        order = order_fixture(finance={
            "items": items,
            "buyer_payment": money(800),
            "seller_net": money(3, 'RUB<svg onload="window.financeInjected=true">'),
            "settlement_label": hostile,
            "notes": [hostile],
        })
        order["items"] = [{"offerName": item["name"], "offerId": item["offer_id"], "count": item["count"]} for item in items]
        self.open_orders([order])
        self.assertIn("另有 2 种商品", self.page.locator(".order-main-row").inner_text())
        self.page.locator(".order-finance-details > summary").click()
        detail_rows = self.page.locator(".finance-items-table tbody tr")
        self.assertEqual(detail_rows.count(), 4)
        self.assertIn("SKU-4", detail_rows.nth(3).inner_text())
        self.assertEqual(detail_rows.nth(0).locator(".order-product-title").inner_text(), hostile)
        self.assertEqual(self.page.locator(".finance-notes li").inner_text(), hostile)
        self.assertEqual(self.page.locator('#ordersView img[src="x"], #ordersView svg[onload]').count(), 0)
        self.assertIsNone(self.page.evaluate("window.financeInjected"))

    def test_product_images_and_titles_match_in_list_and_sku_details(self):
        order = media_order_fixture()
        self.open_orders([order], resources={
            PRODUCT_IMAGE: {"content_type": "image/png", "body": IMAGE_BYTES},
            PRODUCT_URL: {"content_type": "text/html", "body": "<!doctype html><title>Offline product</title>Offline product"},
        })
        self.page.locator(".order-finance-details > summary").click()
        for selector in (".order-items", ".finance-items-table"):
            with self.subTest(surface=selector):
                title = self.page.locator(f"{selector} .order-product-title")
                self.assertEqual(title.inner_text(), "高清行车记录仪 Pro")
                self.assertEqual(title.get_attribute("href"), PRODUCT_URL)
                self.assertEqual(title.get_attribute("target"), "_blank")
                self.assertTrue({"noopener", "noreferrer"}.issubset(set(title.get_attribute("rel").split())))
                product_image = self.page.locator(f"{selector} img")
                expect(product_image).to_be_visible()
                self.assertEqual(product_image.get_attribute("src"), PRODUCT_IMAGE)
                self.assertTrue(product_image.get_attribute("alt"))
                product_image.scroll_into_view_if_needed()
                expect(product_image).to_have_js_property("complete", True)
                self.assertGreater(product_image.evaluate("image => image.naturalWidth"), 0)
                with self.context.expect_page() as popup_event:
                    title.click()
                popup = popup_event.value
                popup.wait_for_load_state()
                self.assertEqual(popup.url, PRODUCT_URL)
                self.assertIsNone(popup.evaluate("window.opener"))
                self.assertEqual(popup.evaluate("document.referrer"), "")
                self.assertEqual(self.page.url, ORIGIN + "/")
                popup.close()
        self.assertEqual(self.resource_requests.count(PRODUCT_URL), 2)

    def test_unsafe_product_urls_and_non_web_images_render_as_plain_content(self):
        unsafe_links = (
            "javascript:window.financeInjected=true",
            "data:text/html,<script>window.financeInjected=true</script>",
            "http://market.yandex.ru/product/123",
            "https://market.yandex.ru.evil.test/product/123",
            "https://market.yandex.ru@evil.test/product/123",
            "https://user:password@market.yandex.ru/product/123",
            "https://market.yandex.ru:8443/product/123",
            "https://evil.test/?next=https://market.yandex.ru/product/123",
        )
        unsafe_images = (
            "javascript:window.financeInjected=true",
            "data:image/svg+xml,<svg onload='window.financeInjected=true' />",
            "file:///C:/private-photo.png",
            "not-an-absolute-image-url",
        )
        orders = [media_order_fixture(
            101 + index,
            title=f"不可信链接商品 {index + 1}",
            product_url=url,
            image_url=unsafe_images[index % len(unsafe_images)],
        ) for index, url in enumerate(unsafe_links)]
        self.open_orders(orders)
        self.page.locator(".order-finance-details").evaluate_all("elements => elements.forEach(element => { element.open = true; })")
        self.assertEqual(self.page.locator("#orderTableBody .order-product-title").count(), 2 * len(orders))
        self.assertEqual(self.page.locator("#orderTableBody a.order-product-title").count(), 0)
        self.assertEqual(self.page.locator("#orderTableBody img").count(), 0)
        self.assertEqual(self.page.locator("#orderTableBody .order-product-placeholder").count(), 2 * len(orders))
        self.assertIsNone(self.page.evaluate("window.financeInjected"))

    def test_missing_and_failed_images_keep_placeholders_without_broken_images(self):
        broken_image = "https://avatars.mds.yandex.net/get-mpic/offline-missing/orig"
        orders = [
            media_order_fixture(101, image_url=None),
            media_order_fixture(102, image_url=""),
            media_order_fixture(103, image_url=broken_image),
        ]
        self.open_orders(orders, resources={
            broken_image: {"status": 404, "content_type": "image/png", "body": b""},
        })
        self.page.locator(".order-finance-details").evaluate_all("elements => elements.forEach(element => { element.open = true; })")
        self.page.locator(".finance-items-table").last.scroll_into_view_if_needed()
        expect(self.page.locator("#orderTableBody img")).to_have_count(0)
        placeholders = self.page.locator("#orderTableBody .order-product-placeholder")
        self.assertEqual(placeholders.count(), 6)
        for placeholder in placeholders.all():
            expect(placeholder).to_be_visible()
        self.assertGreaterEqual(self.resource_requests.count(broken_image), 1)
        self.assertLessEqual(self.resource_requests.count(broken_image), 2)

    def test_order_details_preserve_ids_delivery_boxes_and_escape_api_fields(self):
        hostile_external_id = '<img src=x onerror="window.financeInjected=true"> & external-order'
        order = order_fixture()
        order.update({
            "campaignId": 7300101,
            "externalOrderId": hostile_external_id,
            "updateDate": "2026-09-06T10:15:00Z",
            "paymentType": "PREPAID",
            "paymentMethod": "YANDEX",
            "buyerType": "PERSON",
            "cancelRequested": False,
            "fake": False,
            "sourcePlatform": "IOS",
            "notes": "请联系门卫后送到一楼收货处",
            "delivery": {
                "type": "DELIVERY",
                "serviceName": "Offline Delivery Service",
                "deliveryServiceId": 7300202,
                "deliveryPartnerType": "YANDEX_MARKET",
                "warehouseId": 7300606,
                "tracks": [{"trackCode": "OFFLINE-TRACK-0001", "deliveryServiceId": 7300202}],
                "courier": {
                    "region": {"name": "离线收货地区", "parent": {"name": "离线国家"}},
                    "address": {"street": "离线测试街道", "house": "18A", "floor": 0, "apartment": "OFFLINE-ROOM-18"},
                },
                "dates": {
                    "fromDate": "2026-09-07",
                    "toDate": "2026-09-09",
                    "fromTime": "09:30:00",
                    "toTime": "18:45:00",
                },
                "shipment": {"id": 7300303, "shipmentDate": "2026-09-07"},
                "boxesLayout": [
                    {"boxId": 7300404, "barcode": "OFFLINE-BOX-0001", "items": [{"id": 7300505, "fullCount": 2}]},
                    {"boxId": 7300405, "barcode": "OFFLINE-BOX-0002", "items": [{"id": 7300505, "partialCount": {"current": 1, "total": 3}}]},
                ],
            },
        })
        order["items"][0].update({"id": 7300505, "count": 2, "itemStatuses": [{"status": "PROCESSING", "count": 2}], "prices": {"vat": "VAT_20"}, "requiredInstanceTypes": ["CIS"], "instances": [{"cis": "OFFLINE-CIS-001", "gtd": "OFFLINE-CUSTOMS-001", "countryCode": "CN"}]})
        self.open_orders([order])
        detail = self.page.locator(".order-info-details")
        self.assertEqual(detail.count(), 1)
        self.assertFalse(detail.evaluate("element => element.open"))
        detail.locator(":scope > summary").click()
        self.assertTrue(detail.evaluate("element => element.open"))
        text = detail.inner_text()
        for value in ("7300101", hostile_external_id, "Offline Delivery Service", "7300202", "7300303", "7300404", "OFFLINE-BOX-0001", "7300505", "09:30", "18:45", "OFFLINE-TRACK-0001", "离线测试街道", "OFFLINE-ROOM-18", "OFFLINE-CIS-001", "OFFLINE-CUSTOMS-001", "请联系门卫", "拆分件 1 / 3"):
            with self.subTest(detail=value):
                self.assertIn(value, text)
        self.assertEqual(detail.locator('img[src="x"], script, svg[onload]').count(), 0)
        self.assertIsNone(self.page.evaluate("window.financeInjected"))
        floor = detail.locator(".order-info-field").filter(has=self.page.get_by_text("楼层", exact=True))
        self.assertEqual(floor.locator("dd").inner_text(), "0")

    def test_pickup_details_use_pickup_address_and_preserve_missing_values(self):
        order = order_fixture()
        order["delivery"] = {
            "type": "PICKUP",
            "pickup": {
                "logisticPointId": 7300707,
                "outletCode": "OFFLINE-PICKUP-0001",
                "outletStorageLimitDate": "2026-09-18",
                "address": {"city": "离线自提城市", "street": "离线自提街道"},
            },
            "courier": {"address": {"street": "不应显示的旧送货地址"}},
        }
        self.open_orders([order])
        detail = self.page.locator(".order-info-details")
        detail.locator(":scope > summary").click()
        text = detail.inner_text()
        for value in ("7300707", "OFFLINE-PICKUP-0001", "2026-09-18", "离线自提城市", "离线自提街道"):
            self.assertIn(value, text)
        self.assertNotIn("不应显示的旧送货地址", text)
        self.assertIn("物流单号尚未返回", text)
        self.assertIn("装箱信息尚未返回", text)
        cancel_requested = detail.locator(".order-info-field").filter(has=self.page.get_by_text("申请取消", exact=True))
        self.assertEqual(cancel_requested.locator("dd").inner_text(), "未返回")

    def test_mobile_cards_show_all_prices_and_only_sku_details_scroll(self):
        items = [{"name": "很长的商品名称" * 20, "offer_id": "SKU" + "X" * 100, "count": 3, "listing_total": money(123456.78), "image_url": PRODUCT_IMAGE, "product_url": PRODUCT_URL} for _ in range(4)]
        order = order_fixture(finance={
            "listing_total": money(123456.78),
            "buyer_payment": money(102345.67, "CNY"),
            "seller_net": money(98765.43),
            "seller_shipping": money(99),
            "notes": ["物流费用尚未完整返回，结余为估算。"],
            "items": items,
        })
        order["items"] = [{"offerName": item["name"], "offerId": item["offer_id"], "count": item["count"], "image_url": PRODUCT_IMAGE, "product_url": PRODUCT_URL} for item in items]
        order["externalOrderId"] = "LONG-EXTERNAL-ORDER-" + "X" * 200
        order["notes"] = "很长的配送备注 " * 30
        order["delivery"] = {
            "type": "DELIVERY",
            "tracks": [{"trackCode": "LONG-TRACK-" + "X" * 200}],
            "courier": {"address": {"street": "很长的送货街道地址 " * 30}},
        }
        self.open_orders([order], resources={PRODUCT_IMAGE: {"content_type": "image/png", "body": IMAGE_BYTES}})
        for width in (375, 390, 760):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 900})
                self.page.locator(".order-finance-details").evaluate("element => { element.open = true; }")
                self.page.locator(".order-info-details").evaluate("element => { element.open = true; }")
                dimensions = self.page.evaluate("""() => {
                    const container = document.querySelector('.order-table-wrap');
                    const skuContainer = document.querySelector('.finance-items-wrap');
                    return {
                        viewport: window.innerWidth,
                        document: document.documentElement.scrollWidth,
                        body: document.body.scrollWidth,
                        containerWidth: container.clientWidth,
                        containerContent: container.scrollWidth,
                        skuWidth: skuContainer.clientWidth,
                        skuContent: skuContainer.scrollWidth,
                        skuOverflow: getComputedStyle(skuContainer).overflowX,
                        products: [...document.querySelectorAll('.order-main-row .order-product-title, .order-main-row img')].map(element => {
                            const rect = element.getBoundingClientRect();
                            return { left: rect.left, right: rect.right, width: rect.width };
                        }),
                        cards: [...document.querySelectorAll('.order-main-row .finance-cell')].map(cell => {
                            const rect = cell.getBoundingClientRect();
                            const values = [...cell.querySelectorAll('.money-cell, .finance-line strong')];
                            return {
                                label: getComputedStyle(cell, '::before').content.replaceAll('"', ''),
                                text: cell.innerText,
                                left: rect.left,
                                right: rect.right,
                                amountBounds: values.map(value => {
                                    const bounds = value.getBoundingClientRect();
                                    return { left: bounds.left, right: bounds.right, width: bounds.width };
                                }),
                            };
                        }),
                    };
                }""")
                self.assertLessEqual(dimensions["document"], dimensions["viewport"], dimensions)
                self.assertLessEqual(dimensions["body"], dimensions["viewport"], dimensions)
                self.assertEqual(dimensions["containerWidth"], dimensions["containerContent"], dimensions)
                if width <= 390:
                    self.assertLess(dimensions["skuWidth"], dimensions["skuContent"], dimensions)
                self.assertIn(dimensions["skuOverflow"], ("auto", "scroll"))
                self.assertEqual(len(dimensions["products"]), 4)
                for product in dimensions["products"]:
                    self.assertGreater(product["width"], 0, product)
                    self.assertGreaterEqual(product["left"], 0, product)
                    self.assertLessEqual(product["right"], width, product)
                expected_amounts = {
                    "链接价格": "123,456.78 RUB",
                    "买家付款": "102,345.67 CNY",
                    "卖家结余": "98,765.43 RUB",
                    "运费": "99.00 RUB",
                }
                self.assertEqual(len(dimensions["cards"]), len(expected_amounts))
                for card in dimensions["cards"]:
                    self.assertIn(expected_amounts[card["label"]], card["text"])
                    self.assertGreaterEqual(card["left"], 0, card)
                    self.assertLessEqual(card["right"], width, card)
                    for bounds in card["amountBounds"]:
                        self.assertGreater(bounds["width"], 0, card)
                        self.assertGreaterEqual(bounds["left"], card["left"], card)
                        self.assertLessEqual(bounds["right"], card["right"], card)


if __name__ == "__main__":
    unittest.main()
