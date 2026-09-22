import pytest
import inspect
from unittest.mock import patch

import bit.bit_interface as workbench
from bit import bit_db_api, bit_mysql


def _client():
    workbench.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = workbench.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {
            "id": 1,
            "username": "tester",
            "display_name": "测试用户",
        }
    return client


def test_workbench_contains_order_management_ui():
    response = _client().get("/")

    assert response.status_code == 200
    assert b'data-tab="orders"' in response.data
    assert b'id="tab-orders"' in response.data
    assert b'id="order-status-strip"' in response.data
    assert b'id="order-store-filter"' in response.data
    assert b'id="order-salesperson-filter"' in response.data
    assert b'id="order-group-filter"' in response.data
    assert b'id="order-table-body"' in response.data
    assert b'id="order-board"' in response.data
    assert b'order-density-compact' in response.data
    assert b'data-density="compact"' in response.data
    assert b'data-density="comfortable"' in response.data
    assert b'function setOrderDensity(density, persist = true)' in response.data
    assert b'zeshun-order-density' in response.data
    assert b'class="order-status-overview"' in response.data
    assert b'id="order-sync-dialog"' in response.data
    assert b'id="order-sync-start-date" type="datetime-local"' in response.data
    assert b'id="order-sync-end-date" type="datetime-local"' in response.data
    assert "自定义拉取时间段（北京时间）".encode("utf-8") in response.data
    assert "近 30 天".encode("utf-8") in response.data
    assert b"orderSyncStartDate.disabled = manualSyncRunning" in response.data
    assert b"start.setDate(start.getDate() - 6)" not in response.data
    assert b'data-origin="token"' in response.data
    assert "打包单号和子订单号都支持搜索".encode("utf-8") in response.data
    assert "下单时间（北京时间）".encode("utf-8") in response.data
    assert "预计利润 / 利润率".encode("utf-8") in response.data
    assert "手续费".encode("utf-8") in response.data
    assert "运费".encode("utf-8") in response.data
    assert "结余".encode("utf-8") in response.data
    assert "每 15 分钟重新拉取".encode("utf-8") in response.data
    assert "最近 72 小时".encode("utf-8") in response.data
    assert "每天刷新老订单状态".encode("utf-8") in response.data
    assert "Shipment Costs 实际运费".encode("utf-8") in response.data
    assert "Token 自动拉取".encode("utf-8") in response.data
    order_panel = response.get_data(as_text=True).split('id="tab-orders"', 1)[1].split('<div class="tab-page"', 1)[0]
    assert "智赢导入" not in order_panel
    assert b'id="order-select-all"' in response.data
    assert b'id="order-bulk-status"' in response.data
    assert b'id="order-bulk-purchase-button"' in response.data
    assert b'id="order-bulk-print-button"' in response.data
    assert "添加采购单".encode("utf-8") in response.data
    assert b'id="order-purchase-dialog"' in response.data
    assert b'id="order-purchase-tracking"' in response.data
    assert b'id="order-purchase-tracking-sync-button"' in response.data
    assert b'id="purchase-tracking-sync-dialog"' in response.data
    assert "/api/orders/purchase-tracking/start".encode("utf-8") in response.data
    assert "这里不填写采购平台账号或密码".encode("utf-8") in response.data
    assert b'id="order-purchase-cost"' in response.data
    assert b'id="order-purchase-remark"' in response.data
    assert b'id="order-tracking-dialog"' in response.data
    assert b'id="order-detail-log-list"' in response.data
    assert b'/api/orders/print' in response.data
    assert "店铺（可多选）".encode("utf-8") in response.data
    assert "店铺业务员（可多选）".encode("utf-8") in response.data
    assert '<option value="200" selected>200 条/页</option>'.encode("utf-8") in response.data
    assert "合并单全部 SKU".encode("utf-8") in response.data
    assert "order-detail-sku-media".encode("utf-8") in response.data
    assert b"function orderProductNameMarkup(item)" in response.data
    assert b'class="order-product-link"' in response.data
    assert "打开美客多前台商品".encode("utf-8") in response.data
    assert "订单重量与重量表运费".encode("utf-8") in response.data
    assert "官方重量表运费（美元）".encode("utf-8") in response.data
    assert b"function loadOrderWeightQuote(orderIds)" in response.data
    assert b'id="order-freight-variance-filter"' in response.data
    assert "存在运费差价".encode("utf-8") in response.data
    assert "标价 / 实际运费".encode("utf-8") in response.data


def test_order_detail_contains_weight_quote_ui():
    response = _client().get("/")

    assert response.status_code == 200
    assert "订单重量与重量表运费".encode("utf-8") in response.data
    assert "官方重量表运费（美元）".encode("utf-8") in response.data
    assert b"function loadOrderWeightQuote(orderIds)" in response.data


def test_order_detail_can_start_inventory_inbound_with_order_context():
    response = _client().get("/")

    assert response.status_code == 200
    assert b'id="order-detail-inbound-button"' in response.data
    assert b"function openOrderInboundFromDetail()" in response.data
    assert b"function orderInboundMatches(row)" in response.data
    assert b'openInventoryMovementDialog("inbound", 0, matches)' in response.data
    assert "业务员 ${row.salesperson}".encode("utf-8") in response.data
    assert b"inventory-movement-quantity" in response.data


def test_order_management_displays_pack_number_without_exposing_child_number():
    response = _client().get("/")

    assert response.status_code == 200
    assert "<th>打包单号</th><th>商品</th><th>店铺</th>".encode("utf-8") in response.data
    assert b"function orderDisplayNumber(row)" in response.data
    assert b'return String(row?.pack_id || row?.order_number || "");' in response.data
    assert b"copyOrderNumber('${encodedDisplayNumber}', this)" in response.data
    assert '["子订单号",'.encode("utf-8") not in response.data
    assert '<span>子订单 ${'.encode("utf-8") not in response.data


def test_order_query_displays_pack_id_but_keeps_child_order_id_searchable():
    source = inspect.getsource(bit_mysql.list_orders)

    assert source.count("AS `order_number`") >= 2
    assert source.count("JSON_EXTRACT(synced.`raw_json`, '$.pack_id')") >= 3
    assert "CAST({column('id')} AS CHAR) LIKE %s" in source
    assert "{column('order_number')} LIKE %s" in source


def test_order_management_contains_shipment_split_ui():
    response = _client().get("/")

    assert response.status_code == 200
    assert b'id="order-split-dialog"' in response.data
    assert b'id="order-detail-split-button"' in response.data
    assert b'/api/orders/split/preview' in response.data
    assert b'/api/orders/split' in response.data
    assert "确认拆成两个包裹".encode("utf-8") in response.data
    assert "仅支持 ME2 drop-off / cross-docking".encode("utf-8") in response.data


def test_order_api_requires_login():
    workbench.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    response = workbench.app.test_client().get("/api/orders")

    assert response.status_code == 401


def test_order_api_passes_filters_and_pagination():
    result = {
        "rows": [{"id": 145370454, "country": "巴西", "status": "找货"}],
        "total": 1,
        "page": 2,
        "page_size": 25,
        "pages": 3,
        "status_counts": {"找货": 1},
        "country_counts": {"巴西": 1},
        "summary": {"amount": 7.45, "income": 31.69, "cost": 12, "profit": 19.69},
    }
    with patch.object(workbench, "db_list_orders", return_value=result) as list_orders:
        response = _client().get(
            "/api/orders?country=%E5%B7%B4%E8%A5%BF&status=%E6%89%BE%E8%B4%A7"
            "&search=2000014667&start_date=2026-08-01&end_date=2026-08-23"
            "&origin=token&salesperson=%E5%BC%A0%E4%B8%89"
            "&group_name=%E7%B2%BE%E5%93%81%E7%BB%84&page=2&page_size=25"
        )

    assert response.status_code == 200
    assert response.get_json()["data"]["rows"][0]["id"] == 145370454
    list_orders.assert_called_once_with(
        country="巴西",
        status="找货",
        salesperson="张三",
        group_name="精品组",
        search="2000014667",
        start_date="2026-08-01",
        end_date="2026-08-23",
        origin="token",
        freight_variance="",
        page=2,
        page_size=25,
    )


def test_order_api_caps_page_size():
    with patch.object(
        workbench,
        "db_list_orders",
        return_value={
            "rows": [], "total": 0, "page": 1, "page_size": 200, "pages": 1,
            "status_counts": {}, "country_counts": {}, "summary": {},
        },
    ) as list_orders:
        response = _client().get("/api/orders?page_size=9999")

    assert response.status_code == 200
    assert list_orders.call_args.kwargs["page_size"] == 200


def test_order_api_passes_freight_variance_filter():
    with patch.object(
        workbench,
        "db_list_orders",
        return_value={"rows": [], "total": 0, "page": 1, "page_size": 200},
    ) as list_orders:
        response = _client().get("/api/orders?freight_variance=actual_higher")

    assert response.status_code == 200
    assert list_orders.call_args.kwargs["freight_variance"] == "actual_higher"


def test_order_weight_quote_route_returns_weight_and_rate_card_freight():
    quote = {
        "order_ids": ["20001", "20002"],
        "actual_weight_g": 1100,
        "billable_weight_g": 1300,
        "weight_complete": True,
        "shipping_amount_usd": 12.34,
        "rate_weight_label": "1.0 - 1.5 kg",
    }
    with patch.object(
        workbench.bit_db_api,
        "get_order_weight_quote",
        return_value=quote,
    ) as get_quote:
        response = _client().post(
            "/api/orders/weight-quote",
            json={"order_ids": ["20001", "20002"]},
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["data"]["shipping_amount_usd"] == 12.34
    get_quote.assert_called_once_with(["20001", "20002"])


def test_order_split_preview_route_returns_live_quantities():
    preview = {
        "shipment_id": "30001",
        "order_ids": ["20001"],
        "orders": [{"order_id": "20001", "quantity": 2, "items": []}],
        "total_quantity": 2,
        "eligible": True,
    }
    with patch.object(
        workbench.bit_db_api, "preview_order_split", return_value=preview
    ) as preview_split:
        response = _client().post(
            "/api/orders/split/preview", json={"order_ids": ["20001"]}
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["data"]["total_quantity"] == 2
    preview_split.assert_called_once_with(["20001"])


def test_order_split_route_forwards_operator_and_exact_two_pack_plan():
    packs = [
        {"orders": [{"id": "20001", "quantity": 1}]},
        {"orders": [{"id": "20001", "quantity": 1}]},
    ]
    result = {
        "submitted": True,
        "shipment_id": "30001",
        "order_ids": ["20001"],
        "message": "拆分请求已提交",
    }
    with patch.object(
        workbench.bit_db_api, "split_order_shipment", return_value=result
    ) as split_shipment:
        response = _client().post(
            "/api/orders/split",
            json={
                "order_ids": ["20001"],
                "shipment_id": "30001",
                "reason": "FRAGILE",
                "packs": packs,
            },
        )

    assert response.status_code == 200
    assert response.get_json()["data"]["submitted"] is True
    split_shipment.assert_called_once_with(
        ["20001"],
        shipment_id="30001",
        reason="FRAGILE",
        packs=packs,
        operator_id=1,
        operator_name="测试用户",
    )


def test_order_split_db_api_forwards_to_database_service(monkeypatch):
    captured = {}
    packs = [
        {"orders": [{"id": "20001", "quantity": 1}]},
        {"orders": [{"id": "20001", "quantity": 1}]},
    ]

    def fake_request(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return {"submitted": True}

    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(bit_db_api, "_request", fake_request)

    result = bit_db_api.split_order_shipment(
        ["20001"],
        shipment_id="30001",
        reason="FRAGILE",
        packs=packs,
        operator_id=1,
        operator_name="测试用户",
    )

    assert result["submitted"] is True
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/db/orders/split"
    assert captured["json"]["packs"] == packs
    assert captured["json"]["operator_name"] == "测试用户"


def test_order_api_passes_multiple_stores_and_salespeople():
    with patch.object(
        workbench,
        "db_list_orders",
        return_value={
            "rows": [], "total": 0, "page": 1, "page_size": 50, "pages": 1,
            "status_counts": {}, "country_counts": {}, "store_counts": [],
            "salesperson_counts": {}, "summary": {},
        },
    ) as list_orders:
        response = _client().get(
            "/api/orders?store_id=2&store_id=7"
            "&salesperson=%E5%BC%A0%E4%B8%89&salesperson=%E6%9D%8E%E5%9B%9B"
        )

    assert response.status_code == 200
    assert list_orders.call_args.kwargs["store_ids"] == [2, 7]
    assert list_orders.call_args.kwargs["salespeople"] == ["张三", "李四"]
    assert list_orders.call_args.kwargs["salesperson"] == ""


def test_order_db_api_forwards_multi_value_filters(monkeypatch):
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return {"rows": [], "total": 0}

    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(bit_db_api, "_request", fake_request)

    bit_db_api.list_orders(store_ids=[2, 7], salespeople=["张三", "__unassigned__"])

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/db/orders"
    assert captured["params"]["store_id"] == [2, 7]
    assert captured["params"]["salesperson"] == ["张三", "__unassigned__"]


def test_manual_order_sync_start_uses_selected_token_stores():
    state = {"running": True, "status": "starting", "task_id": "task-1"}
    with patch.object(
        workbench.bit_db_api,
        "start_order_sync",
        return_value={"started": True, "state": state},
    ) as start_sync:
        response = _client().post(
            "/api/order-sync/start",
            json={
                "start_date": "2026-08-01",
                "end_date": "2026-08-23",
                "token_ids": [2, 7],
            },
        )

    assert response.status_code == 202
    assert response.get_json()["data"]["task_id"] == "task-1"
    start_sync.assert_called_once_with(
        start_date="2026-08-01",
        end_date="2026-08-23",
        token_ids=[2, 7],
        mode="manual",
    )


def test_order_sync_status_is_not_cached():
    with patch.object(
        workbench.bit_db_api,
        "get_order_sync_status",
        return_value={
            "running": False,
            "status": "completed",
            "scheduler_enabled": True,
            "sync_interval_seconds": 900,
        },
    ):
        response = _client().get("/api/order-sync/status")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["data"]["sync_interval_seconds"] == 900


def test_bulk_update_orders_supports_status_and_complete_purchase_order():
    with patch.object(
        workbench.bit_db_api,
        "bulk_update_orders",
        return_value={"matched": 2, "changed": 2},
    ) as bulk_update:
        response = _client().post(
            "/api/orders/bulk-update",
            json={
                "order_ids": ["20001", "20002"],
                "workflow_status": "配货",
                "purchase_order": "CG-20260824-01",
                "purchase_tracking": "SF123456",
                "logistics_company": "shunfeng",
                "purchase_cost": "88.50",
                "purchase_remark": "采购备注",
            },
        )

    assert response.status_code == 200
    assert response.get_json()["data"] == {"matched": 2, "changed": 2}
    bulk_update.assert_called_once_with(
        ["20001", "20002"],
        operator_id=1,
        operator_name="测试用户",
        workflow_status="配货",
        purchase_order="CG-20260824-01",
        purchase_tracking="SF123456",
        logistics_company="shunfeng",
        purchase_cost="88.50",
        purchase_remark="采购备注",
    )


def test_order_tracking_route_returns_inline_timeline_data():
    tracking = {
        "tracking_number": "SF123456",
        "external_url": "https://www.kuaidi100.com/chaxun?com=shunfeng&nu=SF123456",
        "events": [{"time": "2026-08-24 10:00", "description": "已揽收"}],
    }
    with patch.object(workbench.bit_db_api, "get_order_tracking", return_value=tracking):
        response = _client().get("/api/orders/20001/tracking")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["data"]["events"][0]["description"] == "已揽收"


def test_purchase_tracking_sync_start_validates_purchase_orders_and_starts_local_browser():
    state = {
        "running": True,
        "phase": "starting",
        "platform": "1688",
        "total": 1,
    }
    with (
        patch.object(
            workbench.bit_db_api,
            "get_purchase_tracking_orders",
            return_value=[{"order_id": "20001", "purchase_order": "PO-1688-1"}],
        ),
        patch.object(
            workbench.purchase_tracking_sync_manager,
            "start",
            return_value=state,
        ) as start_sync,
    ):
        response = _client().post(
            "/api/orders/purchase-tracking/start",
            json={
                "order_ids": ["20001"],
                "platform": "1688",
                "account": "buyer@example.com",
                "password": "one-time-secret",
            },
        )

    assert response.status_code == 202
    assert response.get_json()["data"]["total"] == 1
    assert start_sync.call_args.kwargs["orders"] == [
        {"order_id": "20001", "purchase_order": "PO-1688-1"},
    ]
    assert start_sync.call_args.kwargs["platform"] == "1688"
    assert start_sync.call_args.kwargs["account"] == "buyer@example.com"
    assert start_sync.call_args.kwargs["password"] == "one-time-secret"


def test_purchase_tracking_sync_rejects_selected_order_without_purchase_number():
    with patch.object(
        workbench.bit_db_api,
        "get_purchase_tracking_orders",
        return_value=[{"order_id": "20001", "purchase_order": ""}],
    ):
        response = _client().post(
            "/api/orders/purchase-tracking/start",
            json={"order_ids": ["20001"], "platform": "taobao", "account": "buyer"},
        )

    assert response.status_code == 400
    assert "尚未填写采购订单号" in response.get_json()["message"]


def test_purchase_tracking_sync_status_is_not_cached():
    with patch.object(
        workbench.purchase_tracking_sync_manager,
        "status",
        return_value={"running": False, "phase": "completed", "synced": 2},
    ):
        response = _client().get("/api/orders/purchase-tracking/status")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["data"]["synced"] == 2


def test_purchase_tracking_order_lookup_uses_database_service_in_client_mode(monkeypatch):
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return [{"order_id": "20001", "purchase_order": "PO-1"}]

    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(bit_db_api, "_request", fake_request)

    result = bit_db_api.get_purchase_tracking_orders(["20001"])

    assert result[0]["purchase_order"] == "PO-1"
    assert captured == {
        "method": "POST",
        "path": "/api/db/orders/purchase-tracking",
        "json": {"order_ids": ["20001"]},
    }


def test_order_print_route_returns_pdf_and_records_operator_log():
    with (
        patch.object(
            workbench.bit_db_api,
            "download_order_labels",
            return_value={
                "content": b"%PDF-1.4\n%%EOF",
                "filename": "mercado-label-30001.pdf",
                "order_ids": ["20001"],
                "shipment_count": 1,
            },
        ) as download_labels,
        patch.object(workbench.bit_db_api, "record_order_print_logs", return_value=1) as record_logs,
    ):
        response = _client().post("/api/orders/print", json={"order_ids": ["20001"]})

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data.startswith(b"%PDF")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Mercado-Shipment-Count"] == "1"
    assert response.headers["X-Mercado-Printed-Order-Count"] == "1"
    assert response.headers["X-Mercado-Result"] == "success"
    download_labels.assert_called_once_with(["20001"])
    record_logs.assert_called_once_with(
        ["20001"], operator_id=1, operator_name="测试用户"
    )


def test_order_print_route_returns_partial_pdf_and_records_only_successful_orders():
    with (
        patch.object(
            workbench.bit_db_api,
            "download_order_labels",
            return_value={
                "content": b"%PDF-1.4\n%%EOF",
                "filename": "mercado-label-30001.pdf",
                "order_ids": ["20001"],
                "shipment_count": 1,
                "skipped_order_ids": ["20002"],
                "failed_order_ids": ["20003"],
            },
        ),
        patch.object(workbench.bit_db_api, "record_order_print_logs", return_value=1) as record_logs,
    ):
        response = _client().post(
            "/api/orders/print",
            json={"order_ids": ["20001", "20002", "20003"]},
        )

    assert response.status_code == 200
    assert response.data.startswith(b"%PDF")
    assert response.headers["X-Mercado-Result"] == "partial"
    assert response.headers["X-Mercado-Printed-Order-Count"] == "1"
    assert response.headers["X-Mercado-Skipped-Order-Count"] == "1"
    assert response.headers["X-Mercado-Failed-Order-Count"] == "1"
    record_logs.assert_called_once_with(
        ["20001"], operator_id=1, operator_name="测试用户"
    )


def test_order_print_route_keeps_valid_pdf_when_print_log_write_fails():
    with (
        patch.object(
            workbench.bit_db_api,
            "download_order_labels",
            return_value={
                "content": b"%PDF-1.4\n%%EOF",
                "filename": "mercado-label-30001.pdf",
                "order_ids": ["20001"],
                "shipment_count": 1,
            },
        ),
        patch.object(
            workbench.bit_db_api,
            "record_order_print_logs",
            side_effect=RuntimeError("数据库暂时不可用"),
        ),
    ):
        response = _client().post("/api/orders/print", json={"order_ids": ["20001"]})

    assert response.status_code == 200
    assert response.data.startswith(b"%PDF")
    assert response.headers["X-Mercado-Print-Log"] == "failed"


def test_warehouse_manual_print_marks_only_successful_orders_as_waiting_to_ship():
    client = _client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {
            **flask_session["workbench_user"],
            "role_key": "warehouse",
        }
    with (
        patch.object(
            workbench.bit_db_api,
            "download_order_labels",
            return_value={
                "content": b"%PDF-1.4\n%%EOF",
                "filename": "mercado-label-30001.pdf",
                "order_ids": ["20001"],
                "shipment_count": 1,
                "skipped_order_ids": ["20002"],
            },
        ),
        patch.object(workbench.bit_db_api, "record_order_print_logs", return_value=1),
        patch.object(
            workbench.bit_db_api,
            "bulk_update_orders",
            return_value={"matched": 1, "changed": 1},
        ) as bulk_update,
    ):
        response = client.post(
            "/api/orders/print",
            json={"order_ids": ["20001", "20002"]},
        )

    assert response.status_code == 200
    bulk_update.assert_called_once_with(
        ["20001"],
        workflow_status="待发",
        operator_id=1,
        operator_name="测试用户",
    )


def test_non_warehouse_cannot_manually_set_waiting_to_ship_status():
    with patch.object(workbench.bit_db_api, "bulk_update_orders") as bulk_update:
        response = _client().post(
            "/api/orders/bulk-update",
            json={"order_ids": ["20001"], "workflow_status": "待发"},
        )

    assert response.status_code == 403
    assert "仓库人员" in response.get_json()["message"]
    bulk_update.assert_not_called()


def test_order_operation_logs_route_returns_audit_rows():
    logs = [{
        "id": 1,
        "action_type": "purchase_updated",
        "action_label": "修改采购单",
        "operator_name": "测试用户",
        "changes": {
            "purchase_cost": {"label": "采购成本", "before": "80.00", "after": "88.50"}
        },
        "created_at": "2026-08-24 16:00:00",
    }]
    with patch.object(workbench.bit_db_api, "list_order_operation_logs", return_value=logs) as list_logs:
        response = _client().get("/api/orders/20001/logs")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["data"]["rows"][0]["action_label"] == "修改采购单"
    list_logs.assert_called_once_with("20001", limit=100)


# These route tests mock business data; they must not authenticate against production MySQL.
pytestmark = pytest.mark.usefixtures("isolated_legacy_console_user")
