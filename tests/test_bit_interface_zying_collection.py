from pathlib import Path

import pytest

from bit import bit_db_api, bit_interface


def _logged_in_client():
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}
    return client


def test_zying_collection_console_exposes_page_category_and_dedup_controls():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'data-tab="zying-collection"' in template
    assert 'id="tab-zying-collection"' in template
    assert 'id="zying-collection-start-page"' in template
    assert 'id="zying-collection-end-page"' in template
    assert 'id="zying-collection-category"' in template
    assert 'id="zying-collection-browser-type"' in template
    assert 'id="zying-collection-window-name"' in template
    assert "本地 Edge（9222）" in template
    assert "比特浏览器窗口名称" in template
    assert 'id="start-zying-collection-btn"' in template
    assert "数据库已有的产品编号会在详情采集前直接跳过" in template
    assert 'fetch("/api/zying-collection/start"' in template
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
