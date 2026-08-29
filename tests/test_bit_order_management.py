from unittest.mock import patch

import bit.bit_interface as workbench
from bit import bit_db_api


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
    assert b'id="order-sync-dialog"' in response.data
    assert b'id="order-sync-start-date" type="datetime-local"' in response.data
    assert b'id="order-sync-end-date" type="datetime-local"' in response.data
    assert "自定义拉取时间段（北京时间）".encode("utf-8") in response.data
    assert "近 30 天".encode("utf-8") in response.data
    assert b"orderSyncStartDate.disabled = manualSyncRunning" in response.data
    assert b"start.setDate(start.getDate() - 6)" not in response.data
    assert b'data-origin="token"' in response.data
    assert "商品名、采购备注和订单备注均支持模糊查询".encode("utf-8") in response.data
    assert "下单时间（北京时间）".encode("utf-8") in response.data
    assert "预计利润 / 利润率".encode("utf-8") in response.data
    assert "手续费".encode("utf-8") in response.data
    assert "运费".encode("utf-8") in response.data
    assert "结余".encode("utf-8") in response.data
    assert "每 15 分钟重新拉取".encode("utf-8") in response.data
    assert "最近 72 小时".encode("utf-8") in response.data
    assert "每天北京时间凌晨".encode("utf-8") in response.data
    assert "Token 自动拉取".encode("utf-8") in response.data
    assert "智赢导入".encode("utf-8") not in response.data
    assert b'id="order-select-all"' in response.data
    assert b'id="order-bulk-status"' in response.data
    assert b'id="order-bulk-purchase-button"' in response.data
    assert b'id="order-bulk-print-button"' in response.data
    assert "添加采购单".encode("utf-8") in response.data
    assert b'id="order-purchase-dialog"' in response.data
    assert b'id="order-purchase-tracking"' in response.data
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
    download_labels.assert_called_once_with(["20001"])
    record_logs.assert_called_once_with(
        ["20001"], operator_id=1, operator_name="测试用户"
    )


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
