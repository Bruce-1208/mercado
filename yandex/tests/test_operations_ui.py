"""Offline browser smoke test for links, inventory, returns, reviews and questions."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import expect, sync_playwright


APP_DIR = Path(__file__).resolve().parents[1] / "app"
ORIGIN = "https://yandex-operations.test"


class OperationsBrowserTests(unittest.TestCase):
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
        self.context = self.browser.new_context(
            viewport={"width": 1440, "height": 1000}, service_workers="block"
        )
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors = []
        self.unexpected_requests = []
        self.writes = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("dialog", lambda dialog: dialog.accept())

    def tearDown(self):
        self.assertEqual(self.errors, [], "Browser JavaScript errors")
        self.assertEqual(self.unexpected_requests, [], "All requests must stay mocked")

    def open_console(self):
        static_responses = {
            "/api/health": {"status": "ok"},
            "/api/stores": {"stores": [{"id": 1, "alias": "运营测试店", "store_name": "Fixture", "placement_type": "FBS"}]},
            "/api/zeshun-stores": {"stores": []},
            "/api/exchange-rate": {"exchange_rate": {"rate": 0.08, "effective_date": "2026-09-06"}},
            "/api/orders": {"orders": [], "paging": {}},
            "/api/listings": {
                "offers": [{
                    "offerId": "SKU-1", "status": "PUBLISHED", "available": True,
                    "basicPrice": {"value": 199, "currencyId": "CNY"},
                    "campaignPrice": {"value": 189, "currencyId": "CNY"},
                    "details": {
                        "name": "测试记录仪链接", "vendor": "Fixture",
                        "pictures": [],
                        "showcaseUrls": [{"showcaseType": "B2C", "showcaseUrl": "https://market.yandex.ru/product--fixture/123"}],
                    },
                }],
                "paging": {}, "warning": "",
            },
            "/api/inventory": {
                "stockMethod": "business",
                "warning": "",
                "warehouses": [{"warehouseId": 55, "warehouseName": "莫斯科主仓", "offers": [{"offerId": "SKU-1", "stocks": [{"type": "AVAILABLE", "count": 7}], "updatedAt": "2026-09-06T09:00:00Z", "details": {"name": "测试记录仪", "vendor": "Fixture", "price": {"value": 199, "currencyId": "CNY"}}}]}],
                "paging": {},
            },
            "/api/returns": {
                "returns": [{"id": 31, "orderId": 101, "returnType": "RETURN", "refundStatus": "PREMODERATION_DECISION_WAITING", "shipmentStatus": "READY_FOR_PICKUP", "creationDate": "2026-09-05T09:00:00Z", "updateDate": "2026-09-06T09:00:00Z", "amount": {"value": 199, "currencyId": "CNY"}, "items": [{"shopSku": "SKU-1", "count": 1}]}],
                "paging": {},
            },
            "/api/feedback": {
                "feedbacks": [{"feedbackId": 71, "createdAt": "2026-09-05T09:00:00Z", "needReaction": True, "author": "测试买家", "identifiers": {"offerId": "SKU-1"}, "description": {"comment": "清晰好用"}, "statistics": {"rating": 5, "commentsCount": 0}}],
                "paging": {},
            },
            "/api/questions": {
                "questions": [{"questionIdentifiers": {"id": 81, "offerId": "SKU-1"}, "text": "支持夜视吗？", "createdAt": "2026-09-05T09:00:00Z", "author": {"name": "提问买家"}, "votes": {"likes": 1, "dislikes": 0}}],
                "totalCount": 1,
                "paging": {},
            },
        }
        write_responses = {
            "/api/inventory/stock": {"ok": True},
            "/api/feedback/reply": {"ok": True, "comment": {"id": 72}},
            "/api/feedback/skip": {"ok": True},
            "/api/questions/reply": {"ok": True, "result": {"entity": {"id": 82}}},
            "/api/orders/action": {"ok": True},
            "/api/listings/price": {"ok": True, "priceScope": "campaign"},
            "/api/listings/delete": {"ok": True, "deleted": ["SKU-1"], "notDeletedOfferIds": []},
        }

        def route_request(route):
            request = route.request
            parsed = urlparse(request.url)
            if f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
                self.unexpected_requests.append(request.url)
                return route.abort()
            if parsed.path == "/":
                return route.fulfill(content_type="text/html", body=self.html)
            if parsed.path in self.assets:
                content_type, body = self.assets[parsed.path]
                return route.fulfill(content_type=content_type, body=body)
            if parsed.path in write_responses:
                self.writes.append((parsed.path, json.loads(request.post_data or "{}")))
                return route.fulfill(content_type="application/json", body=json.dumps(write_responses[parsed.path]))
            if parsed.path in static_responses:
                return route.fulfill(content_type="application/json", body=json.dumps(static_responses[parsed.path]))
            self.unexpected_requests.append(request.url)
            return route.abort()

        self.context.route("**/*", route_request)
        self.page.goto(ORIGIN, wait_until="networkidle")

    def test_all_new_operations_render_and_submit_scoped_writes(self):
        self.open_console()

        self.page.locator('[data-view-target="listings"]').click()
        expect(self.page.locator("#listingTableBody")).to_contain_text("测试记录仪链接")
        expect(self.page.locator("#listingPublishedCount")).to_have_text("1")
        self.page.locator("[data-listing-price] input[required]").fill("179")
        self.page.locator("[data-listing-price] button[type=submit]").click()
        self.page.locator("[data-listing-delete]").click()

        self.page.locator('[data-view-target="inventory"]').click()
        expect(self.page.locator("#inventoryTableBody")).to_contain_text("测试记录仪")
        expect(self.page.locator("#inventoryAvailableCount")).to_have_text("7")
        self.page.locator("#inventoryTableBody input[type=number]").fill("0")
        self.page.locator("[data-stock-update]").click()
        expect(self.page.locator("#inventoryTableBody")).to_contain_text("莫斯科主仓")

        self.page.locator('[data-view-target="returns"]').click()
        expect(self.page.locator("#returnList")).to_contain_text("等待卖家决定")
        expect(self.page.locator("#returnPickupCount")).to_have_text("1")

        self.page.locator('[data-view-target="feedback"]').click()
        expect(self.page.locator("#feedbackList")).to_contain_text("清晰好用")
        self.page.locator("[data-feedback-reply] textarea").fill("感谢您的认可")
        self.page.locator("[data-feedback-reply] button[type=submit]").click()

        self.page.locator('[data-feedback-tab="questions"]').click()
        expect(self.page.locator("#questionList")).to_contain_text("支持夜视吗？")
        self.page.locator("[data-question-reply] textarea").fill("支持，请在设置中开启夜视模式")
        self.page.locator("[data-question-reply] button[type=submit]").click()

        writes = dict(self.writes)
        self.assertEqual(writes["/api/listings/price"]["offer_id"], "SKU-1")
        self.assertEqual(writes["/api/listings/price"]["value"], 179)
        self.assertEqual(writes["/api/listings/delete"], {"store_id": 1, "offer_ids": ["SKU-1"]})
        self.assertEqual(writes["/api/inventory/stock"], {"store_id": 1, "offer_id": "SKU-1", "count": 0})
        self.assertEqual(writes["/api/feedback/reply"]["feedback_id"], 71)
        self.assertEqual(writes["/api/questions/reply"]["question_id"], 81)


if __name__ == "__main__":
    unittest.main()
