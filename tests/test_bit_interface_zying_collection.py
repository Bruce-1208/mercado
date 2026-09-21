from pathlib import Path

import pytest

from bit import bit_db_api, bit_interface


def _logged_in_client():
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        # Exercise protected endpoints as an explicitly authorized test user.
        # Production requests still pass through the permission middleware.
        flask_session["workbench_user"] = {
            "username": "tester",
            "access_version": 0,
            "permissions": ["*"],
        }
    return client


def test_zying_collection_is_removed_from_console_navigation_but_keeps_backend_fallback():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'data-tab="zying-collection"' not in template
    assert 'id="tab-zying-collection"' in template
    assert 'id="zying-collection-start-page"' in template
    assert 'id="zying-collection-end-page"' in template
    assert 'id="zying-collection-category"' in template
    assert '<label for="zying-collection-category">产品分类（可选）</label>' in template
    assert '<select id="zying-collection-category">' in template
    assert 'id="zying-collection-category-options"' not in template
    assert "<th>智赢分类</th>" in template
    assert "function mercadoProductZyingCategory(row)" in template
    assert "function zyingCategoryDisplayName(categoryName, categoryId" in template
    assert "return id ? \"分类名称待同步\" : emptyLabel;" in template
    assert 'const category = row.zying_category || row.zying_category_id || "-";' not in template
    assert "` [${row.category_id}]`" not in template
    assert "return `${categoryName}${categoryId ? `（${categoryId}）` : \"\"}`;" not in template
    assert 'id="zying-collection-developer"' in template
    assert 'id="refresh-zying-options-btn"' in template
    assert "function refreshZyingCollectionOptions()" in template
    assert "/api/zying-collection/options/refresh" in template
    assert 'id="zying-collection-browser-type"' in template
    assert 'id="zying-collection-window-name"' in template
    assert 'id="zying-collection-window-options"' in template
    assert "本地 Edge（9222）" in template
    assert "比特浏览器窗口" in template
    assert "采集专用（墨西哥）" in template
    assert 'id="start-zying-collection-btn"' in template
    assert 'id="stop-zying-collection-btn"' in template
    assert 'id="capture-zying-login-btn"' not in template
    assert "登录智赢后直接启动采集" in template
    assert "#zying-collection-log {" in template
    assert "color: #e6edf7;" in template
    assert "数据库已有的产品编号会在详情采集前直接跳过" in template
    assert 'fetch("/api/zying-collection/start"' in template
    assert 'fetch("/api/zying-collection/stop"' in template
    assert "function stopZyingCollection()" in template
    assert "function loadZyingCollectionStatus()" in template


def test_build_zying_collection_params_accepts_resume_page_and_category():
    params = bit_interface.build_zying_collection_params(
        {
            "start_page": 7,
            "end_page": 12,
            "category": "202170568",
            "window_id": " zying-window ",
        }
    )

    assert params == {
        "number": 12,
        "window_id": "zying-window",
        "start_page": 7,
        "category": "202170568",
    }


def test_build_zying_collection_params_rejects_end_before_start():
    with pytest.raises(ValueError, match="起始页 9 不能大于结束页 8"):
        bit_interface.build_zying_collection_params(
            {"start_page": 9, "end_page": 8}
        )


def test_build_zying_collection_params_accepts_product_cursor_and_limit():
    params = bit_interface.build_zying_collection_params(
        {
            "start_product_id": " 801623017 ",
            "max_items": 25,
            "category": "202170568",
        }
    )

    assert params["start_page"] == 1
    assert params["number"] == 10000
    assert params["start_product_id"] == "801623017"
    assert params["max_items"] == 25


@pytest.mark.parametrize("payload", [
    {"start_product_id": "abc", "max_items": 10},
    {"start_product_id": "1", "max_items": 0},
    {"start_product_id": "1", "max_items": 10001},
])
def test_build_zying_collection_params_rejects_bad_product_cursor(payload):
    with pytest.raises(ValueError):
        bit_interface.build_zying_collection_params(payload)


def test_build_zying_collection_params_accepts_product_developer():
    params = bit_interface.build_zying_collection_params(
        {
            "start_page": 1,
            "end_page": 2,
            "product_developer_id": "121658",
            "product_developer_name": " 张三 ",
        }
    )

    assert params["product_developer_id"] == "121658"
    assert params["product_developer_name"] == "张三"


def test_build_zying_collection_params_accepts_edge_or_bitbrowser_name():
    edge = bit_interface.build_zying_collection_params(
        {"start_page": 1, "end_page": 2, "browser_type": "edge"}
    )
    bitbrowser = bit_interface.build_zying_collection_params(
        {
            "start_page": 1,
            "end_page": 2,
            "browser_type": "bitbrowser",
            "window_name": " 智赢专用窗口 ",
        }
    )

    assert edge["browser_type"] == "edge"
    assert edge["window_name"] == ""
    assert bitbrowser["browser_type"] == "bitbrowser"
    assert bitbrowser["window_name"] == "智赢专用窗口"


def test_zying_login_defaults_to_mexico_collection_window():
    params = bit_interface.build_zying_login_params({})

    assert params["browser_type"] == "bitbrowser"
    assert params["window_id"] == bit_interface.bit_zying_caiji.DEFAULT_ZYING_WINDOW_ID
    assert params["window_name"] == bit_interface.bit_zying_caiji.DEFAULT_ZYING_WINDOW_NAME


def test_zying_product_developers_refresh_auth_and_backfill_database(monkeypatch):
    captured = []
    developers = [{"id": "121658", "name": "张三"}]
    monkeypatch.setattr(
        bit_interface.bit_zying_caiji,
        "capture_zying_login_from_browser",
        lambda **params: captured.append(params) or {"configured": True},
    )
    monkeypatch.setattr(
        bit_interface.bit_zying_caiji,
        "list_zying_product_developers",
        lambda: developers,
    )
    monkeypatch.setattr(
        bit_interface,
        "db_sync_zying_product_developers",
        lambda rows: {"zying_products": 4, "product_list": 4},
    )

    response = _logged_in_client().post(
        "/api/zying-collection/developers",
        json={"browser_type": "edge"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "rows": developers,
        "synced": {"zying_products": 4, "product_list": 4},
    }
    assert captured[0]["browser_type"] == "edge"
    assert captured[0]["validate"] is False


def test_zying_collection_options_refreshes_categories_and_developers_together(
    monkeypatch,
):
    captured = []
    refreshed = {
        "categories": [
            {"category_id": "202170568", "category_name": "圆佑同步/家电类"}
        ],
        "developers": [{"id": "121658", "name": "张三"}],
        "auth": {"configured": True},
    }
    monkeypatch.setattr(
        bit_interface.bit_zying_caiji,
        "refresh_zying_collection_options_from_browser",
        lambda **params: captured.append(params) or refreshed,
    )
    monkeypatch.setattr(
        bit_interface,
        "db_sync_zying_product_developers",
        lambda rows: {"zying_products": 2, "product_list": 3},
    )

    response = _logged_in_client().post(
        "/api/zying-collection/options/refresh",
        json={"browser_type": "edge"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"] == {
        **refreshed,
        "synced": {"zying_products": 2, "product_list": 3},
    }
    assert captured == [
        {
            "browser_type": "edge",
            "window_id": bit_interface.bit_zying_caiji.DEFAULT_ZYING_WINDOW_ID,
            "window_name": "",
        }
    ]


def test_zying_collection_categories_prefers_latest_current_page_snapshot(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "db_list_zying_risk_categories",
        lambda: [
            {"category_id": "202170568", "category_name": "历史分类名"},
            {"category_id": "202170531", "category_name": "历史/游戏类"},
        ],
    )
    monkeypatch.setattr(
        bit_interface.bit_zying_caiji,
        "list_cached_zying_categories",
        lambda: [
            {"category_id": "202170568", "category_name": "圆佑同步/家电类"}
        ],
    )

    rows = bit_interface.list_zying_collection_categories()

    assert {row["category_id"]: row["category_name"] for row in rows} == {
        "202170531": "历史/游戏类",
        "202170568": "圆佑同步/家电类",
    }


def test_zying_collection_categories_keep_counts_when_snapshot_updates_name(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "db_list_zying_risk_categories",
        lambda: [{"category_id": "202170568", "category_name": "旧名称", "total": 7}],
    )
    monkeypatch.setattr(
        bit_interface.bit_zying_caiji,
        "list_cached_zying_categories",
        lambda: [{"category_id": "202170568", "category_name": "家电类"}],
    )

    assert bit_interface.list_zying_collection_categories() == [
        {"category_id": "202170568", "category_name": "家电类", "total": 7}
    ]


def test_zying_collection_start_runs_script_with_database_dedup(monkeypatch):
    captured = {}

    class ImmediateThread:
        def __init__(self, target, args=(), **kwargs):
            self.target = target
            self.args = args

        def start(self):
            if self.target is bit_interface.run_zying_collection_job:
                self.target(*self.args)

    def collect_products(**kwargs):
        captured.update(kwargs)
        print("列表已完成产品编号去重")
        return {
            "records": [{"product_id": "801623017"}],
            "collected_count": 1,
            "inserted_count": 1,
            "skipped_existing_count": 3,
            "duplicate_count": 2,
            "detail_failed_count": 0,
        }

    monkeypatch.setattr(bit_interface.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        bit_interface,
        "ensure_mercado_profit_refresh_worker",
        lambda: None,
    )
    monkeypatch.setattr(
        bit_interface.bit_zying_caiji,
        "collect_zying_products",
        collect_products,
    )
    with bit_interface._zying_collection_state_lock:
        previous_state = dict(bit_interface._zying_collection_state)
        previous_logs = list(bit_interface._zying_collection_logs)
    try:
        response = _logged_in_client().post(
            "/api/zying-collection/start",
            json={
                "start_page": 2,
                "end_page": 5,
                "category": "圆佑同步/家电类",
                "window_id": "window-1",
            },
        )
        status_response = _logged_in_client().get("/api/zying-collection/status")
    finally:
        with bit_interface._zying_collection_state_lock:
            bit_interface._zying_collection_state.clear()
            bit_interface._zying_collection_state.update(previous_state)
            bit_interface._zying_collection_logs.clear()
            bit_interface._zying_collection_logs.extend(previous_logs)

    assert response.status_code == 200
    assert captured["number"] == 5
    assert captured["start_page"] == 2
    assert captured["category"] == "圆佑同步/家电类"
    assert captured["product_writer"] is bit_interface.db_insert_zying_product_info
    assert (
        captured["existing_product_id_reader"]
        is bit_interface.db_get_existing_zying_product_ids
    )
    assert (
        captured["product_mirror_writer"]
        is bit_interface.db_upsert_zying_products_to_products
    )
    assert captured["return_summary"] is True
    status = status_response.get_json()["data"]
    assert status["running"] is False
    assert status["status"] == "success"
    assert status["summary"]["skipped_existing_count"] == 3
    assert any("已有产品跳过 3 条" in line for line in status["logs"])


def test_existing_product_ids_internal_api_uses_database_reader(monkeypatch):
    captured = []
    monkeypatch.setattr(
        bit_interface,
        "db_get_existing_zying_product_ids",
        lambda product_ids: captured.extend(product_ids) or {"801623245"},
    )

    response = _logged_in_client().post(
        "/api/db/zying-products/existing",
        json={"product_ids": ["801623245", "801623017"]},
    )

    assert response.status_code == 200
    assert captured == ["801623245", "801623017"]
    assert response.get_json()["data"]["product_ids"] == ["801623245"]


def test_bit_db_api_forwards_existing_product_id_lookup(monkeypatch):
    captured = {}
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")

    def request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"product_ids": ["801623245"]}

    monkeypatch.setattr(bit_db_api, "_request", request)

    result = bit_db_api.get_existing_zying_product_ids(
        ["801623245", "801623017", "801623245"],
    )

    assert result == {"801623245"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/db/zying-products/existing"
    assert captured["json"]["product_ids"] == ["801623245", "801623017"]


def test_bit_db_api_forwards_zying_product_list_mirror(monkeypatch):
    captured = {}
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")

    def request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"count": 1, "skipped": 0}

    monkeypatch.setattr(bit_db_api, "_request", request)
    rows = [{"product_id": "795184904", "listing_snapshot": {"source": {}}}]

    result = bit_db_api.upsert_zying_products_to_products(rows)

    assert result == {"count": 1, "skipped": 0}
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/db/zying-products/product-list"
    assert captured["json"]["rows"] == rows


def test_zying_login_buttons_open_and_capture_visible_browser(monkeypatch):
    opened = []
    captured = []
    monkeypatch.setattr(
        bit_interface.bit_zying_caiji,
        "open_zying_login_window",
        lambda **params: opened.append(params)
        or {"message": "登录窗口已打开", "browser_type": params["browser_type"]},
    )
    monkeypatch.setattr(
        bit_interface.bit_zying_caiji,
        "capture_zying_login_from_browser",
        lambda **params: captured.append(params)
        or {
            "configured": True,
            "saved_at": "2026-08-29 12:00:00",
            "browser_type": params["browser_type"],
            "window_name": params["window_name"],
        },
    )
    monkeypatch.setattr(
        bit_interface.bit_zying_caiji,
        "get_zying_auth_status",
        lambda: {"configured": False},
    )
    client = _logged_in_client()

    open_response = client.post(
        "/api/zying-collection/auth/open",
        json={"browser_type": "bitbrowser", "window_name": "智赢专用窗口"},
    )
    capture_response = client.post(
        "/api/zying-collection/auth/capture",
        json={"browser_type": "bitbrowser", "window_name": "智赢专用窗口"},
    )

    assert open_response.status_code == 200
    assert capture_response.status_code == 200
    assert opened[0]["window_name"] == "智赢专用窗口"
    assert captured[0]["window_name"] == "智赢专用窗口"
    assert capture_response.get_json()["data"]["auth"]["configured"] is True


def test_zying_collection_failure_marks_login_required(monkeypatch):
    monkeypatch.setattr(
        bit_interface.bit_zying_caiji,
        "collect_zying_products",
        lambda **kwargs: (_ for _ in ()).throw(
            bit_interface.bit_zying_caiji.ZyingAuthenticationError("登录已失效")
        ),
    )
    lock = bit_interface.threading.Lock()
    lock.acquire()
    with bit_interface._zying_collection_state_lock:
        previous_state = dict(bit_interface._zying_collection_state)
        previous_logs = list(bit_interface._zying_collection_logs)
    try:
        bit_interface.run_zying_collection_job({}, lock)
        assert bit_interface._zying_collection_state["status"] == "error"
        assert bit_interface._zying_collection_state["requires_login"] is True
        assert "登录已失效" in bit_interface._zying_collection_state["message"]
    finally:
        with bit_interface._zying_collection_state_lock:
            bit_interface._zying_collection_state.clear()
            bit_interface._zying_collection_state.update(previous_state)
            bit_interface._zying_collection_logs.clear()
            bit_interface._zying_collection_logs.extend(previous_logs)


def test_zying_collection_stop_endpoint_sets_safe_stop_event():
    with bit_interface._zying_collection_state_lock:
        previous_state = dict(bit_interface._zying_collection_state)
        previous_logs = list(bit_interface._zying_collection_logs)
        previous_stop = bit_interface._zying_collection_stop_event.is_set()
        bit_interface._zying_collection_stop_event.clear()
        bit_interface._zying_collection_state.update(
            {"running": True, "status": "running", "message": "采集中"}
        )
    try:
        response = _logged_in_client().post("/api/zying-collection/stop", json={})

        assert response.status_code == 200
        assert bit_interface._zying_collection_stop_event.is_set()
        assert bit_interface._zying_collection_state["status"] == "stopping"
        assert any(
            "结束指令" in line for line in bit_interface._zying_collection_logs
        )
    finally:
        with bit_interface._zying_collection_state_lock:
            bit_interface._zying_collection_state.clear()
            bit_interface._zying_collection_state.update(previous_state)
            bit_interface._zying_collection_logs.clear()
            bit_interface._zying_collection_logs.extend(previous_logs)
            if previous_stop:
                bit_interface._zying_collection_stop_event.set()
            else:
                bit_interface._zying_collection_stop_event.clear()


def test_zying_collection_job_records_user_stopped_status(monkeypatch):
    monkeypatch.setattr(
        bit_interface.bit_zying_caiji,
        "collect_zying_products",
        lambda **kwargs: (_ for _ in ()).throw(
            bit_interface.bit_zying_caiji.ZyingCollectionStopped("用户结束采集")
        ),
    )
    lock = bit_interface.threading.Lock()
    lock.acquire()
    with bit_interface._zying_collection_state_lock:
        previous_state = dict(bit_interface._zying_collection_state)
        previous_logs = list(bit_interface._zying_collection_logs)
    try:
        bit_interface.run_zying_collection_job({}, lock)

        assert bit_interface._zying_collection_state["running"] is False
        assert bit_interface._zying_collection_state["status"] == "stopped"
        assert bit_interface._zying_collection_state["requires_login"] is False
        assert "用户结束采集" in bit_interface._zying_collection_state["message"]
    finally:
        with bit_interface._zying_collection_state_lock:
            bit_interface._zying_collection_state.clear()
            bit_interface._zying_collection_state.update(previous_state)
            bit_interface._zying_collection_logs.clear()
            bit_interface._zying_collection_logs.extend(previous_logs)
