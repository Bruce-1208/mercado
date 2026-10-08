"""Payment page preserves print recharge retries and pending-request refunds."""
from copy import deepcopy
from pathlib import Path
from threading import Thread

import pytest
from flask import Flask, jsonify, render_template, request
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


@pytest.fixture
def payment_page():
    root = Path(__file__).resolve().parents[1]
    app = Flask(__name__, template_folder=str(root / "bit/templates"),
                static_folder=str(root / "bit/static"))
    state = {"balance": 10000, "recharges": [], "refunds": [], "request_status": "reserved",
             "can_recharge": True}

    def account():
        return {"user_id": 2, "username": "sales", "display_name": "业务员甲",
                "organization_key": "测试企业", "is_active": True,
                "balance_cents": state["balance"], "total_recharge_cents": 10000,
                "total_charge_cents": 300}

    @app.get("/billing")
    def page():
        return render_template("account_billing.html", current_user={"id": 1},
                               billing_admin=True, wallet_admin=True)

    @app.get("/api/billing/accounts")
    def subscriptions():
        return jsonify(status="success", data=[])

    @app.get("/api/print-wallet/accounts")
    def wallets():
        return jsonify(status="success", data={"accounts": [account()],
                       "unit_price_cents": 300, "can_recharge": state["can_recharge"]})

    @app.get("/api/print-wallet/accounts/2/records")
    def records():
        return jsonify(status="success", data={"account": account(), "total": 1, "records": [{
            "id": 1, "kind": "print", "request_id": "reserved-print-request",
            "request_status": state["request_status"], "amount_cents": -300,
            "balance_after_cents": 10000, "order_id": "ORDER-101", "shop_name": "店铺甲",
            "actor_name": "仓库小王", "printer_name": "仓库小王", "printer_username": "warehouse",
            "created_at": "2026-10-06T09:00:00", "requested_at": "2026-10-06T09:00:00",
            "printed_at": None, "print_result": "pending" if state["request_status"] == "reserved" else "cancelled",
            "note": "批量面单打印",
        }]})

    @app.post("/api/print-wallet/accounts/2/recharges")
    def recharge():
        data = deepcopy(request.get_json())
        state["recharges"].append(data)
        if len(state["recharges"]) == 1:
            state["balance"] += 1000
            # Simulate money committed while the response outcome is unknown.
            return jsonify(status="error", message="充值结果待确认"), 500
        assert data == state["recharges"][0]
        return jsonify(status="success", data={"duplicate": True})

    @app.post("/api/print-wallet/requests/reserved-print-request/cancel")
    def cancel():
        state["refunds"].append(deepcopy(request.get_json()))
        state["request_status"] = "cancelled"
        state["balance"] += 300
        return jsonify(status="success", data={"duplicate": False, "refund_cents": 300})

    server = make_server("127.0.0.1", 0, app)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/billing?section=print-wallet", state
    finally:
        server.shutdown()
        worker.join(timeout=3)


def test_integrated_wallet_recharge_keeps_same_request_after_reload(payment_page):
    url, state = payment_page
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        page.locator('#print-wallet-accounts button[data-action="recharge"]').click()
        page.locator('[name="wallet_amount"]').fill("10.00")
        page.locator('[name="wallet_note"]').fill("收款凭证 RECHARGE-01")
        page.locator("#print-wallet-submit-recharge").click()
        page.wait_for_function("document.querySelector('#print-wallet-recharge-error').textContent.includes('原记录重试')")
        assert page.locator('[name="wallet_amount"]').is_disabled()
        assert page.locator('[name="wallet_note"]').is_disabled()
        original = state["recharges"][0]
        assert state["balance"] == 11000
        page.locator("#print-wallet-cancel-recharge").click()
        page.reload()
        page.locator('#print-wallet-accounts button[data-action="recharge"]').click()
        assert page.locator('[name="wallet_amount"]').input_value() == "10.00"
        assert page.locator('[name="wallet_amount"]').is_disabled()
        page.locator("#print-wallet-submit-recharge").click()
        page.wait_for_function("document.querySelector('#print-wallet-message').textContent.includes('未重复入账')")
        assert state["recharges"] == [original, original]
        assert state["balance"] == 11000
        assert page.evaluate("sessionStorage.getItem('print-wallet.pending-recharges.1')") == "{}"
        assert not errors
        browser.close()


def test_integrated_wallet_refunds_reserved_request_and_obeys_api_capability(payment_page):
    url, state = payment_page
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        page.locator("#print-wallet-records button[data-refund-request]").click()
        assert "reserved-print-request" in page.locator("#print-wallet-refund-account").inner_text()
        page.locator("#print-wallet-refund-note").fill("仓库页面关闭，面单未完成")
        page.locator("#print-wallet-submit-refund").click()
        page.wait_for_function("document.querySelector('#print-wallet-message').textContent.includes('退回 ¥3.00')")
        page.wait_for_function("document.querySelector('#print-wallet-records').textContent.includes('已取消，已退款')")
        assert state["refunds"] == [{"note": "仓库页面关闭，面单未完成"}]
        assert state["balance"] == 10300
        assert page.locator("#print-wallet-records button[data-refund-request]").count() == 0
        state["can_recharge"] = False
        state["request_status"] = "reserved"
        page.reload()
        page.wait_for_function("document.querySelector('#print-wallet-records').textContent.includes('ORDER-101')")
        assert page.locator('#print-wallet-accounts button[data-action="recharge"]').count() == 0
        assert page.locator("#print-wallet-records button[data-refund-request]").count() == 0
        assert not errors
        browser.close()
