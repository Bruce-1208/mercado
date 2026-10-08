"""Offline coverage of purchase-cost net proceeds choices and their audit UI."""
import json
import os
from pathlib import Path

import pytest

from test_console_ui_browser import console_browser, console_page


def open_purchase(page, order_id="100"):
    page.evaluate("""id => {
        orderRows = [{id, title: 'Camisa', product_id: 'MLM123', quantity: 2,
            shop_name: '测试店铺', status: '找货-没汇总', cost: 67,
            purchase_order: '', purchase_tracking: '', purchase_remark: ''}];
        openPurchaseOrderDialog([id]);
    }""", order_id)


def capture_save(page, sync=None):
    calls = []

    def save(route):
        calls.append(route.request.post_data_json)
        data = {"matched": 1, "changed": 1}
        if sync is not None:
            data["net_proceeds_sync"] = sync
        route.fulfill(content_type="application/json", body=json.dumps({
            "status": "success", "data": data,
        }))

    page.route("**/api/orders/bulk-update", save)
    # Isolate the purchase request; its ordinary post-save refresh is covered
    # by order-list tests and need not replace our supplied sample order.
    page.evaluate("loadOrders = async () => {}")
    return calls


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("ratio", [150, 200])
def test_purchase_net_choices_are_readable_and_send_selected_ratio(console_page, width, ratio):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    calls = capture_save(page, {"ratio": ratio, "product_matched": 3,
                                "store_link_matched": 5, "unmatched_titles": []})
    open_purchase(page)
    dialog = page.locator("#order-purchase-dialog")
    choices = dialog.locator("#order-purchase-net-ratio-options")
    assert choices.is_visible()
    assert choices.locator("button").count() == 3
    assert choices.evaluate("el => el.scrollWidth <= el.clientWidth")
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
    assert choices.locator('[data-purchase-net-ratio=""]').get_attribute("aria-pressed") == "true"
    selected = choices.locator(f'[data-purchase-net-ratio="{ratio}"]')
    selected.click()
    assert selected.get_attribute("aria-pressed") == "true"
    assert choices.locator('[aria-pressed="true"]').count() == 1
    hint = dialog.locator("#order-purchase-net-hint").inner_text()
    assert "¥67.00" in hint and f"{ratio}%" in hint
    assert "商品数量" in hint and "汇率" in hint
    screenshot_dir = os.environ.get("PURCHASE_NET_SCREENSHOT_DIR")
    if screenshot_dir:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        dialog.screenshot(path=str(Path(screenshot_dir) / f"purchase-{width}-{ratio}.png"))
    page.locator("#order-purchase-number").fill("PO-100")
    page.locator("#order-purchase-cost").fill("67.50")
    page.locator("#order-purchase-save").click()
    page.wait_for_function("!orderPurchaseSaving")
    assert calls == [{"order_ids": ["100"], "purchase_order": "PO-100",
                      "purchase_tracking": "", "logistics_company": "", "transfer_warehouse": "shahu",
                      "purchase_cost": "67.50", "purchase_remark": "",
                      "purchase_net_proceeds_ratio": ratio}]
    assert not dialog.is_visible()
    message = page.locator("#order-bulk-message").inner_text()
    assert f"{ratio}%" in message
    assert "产品 3 条" in message and "店铺链接 5 条" in message


@pytest.mark.parametrize("cost", ["", "0", "-1"])
def test_purchase_net_sync_requires_positive_cost_before_request(console_page, cost):
    page = console_page
    calls = capture_save(page)
    open_purchase(page)
    page.locator("#order-purchase-number").fill("PO-100")
    page.locator('[data-purchase-net-ratio="150"]').click()
    page.locator("#order-purchase-cost").fill(cost)
    page.locator("#order-purchase-save").click()
    assert not calls
    assert page.locator("#order-purchase-dialog").is_visible()
    assert "大于 0" in page.locator("#order-purchase-message").inner_text()
    assert not page.locator("#order-purchase-save").is_disabled()


def test_purchase_without_ratio_preserves_existing_save_and_resets_on_reopen(console_page):
    page = console_page
    calls = capture_save(page)
    open_purchase(page)
    page.locator('[data-purchase-net-ratio="200"]').click()
    page.get_by_role("button", name="关闭采购单", exact=True).click()
    open_purchase(page, "101")
    assert page.evaluate("orderPurchaseNetProceedsRatio") is None
    assert page.locator('[data-purchase-net-ratio=""]').get_attribute("aria-pressed") == "true"
    page.locator("#order-purchase-number").fill("PO-101")
    page.locator("#order-purchase-cost").fill("")
    page.locator("#order-purchase-save").click()
    page.wait_for_function("!orderPurchaseSaving")
    assert len(calls) == 1
    assert calls[0]["order_ids"] == ["101"]
    assert calls[0]["purchase_cost"] == ""
    assert "purchase_net_proceeds_ratio" not in calls[0]


def test_purchase_save_failure_keeps_ratio_and_explains_backend_error(console_page):
    page = console_page
    calls = []

    def reject(route):
        calls.append(route.request.post_data_json)
        route.fulfill(status=400, content_type="application/json", body=json.dumps({
            "status": "error", "message": "订单含多个不同名称的商品，无法分摊采购成本",
        }))

    page.route("**/api/orders/bulk-update", reject)
    open_purchase(page)
    page.locator("#order-purchase-number").fill("PO-100")
    page.locator('[data-purchase-net-ratio="150"]').click()
    page.locator("#order-purchase-save").click()
    page.wait_for_function("!orderPurchaseSaving")
    assert len(calls) == 1
    assert page.locator("#order-purchase-dialog").is_visible()
    assert "多个不同名称" in page.locator("#order-purchase-message").inner_text()
    assert page.locator('[data-purchase-net-ratio="150"]').get_attribute("aria-pressed") == "true"
    assert not page.locator("#order-purchase-save").is_disabled()


@pytest.mark.parametrize("width", [1440, 390])
def test_purchase_net_audit_exposes_formula_each_target_and_escapes_names(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    title = '<img src=x onerror="window.purchaseAuditInjected=true"> Camisa & "azul"'
    store = '<script>window.purchaseAuditInjected=true</script> 测试店铺'
    sync = {"order_id": "100", "title": title, "purchase_cost_cny": "67.0000",
            "quantity": 2, "unit_cost_cny": "33.5000", "ratio": 150,
            "net_proceeds_usd": "7.5000",
            "exchange_rate": {"cny_per_usd": "6.70000000", "rate_date": "2026-10-06",
                              "source": "ECB", "refreshed_at": "2026-10-07 08:00:00"},
            "product_matched": 1, "product_changed": 1,
            "store_link_matched": 1, "store_link_changed": 1,
            "unmatched_titles": [], "targets": [
                {"target_type": "product", "id": 8, "title": title,
                 "source_item_id": "1688-8", "old_net_proceeds_usd": "5.0000",
                 "new_net_proceeds_usd": "7.5000", "changed": True},
                {"target_type": "store_link", "id": 9, "title": title,
                 "store_name": store, "token_id": 7, "item_id": "MLM123",
                 "old_net_proceeds_usd": "6.0000", "new_net_proceeds_usd": "7.5000",
                 "changed": True},
            ]}

    def logs(route):
        route.fulfill(content_type="application/json", body=json.dumps({
            "status": "success", "data": {"rows": [{
                "action_label": "采购成本同步净收益", "operator_name": "测试用户",
                "created_at": "2026-10-07 08:00:00", "changes": {"net_proceeds_sync": {
                    "label": "采购成本同步净收益", "before": None,
                    "after": "成本的 150% · USD 7.5000 · 产品 1 / 链接 1",
                }},
                "after": {"net_proceeds_sync": sync},
            }]},
        }))

    page.route("**/api/orders/100/logs", logs)
    open_purchase(page)
    page.get_by_role("button", name="关闭采购单", exact=True).click()
    page.evaluate("showOrderDetail(0)")
    page.locator("#order-detail-logs > summary").click()
    details = page.locator(".order-net-sync-details")
    details.wait_for()
    details.locator("summary").click()
    text = details.inner_text()
    for expected in ("150%", "67.0000", "33.5000", "6.70000000", "2026-10-06", "ECB"):
        assert expected in text
    targets = details.locator(".order-net-sync-target")
    assert targets.count() == 2
    assert "1688-8" in targets.nth(0).inner_text()
    assert "MLM123" in targets.nth(1).inner_text()
    assert store in targets.nth(1).inner_text()
    assert title in targets.nth(0).inner_text()
    assert "5.0000 → 7.5000" in targets.nth(0).inner_text()
    assert "6.0000 → 7.5000" in targets.nth(1).inner_text()
    assert details.locator("img,script").count() == 0
    assert "[object Object]" not in page.locator("#order-detail-log-list").inner_text()
    assert not page.evaluate("Boolean(window.purchaseAuditInjected)")
    assert details.evaluate("el => el.scrollWidth <= el.clientWidth")
    screenshot_dir = os.environ.get("PURCHASE_NET_SCREENSHOT_DIR")
    if screenshot_dir:
        details.screenshot(path=str(Path(screenshot_dir) / f"purchase-audit-{width}.png"))


def test_purchase_net_audit_keeps_unmatched_product_name_visible(console_page):
    page = console_page
    page.evaluate("""() => {
        orderRows = [{id: '100', title: 'Camisa', product_id: 'MLM123'}];
        showOrderDetail(0);
        document.getElementById('order-detail-logs').dataset.loaded = '1';
        document.getElementById('order-detail-logs').open = true;
        document.getElementById('order-detail-log-list').innerHTML = renderPurchaseNetProceedsLog({
            title: 'Camisa sin coincidencia', purchase_cost_cny: '67', quantity: 2,
            unit_cost_cny: '33.5', ratio: 200, net_proceeds_usd: '10',
            exchange_rate: {cny_per_usd: '6.7', rate_date: '2026-10-06', source: 'ECB'},
            product_matched: 0, store_link_matched: 0,
            unmatched_titles: ['Camisa sin coincidencia'], targets: []
        });
    }""")
    details = page.locator(".order-net-sync-details")
    details.locator("summary").click()
    assert "未匹配" in details.inner_text()
    assert "Camisa sin coincidencia" in details.inner_text()
    assert details.locator(".order-net-sync-target").count() == 0
