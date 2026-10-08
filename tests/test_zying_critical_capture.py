"""Critical Zying values survive capture, task transport and product storage."""
import json
from decimal import Decimal

import pytest

from bit import bit_zying_caiji as collector
from erp import mercadolibre_collection_store as store
from test_browser_extension_management_categories import workbench, HEADERS
from test_erp_mercadolibre_collection_store import _FakeConnection


@pytest.mark.parametrize("net", ["12.25", "0", "-3.5"])
@pytest.mark.parametrize("currency", ["USD", "CNY"])
def test_detail_values_reach_product_columns_even_with_non_usd_price(net, currency):
    record = {"product_id": "123", "title": "产品", "salesperson": "业务乙"}
    collector._merge_detail_record(record, {}, {
        "sale_cur": currency, "sale_cost": 50, "sale_netproceed": net,
        "sale_weight": 240, "sale_size": "[9, 5, 11]",
    })
    collector._finalize_zying_listing_snapshot(record)
    connection = _FakeConnection()
    store.upsert_zying_products_to_products([record], connection_factory=lambda: connection)
    sql, rows = next((s, p) for s, p in connection.fake_cursor.queries
                     if s.startswith(f"INSERT INTO `{store.PRODUCT_TABLE}`"))
    row = rows[0]
    assert row[10] == Decimal("240")
    assert row[12:15] == (Decimal("9"), Decimal("5"), Decimal("11"))
    assert row[19] == Decimal(net)
    assert row[9] == currency
    snapshot = json.loads(row[22])
    assert snapshot["plugin_snapshot"]["salesperson"] == "业务乙"
    assert store._mirror_zying_snapshot_fields({
        "source_type": "zying", "source_snapshot_json": row[22],
    })["salesperson"] == "业务乙"
    assert "COALESCE(VALUES(`net_proceeds_usd`), `net_proceeds_usd`)" in sql
    assert "COALESCE(VALUES(`weight_g`), `weight_g`)" in sql


def test_clicked_form_values_survive_snapshot_when_api_metrics_are_missing():
    record = {"title": "产品", "sale_price": "CNY 50", "detail_data": {}}
    collector._merge_ui_detail_record(record, {
        "product_id": "123", "sale_price": "50", "net_income": "12.5",
        "package_gross_weight": "240", "size_length": "9", "size_width": "5",
        "size_height": "11", "review_status": "通过",
    })
    snapshot = collector._finalize_zying_listing_snapshot(record)["listing_snapshot"]
    assert snapshot["weight_g"] == Decimal("240")
    assert snapshot["package_height_cm"] == Decimal("11")
    assert snapshot["net_proceeds_usd"] == Decimal("12.5")


def test_source_snapshot_and_form_fields_can_recover_missing_record_values():
    assert store.zying_captured_metrics({
        "zying_net_proceeds_usd": "USD 7.5",
        "page_snapshot": {"detail_form_fields": {
            "weight": "120", "sizeLength": "3", "sizeWidth": "4", "sizeHeight": "5",
        }},
    }) == {"net_proceeds_usd": Decimal("7.5"), "weight_g": Decimal("120"),
           "package_length_cm": Decimal("3"), "package_width_cm": Decimal("4"),
           "package_height_cm": Decimal("5")}
    assert all(value is None for value in store.zying_captured_metrics({}).values())


def test_detail_can_use_explicit_list_metrics_when_detail_omits_them():
    record = {"product_id": "123", "title": "产品"}
    collector._merge_detail_record(record, {"netproceed": 0, "weight": 240,
                                          "size": [9, 5, 11]}, {})
    snapshot = collector._finalize_zying_listing_snapshot(record)["listing_snapshot"]
    assert snapshot["net_proceeds_usd"] == 0
    assert snapshot["weight_g"] == 240
    assert snapshot["package_height_cm"] == 11


def test_plugin_start_uses_authenticated_salesperson(workbench, monkeypatch):
    received = []
    monkeypatch.setattr(workbench, "_remember_browser_extension_zying_categories", lambda data: [])
    monkeypatch.setattr(workbench, "_start_zying_collection_task",
                        lambda params, **kwargs: (received.append(params) or {}, "", 200))
    response = workbench.app.test_client().post(
        "/api/browser-extension/zying/start", headers=HEADERS,
        json={"credential": "offline-token", "salesperson": "错误业务员"},
    )
    assert response.status_code == 200
    assert received[0]["salesperson"] == "采集员"


def test_api_task_carries_salesperson_to_both_writers(monkeypatch):
    monkeypatch.setattr(collector, "list_zying_product_developers", lambda token: [])
    monkeypatch.setattr(collector, "_load_zying_category_name_lookup", lambda reader: {})
    def api_post(session, token, action, payload):
        if action == "sale.stat":
            return {"list": {"data": [{"id": 123, "title": "产品",
                    "netproceed": 0, "weight": 240, "size": [9, 5, 11]}]}}
        assert action == "sale.detail"
        assert payload == {"id": "123"}
        return {"root": [{"sale_id": 123, "sale_cur": "CNY", "sale_cost": 50}]}

    monkeypatch.setattr(collector, "_zying_api_post", api_post)
    monkeypatch.setattr(collector, "_enrich_product_categories", lambda *args: None)
    writes = []
    collector.collect_zying_products(
        auth_token="offline-token", max_items=1, salesperson="业务乙",
        existing_product_id_reader=lambda ids: set(),
        product_writer=lambda rows: writes.append(rows) or len(rows),
        product_mirror_writer=lambda rows: writes.append(rows) or {"count": len(rows)},
    )
    assert len(writes) == 2
    for rows in writes:
        assert rows[0]["salesperson"] == "业务乙"
        snapshot = rows[0]["listing_snapshot"]
        assert snapshot["plugin_snapshot"]["salesperson"] == "业务乙"
        assert snapshot["net_proceeds_usd"] == 0
        assert snapshot["weight_g"] == 240
        assert snapshot["package_height_cm"] == 11
