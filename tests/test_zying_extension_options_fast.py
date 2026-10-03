"""Offline regressions for the extension's optional ZYing selector refresh."""
import json
import time

import pytest
from flask import Flask

from bit import service_split


def _unexpected(*_args, **_kwargs):
    pytest.fail("Selector refresh must not contact ZYing or the task worker")


@pytest.fixture
def options_client(monkeypatch):
    from bit import bit_interface as workbench

    monkeypatch.setenv("BIT_SERVICE_MODE", "web")
    monkeypatch.setitem(workbench.app.config, "TESTING", True)
    monkeypatch.setattr(workbench.app, "secret_key", "zying-options-offline")
    monkeypatch.setattr(workbench, "_browser_extension_zying_options_cache", {})
    monkeypatch.setattr(workbench, "_BROWSER_EXTENSION_ZYING_OPTIONS_CACHE_SECONDS", 60)
    monkeypatch.setattr(workbench.bit_zying_caiji, "_ZYING_CATEGORY_CACHE", [])
    monkeypatch.setattr(workbench.bit_zying_caiji, "_ZYING_DEVELOPER_CACHE", {})
    monkeypatch.setattr(workbench.bit_zying_caiji, "ZYING_DEVELOPER_CACHE_SECONDS", 60)
    monkeypatch.setattr(workbench.bit_zying_caiji, "list_zying_product_developers", _unexpected)
    monkeypatch.setattr(service_split.requests, "Session", _unexpected)
    monkeypatch.setattr(workbench, "db_list_mercado_product_items", _unexpected)
    monkeypatch.setattr(workbench, "db_sync_zying_product_developers", _unexpected)
    user = {"id": 7, "username": "collector", "access_version": 0}
    token = workbench.create_browser_extension_token(user)
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "credential": "page-credential",
        "categories": [{"category_id": "8", "category_name": "家居"}],
        "page_only": True,
        "refresh": True,
    }
    return workbench, workbench.app.test_client(), headers, body


def test_web_selector_refresh_preserves_page_developer_sync(options_client, monkeypatch):
    workbench, client, headers, body = options_client
    synced_rows = []
    monkeypatch.setattr(workbench, "db_sync_zying_product_developers", lambda rows: (
        synced_rows.extend(rows) or {"zying_products": 1, "product_list": 2}
    ))
    response = client.post("/api/browser-extension/zying/options", json={
        **body, "developers": [{"id": 17, "name": "产品开发甲"}],
    }, headers=headers)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["developers"] == synced_rows == [{"id": "17", "name": "产品开发甲"}]
    assert data["synced"] == {"zying_products": 1, "product_list": 2}
    assert workbench.bit_zying_caiji.list_cached_zying_product_developers(
        body["credential"]
    ) == synced_rows


def test_page_only_refresh_uses_history_without_api_or_redundant_sync(options_client, monkeypatch):
    workbench, client, headers, body = options_client
    monkeypatch.setattr(workbench, "db_list_mercado_product_items", lambda **_kwargs: {
        "rows": [{"product_developer_id": "17", "product_developer_name": "产品开发甲"}],
    })
    response = client.post("/api/browser-extension/zying/options", json=body, headers=headers)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["developers"] == [{"id": "17", "name": "产品开发甲"}]
    assert "历史智赢商品" in data["developer_warning"]
    assert data["synced"] == {"zying_products": 0, "product_list": 0}


def test_page_only_refresh_uses_cached_directory_without_history(options_client):
    workbench, client, headers, body = options_client
    rows = [{"id": "17", "name": "产品开发甲"}]
    workbench.bit_zying_caiji.cache_zying_product_developers(body["credential"], rows)
    response = client.post("/api/browser-extension/zying/options", json=body, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["data"]["developers"] == rows


def test_history_failure_keeps_current_categories_available(options_client, monkeypatch):
    workbench, client, headers, body = options_client

    def unavailable(**_kwargs):
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(workbench, "db_list_mercado_product_items", unavailable)
    response = client.post("/api/browser-extension/zying/options", json=body, headers=headers)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["categories"][0]["category_id"] == "8"
    assert data["developers"] == []
    assert "展开人员筛选" in data["developer_warning"]


def test_page_only_cache_does_not_suppress_legacy_directory_lookup(options_client, monkeypatch):
    workbench, client, headers, body = options_client
    monkeypatch.setattr(workbench, "db_list_mercado_product_items", lambda **_kwargs: {"rows": []})
    body.pop("refresh")
    first = client.post("/api/browser-extension/zying/options", json=body, headers=headers)
    assert first.status_code == 200
    assert first.get_json()["data"]["developers"] == []
    lookups = []
    rows = [{"id": "17", "name": "产品开发甲"}]
    monkeypatch.setattr(workbench.bit_zying_caiji, "list_zying_product_developers", lambda credential: (
        lookups.append(credential) or rows
    ))
    monkeypatch.setattr(workbench, "db_sync_zying_product_developers", lambda _rows: {})
    body.pop("page_only")
    response = client.post("/api/browser-extension/zying/options", json=body, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["data"]["developers"] == rows
    assert lookups == ["page-credential"]


def test_web_options_still_require_extension_login_and_view_permission(options_client, monkeypatch):
    workbench, client, headers, body = options_client
    response = client.post("/api/browser-extension/zying/options", json=body)
    assert response.status_code == 401
    monkeypatch.setattr(workbench, "_browser_extension_user_from_token", lambda _token: {
        "id": 7, "username": "collector", "access_version": 1, "permissions": [],
    })
    response = client.post("/api/browser-extension/zying/options", json=body, headers=headers)
    assert response.status_code == 403
    assert response.get_json()["required_permissions"] == ["zying_collection.view"]


def test_cached_directory_is_token_scoped_copied_and_expires(options_client):
    workbench, _client, _headers, body = options_client
    zy = workbench.bit_zying_caiji
    zy.cache_zying_product_developers(body["credential"], [{"id": "17", "name": "产品开发甲"}])
    assert zy.list_cached_zying_product_developers("other-credential") == []
    rows = zy.list_cached_zying_product_developers(body["credential"])
    rows[0]["name"] = "changed"
    assert zy.list_cached_zying_product_developers(body["credential"])[0]["name"] == "产品开发甲"
    entry = zy._ZYING_DEVELOPER_CACHE[zy._zying_developer_cache_key(body["credential"])]
    entry["expires_at"] = time.monotonic() - 1
    assert zy.list_cached_zying_product_developers(body["credential"]) == []


def test_web_options_remain_local_while_task_endpoints_keep_worker_owner(monkeypatch):
    monkeypatch.setenv("BIT_SERVICE_MODE", "web")
    app = Flask(__name__)
    app.secret_key = "offline"
    service_split.install_service_routing(app)
    app.add_url_rule("/api/browser-extension/zying/options", "options", lambda: "local", methods=["POST"])
    calls = []

    class UnavailableWorker:
        def request(self, method, url, **_kwargs):
            calls.append((method, url))
            raise service_split.requests.ConnectionError("worker unavailable")

        def close(self):
            pass

    monkeypatch.setattr(service_split.requests, "Session", UnavailableWorker)
    client = app.test_client()
    assert client.post("/api/browser-extension/zying/options").get_data(as_text=True) == "local"
    assert calls == []
    for action, method in (("start", "POST"), ("status", "GET"), ("stop", "POST")):
        path = "/api/browser-extension/zying/" + action
        assert service_split.is_worker_path(path)
        assert client.open(path, method=method).status_code == 503
    assert len(calls) == 3
    assert not service_split.is_worker_path("/api/browser-extension/zying/options")


def test_worker_start_seeds_page_developers_before_starting_collection(options_client, monkeypatch):
    workbench, client, headers, body = options_client
    monkeypatch.setenv("BIT_SERVICE_MODE", "worker")
    developers = [{"id": "17", "name": "产品开发甲"}]
    body = {**body, "developers": developers, "start_page": 1, "end_page": 1}
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    path = "/api/browser-extension/zying/start"
    timestamp = str(int(time.time()))
    original_ip = "198.51.100.2"
    headers = {
        **headers, "Content-Type": "application/json", "X-Workbench-Original-IP": original_ip,
        service_split.HOP_HEADER: timestamp + ":" + service_split.signature(
            workbench.app.secret_key, "POST", path, encoded, timestamp, original_ip,
        ),
    }
    started = []

    def start_task(params, **_kwargs):
        assert workbench.bit_zying_caiji.list_cached_zying_product_developers(
            params["auth_token"]
        ) == developers
        assert "developers" not in params
        started.append(params)
        return {"running": True}, "", 200

    monkeypatch.setattr(workbench, "_start_zying_collection_task", start_task)
    response = client.post(path, data=encoded, headers=headers)

    assert response.status_code == 200
    assert len(started) == 1
    assert started[0]["auth_token"] == body["credential"]
    assert "page-credential" not in response.get_data(as_text=True)
