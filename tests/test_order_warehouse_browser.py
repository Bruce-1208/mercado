"""Real purchase and employee forms with offline API responses."""
import json
from pathlib import Path

import pytest

from test_console_ui_browser import console_browser, console_page
from test_purchase_net_proceeds_browser import capture_save, open_purchase


@pytest.mark.parametrize("width", [1440, 390])
def test_purchase_defaults_and_saves_selected_destination(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    calls = capture_save(page)
    open_purchase(page)
    warehouse = page.locator("#order-transfer-warehouse")
    assert warehouse.input_value() == "shahu"
    assert warehouse.locator("option").all_text_contents() == ["沙湖仓库", "东莞仓", "义乌仓"]
    warehouse.select_option("yiwu")
    assert page.locator("#order-purchase-dialog").evaluate("el => el.scrollWidth <= el.clientWidth")
    out = Path('/Users/a11/mercado/artifacts/order-warehouses-20261008')
    out.mkdir(parents=True, exist_ok=True)
    page.locator("#order-purchase-dialog").screenshot(path=str(out / f'purchase-{width}.png'))
    page.locator("#order-purchase-number").fill("PO-1")
    page.locator("#order-purchase-save").click()
    page.wait_for_function("!orderPurchaseSaving")
    assert calls[0]["transfer_warehouse"] == "yiwu"
    page.evaluate("orderRows[0].transfer_warehouse = 'dongguan'; openPurchaseOrderDialog(['100'])")
    assert warehouse.input_value() == "dongguan"


def test_mixed_destination_batch_requires_explicit_choice(console_page):
    page = console_page
    calls = capture_save(page)
    page.evaluate("""() => {
        orderRows = [{id:'100', transfer_warehouse:'shahu'}, {id:'101', transfer_warehouse:'yiwu'}];
        openPurchaseOrderDialog(['100', '101']);
    }""")
    assert page.locator("#order-transfer-warehouse").input_value() == ""
    page.locator("#order-purchase-number").fill("PO-batch")
    page.locator("#order-purchase-save").click()
    assert not calls
    assert "请选择转运商" in page.locator("#order-purchase-message").inner_text()
    page.locator("#order-transfer-warehouse").select_option("dongguan")
    page.locator("#order-purchase-save").click()
    page.wait_for_function("!orderPurchaseSaving")
    assert calls[0]["order_ids"] == ["100", "101"]
    assert calls[0]["transfer_warehouse"] == "dongguan"


def test_warehouse_personnel_cannot_change_destination_in_form(console_page):
    page = console_page
    page.evaluate("""() => {
        currentWorkbenchUser.role_key = 'warehouse';
        currentWorkbenchUser.warehouse_key = 'dongguan';
        orderRows = [{id:'100', transfer_warehouse:'dongguan'}];
        openPurchaseOrderDialog(['100']);
    }""")
    assert page.locator("#order-transfer-warehouse").input_value() == "dongguan"
    assert page.locator("#order-transfer-warehouse").is_disabled()


def test_employee_warehouse_binding_required_and_submitted(console_page):
    page = console_page
    calls = []
    page.route("**/api/access/users", lambda route: (
        calls.append(route.request.post_data_json),
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": 9})),
    ))
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-access').classList.add('active');
        currentWorkbenchUser.permissions = ['*'];
        accessRoles = [{role_key:'member', role_name:'成员'}, {role_key:'warehouse', role_name:'仓库人员'}];
        loadAccessManagement = async () => {};
        openAccessUserEditor();
    }""")
    page.locator("#access-username").fill("new_warehouse")
    page.locator("#access-display-name").fill("仓库人员")
    page.locator("#access-user-password").fill("password123")
    page.locator("#access-user-role").select_option("warehouse")
    binding = page.locator("#access-user-warehouse")
    assert binding.is_visible()
    assert binding.get_attribute("required") is not None
    page.locator("#save-access-user-btn").click()
    assert not calls
    binding.select_option("yiwu")
    page.locator("#save-access-user-btn").click()
    page.wait_for_function("document.getElementById('access-user-form').hidden")
    assert calls[0]["role_key"] == "warehouse"
    assert calls[0]["warehouse_key"] == "yiwu"
