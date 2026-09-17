from datetime import datetime
import asyncio
from pathlib import Path
from unittest.mock import patch

import bit.bit_interface as workbench


def _actual_shipping(weight):
    return {
        "weight_basis": "plugin_actual",
        "billable_weight_g": weight,
        "shipping_weight_rule": "free_shipping:actual_weight_only:rate_card",
        "profitability_updated_at": "2026-09-12 12:00:00",
        "profitability_source": "mercadolibre_official_api",
    }


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


def test_collection_login_edge_starts_at_mercado_home_only(monkeypatch):
    from erp import mercadolibre_playwright_collector as collector

    launches = []
    opened = []
    monkeypatch.setattr(
        workbench.bit_zying_caiji,
        "ensure_visible_zying_edge_login_window",
        lambda debugger, **kwargs: launches.append((debugger, kwargs)),
    )
    monkeypatch.setattr(
        collector,
        "open_playwright_login_setup",
        lambda **kwargs: opened.append(kwargs),
    )

    workbench._run_mercado_playwright_setup("edge", "", "Edge")

    assert launches == [(
        collector.DEFAULT_CDP_URL,
        {"start_url": "https://www.mercadolibre.com/"},
    )]
    assert opened == [{"window_id": ""}]


def test_profitability_worker_continues_when_reference_refresh_fails(monkeypatch):
    from erp import ecb_exchange_rates, mercadolibre_collection_store as store
    from erp import mercadolibre_profitability as profitability
    from erp import mercadolibre_shipping_rate_cards as cards

    class StopAfterBatch:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            self.stopped = True

    class Client:
        def __init__(self, token):
            self.token = token

        def estimate(self, row):
            return {"shipping_fee_usd": 6.4, "commission_amount_usd": 3.5}

    def unavailable(*args, **kwargs):
        raise RuntimeError("reference endpoint unavailable")

    saved = []
    monkeypatch.setattr(workbench, "_mercado_profit_refresh_stop_event", StopAfterBatch())
    monkeypatch.setattr(profitability, "MercadoProfitabilityClient", Client)
    monkeypatch.setattr(profitability, "active_store_token", lambda: {"access_token": "test"})
    monkeypatch.setattr(profitability, "refresh_supported_exchange_rates", unavailable)
    monkeypatch.setattr(ecb_exchange_rates, "refresh_usd_cny_daily_rates", unavailable)
    monkeypatch.setattr(cards.OfficialShippingRateCardStore, "needs_refresh", lambda *a, **k: False)
    monkeypatch.setattr(store, "list_stale_profitability_items", lambda **kwargs: [
        {
            "id": 1,
            "source_type": "pulled",
            "source_item_id": "MLM1",
            "updated_at": "2026-09-12 12:00:00",
            "price": 299,
            "weight_g": 420,
        },
    ])
    monkeypatch.setattr(store, "update_item_profitability", lambda item_id, row: saved.append(row))

    workbench._mercado_profit_refresh_loop()

    assert len(saved) == 1
    assert saved[0]["source_type"] == "pulled"
    assert saved[0]["shipping_fee_usd"] == 6.4
    assert saved[0]["_expected_profitability_inputs"]["updated_at"] == (
        "2026-09-12 12:00:00"
    )
    assert saved[0]["_expected_profitability_inputs"]["weight_g"] == 420


def test_collection_matches_fixed_shipping_before_write_and_wakes_profit_worker(
    monkeypatch,
):
    from erp import mercadolibre_batch_collector as collector
    from erp import mercadolibre_shipping_rate_cards as cards

    saved_batches = []
    task_updates = []
    wakeup = workbench.threading.Event()
    monkeypatch.setattr(workbench, "_mercado_profit_refresh_wakeup_event", wakeup)
    monkeypatch.setattr(
        cards.OfficialShippingRateCardStore,
        "list_rates",
        lambda self: {
            "rows": [{
                "site_id": "MLM",
                "rate_kind": "above_threshold",
                "weight_min_g": 500,
                "weight_max_g": 600,
                "shipping_amount_usd": 7.16,
            }]
        },
    )
    monkeypatch.setattr(
        workbench,
        "db_upsert_mercado_collection_items",
        lambda task_id, rows: saved_batches.append((task_id, list(rows))),
    )
    monkeypatch.setattr(
        workbench,
        "db_update_mercado_collection_task",
        lambda task_id, **changes: task_updates.append((task_id, changes)),
    )
    monkeypatch.setattr(workbench, "_mercado_collection_state_update", lambda **_changes: None)

    def collect(_source_url, _requested_count, **kwargs):
        row = {
            "source_item_id": "MLM3016972321",
            "source_url": "https://articulo.mercadolibre.com.mx/MLM-3016972321",
            "title": "Producto",
            "main_image_url": "https://example.test/product.webp",
            "price": 350,
            "currency_id": "MXN",
            "weight_g": 600,
            "volumetric_weight_kg": 9,
            "weight_basis": "plugin_actual",
            "scrape_status": "ok",
        }
        kwargs["on_item"](row)
        return {
            "candidate_count": 1,
            "skipped_us_count": 0,
            "rows": [row],
        }

    monkeypatch.setattr(collector, "collect_marketplace_listing", collect)
    workbench._mercado_collection_stop_event.clear()

    workbench._run_mercado_collection_task(
        77,
        "https://listado.mercadolibre.com.mx/cosplay",
        1,
        4,
        "cross_border",
    )

    assert len(saved_batches) == 1
    saved = saved_batches[0][1][0]
    assert saved["shipping_fee_usd"] == 7.16
    assert saved["billable_weight_g"] == 600
    assert saved["volumetric_weight_kg"] == 9
    assert "actual_weight_only" in saved["shipping_weight_rule"]
    assert wakeup.is_set()
    assert task_updates[-1][1]["status"] == "completed"


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
            group_publish_mode="",
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
            skipped_other_account_count=0,
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
    assert b'id="publish-record-start-date"' in response.data
    assert b'id="publish-record-end-date"' in response.data
    assert b'id="publish-record-group"' in response.data
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
    assert b'id="mercado-management-category-filter"' in response.data
    assert b'id="mercado-platform-category-filter"' in response.data
    assert b'id="mercado-management-category-bulk"' in response.data
    assert b'id="mercado-category-dialog"' in response.data
    assert "运营分类管理".encode("utf-8") in response.data
    assert "全部产品分类".encode("utf-8") in response.data
    assert "美客多分类".encode("utf-8") in response.data
    assert b'id="mercado-collection-workers"' in response.data
    assert b'id="mercado-collection-success"' in response.data
    assert b'id="mercado-collection-failed"' in response.data
    assert "预计剩余".encode("utf-8") in response.data
    assert b'id="mercado-collection-worker-count"' in response.data
    assert b'id="mercado-collection-elapsed"' in response.data
    assert b'id="mercado-collection-workers" type="number" min="1" max="10"' in response.data
    assert b'id="mercado-collection-site"' in response.data
    assert b'id="mercado-collection-scope"' in response.data
    assert b'id="mercado-collection-browser-type"' in response.data
    assert b'id="mercado-collection-window"' in response.data
    assert "本地 Edge（9222）".encode("utf-8") in response.data
    assert "采集专用（墨西哥）".encode("utf-8") in response.data
    assert b'/api/mercado-collection/browser-windows' in response.data
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
    assert "只按智赢实际重量，不使用计泡重".encode("utf-8") in response.data
    assert b'mercadoListMode === "collection"' in response.data
    assert b'id="mercado-delete-selected"' in response.data
    assert b'id="mercado-publish-store"' in response.data
    assert b'id="mercado-publish-site"' in response.data
    assert b'id="mercado-publish-mode"' in response.data
    assert b'id="mercado-publish-group"' in response.data
    assert b'id="mercado-group-publish-mode"' in response.data
    assert b'value="random" selected' in response.data
    assert b'value="polling"' in response.data
    assert b'id="mercado-publish-store" multiple' in response.data
    assert b'id="mercado-publish-site" multiple' in response.data
    assert "按分组".encode("utf-8") in response.data
    assert "账号、分组和站点均支持勾选多项".encode("utf-8") in response.data
    assert b'id="mercado-publish-workers"' in response.data
    assert b'id="mercado-publish-quantity" type="number" min="1" max="9999" value="500"' in response.data
    assert b'id="mercado-publish-workers" type="number" min="1" value="16"' in response.data
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
    assert b'id="mercado-collection-filter-note"' not in response.data
    assert b'class="market-product-filters visible"' in response.data
    assert b'.market-list-panel.collection-mode .market-product-filters' not in response.data
    assert "采集列表支持实重可用、未审核、未上架及重量、售价、收益和采集时间组合筛选".encode("utf-8") in response.data
    review_filter_markup = response.data.split(b'id="mercado-review-filter"', 1)[0].rsplit(b'<div', 1)[-1]
    publish_filter_markup = response.data.split(b'id="mercado-publish-filter"', 1)[0].rsplit(b'<div', 1)[-1]
    assert b'market-product-only-filter' not in review_filter_markup
    assert b'market-product-only-filter' not in publish_filter_markup
    assert b'id="mercado-weight-min"' in response.data
    assert b'id="mercado-weight-status-filter"' in response.data
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
    assert b'/api/mercado-collection/bulk-edit' in response.data
    assert "修改产品".encode("utf-8") in response.data
    assert "采集原价".encode("utf-8") in response.data
    assert b".market-product-table th:nth-child(7)" in response.data
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
        "采集失败：入库 20 件，实际重量可用 0 件，待补充 20 件",
    )
    assert workbench._mercado_collection_finish_status(20, 18, 2) == (
        "partial",
        "采集部分完成：入库 20 件，实际重量可用 18 件，待补充 2 件",
    )
    assert workbench._mercado_collection_finish_status(20, 20, 0) == (
        "completed",
        "采集完成：入库 20 件，实际重量可用 20 件，待补充 0 件",
    )
    assert workbench._mercado_collection_finish_status(1, 1, 0, 100) == (
        "partial",
        "采集部分完成：入库 1 件，实际重量可用 1 件，待补充 0 件，距离目标还差 99 件",
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


def test_collection_quality_pass_retries_only_rows_missing_actual_weight():
    rows = [
        {
            "source_item_id": "MLM1",
            "title": "Product 1",
            "main_image_url": "https://example.test/1.webp",
            "scrape_status": "partial",
            "error_message": "旧状态未刷新",
            "weight_g": 300,
            "package_length_cm": 10,
            "package_width_cm": 20,
            "package_height_cm": 5,
        },
        {
            "source_item_id": "MLM2",
            "title": "Product 2",
            "main_image_url": "https://example.test/2.webp",
            "scrape_status": "partial",
            "error_message": "智赢插件已显示，但 DOM 中没有完整的重量/尺寸",
        },
        {
            "source_item_id": "MLM3",
            "title": "Product 3",
            "main_image_url": "https://example.test/3.webp",
            "scrape_status": "partial",
            "error_message": "详情页未检测到智赢插件重量尺寸",
        },
        {
            "source_item_id": "MLM4",
            "title": "Product 4",
            "main_image_url": "https://example.test/4.webp",
            "scrape_status": "partial",
            "weight_g": 300,
            "error_message": "尺寸未完整",
        },
        {
            "source_item_id": "MLM5",
            "title": "Product 5",
            "main_image_url": "https://example.test/5.webp",
            "scrape_status": "partial",
            "error_message": "列表页未读取到智赢批量重量数据",
            "page_snapshot": {"detail_acquisition": "listing_page_batch"},
        },
    ]

    assert [
        row["source_item_id"]
        for row in workbench._mercado_collection_rows_needing_repair(rows)
    ] == ["MLM2", "MLM3"]

    template = Path(workbench.app.template_folder, "index.html").read_text(
        encoding="utf-8"
    )
    assert "hasUsableActualWeight && row.title && row.main_image_url" in template
    assert "旧计泡运费已停用，待按实际重量重算" in template


def test_product_list_select_all_uses_current_page_rows_as_source_of_truth():
    template = Path(workbench.app.template_folder, "index.html").read_text(
        encoding="utf-8"
    )

    assert "function mercadoCurrentPageSelectableIds()" in template
    assert "mercadoCurrentPageSelectableIds().forEach(rowId =>" in template
    assert 'mercadoListBody.querySelectorAll(".mercado-item-select:not(:disabled)")' in template
    assert 'document.querySelectorAll(".mercado-item-select:not(:disabled)")' not in template


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


def test_start_collection_requests_login_setup_cleanup_before_running(monkeypatch):
    _reset_state()

    class SetupThread:
        def __init__(self):
            self.alive = True
            self.join_calls = []

        def join(self, timeout=None):
            self.join_calls.append(timeout)
            self.alive = False
            workbench._mercado_playwright_setup_state.update(running=False)

        def is_alive(self):
            return self.alive

    setup_thread = SetupThread()
    workbench._mercado_playwright_setup_thread = setup_thread
    workbench._mercado_playwright_setup_state.update(running=True, status="starting")

    collection_thread = type(
        "CollectionThread",
        (),
        {"start": lambda self: setattr(self, "started", True)},
    )()
    monkeypatch.setattr(
        workbench.threading,
        "Thread",
        lambda **_kwargs: collection_thread,
    )
    monkeypatch.setattr(workbench, "db_create_mercado_collection_task", lambda *args, **kwargs: 43)

    response = _client().post(
        "/api/mercado-collection/start",
        json={
            "source_url": "https://listado.mercadolibre.com.mx/bolsas",
            "requested_count": 1,
            "worker_count": 1,
        },
    )

    assert response.status_code == 200
    assert setup_thread.join_calls == [10.0]
    assert workbench._mercado_playwright_setup_stop_event.is_set()
    assert collection_thread.started is True
    _reset_state()
    workbench._mercado_playwright_setup_thread = None
    workbench._mercado_playwright_setup_stop_event.clear()


def test_start_collection_reports_when_login_setup_cannot_close(monkeypatch):
    _reset_state()

    class SetupThread:
        def join(self, timeout=None):
            self.join_timeout = timeout

        def is_alive(self):
            return True

    setup_thread = SetupThread()
    workbench._mercado_playwright_setup_thread = setup_thread
    workbench._mercado_playwright_setup_state.update(running=True, status="starting")

    response = _client().post(
        "/api/mercado-collection/start",
        json={
            "source_url": "https://listado.mercadolibre.com.mx/bolsas",
            "requested_count": 1,
            "worker_count": 1,
        },
    )

    assert response.status_code == 409
    assert response.get_json()["message"] == "登录窗口正在关闭，请稍后再开始采集"
    _reset_state()
    workbench._mercado_playwright_setup_thread = None
    workbench._mercado_playwright_setup_stop_event.clear()


def test_playwright_login_setup_stop_event_closes_temporary_tab(monkeypatch):
    from erp import mercadolibre_playwright_collector as collector

    class Page:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        async def bring_to_front(self):
            return None

        async def close(self):
            self.closed = True

    page = Page()
    closed_runtimes = []
    stop_event = workbench.threading.Event()
    stop_event.set()

    async def fake_open_runtime():
        return object()

    async def fake_new_page(_runtime):
        return page

    async def fake_goto(_page, _url):
        return None

    async def fake_close_runtime(runtime):
        closed_runtimes.append(runtime)

    monkeypatch.setattr(collector, "_open_runtime", fake_open_runtime)
    monkeypatch.setattr(collector, "_new_page", fake_new_page)
    monkeypatch.setattr(collector, "_goto", fake_goto)
    monkeypatch.setattr(collector, "_close_runtime", fake_close_runtime)
    collector.set_playwright_login_setup_stop_event(stop_event)

    asyncio.run(collector._open_login_setup_async("https://www.mercadolibre.com/"))

    assert page.closed is True
    assert len(closed_runtimes) == 1


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


def test_collection_browser_params_default_to_mexico_bit_window(monkeypatch):
    from erp import mercadolibre_batch_collector as collector

    default_browser = workbench.build_mercado_collection_browser_params({})
    edge_browser = workbench.build_mercado_collection_browser_params(
        {"browser_type": "edge", "window_id": "ignored"}
    )
    monkeypatch.setattr(workbench, "getBrowserIdByName", lambda name: "window-custom")
    custom_browser = workbench.build_mercado_collection_browser_params(
        {"browser_type": "bitbrowser", "window_name": "另一个采集窗口"}
    )

    assert default_browser == {
        "browser_type": "bitbrowser",
        "window_id": collector.DEFAULT_ZYING_WINDOW_ID,
        "window_name": "采集专用（墨西哥）",
    }
    assert edge_browser == {
        "browser_type": "edge",
        "window_id": "",
        "window_name": "",
    }
    assert custom_browser["window_id"] == "window-custom"


def test_collection_browser_windows_endpoint_returns_safe_choices(monkeypatch):
    monkeypatch.setattr(
        workbench,
        "listBrowsers",
        lambda: [
            {"id": "window-other", "name": "其他窗口", "proxy": "secret"},
            {
                "id": "e27ab66368b141a993f9c6847f51222b",
                "name": "采集专用（墨西哥）",
                "proxy": "secret",
            },
        ],
    )

    response = _client().get("/api/mercado-collection/browser-windows")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["default_browser_type"] == "bitbrowser"
    assert data["default_window_name"] == "采集专用（墨西哥）"
    assert data["rows"][0] == {
        "window_id": "e27ab66368b141a993f9c6847f51222b",
        "window_name": "采集专用（墨西哥）",
    }
    assert "proxy" not in data["rows"][0]


def test_collection_login_setup_uses_selected_edge_browser():
    _reset_state()
    with patch.object(workbench.threading, "Thread") as thread_class:
        response = _client().post(
            "/api/mercado-collection/playwright-setup",
            json={"browser_type": "edge"},
        )

    assert response.status_code == 200
    assert thread_class.call_args.kwargs["args"] == ("edge", "", "")
    assert response.get_json()["data"]["browser_type"] == "edge"
    thread_class.return_value.start.assert_called_once()
    _reset_state()


def test_start_collection_passes_selected_browser_to_worker():
    _reset_state()
    with patch.object(
        workbench, "db_create_mercado_collection_task", return_value=44
    ), patch.object(workbench.threading, "Thread") as thread_class:
        response = _client().post(
            "/api/mercado-collection/start",
            json={
                "keyword": "bolsa",
                "site_id": "MLM",
                "requested_count": 5,
                "worker_count": 2,
                "browser_type": "bitbrowser",
                "window_id": "window-selected",
                "window_name": "选中的窗口",
            },
        )

    assert response.status_code == 200
    assert thread_class.call_args.kwargs["args"][-3:] == (
        "bitbrowser",
        "window-selected",
        "选中的窗口",
    )
    thread_class.return_value.start.assert_called_once()
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
            "&review_status=risk&publish_status=failed&weight_status=available"
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
        review_status="risk",
        publish_status="failed",
        weight_status="available",
        weight_min="100",
        weight_max="500",
        price_min="20",
        price_max="80",
        net_proceeds_min="1",
        net_proceeds_max="40",
        date_from="2026-08-25",
        date_to="2026-08-30",
        management_category_id="",
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


def test_management_category_endpoints_support_crud_assignment_and_filtering():
    client = _client()
    category_rows = {
        "total": 1,
        "rows": [{"id": 3, "name": "高利润", "collection_count": 2, "product_count": 4}],
    }
    with patch.object(
        workbench, "db_list_mercado_management_categories", return_value=category_rows
    ):
        response = client.get("/api/mercado-management-categories")
    assert response.status_code == 200
    assert response.get_json()["data"]["rows"][0]["name"] == "高利润"

    with patch.object(
        workbench,
        "db_create_mercado_management_category",
        return_value={"id": 5, "name": "待测款"},
    ) as create_category:
        response = client.post(
            "/api/mercado-management-categories", json={"name": "待测款"}
        )
    assert response.status_code == 200
    create_category.assert_called_once_with("待测款")

    with patch.object(
        workbench,
        "db_update_mercado_management_category",
        return_value={"id": 5, "name": "重点款", "changed": 1},
    ) as update_category:
        response = client.patch(
            "/api/mercado-management-categories/5", json={"name": "重点款"}
        )
    assert response.status_code == 200
    update_category.assert_called_once_with(5, "重点款")

    with patch.object(
        workbench,
        "db_assign_mercado_management_category",
        return_value={"requested": 2, "changed": 2, "category_id": 5},
    ) as assign_category:
        response = client.post(
            "/api/mercado-management-categories/assign",
            json={"item_type": "collection", "item_ids": [7, 8], "category_id": 5},
        )
    assert response.status_code == 200
    assign_category.assert_called_once_with("collection", [7, 8], 5)

    with patch.object(
        workbench, "db_delete_mercado_management_category",
        return_value={"id": 5, "deleted": 1, "cleared_items": 2},
    ) as delete_category:
        response = client.delete("/api/mercado-management-categories/5")
    assert response.status_code == 200
    delete_category.assert_called_once_with(5)

    with patch.object(
        workbench, "db_list_mercado_product_items", return_value={"total": 0, "rows": []}
    ) as list_products:
        response = client.get(
            "/api/mercado-products?management_category_id=uncategorized"
        )
    assert response.status_code == 200
    assert list_products.call_args.kwargs["management_category_id"] == "uncategorized"


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
            "?search=MLM301&status=failed&store_name=泽顺&site_id=MLB"
            "&group_name=精品组&start_date=2026-09-12T00:00"
            "&end_date=2026-09-12T23:59&limit=100"
        )

    assert response.status_code == 200
    assert response.get_json()["data"]["rows"][0]["failure_reason"] == "category rejected"
    assert response.get_json()["data"]["publish_running"] is False
    list_records.assert_called_once_with(
        search="MLM301",
        status="failed",
        store_name="泽顺",
        site_id="MLB",
        group_name="精品组",
        start_date="2026-09-12T00:00",
        end_date="2026-09-12T23:59",
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
            **_actual_shipping(350),
            "net_proceeds_usd": 8,
        },
        {
            "id": 10,
            "source_item_id": "MLM222",
            "review_status": "approved",
            "weight_g": 420,
            **_actual_shipping(420),
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
    ) as create_task, patch.object(workbench.threading, "Thread") as thread_class:
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
    assert thread_class.call_args.kwargs["kwargs"] == {"keyword": "bolsa feminina"}
    thread_class.return_value.start.assert_called_once()
    _reset_state()


def test_product_list_filters_and_review_status_endpoint():
    client = _client()
    rows = {"total": 1, "rows": [{"id": 9, "review_status": "risk"}]}
    with patch.object(
        workbench, "db_list_mercado_product_items", return_value=rows
    ) as list_products:
        response = client.get(
            "/api/mercado-products?search=bag&source_type=pulled&review_status=risk"
            "&publish_status=failed&weight_status=missing&weight_min=100&weight_max=500"
            "&price_min=200&price_max=900&net_proceeds_min=-5&net_proceeds_max=40"
            "&date_from=2026-08-01&date_to=2026-08-25&mercado_category=MLM123"
            "&limit=500&offset=500"
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
        weight_status="missing",
        weight_min="100",
        weight_max="500",
        price_min="200",
        price_max="900",
        net_proceeds_min="-5",
        net_proceeds_max="40",
        date_from="2026-08-01",
        date_to="2026-08-25",
        management_category_id="",
        mercado_category="MLM123",
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


def test_product_list_supports_zying_category_and_developer_filters():
    client = _client()
    rows = {"total": 0, "rows": []}
    with patch.object(
        workbench, "db_list_mercado_product_items", return_value=rows
    ) as list_products:
        response = client.get(
            "/api/mercado-products?zying_category=圆佑同步%2F家电类"
            "&product_developer_id=121658"
        )

    assert response.status_code == 200
    assert list_products.call_args.kwargs["zying_category"] == "圆佑同步/家电类"
    assert list_products.call_args.kwargs["product_developer_id"] == "121658"


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


def test_bulk_collection_measurement_update_endpoint():
    _reset_publish_state()
    client = _client()
    changes = {
        "weight_g": 480,
        "package_length_cm": 30,
        "package_width_cm": 20,
        "package_height_cm": 10,
        "title": "ignored for collection rows",
    }
    with patch.object(
        workbench,
        "db_update_mercado_collection_items",
        return_value={
            "requested": 2,
            "changed": 2,
            "updated_fields": [
                "weight_g", "package_length_cm", "package_width_cm",
                "package_height_cm",
            ],
            "profitability_refresh_pending": True,
        },
    ) as update_collection:
        response = client.patch(
            "/api/mercado-collection/bulk-edit",
            json={"collection_item_ids": [3, 4], "changes": changes},
        )

    assert response.status_code == 200
    assert response.get_json()["data"]["changed"] == 2
    update_collection.assert_called_once_with(
        [3, 4],
        {
            "weight_g": 480,
            "package_length_cm": 30,
            "package_width_cm": 20,
            "package_height_cm": 10,
        },
    )


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
    rows = [{
        "id": 9,
        "source_item_id": "MLM3016972321",
        "source_url": "source",
        "review_status": "approved",
        "weight_g": 350,
        "weight_basis": "plugin_actual",
        "billable_weight_g": 900,
        "shipping_weight_rule": "free_shipping:max_gross_or_volumetric:legacy",
        "profitability_updated_at": "2026-09-12 12:00:00",
        "profitability_source": "mercadolibre_global_selling_cainiao_rate_card_daily_database_cache",
        "net_proceeds_usd": 8,
    }]
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
        **_actual_shipping(350),
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
        **_actual_shipping(350),
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
    assert thread_class.call_args.kwargs["args"][3] == 16
    assert [(target["token_id"], target["site_id"]) for target in targets] == [
        (5, "MLM"),
        (6, "MLM"),
        (7, "MLB"),
    ]
    assert all(target["group_name"] == "精品组" for target in targets)
    _reset_publish_state()


def test_group_random_publish_assigns_each_product_to_one_account_and_all_its_sites():
    targets = [
        {"token_id": 5, "site_id": "MLM"},
        {"token_id": 5, "site_id": "MLB"},
        {"token_id": 6, "site_id": "MLM"},
    ]
    rows = [{"id": value} for value in range(1, 7)]

    with patch.object(workbench.random, "shuffle", side_effect=lambda values: None):
        assigned, skipped = workbench._assign_mercado_group_publish_rows(
            rows, targets, "random", {1: 5, 2: 99}
        )

    assert skipped == 1
    rows_by_target = {
        (target["token_id"], target["site_id"]): [row["id"] for row in target["product_rows"]]
        for target in assigned
    }
    assert rows_by_target[(5, "MLM")] == rows_by_target[(5, "MLB")]
    assert 1 in rows_by_target[(5, "MLM")]
    account_5 = set(rows_by_target[(5, "MLM")])
    account_6 = set(rows_by_target[(6, "MLM")])
    assert account_5.isdisjoint(account_6)
    assert account_5 | account_6 == {1, 3, 4, 5, 6}


def test_group_polling_publish_moves_to_next_account_every_ten_products():
    targets = [
        {"token_id": 5, "site_id": "MLM"},
        {"token_id": 6, "site_id": "MLM"},
    ]
    rows = [{"id": value} for value in range(1, 26)]

    assigned, skipped = workbench._assign_mercado_group_publish_rows(
        rows, targets, "polling"
    )

    assert skipped == 0
    rows_by_account = {
        target["token_id"]: [row["id"] for row in target["product_rows"]]
        for target in assigned
    }
    assert rows_by_account[5] == list(range(1, 11)) + list(range(21, 26))
    assert rows_by_account[6] == list(range(11, 21))


def test_group_publish_endpoint_applies_strategy_and_historical_account_ownership():
    _reset_publish_state()
    rows = [{
        "id": value,
        "source_item_id": f"MLM{value}",
        "review_status": "approved",
        "weight_g": 350,
        **_actual_shipping(350),
        "net_proceeds_usd": 8,
    } for value in range(1, 24)]
    tokens = {"rows": [
        {
            "id": 5,
            "display_name": "跨境店",
            "site_id": "CBT",
            "site_settings": [
                {"site_id": "MLM", "group_name": "精品组", "discount_rate": 90},
                {"site_id": "MLB", "group_name": "精品组", "discount_rate": 95},
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
    ]}
    with patch.object(
        workbench, "db_get_mercado_product_items_by_ids", return_value=rows
    ), patch.object(
        workbench, "db_get_published_mercado_product_account_ids", return_value={1: 5, 2: 99}
    ), patch.object(
        workbench.bit_db_api, "list_mercado_store_tokens", return_value=tokens
    ), patch.object(workbench.threading, "Thread") as thread_class:
        response = _client().post("/api/mercado-products/publish", json={
            "product_item_ids": list(range(1, 24)),
            "selection_mode": "groups",
            "group_names": ["精品组"],
            "group_publish_mode": "polling",
            "site_ids": ["MLM", "MLB"],
        })

    payload = response.get_json()["data"]
    targets = thread_class.call_args.kwargs["args"][1]
    rows_by_target = {
        (target["token_id"], target["site_id"]): [row["id"] for row in target["product_rows"]]
        for target in targets
    }
    assert response.status_code == 200
    assert payload["group_publish_mode"] == "polling"
    assert payload["skipped_other_account_count"] == 1
    assert rows_by_target[(5, "MLM")] == rows_by_target[(5, "MLB")]
    assert rows_by_target[(5, "MLM")] == [1] + list(range(3, 13)) + [23]
    assert rows_by_target[(6, "MLM")] == list(range(13, 23))
    assert payload["requested_count"] == 34
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
        workbench, "db_get_existing_mercado_user_product_ids", return_value={}
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
    ), patch.object(
        workbench, "db_get_existing_mercado_user_product_ids", return_value={}
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
    ), patch.object(
        workbench, "db_get_existing_mercado_user_product_ids", return_value={}
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
            **_actual_shipping(350),
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
            **_actual_shipping(350),
            "net_proceeds_usd": 8,
        },
        {
            "id": 12,
            "source_item_id": "MLM3016972324",
            "review_status": "approved",
            "weight_g": 350,
            **_actual_shipping(350),
            "net_proceeds_usd": 0,
        },
        {
            "id": 10,
            "source_item_id": "MLM3016972322",
            "source_url": "source",
            "review_status": "approved",
            "weight_g": 350,
            **_actual_shipping(350),
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
        **_actual_shipping(350),
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
    rows = [{"id": 9, "source_item_id": "MLM3016972321", "source_url": "source", "review_status": "approved", "weight_g": 350, **_actual_shipping(350), "net_proceeds_usd": 8}]
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
