"""Exercise the unified payment page with mocked billing and print APIs."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from test_billing_print_integration import ROOT, application
from test_console_ui_browser import console_browser  # noqa: F401


@pytest.mark.parametrize("width,role", [(1440, "super_admin"), (390, "enterprise_admin"), (1440, "warehouse"), (390, "member")])
def test_unified_payment_print_details_filters_and_permissions(console_browser, width, role):
    app, _ = application({"id": 7, "role_key": role})
    html = app.test_client().get("/billing").get_data(as_text=True)
    page = console_browser.new_page(viewport={"width": width, "height": 1000})
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    account = {"user_id": 2, "username": "sales-a", "display_name": "业务员甲", "organization_key": "alpha",
               "is_active": True, "balance_cents": 900, "total_recharge_cents": 1200, "total_charge_cents": 300}
    subscription = {"user_id": 7, "username": "current", "display_name": "当前账户", "organization_key": "alpha",
                    "is_active": True, "plan_name": "个人版", "plan": "personal", "actual_sites": 1,
                    "site_count": 1, "capacity": 10, "monthly_cents": 29900, "status": "active",
                    "expires_at": "2026-11-06T12:00:00", "days_remaining": 31}
    record = {"id": 1, "user_id": 2, "kind": "print", "order_id": "200001", "shop_name": "测试店铺",
              "site_id": "MLM", "amount_cents": -300, "balance_after_cents": 900,
              "actor_name": "仓库小王", "actor_username": "warehouse-wang", "actor_role_key": "warehouse",
              "printer_id": 3, "printer_name": "仓库小王", "printer_username": "warehouse-wang",
              "printer_role_key": "warehouse", "created_at": "2026-10-06T09:10:00",
              "requested_at": "2026-10-06T09:10:00", "printed_at": "2026-10-06T09:10:08",
              "print_time_source": "completed", "settled_at": "2026-10-06T09:10:08",
              "request_status": "settled", "print_result": "completed", "request_id": "mock-print-001",
              "note": "<script>untrusted note</script>"}

    def route(handler):
        request = handler.request
        parsed = urlsplit(request.url)
        path = parsed.path
        if path == "/billing":
            handler.fulfill(body=html, content_type="text/html")
            return
        if path.startswith("/static/"):
            handler.fulfill(path=str(ROOT / "bit" / path.lstrip("/")))
            return
        requests.append((request.method, path, parse_qs(parsed.query)))
        if path == "/api/print-wallet/accounts":
            data = {"accounts": [account], "unit_price_cents": 300, "can_recharge": role in {"super_admin", "enterprise_admin"}}
        elif path.endswith("/records") and path.startswith("/api/print-wallet/"):
            query = parse_qs(parsed.query)
            offset = int(query.get("offset", [0])[0])
            data = {"account": account, "records": [record] if not offset else [{**record, "order_id": "200051"}],
                    "total": 51, "limit": 50, "offset": offset}
        elif path == "/api/billing/accounts":
            data = [subscription]
        elif path == "/api/billing/me":
            data = subscription
        else:
            data = []
        handler.fulfill(body=json.dumps({"status": "success", "data": data}), content_type="application/json")

    page.route("**/*", route)
    try:
        page.goto("http://billing.test/billing?section=print-wallet")
        rows = page.locator("#print-wallet-records")
        page.wait_for_function("document.querySelector('#print-wallet-records').textContent.includes('200001')")
        assert page.locator("#print-wallet-tab").get_attribute("aria-selected") == "true"
        assert page.locator("#print-wallet").is_visible() and not page.locator("#subscription").is_visible()
        assert "仓库小王" in rows.inner_text() and "warehouse-wang" in rows.inner_text()
        assert "2026-10-06 09:10:08" in rows.inner_text() and "测试店铺" in rows.inner_text()
        assert "面单已生成" in rows.inner_text()
        assert rows.locator("script").count() == 0 and "<script>untrusted note</script>" in rows.inner_text()
        recharge = page.locator('#print-wallet-accounts button[data-action="recharge"]')
        assert recharge.count() == (1 if role in {"super_admin", "enterprise_admin"} else 0)
        page.locator("#print-wallet-record-next").click()
        page.wait_for_function("document.querySelector('#print-wallet-records').textContent.includes('200051')")
        assert page.locator("#print-wallet-record-page").inner_text().startswith("第 2 / 2 页")
        page.locator("#print-wallet-record-order").fill("200001")
        page.locator("#print-wallet-record-printer").fill("小王")
        page.locator("#print-wallet-record-date-from").fill("2026-10-01")
        page.locator("#print-wallet-record-date-to").fill("2026-10-06")
        page.locator("#print-wallet-record-kind").select_option("print")
        page.locator('#print-wallet-record-filters button[type="submit"]').click()
        page.wait_for_function("document.querySelector('#print-wallet-records').textContent.includes('200001')")
        query = [query for _, path, query in requests if path.endswith("/records")][-1]
        assert query == {"order_id": ["200001"], "printer": ["小王"], "date_from": ["2026-10-01"],
                         "date_to": ["2026-10-06"], "kind": ["print"], "offset": ["0"], "limit": ["50"]}
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        output = ROOT / "artifacts" / "billing-print-integration"
        output.mkdir(exist_ok=True)
        page.screenshot(path=str(output / f"print-details-{role}-{width}.png"), full_page=True)
        page.locator("#subscription-tab").click()
        assert page.locator("#subscription").is_visible() and not page.locator("#print-wallet").is_visible()
        assert page.locator('#accounts button[data-action="recharge"]').count() == (1 if role == "super_admin" else 0)
        assert all(method == "GET" for method, _, _ in requests)
        assert not errors
    finally:
        page.close()
