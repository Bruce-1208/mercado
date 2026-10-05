import time

from bit.purchase_tracking_sync import (
    BrowserPurchasePlatform,
    PurchaseTrackingSyncManager,
    extract_tracking,
    supported_platforms,
)


def test_supported_purchase_platforms_include_requested_sites():
    assert {row["id"] for row in supported_platforms()} == {
        "1688", "taobao", "pdd", "xianyu",
    }


def test_extract_tracking_number_and_company_from_platform_text():
    result = extract_tracking("物流公司：顺丰速运  运单号：SF123456789CN 已揽收")

    assert result == {
        "tracking_number": "SF123456789CN",
        "logistics_company": "shunfeng",
    }


def test_found_1688_order_without_tracking_is_pending(monkeypatch):
    purchase_order = "5127480146121008319"
    adapter = BrowserPurchasePlatform("1688", "buyer", "")
    monkeypatch.setattr(
        adapter, "_search_order",
        lambda _order: f"订单号：{purchase_order} 等待卖家发货",
    )

    assert adapter.fetch_tracking(purchase_order) == {
        "status": "pending", "message": "未发货",
    }


def test_missing_1688_order_remains_not_found(monkeypatch):
    adapter = BrowserPurchasePlatform("1688", "buyer", "")
    monkeypatch.setattr(adapter, "_search_order", lambda _order: "没有符合条件的订单")

    assert adapter.fetch_tracking("5127480146121008319") == {
        "status": "not_found", "message": "平台未找到该采购订单",
    }


def test_sync_manager_updates_each_order_without_exposing_password():
    opened = []
    updated = []

    class FakeAdapter:
        def __init__(self, platform, account, password, state_callback):
            opened.append((platform, account, password))
            self.state_callback = state_callback

        def open(self):
            self.state_callback(phase="syncing", message="已登录")

        def fetch_tracking(self, purchase_order):
            if purchase_order == "PO-2":
                return {"status": "pending", "message": "尚未发货"}
            return {
                "status": "synced",
                "tracking_number": "SF123456789CN",
                "logistics_company": "shunfeng",
            }

        def close(self):
            pass

    manager = PurchaseTrackingSyncManager(adapter_factory=FakeAdapter)
    manager.start(
        platform="1688",
        account="buyer@example.com",
        password="secret-value",
        orders=[
            {"order_id": "20001", "purchase_order": "PO-1"},
            {"order_id": "20002", "purchase_order": "PO-2"},
        ],
        update_order=lambda *args: updated.append(args),
    )
    deadline = time.monotonic() + 2
    while manager.status()["running"] and time.monotonic() < deadline:
        time.sleep(0.01)

    state = manager.status()
    assert opened == [("1688", "buyer@example.com", "secret-value")]
    assert updated == [("20001", "SF123456789CN", "shunfeng")]
    assert state["phase"] == "completed"
    assert state["synced"] == 1
    assert state["pending"] == 1
    assert "secret-value" not in repr(state)


def test_sync_manager_rejects_orders_without_purchase_number():
    manager = PurchaseTrackingSyncManager()

    try:
        manager.start(
            platform="taobao",
            account="buyer",
            password="",
            orders=[{"order_id": "20001", "purchase_order": ""}],
            update_order=lambda *_args: None,
        )
    except ValueError as exc:
        assert "采购订单号" in str(exc)
    else:
        raise AssertionError("expected ValueError")



def test_plugin_sync_skip_is_persistent_and_user_scoped(tmp_path):
    from bit.purchase_tracking_sync import PluginPurchaseTrackingManager

    path = tmp_path / 'tracking.sqlite3'
    manager = PluginPurchaseTrackingManager(path)
    state = manager.create(7, platform='1688', orders=[
        {'order_id': '1', 'purchase_order': 'PO-1'},
        {'order_id': '2', 'purchase_order': 'PO-2'},
    ])
    request_id = state['request_id']
    manager.mark_waiting_login(7, request_id)
    manager.claim(7, request_id)
    manager.record_result(7, request_id, order_id='1', status='not_found')
    # Restart before the second result: recover the active request and its log.
    restored = PluginPurchaseTrackingManager(path)
    assert restored.status(7)['processed'] == 1
    restored.record_result(7, request_id, order_id='2', status='synced', tracking_number='SF12345678')
    final = restored.status(7)
    assert final['phase'] == 'completed'
    assert (final['synced'], final['skipped'], final['failed']) == (1, 1, 0)
    assert restored.history(8) == []
    assert len(restored.history(7)) == 1
    assert final['results'][0]['recorded_at']
    restored.create(7, platform='taobao', orders=[{'order_id': '1', 'purchase_order': 'PO-1'}])
    assert len(restored.history(7)) == 2


def test_plugin_sync_pending_is_persistent_without_skip_or_failure(tmp_path):
    from bit.purchase_tracking_sync import PluginPurchaseTrackingManager

    path = tmp_path / "tracking.sqlite3"
    manager = PluginPurchaseTrackingManager(path)
    state = manager.create(7, platform="1688", orders=[
        {"order_id": "1", "purchase_order": "5127480146121008319"},
    ])
    request_id = state["request_id"]
    manager.claim(7, request_id)
    manager.record_result(7, request_id, order_id="1", status="pending")

    restored = PluginPurchaseTrackingManager(path)
    final = restored.status(7)
    assert final["phase"] == "completed"
    assert (final["pending"], final["skipped"], final["failed"]) == (1, 0, 0)
    assert final["results"][0]["status"] == "pending"
    assert final["results"][0]["message"] == "未发货"
    assert final["results"][0]["tracking_number"] == ""


def test_plugin_direct_start_and_result_routes(monkeypatch, tmp_path):
    from erp.ai_weight_price import service

    # Import the workbench without initializing its production MySQL/API store.
    monkeypatch.setenv("BIT_RUNTIME_ROLE", "client")
    original_service = service.Service
    monkeypatch.setattr(
        service, "Service",
        lambda *args, **kwargs: original_service(
            tmp_path / "ai-weight-price", storage_backend="sqlite",
            migrate_legacy_state=False,
        ),
    )
    import bit.bit_interface as workbench
    from bit.purchase_tracking_sync import PluginPurchaseTrackingManager
    manager = PluginPurchaseTrackingManager(tmp_path / 'tracking.sqlite3')
    monkeypatch.setattr(workbench, 'plugin_purchase_tracking_manager', manager)
    read = []
    updated = []
    monkeypatch.setattr(workbench.bit_db_api, 'get_purchase_tracking_orders',
                        lambda ids, **kwargs: read.append((ids, kwargs)) or [
                            {'order_id': '1', 'purchase_order': 'PO-1'},
                            {'order_id': '2', 'purchase_order': 'PO-2'},
                        ])
    monkeypatch.setattr(workbench.bit_db_api, 'bulk_update_orders',
                        lambda ids, **kwargs: updated.append((ids, kwargs)) or {'matched': 1})
    workbench.app.config.update(TESTING=True, SECRET_KEY='tracking-test')
    user = {'id': 7, 'username': 'collector', 'access_version': 0}
    token = workbench.create_browser_extension_token(user)
    headers = {'Authorization': f'Bearer {token}'}
    client = workbench.app.test_client()
    response = client.post('/api/browser-extension/purchase-tracking/start', json={'platform': '1688', 'date_start': '2026-10-01', 'date_end': '2026-10-03'}, headers=headers)
    assert response.status_code == 202, response.json
    assert read == [([], {'all_orders': True, 'date_start': '2026-10-01', 'date_end': '2026-10-03', 'missing_tracking': True})]
    request_id = response.json['data']['request_id']
    for dates in ({}, {'date_start': '2026-10-04', 'date_end': '2026-10-03'}, {'date_start': 'bad', 'date_end': '2026-10-03'}):
        invalid = client.post('/api/browser-extension/purchase-tracking/start', json={'platform': '1688', **dates}, headers=headers)
        assert invalid.status_code == 400
    assert len(read) == 1
    assert client.post('/api/browser-extension/purchase-tracking/prepare', json={'request_id': request_id}, headers=headers).status_code == 200
    assert client.post('/api/browser-extension/purchase-tracking/claim', json={'request_id': request_id}, headers=headers).status_code == 200
    for order_id, status, tracking in [('1', 'not_found', ''), ('2', 'synced', 'SF12345678')]:
        response = client.post('/api/browser-extension/purchase-tracking/result', headers=headers,
                               json={'request_id': request_id, 'order_id': order_id, 'status': status, 'tracking_number': tracking})
        assert response.status_code == 200, response.json
    assert [entry[0] for entry in updated] == [['2']]
    assert updated[0][1]['purchase_tracking'] == 'SF12345678'
    with client.session_transaction() as login_session:
        login_session['workbench_user'] = user
    response = client.get('/api/orders/purchase-tracking/logs')
    assert response.status_code == 200
    assert response.json['data']['rows'][0]['skipped'] == 1


def test_tracking_extension_and_console_share_worker_owner():
    from bit.service_split import is_worker_path
    for path in ('/api/browser-extension/purchase-tracking/start',
                 '/api/browser-extension/purchase-tracking/result',
                 '/api/orders/purchase-tracking/logs'):
        assert is_worker_path(path)


def test_read_all_purchase_orders_has_no_selection_limit(monkeypatch):
    from bit import bit_mysql
    queries = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params): queries.append((sql, params))
        def fetchall(self): return [{'order_id': '42', 'purchase_order': ' PO-42 '}]

    class Connection:
        def cursor(self): return Cursor()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(bit_mysql.pymysql, 'connect', lambda **kwargs: Connection())
    monkeypatch.setattr(bit_mysql, '_ensure_mercado_synced_orders_table', lambda cursor: None)
    monkeypatch.setattr(bit_mysql, '_ensure_mercado_store_tokens_table', lambda cursor: None)
    rows = bit_mysql.get_mercado_purchase_tracking_orders([], all_orders=True)
    assert rows == [{'order_id': '42', 'purchase_order': 'PO-42'}]
    assert 'IN ()' not in queries[0][0]
    assert "COALESCE(synced.`purchase_order`, '') <> ''" in queries[0][0]
    assert 'LIMIT' not in queries[0][0]


def test_tracking_date_range_and_missing_tracking_query(monkeypatch):
    from datetime import datetime
    from bit import bit_mysql
    queries = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params): queries.append((sql, params))
        def fetchall(self): return []
    class Connection:
        def cursor(self): return Cursor()
        def commit(self): pass
        def close(self): pass
    monkeypatch.setattr(bit_mysql.pymysql, 'connect', lambda **kwargs: Connection())
    monkeypatch.setattr(bit_mysql, '_ensure_mercado_synced_orders_table', lambda cursor: None)
    monkeypatch.setattr(bit_mysql, '_ensure_mercado_store_tokens_table', lambda cursor: None)
    bit_mysql.get_mercado_purchase_tracking_orders([], all_orders=True, date_start='2026-10-01', date_end='2026-10-03', missing_tracking=True)
    sql, params = queries[0]
    assert "TRIM(COALESCE(synced.`purchase_tracking`, '')) = ''" in sql
    assert "TRIM(COALESCE(synced.`purchase_order`, '')) <> ''" in sql
    assert "synced.`date_created` >= %s" in sql
    assert "synced.`date_created` < %s" in sql
    assert params == [datetime(2026, 9, 30, 16), datetime(2026, 10, 3, 16)]
