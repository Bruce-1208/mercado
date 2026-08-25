from unittest.mock import patch

import bit.bit_interface as workbench


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
            requested_count=0,
            processed_count=0,
            published_count=0,
            failed_count=0,
            results=[],
        )


def test_workbench_contains_collection_and_product_list_ui():
    client = _client()
    response = client.get("/")

    assert response.status_code == 200
    assert b'data-tab="mercado-collection"' in response.data
    assert b'data-tab="mercado-publish-records"' in response.data
    assert b'id="tab-mercado-publish-records"' in response.data
    assert b'id="publish-record-body"' in response.data
    assert "失败原因 / 接口明细".encode("utf-8") in response.data
    assert b'id="mercado-list-body"' in response.data
    assert b'id="mercado-add-selected"' in response.data
    assert b'id="mercado-collection-workers"' in response.data
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
    assert "净收益".encode("utf-8") in response.data
    assert "不超过500g：只按实重".encode("utf-8") in response.data
    assert b'id="mercado-delete-selected"' in response.data
    assert b'id="mercado-publish-store"' in response.data
    assert b'id="mercado-publish-site"' in response.data
    assert b'id="mercado-publish-workers"' in response.data
    for site_name in ("墨西哥", "巴西", "阿根廷", "智利", "哥伦比亚", "乌拉圭"):
        assert site_name.encode("utf-8") in response.data
    assert b'id="mercado-publish-selected"' in response.data
    assert b'id="mercado-product-review-actions"' in response.data
    assert b'id="mercado-source-collected"' in response.data
    assert b'id="mercado-source-pulled"' in response.data
    assert b'id="mercado-review-filter"' in response.data
    assert b'id="mercado-review-bulk"' in response.data
    for status_name in ("未审核", "通过", "疑似", "侵权", "风险"):
        assert status_name.encode("utf-8") in response.data
    assert "仅“通过”状态可上架".encode("utf-8") in response.data
    assert "批量上架".encode("utf-8") in response.data
    assert 'partial: "部分完成"'.encode("utf-8") in response.data


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
                "worker_count": 5,
            },
        )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["data"]["task_id"] == 42
    assert payload["data"]["running"] is True
    assert payload["data"]["worker_count"] == 5
    create_task.assert_called_once_with(
        "https://listado.mercadolibre.com.mx/bolsas", 12, "测试用户"
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
    ):
        response = client.get("/api/mercado-collection/items?search=Lonchera")
    assert response.status_code == 200
    assert response.get_json()["data"]["rows"][0]["weight_g"] == 333

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
    list_records.assert_called_once_with(
        search="MLM301",
        status="failed",
        store_name="泽顺",
        site_id="MLB",
        limit=100,
        offset=0,
    )


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
        )

    assert response.status_code == 200
    assert response.get_json()["data"] == rows
    list_products.assert_called_once_with(
        search="bag",
        limit=500,
        offset=0,
        source_type="pulled",
        review_status="risk",
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
    rows = [{"id": 9, "source_item_id": "MLM3016972321", "source_url": "source", "review_status": "approved"}]
    tokens = {
        "total": 1,
        "rows": [{"id": 5, "display_name": "泽顺墨西哥", "nickname": "SHOP"}],
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
    get_products.assert_called_once_with([9])
    start_thread.assert_called_once()
    _reset_publish_state()


def test_batch_publish_endpoint_rejects_unsupported_site():
    _reset_publish_state()
    response = _client().post(
        "/api/mercado-products/publish",
        json={"product_item_ids": [9], "token_id": 5, "site_id": "MPE"},
    )

    assert response.status_code == 400
    assert "不支持的目标站点" in response.get_json()["message"]


def test_batch_publish_endpoint_rejects_product_that_is_not_approved():
    _reset_publish_state()
    rows = [
        {
            "id": 9,
            "source_item_id": "MLM3016972321",
            "source_url": "source",
            "review_status": "unreviewed",
        }
    ]
    with patch.object(
        workbench, "db_get_mercado_product_items_by_ids", return_value=rows
    ), patch.object(workbench.threading.Thread, "start") as start_thread:
        response = _client().post(
            "/api/mercado-products/publish",
            json={"product_item_ids": [9], "token_id": 5, "site_id": "MLM"},
        )

    assert response.status_code == 400
    assert "只有审核状态为“通过”的产品可以上架" in response.get_json()["message"]
    start_thread.assert_not_called()


def test_batch_publish_endpoint_rejects_invalid_worker_count():
    _reset_publish_state()
    response = _client().post(
        "/api/mercado-products/publish",
        json={
            "product_item_ids": [9],
            "token_id": 5,
            "site_id": "MLM",
            "worker_count": 9,
        },
    )

    assert response.status_code == 400
    assert "1-8" in response.get_json()["message"]


def test_batch_publish_endpoint_rejects_cross_site_for_local_store():
    _reset_publish_state()
    rows = [{"id": 9, "source_item_id": "MLM3016972321", "source_url": "source", "review_status": "approved"}]
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
