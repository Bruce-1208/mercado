from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import bit.bit_interface as workbench


def test_startup_maintenance_runs_in_daemon_threads(monkeypatch):
    created_threads = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            self.started = False
            created_threads.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(workbench.threading, "Thread", FakeThread)
    monkeypatch.setattr(workbench.bit_db_api, "DB_MODE", "mysql")

    recovery_thread = workbench.start_interrupted_collection_recovery()
    scheduler_thread = workbench.start_store_link_scheduler_bootstrap()

    assert recovery_thread is created_threads[0]
    assert scheduler_thread is created_threads[1]
    assert [thread.name for thread in created_threads] == [
        "mercado-collection-startup-recovery",
        "mercado-store-link-scheduler-bootstrap",
    ]
    assert all(thread.daemon and thread.started for thread in created_threads)


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


def _reset_state():
    with workbench._mercado_collection_lock:
        workbench._mercado_collection_state.update(
            {
                "running": False,
                "task_id": None,
                "status": "idle",
                "message": "等待启动",
                "requested_count": 0,
                "processed_count": 0,
            }
        )
        workbench._mercado_collection_stop_event.clear()
        workbench._mercado_playwright_setup_state.update(
            running=False,
            status="idle",
            message="采集浏览器尚未打开",
        )


def _reset_publish_state():
    with workbench._mercado_publish_lock:
        workbench._mercado_publish_state.update(
            running=False,
            batch_id="",
            status="idle",
            message="等待选择产品上架",
            selection_mode="accounts",
            token_id=None,
            token_ids=[],
            group_names=[],
            site_id="MLM",
            site_ids=["MLM"],
            target_count=0,
            completed_target_count=0,
            skipped_target_count=0,
            quantity=500,
            worker_count=10,
            requested_count=0,
            processed_count=0,
            published_count=0,
            failed_count=0,
            moved_to_collection_count=0,
            skipped_published_count=0,
            elapsed_seconds=0,
            average_seconds_per_item=0,
            items_per_minute=0,
            estimated_remaining_seconds=0,
            results=[],
        )


def test_workbench_splits_collection_and_product_list_into_separate_modules():
    client = _client()
    response = client.get("/")

    assert response.status_code == 200
    assert b'data-ui-version="2026-09-01-mercado-filters-v2"' in response.data
    assert b'window.location.protocol === "file:"' in response.data
    assert b'window.location.replace("http://127.0.0.1:5000/")' in response.data
    assert b'data-tab="mercado-collection"' in response.data
    assert b'data-tab="mercado-products"' in response.data
    assert b'id="tab-mercado-collection"' in response.data
    assert b'id="tab-mercado-products"' in response.data
    assert b'id="mercado-list-collection-host"' in response.data
    assert b'id="mercado-list-products-host"' in response.data
    assert b'id="mercado-list-module"' in response.data
    assert b'id="mercado-view-collection"' not in response.data
    assert b'id="mercado-view-products"' not in response.data
    assert "商品采集".encode("utf-8") in response.data
    assert "产品资料库".encode("utf-8") in response.data
    assert "审核工作区".encode("utf-8") in response.data
    assert b'mountMercadoListModule("collection")' in response.data
    assert b'mountMercadoListModule("products")' in response.data
    assert b'data-tab="mercado-publish-records"' in response.data
    assert b'id="tab-mercado-publish-records"' in response.data
    assert b'id="publish-record-body"' in response.data
    assert b'id="publish-record-select-all"' in response.data
    assert b'id="publish-record-retry"' in response.data
    assert "重新上架所选".encode("utf-8") in response.data
    assert "失败原因 / 接口明细".encode("utf-8") in response.data
    assert b"mercadoPublishRecordPollTimer" not in response.data
    assert b'id="mercado-list-body"' in response.data
    assert b'class="tab-page mercado-workbench"' in response.data
    assert b'class="tab-heading mercado-workbench-heading"' in response.data
    assert b'class="market-flow-indicator"' in response.data
    assert "创建任务".encode("utf-8") in response.data
    assert "审核资料".encode("utf-8") in response.data
    assert b'class="market-task-dashboard"' in response.data
    assert b'class="market-collector-help"' in response.data
    assert b'id="mercado-add-selected"' in response.data
    assert b'id="mercado-collection-workers"' in response.data
    assert b'id="mercado-collection-success"' in response.data
    assert b'id="mercado-collection-failed"' in response.data
    assert "预计剩余".encode("utf-8") in response.data
    assert b'id="mercado-collection-worker-count"' in response.data
    assert b'id="mercado-collection-elapsed"' in response.data
    assert b'id="mercado-collection-workers" type="number" min="1" max="10"' in response.data
    assert b'id="mercado-collection-site"' in response.data
    assert b'id="mercado-collection-scope"' in response.data
    assert b'id="mercado-collection-front-link"' in response.data
    assert "跨境卖家专区".encode("utf-8") in response.data
    assert b'id="mercado-playwright-setup"' in response.data
    assert "不使用键鼠 RPA、截图或 OCR".encode("utf-8") in response.data
    assert "计泡重".encode("utf-8") in response.data
    assert "长×宽×高 ÷ 6000".encode("utf-8") in response.data
    assert "美元售价".encode("utf-8") in response.data
    assert "分类佣金".encode("utf-8") in response.data
    assert "最新运费".encode("utf-8") in response.data
    assert b'data-tab="mercado-shipping-standards"' in response.data
    assert b'id="tab-mercado-shipping-standards"' in response.data
    assert b'id="mercado-shipping-rate-content"' in response.data
    assert b'id="mercado-shipping-rate-refresh"' in response.data
    assert "美客多运费标准".encode("utf-8") in response.data
    assert "Global Selling 跨境运费公告".encode("utf-8") in response.data
    assert "更新官方最新标准".encode("utf-8") in response.data
    assert "不混用本地卖家信誉表".encode("utf-8") in response.data
    assert b"loadMercadoShippingRates" in response.data
    assert "净收益".encode("utf-8") in response.data
    assert "采集列表：只按实际重量".encode("utf-8") in response.data
    assert "Global Selling：毛重/体积重取较大".encode("utf-8") in response.data
    assert b'mercadoListMode === "collection"' in response.data
    assert b'id="mercado-delete-selected"' in response.data
    assert b'id="mercado-publish-store"' in response.data
    assert b'id="mercado-publish-site"' in response.data
    assert b'id="mercado-publish-mode"' in response.data
    assert b'id="mercado-publish-group"' in response.data
    assert b'id="mercado-publish-store" multiple' in response.data
    assert b'id="mercado-publish-site" multiple' in response.data
    assert "按分组".encode("utf-8") in response.data
    assert "账号、分组和站点均支持勾选多项".encode("utf-8") in response.data
    assert b'id="mercado-publish-workers"' in response.data
    assert b'id="mercado-publish-quantity" type="number" min="1" max="9999" value="500"' in response.data
    assert b'id="mercado-publish-workers" type="number" min="1" value="10"' in response.data
    assert b'market-multi-picker' in response.data
    assert b'class="market-selection-meta"' in response.data
    assert b'class="market-selection-tools"' in response.data
    assert b'class="market-publish-footer"' in response.data
    assert b'class="market-table-heading"' in response.data
    assert "批量发布设置".encode("utf-8") in response.data
    for site_name in ("墨西哥", "巴西", "阿根廷", "智利", "哥伦比亚", "乌拉圭"):
        assert site_name.encode("utf-8") in response.data
    assert b'id="mercado-publish-selected"' in response.data
    assert b'id="mercado-product-review-actions"' in response.data
    assert b'id="mercado-source-collected"' in response.data
    assert b'id="mercado-source-pulled"' in response.data
    assert b'id="mercado-source-zying"' in response.data
    assert b'class="market-source-filter market-product-only-filter"' in response.data
    assert b'class="market-source-option active" id="mercado-source-all"' in response.data
    assert b'class="market-source-tag-icon"' in response.data
    assert "美客多采集".encode("utf-8") in response.data
    assert "店铺同步".encode("utf-8") in response.data
    assert "智赢采集".encode("utf-8") in response.data
    assert b'id="mercado-list-pagination"' in response.data
    assert b'id="mercado-page-size"' in response.data
    assert b'<option value="500" selected>500' in response.data
    assert b'offset: String((mercadoListPage - 1) * mercadoListPageSize)' in response.data
    assert b'function goToMercadoListPage(page)' in response.data
    assert b'id="mercado-review-filter"' in response.data
    assert b'id="mercado-publish-filter"' in response.data
    assert b'id="mercado-collection-filter-note"' in response.data
    assert b'class="market-product-filters visible"' in response.data
    assert b'.market-list-panel.collection-mode .market-product-filters' not in response.data
    assert "采集列表支持重量、售价、收益和采集时间组合筛选".encode("utf-8") in response.data
    assert b'onclick="switchTab(\'mercado-products\')"' in response.data
    assert b'id="mercado-weight-min"' in response.data
    assert b'id="mercado-price-min"' in response.data
    assert b'id="mercado-net-min"' in response.data
    assert b'id="mercado-date-from"' in response.data
    assert b'id="mercado-review-bulk"' in response.data
    assert b'id="mercado-product-edit-dialog"' in response.data
    assert b'id="mercado-product-edit-description"' in response.data
    assert b'id="mercado-bulk-edit-selected"' in response.data
    assert b'id="mercado-bulk-edit-dialog"' in response.data
    assert b'id="mercado-bulk-edit-form"' in response.data
    assert b'data-bulk-field="weight_g"' in response.data
    assert b'data-bulk-field="dimensions"' in response.data
    assert "只覆盖已勾选的字段".encode("utf-8") in response.data
    assert "批量修改所选".encode("utf-8") in response.data
    assert b'/api/mercado-products/bulk-edit' in response.data
    assert "修改产品".encode("utf-8") in response.data
    assert "采集原价".encode("utf-8") in response.data
    assert b".market-product-table th:nth-child(5)" in response.data
    assert b"min-width: 520px" in response.data
    assert b"-webkit-line-clamp: unset" in response.data
    assert b"openMercadoProductEditor" in response.data
    for status_name in ("未审核", "通过", "疑似", "侵权", "风险"):
        assert status_name.encode("utf-8") in response.data
    assert "仅“通过”状态可上架".encode("utf-8") in response.data
    assert "批量上架".encode("utf-8") in response.data
    assert "不可上架，将自动移回采集列表".encode("utf-8") in response.data
    assert "最终上架净收益 = 产品净收益 ×".encode("utf-8") in response.data
    assert 'partial: "部分完成"'.encode("utf-8") in response.data


def test_official_shipping_rate_endpoints_list_and_start_refresh(monkeypatch):
    from erp import mercadolibre_shipping_rate_cards as cards

    client = _client()
    monkeypatch.setattr(
        cards.OfficialShippingRateCardStore,
        "list_rates",
        lambda self, site_id="": {
            "site_id": site_id,
            "rows": [{"site_id": "MLM", "shipping_amount_usd": 1.76}],
            "sites": [{"site_id": "MLM", "country_name": "墨西哥", "row_count": 1}],
        },
    )
    with workbench._mercado_shipping_rate_refresh_lock:
        workbench._mercado_shipping_rate_refresh_state.update(
            running=False, status="idle", message="等待从官方更新"
        )

    response = client.get("/api/mercado-shipping-rates?site_id=MLM")

    assert response.status_code == 200
    assert response.get_json()["data"]["site_id"] == "MLM"
    assert response.get_json()["data"]["rows"][0]["shipping_amount_usd"] == 1.76

    monkeypatch.setattr(
        workbench,
        "_start_mercado_shipping_rate_refresh",
        lambda **_kwargs: True,
    )
    response = client.post("/api/mercado-shipping-rates/refresh", json={})

    assert response.status_code == 202
    assert "已开始" in response.get_json()["message"]


def test_collection_api_requires_login():
    workbench.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    response = workbench.app.test_client().post(
        "/api/mercado-collection/start",
        json={
            "source_url": "https://listado.mercadolibre.com.mx/bolsas",
            "requested_count": 10,
        },
    )
    assert response.status_code == 401


def test_collection_finish_status_does_not_label_all_failures_completed():
    assert workbench._mercado_collection_finish_status(20, 0, 20) == (
        "error",
        "采集失败：入库 20 件，重量尺寸完整 0 件，待补充 20 件",
    )
    assert workbench._mercado_collection_finish_status(20, 18, 2) == (
        "partial",
        "采集部分完成：入库 20 件，重量尺寸完整 18 件，待补充 2 件",
    )
    assert workbench._mercado_collection_finish_status(20, 20, 0) == (
        "completed",
        "采集完成：入库 20 件，重量尺寸完整 20 件，待补充 0 件",
    )
    assert workbench._mercado_collection_finish_status(1, 1, 0, 100) == (
        "partial",
        "采集部分完成：入库 1 件，重量尺寸完整 1 件，待补充 0 件，距离目标还差 99 件",
    )


def test_collection_duration_is_live_then_stable_after_finish():
    now = datetime(2026, 8, 26, 12, 3, 5)
    assert workbench._mercado_collection_elapsed_seconds(
        {"started_at": "2026-08-26 12:00:00"}, now=now
    ) == 185
    assert workbench._mercado_collection_elapsed_seconds(
        {},
        {
            "started_at": "2026-08-26 12:00:00",
            "finished_at": "2026-08-26 12:02:07",
            "elapsed_seconds": 127,
        },
        now=now,
    ) == 127
    assert workbench._format_mercado_elapsed(3723) == "1小时2分3秒"


def test_collection_quality_pass_retries_all_incomplete_plugin_rows():
    rows = [
        {
            "source_item_id": "MLM1",
            "scrape_status": "partial",
            "error_message": "旧状态未刷新",
            "weight_g": 300,
            "package_length_cm": 10,
            "package_width_cm": 20,
            "package_height_cm": 5,
        },
        {
            "source_item_id": "MLM2",
            "scrape_status": "partial",
            "error_message": "智赢插件已显示，但 DOM 中没有完整的重量/尺寸",
        },
        {
            "source_item_id": "MLM3",
            "scrape_status": "partial",
            "error_message": "详情页未检测到智赢插件重量尺寸",
        },
    ]

    assert [
        row["source_item_id"]
        for row in workbench._mercado_collection_rows_needing_repair(rows)
    ] == ["MLM2", "MLM3"]

    template = Path(workbench.app.template_folder, "index.html").read_text(
        encoding="utf-8"
    )
    assert "row.weight_dimensions_complete || row.scrape_status" in template


def test_collection_database_write_retries_transient_network_failure(monkeypatch):
    calls = []

    def operation(value):
        calls.append(value)
        if len(calls) < 3:
            raise OSError("temporary database network failure")
        return "saved"

    monkeypatch.setattr(workbench.time, "sleep", lambda _seconds: None)

    assert workbench._mercado_collection_db_call(
        operation, 42, attempts=4
    ) == "saved"
    assert calls == [42, 42, 42]


def test_start_collection_creates_background_task():
    _reset_state()


def test_playwright_login_setup_starts_background_window():
    _reset_state()
    client = _client()
    with patch.object(workbench.threading.Thread, "start") as start_thread:
        response = client.post("/api/mercado-collection/playwright-setup", json={})

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["data"]["running"] is True
    start_thread.assert_called_once()
    _reset_state()
    client = _client()
    with patch.object(
        workbench, "db_create_mercado_collection_task", return_value=42
    ) as create_task, patch.object(workbench.threading.Thread, "start"):
        response = client.post(
            "/api/mercado-collection/start",
            json={
                "source_url": "https://listado.mercadolibre.com.mx/bolsas",
                "requested_count": 12,
                "worker_count": 10,
            },
        )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["data"]["task_id"] == 42
    assert payload["data"]["running"] is True
    assert payload["data"]["worker_count"] == 10
    create_task.assert_called_once_with(
        "https://listado.mercadolibre.com.mx/bolsas",
        12,
        "测试用户",
        worker_count=10,
    )
    _reset_state()


def test_collection_list_and_batch_add_endpoints():
    _reset_state()
    client = _client()
    rows = {
        "total": 1,
        "rows": [
            {
                "id": 7,
                "source_item_id": "MLM3016972321",
                "title": "Lonchera",
                "weight_g": 333,
            }
        ],
    }
    with patch.object(
        workbench, "db_list_mercado_collection_items", return_value=rows
    ) as list_collection:
        response = client.get(
            "/api/mercado-collection/items?search=Lonchera"
            "&weight_min=100&weight_max=500&price_min=20&price_max=80"
            "&net_proceeds_min=1&net_proceeds_max=40"
            "&date_from=2026-08-25&date_to=2026-08-30"
        )
    assert response.status_code == 200
    assert response.get_json()["data"]["rows"][0]["weight_g"] == 333
    list_collection.assert_called_once_with(
        search="Lonchera",
        limit=500,
        offset=0,
        task_id=None,
        weight_min="100",
        weight_max="500",
        price_min="20",
        price_max="80",
        net_proceeds_min="1",
        net_proceeds_max="40",
        date_from="2026-08-25",
        date_to="2026-08-30",
        exclude_added=True,
    )

    with patch.object(
        workbench,
        "db_add_mercado_collection_items_to_products",
        return_value={"count": 1, "mirrored": 1, "mirror_errors": []},
    ) as add_products:
        response = client.post(
            "/api/mercado-products/add", json={"collection_item_ids": [7]}
        )
    assert response.status_code == 200
    assert response.get_json()["data"]["count"] == 1
    add_products.assert_called_once_with([7])


def test_product_publish_record_list_endpoint_supports_filters():
    _reset_publish_state()
    client = _client()
    records = {
        "total": 1,
        "counts": {"all": 2, "published": 1, "failed": 1},
        "rows": [
            {
                "id": 81,
                "product_item_id": 9,
                "source_item_id": "MLM3016972321",
                "status": "failed",
                "failure_reason": "category rejected",
            }
        ],
    }
    with patch.object(
        workbench, "db_list_mercado_product_publish_records", return_value=records
    ) as list_records:
        response = client.get(
            "/api/mercado-publish-records"
            "?search=MLM301&status=failed&store_name=泽顺&site_id=MLB&limit=100"
        )

    assert response.status_code == 200
    assert response.get_json()["data"]["rows"][0]["failure_reason"] == "category rejected"
    assert response.get_json()["data"]["publish_running"] is False
    list_records.assert_called_once_with(
        search="MLM301",
        status="failed",
        store_name="泽顺",
        site_id="MLB",
        limit=100,
        offset=0,
    )


def test_retry_publish_records_starts_grouped_background_task():
    _reset_publish_state()
    client = _client()
    records = [
        {
            "id": 82,
            "product_item_id": 10,
            "token_id": 5,
            "store_name": "泽顺巴西",
            "site_id": "MLB",
            "site_name": "巴西",
            "quantity": 5,
            "status": "publishing",
        },
        {
            "id": 81,
            "product_item_id": 9,
            "token_id": 5,
            "store_name": "泽顺巴西",
            "site_id": "MLB",
            "site_name": "巴西",
            "quantity": 3,
            "status": "failed",
        },
    ]
    products = [
        {
            "id": 9,
            "source_item_id": "MLM111",
            "review_status": "approved",
            "weight_g": 350,
            "net_proceeds_usd": 8,
        },
        {
            "id": 10,
            "source_item_id": "MLM222",
            "review_status": "approved",
            "weight_g": 420,
            "net_proceeds_usd": 9,
        },
    ]
    tokens = {
        "rows": [
            {
                "id": 5,
                "display_name": "泽顺巴西",
                "site_id": "CBT",
                "site_settings": [{"site_id": "MLB", "discount_rate": 95}],
            }
        ]
    }
    with patch.object(
        workbench, "db_get_mercado_product_publish_records_by_ids", return_value=records
    ) as get_records, patch.object(
        workbench, "db_get_mercado_product_items_by_ids", return_value=products
    ) as get_products, patch.object(
        workbench.bit_db_api, "list_mercado_store_tokens", return_value=tokens
    ), patch.object(workbench.threading, "Thread") as thread_class:
        response = client.post(
            "/api/mercado-publish-records/retry",
            json={"record_ids": [81, 82]},
        )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["data"]["running"] is True
    assert payload["data"]["selection_mode"] == "retry"
    assert payload["data"]["requested_count"] == 2
    assert payload["data"]["target_count"] == 2
    get_records.assert_called_once_with([81, 82])
    get_products.assert_called_once_with([10, 9])
    targets = thread_class.call_args.kwargs["args"][1]
    assert {target["quantity"] for target in targets} == {3, 5}
    assert all(target["site_id"] == "MLB" for target in targets)
    thread_class.return_value.start.assert_called_once()
    _reset_publish_state()


def test_retry_publish_records_rejects_successful_record():
    _reset_publish_state()
    with patch.object(
        workbench,
        "db_get_mercado_product_publish_records_by_ids",
        return_value=[{
            "id": 81,
            "product_item_id": 9,
            "token_id": 5,
            "site_id": "MLB",
            "status": "published",
        }],
    ), patch.object(workbench.threading.Thread, "start") as start_thread:
        response = _client().post(
            "/api/mercado-publish-records/retry",
            json={"record_ids": [81]},
        )

    assert response.status_code == 400
    assert "只有上架暂停或上架失败" in response.get_json()["message"]
    start_thread.assert_not_called()


def test_start_collection_builds_country_url_from_keyword_and_scope():
    _reset_state()
    client = _client()
    with patch.object(
        workbench, "db_create_mercado_collection_task", return_value=43
    ) as create_task, patch.object(workbench.threading.Thread, "start"):
        response = client.post(
            "/api/mercado-collection/start",
            json={
                "keyword": "bolsa feminina",
                "site_id": "MLB",
                "collection_scope": "cross_border",
                "requested_count": 15,
                "worker_count": 4,
            },
        )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["data"]["source_url"] == (
        "https://lista.mercadolivre.com.br/"
        "bolsa-feminina_NoIndex_True_SHIPPING*ORIGIN_10215069"
    )
    assert payload["data"]["source_site_id"] == "MLB"
    assert payload["data"]["source_site_name"] == "巴西"
    assert payload["data"]["collection_scope"] == "cross_border"
    create_task.assert_called_once_with(
        "https://lista.mercadolivre.com.br/"
        "bolsa-feminina_NoIndex_True_SHIPPING*ORIGIN_10215069",
        15,
        "测试用户",
        worker_count=4,
    )
    _reset_state()


def test_product_list_filters_and_review_status_endpoint():
    client = _client()
    rows = {"total": 1, "rows": [{"id": 9, "review_status": "risk"}]}
    with patch.object(
        workbench, "db_list_mercado_product_items", return_value=rows
    ) as list_products:
        response = client.get(
            "/api/mercado-products?search=bag&source_type=pulled&review_status=risk"
            "&publish_status=failed&weight_min=100&weight_max=500"
            "&price_min=200&price_max=900&net_proceeds_min=-5&net_proceeds_max=40"
            "&date_from=2026-08-01&date_to=2026-08-25&limit=500&offset=500"
        )

    assert response.status_code == 200
    assert response.get_json()["data"] == rows
    list_products.assert_called_once_with(
        search="bag",
        limit=500,
        offset=500,
        source_type="pulled",
        review_status="risk",
        publish_status="failed",
        weight_min="100",
        weight_max="500",
        price_min="200",
        price_max="900",
        net_proceeds_min="-5",
        net_proceeds_max="40",
        date_from="2026-08-01",
        date_to="2026-08-25",
    )

    with patch.object(
        workbench,
        "db_update_mercado_product_review_status",
        return_value={"requested": 2, "changed": 2},
    ) as update_review:
        response = client.post(
            "/api/mercado-products/review-status",
            json={"product_item_ids": [9, 10], "review_status": "approved"},
        )

    assert response.status_code == 200
    assert response.get_json()["data"]["changed"] == 2
    update_review.assert_called_once_with([9, 10], "approved")


def test_product_content_update_endpoint():
    _reset_publish_state()
    client = _client()
    changes = {
        "title": "Título completo actualizado",
        "description_text": "Descripción actualizada",
        "main_image_url": "https://http2.mlstatic.com/new.jpg",
        "price": 1299.9,
        "weight_g": 420,
        "package_length_cm": 30,
        "package_width_cm": 20,
        "package_height_cm": 10,
        "unknown_field": "ignored",
    }
    with patch.object(
        workbench,
        "db_update_mercado_product_item",
        return_value={
            "product_item_id": 9,
            "changed": 1,
            "profitability_refresh_pending": True,
        },
    ) as update_product:
        response = client.patch("/api/mercado-products/9", json=changes)

    assert response.status_code == 200
    assert response.get_json()["data"]["profitability_refresh_pending"] is True
    expected = dict(changes)
    expected.pop("unknown_field")
    update_product.assert_called_once_with(9, expected)


def test_bulk_product_content_update_endpoint():
    _reset_publish_state()
    client = _client()
    changes = {
        "weight_g": 560,
        "category_id": "MLM999",
        "unknown_field": "ignored",
    }
    with patch.object(
        workbench,
        "db_update_mercado_product_items",
        return_value={
            "requested": 3,
            "changed": 3,
            "updated_fields": ["category_id", "weight_g"],
            "profitability_refresh_pending": True,
        },
    ) as update_products:
        response = client.patch(
            "/api/mercado-products/bulk-edit",
            json={"product_item_ids": [7, 9, 12], "changes": changes},
        )

    assert response.status_code == 200
    assert response.get_json()["data"]["requested"] == 3
    update_products.assert_called_once_with(
        [7, 9, 12], {"weight_g": 560, "category_id": "MLM999"}
    )

    response = client.patch(
        "/api/mercado-products/bulk-edit",
        json={"product_item_ids": [7], "changes": []},
    )
    assert response.status_code == 422


def test_collection_and_product_delete_endpoints():
    _reset_publish_state()
    client = _client()
    with patch.object(
        workbench,
        "db_delete_mercado_collection_items",
        return_value={"requested": 2, "deleted": 2},
    ) as delete_collection:
        response = client.delete(
            "/api/mercado-collection/items",
            json={"collection_item_ids": [3, 4]},
        )
    assert response.status_code == 200
    assert response.get_json()["data"]["deleted"] == 2
    delete_collection.assert_called_once_with([3, 4])

    with patch.object(
        workbench,
        "db_delete_mercado_product_items",
        return_value={"requested": 1, "deleted": 1},
    ) as delete_products:
        response = client.delete(
            "/api/mercado-products",
            json={"product_item_ids": [9]},
        )
    assert response.status_code == 200
    delete_products.assert_called_once_with([9])


def test_batch_publish_endpoint_starts_background_task_for_selected_store():
    _reset_publish_state()
    client = _client()
    rows = [{"id": 9, "source_item_id": "MLM3016972321", "source_url": "source", "review_status": "approved", "weight_g": 350, "net_proceeds_usd": 8}]
    tokens = {
        "total": 1,
        "rows": [{"id": 5, "display_name": "泽顺墨西哥", "nickname": "SHOP", "site_settings": [{"site_id": "MLB", "discount_rate": 95}]}],
    }
    with patch.object(
        workbench, "db_get_mercado_product_items_by_ids", return_value=rows
    ) as get_products, patch.object(
        workbench.bit_db_api, "list_mercado_store_tokens", return_value=tokens
    ), patch.object(workbench.threading.Thread, "start") as start_thread:
        response = client.post(
            "/api/mercado-products/publish",
            json={
                "product_item_ids": [9],
                "token_id": 5,
                "site_id": "MLB",
                "quantity": 3,
                "worker_count": 6,
            },
        )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["data"]["running"] is True
    assert payload["data"]["store_name"] == "泽顺墨西哥"
    assert payload["data"]["site_id"] == "MLB"
    assert payload["data"]["site_name"] == "巴西"
    assert payload["data"]["quantity"] == 3
    assert payload["data"]["worker_count"] == 1
    assert payload["data"]["discount_rate"] == 95
    get_products.assert_called_once_with([9])
    start_thread.assert_called_once()
    _reset_publish_state()


def test_batch_publish_endpoint_builds_all_compatible_account_site_targets():
    _reset_publish_state()
    rows = [{
        "id": 9,
        "source_item_id": "MLM3016972321",
        "source_url": "source",
        "review_status": "approved",
        "weight_g": 350,
        "net_proceeds_usd": 8,
    }]
    tokens = {
        "total": 2,
        "rows": [
            {
                "id": 5,
                "display_name": "跨境店",
                "site_id": "CBT",
                "site_settings": [
                    {"site_id": "MLM", "discount_rate": 90},
                    {"site_id": "MLB", "discount_rate": 95},
                ],
            },
            {
                "id": 6,
                "display_name": "墨西哥本土店",
                "site_id": "MLM",
                "site_settings": [{"site_id": "MLM", "discount_rate": 88}],
            },
        ],
    }
    with patch.object(
        workbench, "db_get_mercado_product_items_by_ids", return_value=rows
    ), patch.object(
        workbench.bit_db_api, "list_mercado_store_tokens", return_value=tokens
    ), patch.object(workbench.threading, "Thread") as thread_class:
        response = _client().post(
            "/api/mercado-products/publish",
            json={
                "product_item_ids": [9],
                "selection_mode": "accounts",
                "token_ids": [5, 6],
                "site_ids": ["MLM", "MLB"],
                "quantity": 2,
                "worker_count": 60,
            },
        )

    payload = response.get_json()["data"]
    assert response.status_code == 200
    assert payload["running"] is True
    assert payload["token_ids"] == [5, 6]
    assert payload["site_ids"] == ["MLM", "MLB"]
    assert payload["target_count"] == 3
    assert payload["skipped_target_count"] == 1
    assert payload["requested_count"] == 3
    assert payload["store_name"] == "2 个账号"
    assert payload["site_name"] == "2 个站点"
    targets = thread_class.call_args.kwargs["args"][1]
    assert thread_class.call_args.kwargs["args"][3] == 60
    assert [(target["token_id"], target["site_id"]) for target in targets] == [
        (5, "MLM"),
        (5, "MLB"),
        (6, "MLM"),
    ]
    assert [target["discount_rate"] for target in targets] == [90, 95, 88]
    thread_class.return_value.start.assert_called_once()
    _reset_publish_state()


def test_batch_publish_endpoint_can_select_targets_by_site_group():
    _reset_publish_state()
    rows = [{
        "id": 9,
        "source_item_id": "MLM3016972321",
        "source_url": "source",
        "review_status": "approved",
        "weight_g": 350,
        "net_proceeds_usd": 8,
    }]
    tokens = {
        "total": 3,
        "rows": [
            {
                "id": 5,
                "display_name": "跨境店",
                "site_id": "CBT",
                "site_settings": [
                    {"site_id": "MLM", "group_name": "精品组", "discount_rate": 90},
                    {"site_id": "MLB", "group_name": "普通组", "discount_rate": 95},
                ],
            },
            {
                "id": 6,
                "display_name": "墨西哥店",
                "site_id": "MLM",
                "site_settings": [
                    {"site_id": "MLM", "group_name": "精品组", "discount_rate": 88},
                ],
            },
            {
                "id": 7,
                "display_name": "巴西店",
                "site_id": "MLB",
                "site_settings": [
                    {"site_id": "MLB", "group_name": "精品组", "discount_rate": 92},
                ],
            },
        ],
    }
    with patch.object(
        workbench, "db_get_mercado_product_items_by_ids", return_value=rows
    ), patch.object(
        workbench.bit_db_api, "list_mercado_store_tokens", return_value=tokens
    ), patch.object(workbench.threading, "Thread") as thread_class:
        response = _client().post(
            "/api/mercado-products/publish",
            json={
                "product_item_ids": [9],
                "selection_mode": "groups",
                "group_names": ["精品组"],
                "site_ids": ["MLM", "MLB"],
            },
        )

    payload = response.get_json()["data"]
    assert response.status_code == 200
    assert payload["selection_mode"] == "groups"
    assert payload["group_names"] == ["精品组"]
    assert payload["token_ids"] == [5, 6, 7]
    assert payload["target_count"] == 3
    assert payload["requested_count"] == 3
    assert payload["quantity"] == 500
    targets = thread_class.call_args.kwargs["args"][1]
    assert thread_class.call_args.kwargs["args"][2] == 500
    assert thread_class.call_args.kwargs["args"][3] == 10
    assert [(target["token_id"], target["site_id"]) for target in targets] == [
        (5, "MLM"),
        (6, "MLM"),
        (7, "MLB"),
    ]
    assert all(target["group_name"] == "精品组" for target in targets)
    _reset_publish_state()


def test_multi_target_publish_runner_aggregates_progress_and_results():
    _reset_publish_state()
    targets = [
        {
            "token_id": 5,
            "store_name": "跨境店",
            "site_id": "MLM",
            "site_name": "墨西哥",
            "discount_rate": 90,
        },
        {
            "token_id": 5,
            "store_name": "跨境店",
            "site_id": "MLB",
            "site_name": "巴西",
            "discount_rate": 95,
        },
    ]
    results = [
        {
            "requested_count": 1,
            "published_count": 1,
            "failed_count": 0,
            "results": [{"product_id": 9, "status": "published"}],
        },
        {
            "requested_count": 1,
            "published_count": 0,
            "failed_count": 1,
            "results": [{"product_id": 9, "status": "failed"}],
        },
    ]
    with patch.object(
        workbench, "db_get_published_mercado_product_item_ids", return_value=[]
    ), patch.object(
        workbench.time, "monotonic", side_effect=[100, 104, 110, 110]
    ), patch(
        "erp.mercadolibre_batch_publish.publish_product_batch",
        side_effect=results,
    ) as publish_batch:
        workbench._run_mercado_product_publish_targets(
            [{"id": 9, "source_item_id": "MLM3016972321"}],
            targets,
            quantity=1,
            worker_count=4,
            batch_id="batch-main",
            created_by="测试用户",
        )

    state = dict(workbench._mercado_publish_state)
    assert publish_batch.call_count == 2
    assert state["running"] is False
    assert state["status"] == "partial"
    assert state["requested_count"] == 2
    assert state["processed_count"] == 2
    assert state["published_count"] == 1
    assert state["failed_count"] == 1
    assert state["completed_target_count"] == 2
    assert state["elapsed_seconds"] == 10
    assert state["average_seconds_per_item"] == 5
    assert state["items_per_minute"] == 12
    assert state["estimated_remaining_seconds"] == 0
    assert [row["site_id"] for row in state["results"]] == ["MLM", "MLB"]
    _reset_publish_state()


def test_multi_target_publish_runner_uses_retry_rows_and_original_quantities():
    _reset_publish_state()
    targets = [
        {
            "token_id": 5,
            "store_name": "跨境店",
            "site_id": "MLB",
            "site_name": "巴西",
            "discount_rate": 95,
            "quantity": 3,
            "product_rows": [{"id": 9, "source_item_id": "MLM9"}],
        },
        {
            "token_id": 5,
            "store_name": "跨境店",
            "site_id": "MLB",
            "site_name": "巴西",
            "discount_rate": 95,
            "quantity": 5,
            "product_rows": [{"id": 10, "source_item_id": "MLM10"}],
        },
    ]
    with patch.object(
        workbench, "db_get_published_mercado_product_item_ids", return_value=[]
    ), patch(
        "erp.mercadolibre_batch_publish.publish_product_batch",
        side_effect=[
            {"requested_count": 1, "published_count": 1, "failed_count": 0},
            {"requested_count": 1, "published_count": 1, "failed_count": 0},
        ],
    ) as publish_batch:
        workbench._run_mercado_product_publish_targets(
            [], targets, quantity=1, worker_count=4,
            batch_id="retry-batch", created_by="测试用户",
        )

    assert publish_batch.call_count == 2
    assert [call.args[0][0]["id"] for call in publish_batch.call_args_list] == [9, 10]
    assert [call.kwargs["quantity"] for call in publish_batch.call_args_list] == [3, 5]
    assert workbench._mercado_publish_state["requested_count"] == 2
    assert workbench._mercado_publish_state["published_count"] == 2
    _reset_publish_state()


def test_multi_target_publish_runner_skips_historical_success_for_same_target():
    _reset_publish_state()
    target = [{
        "token_id": 5,
        "store_name": "跨境店",
        "site_id": "MLM",
        "site_name": "墨西哥",
        "discount_rate": 90,
    }]
    with patch.object(
        workbench, "db_get_published_mercado_product_item_ids", return_value=[9]
    ), patch(
        "erp.mercadolibre_batch_publish.publish_product_batch"
    ) as publish_batch:
        workbench._run_mercado_product_publish_targets(
            [{"id": 9, "source_item_id": "MLM3016972321"}],
            target,
            quantity=1,
            worker_count=10,
            batch_id="batch-skip",
            created_by="测试用户",
        )

    state = dict(workbench._mercado_publish_state)
    publish_batch.assert_not_called()
    assert state["status"] == "completed"
    assert state["requested_count"] == 1
    assert state["processed_count"] == 1
    assert state["skipped_published_count"] == 1
    assert state["failed_count"] == 0
    assert state["average_seconds_per_item"] == 0
    assert state["items_per_minute"] == 0
    _reset_publish_state()


def test_batch_publish_endpoint_rejects_unsupported_site():
    _reset_publish_state()
    response = _client().post(
        "/api/mercado-products/publish",
        json={"product_item_ids": [9], "token_id": 5, "site_id": "MPE"},
    )

    assert response.status_code == 400
    assert "不支持的目标站点" in response.get_json()["message"]


def test_batch_publish_endpoint_moves_unapproved_product_to_collection():
    _reset_publish_state()
    rows = [
        {
            "id": 9,
            "source_item_id": "MLM3016972321",
            "source_url": "source",
            "review_status": "unreviewed",
            "weight_g": 350,
            "net_proceeds_usd": 8,
        }
    ]
    with patch.object(
        workbench, "db_get_mercado_product_items_by_ids", return_value=rows
    ), patch.object(
        workbench,
        "db_move_mercado_product_items_to_collection",
        return_value={"requested": 1, "moved": 1, "deleted": 1},
    ) as move_products, patch.object(
        workbench.threading.Thread, "start"
    ) as start_thread:
        response = _client().post(
            "/api/mercado-products/publish",
            json={"product_item_ids": [9], "token_id": 5, "site_id": "MLM"},
        )

    assert response.status_code == 200
    assert response.get_json()["data"]["moved_to_collection_count"] == 1
    move_products.assert_called_once_with([9], reason="审核状态未通过 1 件")
    start_thread.assert_not_called()
    _reset_publish_state()


def test_batch_publish_endpoint_moves_missing_weight_back_to_collection():
    _reset_publish_state()
    rows = [{
        "id": 9,
        "source_item_id": "MLM3016972321",
        "review_status": "approved",
        "weight_g": None,
        "net_proceeds_usd": 8,
    }]
    with (
        patch.object(
            workbench, "db_get_mercado_product_items_by_ids", return_value=rows
        ),
        patch.object(
            workbench,
            "db_move_mercado_product_items_to_collection",
            return_value={"requested": 1, "moved": 1, "deleted": 1},
        ) as move_products,
        patch.object(workbench.threading.Thread, "start") as start_thread,
    ):
        response = _client().post(
            "/api/mercado-products/publish",
            json={"product_item_ids": [9], "token_id": 5, "site_id": "MLM"},
        )

    assert response.status_code == 200
    assert response.get_json()["data"]["moved_to_collection_count"] == 1
    assert "移回采集列表" in response.get_json()["data"]["message"]
    move_products.assert_called_once_with(
        [9], reason="未填写有效重量 1 件"
    )
    start_thread.assert_not_called()
    _reset_publish_state()


def test_batch_publish_endpoint_ignores_missing_weight_and_starts_valid_rows():
    _reset_publish_state()
    rows = [
        {
            "id": 9,
            "source_item_id": "MLM3016972321",
            "review_status": "approved",
            "weight_g": None,
            "net_proceeds_usd": None,
        },
        {
            "id": 11,
            "source_item_id": "MLM3016972323",
            "review_status": "unreviewed",
            "weight_g": 350,
            "net_proceeds_usd": 8,
        },
        {
            "id": 12,
            "source_item_id": "MLM3016972324",
            "review_status": "approved",
            "weight_g": 350,
            "net_proceeds_usd": 0,
        },
        {
            "id": 10,
            "source_item_id": "MLM3016972322",
            "source_url": "source",
            "review_status": "approved",
            "weight_g": 350,
            "net_proceeds_usd": 8,
        },
    ]
    tokens = {
        "total": 1,
        "rows": [{
            "id": 5,
            "display_name": "泽顺墨西哥",
            "nickname": "SHOP",
            "site_settings": [{"site_id": "MLM", "discount_rate": 95}],
        }],
    }
    with patch.object(
        workbench, "db_get_mercado_product_items_by_ids", return_value=rows
    ), patch.object(workbench.threading.Thread, "start") as start_thread:
        with patch.object(
            workbench,
            "db_move_mercado_product_items_to_collection",
            return_value={"requested": 3, "moved": 3, "deleted": 3},
        ) as move_products, patch.object(
            workbench.bit_db_api, "list_mercado_store_tokens", return_value=tokens
        ):
            response = _client().post(
                "/api/mercado-products/publish",
                json={
                    "product_item_ids": [9, 10, 11, 12],
                    "token_id": 5,
                    "site_id": "MLM",
                },
            )

    payload = response.get_json()["data"]
    assert response.status_code == 200
    assert payload["running"] is True
    assert payload["requested_count"] == 1
    assert payload["moved_to_collection_count"] == 3
    move_products.assert_called_once_with(
        [9, 11, 12],
        reason=(
            "未填写有效重量 1 件；净收益尚未计算 1 件；"
            "审核状态未通过 1 件；净收益小于等于 0 1 件"
        ),
    )
    start_thread.assert_called_once()
    _reset_publish_state()


def test_batch_publish_endpoint_moves_nonpositive_net_to_collection():
    _reset_publish_state()
    rows = [{
        "id": 9,
        "source_item_id": "MLM3016972321",
        "review_status": "approved",
        "weight_g": 350,
        "net_proceeds_usd": -0.01,
    }]
    with patch.object(
        workbench, "db_get_mercado_product_items_by_ids", return_value=rows
    ), patch.object(
        workbench,
        "db_move_mercado_product_items_to_collection",
        return_value={"requested": 1, "moved": 1, "deleted": 1},
    ) as move_products, patch.object(
        workbench.threading.Thread, "start"
    ) as start_thread:
        response = _client().post(
            "/api/mercado-products/publish",
            json={"product_item_ids": [9], "token_id": 5, "site_id": "MLM"},
        )

    assert response.status_code == 200
    assert response.get_json()["data"]["moved_to_collection_count"] == 1
    move_products.assert_called_once_with([9], reason="净收益小于等于 0 1 件")
    start_thread.assert_not_called()
    _reset_publish_state()


def test_batch_publish_endpoint_rejects_nonpositive_worker_count():
    _reset_publish_state()
    response = _client().post(
        "/api/mercado-products/publish",
        json={
            "product_item_ids": [9],
            "token_id": 5,
            "site_id": "MLM",
            "worker_count": 0,
        },
    )

    assert response.status_code == 400
    assert "大于 0" in response.get_json()["message"]


def test_batch_publish_endpoint_rejects_cross_site_for_local_store():
    _reset_publish_state()
    rows = [{"id": 9, "source_item_id": "MLM3016972321", "source_url": "source", "review_status": "approved", "weight_g": 350, "net_proceeds_usd": 8}]
    tokens = {
        "total": 1,
        "rows": [{"id": 5, "display_name": "本地墨西哥店", "site_id": "MLM"}],
    }
    with patch.object(
        workbench, "db_get_mercado_product_items_by_ids", return_value=rows
    ), patch.object(
        workbench.bit_db_api, "list_mercado_store_tokens", return_value=tokens
    ):
        response = _client().post(
            "/api/mercado-products/publish",
            json={"product_item_ids": [9], "token_id": 5, "site_id": "MLB"},
        )

    assert response.status_code == 400
    assert "Global Selling" in response.get_json()["message"]
