from decimal import Decimal

import pytest

from bit import bit_interface, bit_inventory


def _user(permissions=None):
    return {
        "id": 17,
        "username": "warehouse",
        "display_name": "仓库测试员",
        "role_name": "运营人员",
        "permissions": list(permissions or ["*"]),
        "access_version": 1,
    }


def _client(permissions=None):
    bit_interface.app.config.update(TESTING=True)
    client = bit_interface.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = _user(permissions)
    return client


def test_inbound_uses_weighted_average_cost():
    effect = bit_inventory.movement_effect(10, "5.0000", "inbound", 5, "8.0000")

    assert effect == {
        "before_quantity": 10,
        "after_quantity": 15,
        "unit_cost": Decimal("6.0000"),
        "movement_unit_cost": Decimal("8.0000"),
    }


def test_outbound_keeps_average_cost_and_rejects_negative_balance():
    effect = bit_inventory.movement_effect(7, "6.1250", "outbound", 3)

    assert effect["after_quantity"] == 4
    assert effect["movement_unit_cost"] == Decimal("6.1250")
    with pytest.raises(ValueError, match="不能超过当前库存 7"):
        bit_inventory.movement_effect(7, "6.1250", "outbound", 8)


@pytest.mark.parametrize("quantity", [0, -1, "abc", None])
def test_movement_quantity_must_be_positive_integer(quantity):
    with pytest.raises(ValueError, match="数量"):
        bit_inventory.movement_effect(1, 2, "inbound", quantity, 3)


def test_shelf_payload_normalizes_code_and_validates_capacity():
    payload = bit_inventory._normalize_shelf_payload(
        {"code": " wh-a/01 ", "name": "A 区货架", "capacity": "50"}
    )

    assert payload["code"] == "WH-A/01"
    assert payload["capacity"] == 50
    with pytest.raises(ValueError, match="货架编码"):
        bit_inventory._normalize_shelf_payload({"code": "A 区", "name": "货架"})


def test_order_cost_is_suggested_only_when_item_allocation_is_unambiguous():
    one_product = [
        {"product_id": "MLM1", "quantity": 2},
        {"product_id": "MLM1", "quantity": 3},
    ]
    mixed_products = [
        {"product_id": "MLM1", "quantity": 1},
        {"product_id": "MLM2", "quantity": 1},
    ]

    assert bit_inventory._suggested_unit_cost("50", one_product, "MLM1") == Decimal("10.0000")
    assert bit_inventory._suggested_unit_cost("50", mixed_products, "MLM1") is None


def test_matched_order_product_carries_order_context_into_inventory(monkeypatch):
    class Cursor:
        def __init__(self):
            self.rows = [
                {
                    "order_id": "20001",
                    "title": "订单商品",
                    "image_url": "https://img.example/item.jpg",
                    "raw_json": "{}",
                    "purchase_cost": "24",
                    "purchase_order": "PO-88",
                    "purchase_remark": "易碎",
                    "status_detail": "买家要求加固",
                    "token_id": 7,
                    "site_id": "MLM",
                },
                {"salesperson": "张三"},
            ]

        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return self.rows.pop(0)

    monkeypatch.setattr(bit_inventory, "_table_exists", lambda *_args: True)
    monkeypatch.setattr(
        bit_inventory,
        "_order_items",
        lambda *_args: [{
            "product_id": "MLM1",
            "title": "蓝色水杯",
            "image_url": "https://img.example/blue.jpg",
            "quantity": 2,
        }],
    )

    context = bit_inventory._matched_order_product(Cursor(), "20001", "MLM1")

    assert context["product_name"] == "蓝色水杯"
    assert context["image_url"] == "https://img.example/blue.jpg"
    assert context["suggested_unit_cost"] == Decimal("12.0000")
    assert context["reference_no"] == "PO-88"
    assert context["salesperson"] == "张三"
    assert context["order_remark"] == "采购备注：易碎；订单备注：买家要求加固"


def test_inventory_permission_mapping_distinguishes_views_movements_and_shelves():
    assert bit_interface._required_workbench_permissions(
        "/api/inventory/stocks", "GET"
    ) == ("inventory.view",)
    assert bit_interface._required_workbench_permissions(
        "/api/inventory/movements", "POST"
    ) == ("inventory.execute",)
    assert bit_interface._required_workbench_permissions(
        "/api/inventory/shelves/3", "PATCH"
    ) == ("inventory.manage",)


def test_inventory_stock_api_passes_filters_to_database_adapter(monkeypatch):
    seen = {}

    def fake_list(**filters):
        seen.update(filters)
        return {"rows": [], "total": 0, "page": 2, "pages": 2, "summary": {}}

    monkeypatch.setattr(bit_interface.bit_db_api, "list_inventory_stock", fake_list)
    monkeypatch.setattr(
        bit_interface, "get_current_workbench_user", lambda: _user(["inventory.view"])
    )
    response = _client(["inventory.view"]).get(
        "/api/inventory/stocks?search=MLM&shelf_id=4&stock_status=all&page=2&page_size=25"
    )

    assert response.status_code == 200
    assert seen == {
        "search": "MLM",
        "shelf_id": 4,
        "stock_status": "all",
        "page": 2,
        "page_size": 25,
    }
    assert response.headers["Cache-Control"] == "no-store"


def test_inventory_movement_api_uses_authenticated_operator(monkeypatch):
    seen = {}

    def fake_create(record):
        seen.update(record)
        return {"movement_id": 9, "before_quantity": 0, "after_quantity": 2}

    monkeypatch.setattr(bit_interface.bit_db_api, "create_inventory_movement", fake_create)
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _user(["inventory.view", "inventory.execute"]),
    )
    response = _client(["inventory.view", "inventory.execute"]).post(
        "/api/inventory/movements",
        json={
            "movement_type": "inbound",
            "shelf_id": 1,
            "order_id": "20001",
            "product_id": "MLM1",
            "quantity": 2,
            "unit_cost": 4.5,
            "operator_id": 999,
            "operator_name": "伪造操作人",
        },
    )

    assert response.status_code == 200
    assert seen["operator_id"] == 17
    assert seen["operator_name"] == "仓库测试员"


def test_inventory_execute_requires_permission(monkeypatch):
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "create_inventory_movement",
        lambda _record: pytest.fail("permission check should run before the handler"),
    )
    monkeypatch.setattr(
        bit_interface, "get_current_workbench_user", lambda: _user(["inventory.view"])
    )

    response = _client(["inventory.view"]).post(
        "/api/inventory/movements",
        json={"movement_type": "outbound", "stock_id": 1, "quantity": 1},
    )

    assert response.status_code == 403
    assert response.get_json()["required_permissions"] == ["inventory.execute"]
