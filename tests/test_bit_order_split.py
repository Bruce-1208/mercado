import pytest

from bit import bit_order_split


def _contexts():
    return [
        {
            "order_id": "20001",
            "shipping_id": "30001",
            "token_id": 7,
            "access_token": "token",
            "refresh_token": "refresh",
            "expires_at": None,
        },
        {
            "order_id": "20002",
            "shipping_id": "30001",
            "token_id": 7,
            "access_token": "token",
            "refresh_token": "refresh",
            "expires_at": None,
        },
    ]


class FakeClient:
    split_calls = []

    def __init__(self, access_token):
        self.access_token = access_token

    def get_shipment(self, shipment_id):
        assert shipment_id == "30001"
        return {
            "id": shipment_id,
            "status": "ready_to_ship",
            "mode": "me2",
            "logistic_type": "drop_off",
        }

    def get_shipment_items(self, shipment_id):
        assert shipment_id == "30001"
        return [
            {
                "order_id": "20001",
                "item_id": "MLM1",
                "description": "商品 A",
                "quantity": 2,
            },
            {
                "order_id": "20002",
                "item_id": "MLM2",
                "description": "商品 B",
                "quantity": 1,
            },
        ]

    def split_shipment(self, shipment_id, *, reason, packs):
        self.split_calls.append((shipment_id, reason, packs))
        return {}


def test_preview_order_split_uses_live_shipment_item_quantities(monkeypatch):
    monkeypatch.setattr(
        bit_order_split.bit_mysql, "get_mercado_order_split_contexts", lambda _ids: _contexts()
    )
    monkeypatch.setattr(bit_order_split, "MercadoLibreClient", FakeClient)

    preview = bit_order_split.preview_order_split(["20001"])

    assert preview["eligible"] is True
    assert preview["shipment_id"] == "30001"
    assert preview["total_quantity"] == 3
    assert preview["orders"][0]["quantity"] == 2
    assert preview["logistic_type"] == "drop_off"


def test_preview_order_split_rejects_fulfillment(monkeypatch):
    class FulfillmentClient(FakeClient):
        def get_shipment(self, shipment_id):
            result = super().get_shipment(shipment_id)
            result["logistic_type"] = "fulfillment"
            return result

    monkeypatch.setattr(
        bit_order_split.bit_mysql, "get_mercado_order_split_contexts", lambda _ids: _contexts()
    )
    monkeypatch.setattr(bit_order_split, "MercadoLibreClient", FulfillmentClient)

    with pytest.raises(bit_order_split.MercadoOrderSplitError, match="drop-off / cross-docking"):
        bit_order_split.preview_order_split(["20001"])


def test_split_order_shipment_revalidates_totals_submits_and_logs(monkeypatch):
    FakeClient.split_calls = []
    recorded = []
    monkeypatch.setattr(
        bit_order_split.bit_mysql, "get_mercado_order_split_contexts", lambda _ids: _contexts()
    )
    monkeypatch.setattr(bit_order_split, "MercadoLibreClient", FakeClient)
    monkeypatch.setattr(
        bit_order_split.bit_mysql,
        "record_mercado_order_split_logs",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    packs = [
        {"orders": [{"id": "20001", "quantity": 1}]},
        {
            "orders": [
                {"id": "20001", "quantity": 1},
                {"id": "20002", "quantity": 1},
            ]
        },
    ]

    result = bit_order_split.split_order_shipment(
        ["20001", "20002"],
        shipment_id="30001",
        reason="fragile",
        packs=packs,
        operator_id=1,
        operator_name="测试用户",
    )

    assert result["submitted"] is True
    assert FakeClient.split_calls == [("30001", "FRAGILE", packs)]
    assert recorded[0][0][0] == ["20001", "20002"]
    assert recorded[0][1]["operator_name"] == "测试用户"


def test_split_order_shipment_rejects_quantity_mismatch(monkeypatch):
    monkeypatch.setattr(
        bit_order_split.bit_mysql, "get_mercado_order_split_contexts", lambda _ids: _contexts()
    )
    monkeypatch.setattr(bit_order_split, "MercadoLibreClient", FakeClient)

    with pytest.raises(bit_order_split.MercadoOrderSplitError, match="完全一致"):
        bit_order_split.split_order_shipment(
            ["20001", "20002"],
            shipment_id="30001",
            reason="FRAGILE",
            packs=[
                {"orders": [{"id": "20001", "quantity": 1}]},
                {"orders": [{"id": "20002", "quantity": 1}]},
            ],
        )
