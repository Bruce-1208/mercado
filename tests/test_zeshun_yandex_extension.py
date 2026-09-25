"""Verify the plugin's authenticated bridge to the existing Yandex scraper."""

from unittest.mock import Mock

import pytest


@pytest.fixture
def bridge(monkeypatch):
    import bit.bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="yandex-plugin-test")
    user = {"id": 1, "username": "tester", "access_version": 1, "permissions": ["*"]}
    monkeypatch.setattr(workbench, "_browser_extension_user_from_token", lambda token: user if token == "test-token" else None)
    ready = Mock(return_value=(True, "Yandex 控制台已启动"))
    proxy = Mock(side_effect=lambda path: workbench.jsonify({"run_id": 42, "status": "queued"}) if path == "api/search" else workbench.jsonify({
        "run": {"id": 42, "status": "running", "found_count": 3, "scanned_count": 8, "requested_count": 10}
    }))
    monkeypatch.setattr(workbench, "ensure_yandex_console", ready)
    monkeypatch.setattr(workbench, "_proxy_yandex_console_request", proxy)
    return workbench.app.test_client(), ready, proxy


def test_yandex_bridge_requires_plugin_login(bridge):
    client, ready, proxy = bridge
    response = client.post("/api/browser-extension/yandex/search", json={"keyword": "phone", "count": 10})
    assert response.status_code == 401
    ready.assert_not_called()
    proxy.assert_not_called()


def test_yandex_bridge_validates_inputs_before_starting_worker(bridge):
    client, ready, proxy = bridge
    headers = {"Authorization": "Bearer test-token"}
    for payload in ({"keyword": "", "count": 10}, {"keyword": "phone", "count": True}, {"keyword": "phone", "count": 501}):
        response = client.post("/api/browser-extension/yandex/search", json=payload, headers=headers)
        assert response.status_code == 400
    ready.assert_not_called()
    proxy.assert_not_called()


def test_yandex_bridge_starts_and_reads_only_the_search_status(bridge):
    client, ready, proxy = bridge
    headers = {"Authorization": "Bearer test-token"}
    started = client.post("/api/browser-extension/yandex/search", json={"keyword": "行车记录仪", "count": 10}, headers=headers)
    progress = client.get("/api/browser-extension/yandex/search/42", headers=headers)
    assert started.status_code == 200
    assert started.get_json()["run_id"] == 42
    assert progress.get_json()["run"]["found_count"] == 3
    assert ready.call_count == 2
    assert [call.args[0] for call in proxy.call_args_list] == ["api/search", "api/search/42/status"]


def test_yandex_bridge_reports_unavailable_worker(bridge):
    client, ready, proxy = bridge
    ready.return_value = (False, "请先安装 Yandex 运行环境")
    response = client.post("/api/browser-extension/yandex/search", json={"keyword": "phone", "count": 10}, headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 503
    assert "请先安装" in response.get_json()["message"]
    proxy.assert_not_called()
