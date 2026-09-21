from pathlib import Path

from flask import g

from bit import bit_db_api, bit_interface


def test_database_health_requires_shared_token_for_remote_clients(monkeypatch):
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setattr(
        bit_interface,
        "mysql_config",
        {"host": "192.168.1.11"},
        raising=False,
    )
    monkeypatch.setattr(
        bit_interface,
        "pool_status",
        lambda: {
            "pool_count": 1,
            "active": 2,
            "idle": 1,
            "physical": 3,
            "max_connections": 12,
            "pools": [],
        },
    )
    monkeypatch.setenv("BIT_DB_API_TOKEN", "shared-secret")
    monkeypatch.setattr(bit_interface.app, "testing", True)
    client = bit_interface.app.test_client()

    denied = client.get(
        "/api/db/health",
        environ_overrides={"REMOTE_ADDR": "10.0.0.25"},
    )
    allowed = client.get(
        "/api/db/health",
        headers={"X-Internal-Token": "shared-secret"},
        environ_overrides={"REMOTE_ADDR": "10.0.0.25"},
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.get_json()["data"] == {
        "role": "server",
        "database_host": "192.168.1.11",
        "connection_pool": {
            "pool_count": 1,
            "active": 2,
            "idle": 1,
            "physical": 3,
            "max_connections": 12,
            "pools": [],
        },
    }


def test_client_mode_does_not_expose_database_routes(monkeypatch):
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    monkeypatch.setattr(bit_interface.app, "testing", True)

    response = bit_interface.app.test_client().get("/api/db/health")

    assert response.status_code == 503
    assert "客户端模式" in response.get_json()["message"]


def test_public_workbench_issues_short_lived_local_executor_token(monkeypatch):
    user = {
        "id": 7,
        "username": "operator",
        "permissions": ["tasks.view", "tasks.execute"],
        "access_version": 1,
    }
    monkeypatch.setenv("BIT_DB_API_TOKEN", "shared-local-executor-secret")
    monkeypatch.setattr(bit_interface, "get_current_workbench_user", lambda: user)

    response = bit_interface.app.test_client().post(
        "/api/execution-targets/local-token",
        json={"permission": "tasks.execute"},
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["base_url"] == bit_interface.LOCAL_EXECUTOR_BROWSER_URL
    assert data["target_address_space"] == "loopback"
    assert bit_interface._local_executor_user_from_token(data["token"]) == {
        "id": 7,
        "username": "operator",
        "permission": "tasks.execute",
    }


def test_local_executor_bridge_only_accepts_loopback_client_requests(monkeypatch):
    monkeypatch.setenv("BIT_DB_API_TOKEN", "shared-local-executor-secret")
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    token = bit_interface.create_local_executor_token(
        {"id": 7, "username": "operator"},
        "tasks.view",
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": "https://wuhanzeshun.com",
    }
    client = bit_interface.app.test_client()

    allowed = client.get(
        "/api/local-executor/health",
        headers=headers,
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    denied = client.get(
        "/api/local-executor/health",
        headers=headers,
        environ_overrides={"REMOTE_ADDR": "10.0.0.25"},
    )
    preflight = client.options(
        "/api/local-executor/health",
        headers={
            "Origin": "https://wuhanzeshun.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
            "Access-Control-Request-Private-Network": "true",
        },
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert allowed.status_code == 200
    assert allowed.get_json()["data"]["execution_target"] == "local"
    assert allowed.headers["Access-Control-Allow-Origin"] == "https://wuhanzeshun.com"
    assert denied.status_code == 403
    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Private-Network"] == "true"


def test_local_executor_target_address_space_matches_browser_destination():
    assert (
        bit_interface.local_executor_target_address_space("http://127.0.0.1:5000")
        == "loopback"
    )
    assert (
        bit_interface.local_executor_target_address_space("http://localhost:5000")
        == "loopback"
    )
    assert (
        bit_interface.local_executor_target_address_space("http://192.168.1.50:5000")
        == "local"
    )


def test_public_workbench_labels_loopback_fetch_with_server_target_space():
    template = Path(bit_interface.app.template_folder, "index.html").read_text(
        encoding="utf-8"
    )

    assert "targetAddressSpace: credential.targetAddressSpace" in template
    assert 'targetAddressSpace: "local"' not in template


def test_local_executor_context_forces_daily_task_to_local(monkeypatch):
    with bit_interface.app.test_request_context(
        "/api/local-executor/tasks/daily/start",
        method="POST",
        json={"execution_target": "server", "appeal_type": "侵权"},
    ):
        g.local_executor_user = {"permission": "tasks.execute"}
        params = bit_interface.build_daily_task_params(
            {"execution_target": "server", "appeal_type": "侵权"}
        )

    assert params["execution_target"] == "local"


def test_client_mode_skips_all_central_background_services(monkeypatch):
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    called = []
    service_names = (
        "start_interrupted_collection_recovery",
        "start_store_link_scheduler_bootstrap",
        "start_prohibited_listing_scheduler_bootstrap",
        "start_official_infraction_scheduler_bootstrap",
        "start_api_reputation_scheduler_bootstrap",
        "start_token_refresh_scheduler_bootstrap",
        "start_store_email_sync_scheduler_bootstrap",
        "start_yandex_console_bootstrap",
        "ensure_mercado_profit_refresh_worker",
    )
    for name in service_names:
        monkeypatch.setattr(
            bit_interface,
            name,
            lambda current=name: called.append(current),
        )

    bit_interface.start_interface_background_services()

    assert called == []


def test_test_server_mode_skips_all_central_background_services(monkeypatch):
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setenv("BIT_BACKGROUND_SERVICES_DISABLED", "1")
    called = []
    service_names = (
        "start_interrupted_collection_recovery",
        "start_store_link_scheduler_bootstrap",
        "start_prohibited_listing_scheduler_bootstrap",
        "start_official_infraction_scheduler_bootstrap",
        "start_api_reputation_scheduler_bootstrap",
        "start_token_refresh_scheduler_bootstrap",
        "start_store_email_sync_scheduler_bootstrap",
        "start_yandex_console_bootstrap",
        "ensure_mercado_profit_refresh_worker",
    )
    for name in service_names:
        monkeypatch.setattr(
            bit_interface,
            name,
            lambda current=name: called.append(current),
        )
    monkeypatch.setattr(
        bit_interface.bit_order_sync,
        "ensure_order_sync_scheduler",
        lambda: called.append("order_sync"),
    )

    bit_interface.start_interface_background_services()

    assert called == []


def test_test_server_mode_does_not_start_schedulers_on_first_request(monkeypatch):
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setattr(bit_interface.app, "testing", False)
    monkeypatch.setenv("BIT_BACKGROUND_SERVICES_DISABLED", "1")
    called = []
    monkeypatch.setattr(
        bit_interface.bit_order_sync,
        "ensure_order_sync_scheduler",
        lambda: called.append("order_sync"),
    )
    monkeypatch.setattr(
        bit_interface.bit_order_sync,
        "ensure_order_financial_backfill_worker",
        lambda: called.append("financial_backfill"),
    )
    monkeypatch.setattr(
        bit_interface.bit_order_sync,
        "ensure_order_image_backfill_worker",
        lambda: called.append("image_backfill"),
    )

    bit_interface._start_order_sync_scheduler()

    assert called == []


def test_test_server_mode_does_not_start_profitability_worker(monkeypatch):
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setattr(bit_interface.app, "testing", False)
    monkeypatch.setenv("BIT_BACKGROUND_SERVICES_DISABLED", "1")

    def unexpected_thread(*_args, **_kwargs):
        raise AssertionError("profitability worker must not start")

    monkeypatch.setattr(bit_interface.threading, "Thread", unexpected_thread)

    bit_interface.ensure_mercado_profit_refresh_worker()


def test_server_mode_starts_reputation_and_order_sync_schedulers(monkeypatch):
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.delenv("BIT_BACKGROUND_SERVICES_DISABLED", raising=False)
    for name in (
        "start_interrupted_collection_recovery",
        "start_store_link_scheduler_bootstrap",
        "start_prohibited_listing_scheduler_bootstrap",
        "start_official_infraction_scheduler_bootstrap",
        "start_token_refresh_scheduler_bootstrap",
        "start_store_email_sync_scheduler_bootstrap",
        "start_yandex_console_bootstrap",
    ):
        monkeypatch.setattr(bit_interface, name, lambda: None)
    started = []
    monkeypatch.setattr(
        bit_interface,
        "start_api_reputation_scheduler_bootstrap",
        lambda: started.append("api_reputation"),
    )
    monkeypatch.setattr(
        bit_interface,
        "ensure_mercado_profit_refresh_worker",
        lambda: started.append("profitability"),
    )
    monkeypatch.setattr(
        bit_interface.bit_order_sync,
        "ensure_order_sync_scheduler",
        lambda: started.append("order_sync"),
    )
    monkeypatch.setattr(
        bit_interface.bit_order_sync,
        "ensure_order_financial_backfill_worker",
        lambda: None,
    )
    monkeypatch.setattr(
        bit_interface.bit_order_sync,
        "ensure_order_image_backfill_worker",
        lambda: None,
    )

    bit_interface.start_interface_background_services()

    assert started == ["api_reputation", "profitability", "order_sync"]


def test_database_api_health_client_uses_http_route(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs))
        or {"role": "server", "database_host": "192.168.1.11"},
    )

    result = bit_db_api.get_database_api_health()

    assert result["role"] == "server"
    assert calls == [("GET", "/api/db/health", {"timeout": 10})]


def test_official_infraction_dashboard_client_uses_http_route(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs))
        or {"rows": []},
    )

    result = bit_db_api.list_official_infraction_dashboard(
        days=30,
        view_mode="current",
        search="MLM123",
    )

    assert result == {"rows": []}
    assert calls == [(
        "GET",
        "/api/db/official-infractions/dashboard",
        {"params": {"days": 30, "view_mode": "current", "search": "MLM123"}},
    )]


def test_official_infraction_dashboard_client_supports_long_export_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs))
        or {"rows": []},
    )

    bit_db_api.list_official_infraction_dashboard(
        rows_only=True,
        page_size=50000,
        _request_timeout=300,
    )

    assert calls == [(
        "GET",
        "/api/db/official-infractions/dashboard",
        {
            "params": {"rows_only": True, "page_size": 50000},
            "timeout": 300,
        },
    )]


def test_official_infraction_counts_rebuild_tuple_keys_from_http(monkeypatch):
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda *_args, **_kwargs: {
            "days": 100,
            "last_synced_at": "2026-09-04 12:00:00",
            "count_rows": [{
                "token_id": 7,
                "site_id": "MLM",
                "infraction_count": 4,
                "rights_holder_count": 2,
                "latest_infraction_at": "2026-09-03 10:00:00",
            }],
        },
    )

    result = bit_db_api.get_current_infraction_counts_by_token_site(100)

    assert result["counts"][(7, "MLM")] == {
        "infraction_count": 4,
        "rights_holder_count": 2,
        "latest_infraction_at": "2026-09-03 10:00:00",
    }


def test_live_infraction_collection_client_uses_server_route(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(bit_db_api.time, "sleep", lambda _seconds: None)

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "POST":
            return {"job_id": "job-123", "status": "queued"}
        return {
            "job_id": "job-123",
            "status": "completed",
            "result": {"data": [], "results": [], "failed_stores": []},
        }

    monkeypatch.setattr(
        bit_db_api,
        "_request",
        request,
    )
    targets = [{"token_id": 7, "name": "授权店铺", "site_ids": ["MLM"]}]

    result = bit_db_api.collect_live_detection_infractions(
        targets,
        recent_days=30,
        max_workers=3,
    )

    assert result["data"] == []
    assert calls == [
        (
            "POST",
            "/api/db/official-infractions/live/jobs",
            {
                "timeout": 30,
                "json": {
                    "targets": targets,
                    "recent_days": 30,
                    "max_workers": 3,
                },
            },
        ),
        (
            "GET",
            "/api/db/official-infractions/live/jobs/job-123",
            {"timeout": 30},
        ),
    ]


def test_live_infraction_collection_falls_back_during_rolling_deployment(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/live/jobs"):
            raise RuntimeError("数据库接口返回非 JSON，状态码：404")
        return {"data": [], "results": [], "failed_stores": []}

    monkeypatch.setattr(bit_db_api, "_request", request)

    result = bit_db_api.collect_live_detection_infractions(
        [{"token_id": 7}], recent_days=30, max_workers=1
    )

    assert result["data"] == []
    assert [path for _method, path, _kwargs in calls] == [
        "/api/db/official-infractions/live/jobs",
        "/api/db/official-infractions/live",
    ]


def test_live_infraction_collection_transparently_delegates_in_client_mode(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "collect_live_detection_infractions",
        lambda targets, **kwargs: calls.append((targets, kwargs))
        or {"data": [{"编号": "MLM123"}]},
    )
    targets = [{"token_id": 7, "site_ids": ["MLM"]}]

    result = bit_interface.mercado_infraction_sync.collect_live_detection_infractions(
        targets,
        recent_days=45,
        max_workers=2,
    )

    assert result == {"data": [{"编号": "MLM123"}]}
    assert calls == [(
        targets,
        {"recent_days": 45, "max_workers": 2, "stop_event": None},
    )]


def test_live_infraction_server_route_reads_tokens_on_server(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setattr(
        bit_interface.mercado_infraction_sync,
        "collect_live_detection_infractions",
        lambda targets, **kwargs: calls.append((targets, kwargs))
        or {"data": [{"编号": "MLM123"}], "results": []},
    )
    targets = [{"token_id": 7, "name": "授权店铺", "site_ids": ["MLM"]}]

    response = bit_interface.app.test_client().post(
        "/api/db/official-infractions/live",
        json={"targets": targets, "recent_days": 30, "max_workers": 4},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["data"] == [{"编号": "MLM123"}]
    assert calls == [(targets, {"recent_days": 30, "max_workers": 4})]


def test_live_infraction_background_job_avoids_long_http_request(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setattr(
        bit_interface.mercado_infraction_sync,
        "collect_live_detection_infractions",
        lambda targets, **kwargs: calls.append((targets, kwargs))
        or {"data": [{"编号": "MLM456"}], "results": []},
    )
    targets = [{"token_id": 8, "name": "授权店铺二", "site_ids": ["MLM"]}]
    client = bit_interface.app.test_client()

    start_response = client.post(
        "/api/db/official-infractions/live/jobs",
        json={"targets": targets, "recent_days": 45, "max_workers": 2},
    )
    assert start_response.status_code == 200
    job_id = start_response.get_json()["data"]["job_id"]

    state = None
    for _ in range(100):
        status_response = client.get(
            f"/api/db/official-infractions/live/jobs/{job_id}"
        )
        assert status_response.status_code == 200
        state = status_response.get_json()["data"]
        if state["status"] in {"completed", "failed"}:
            break
        bit_interface.time.sleep(0.01)

    assert state["status"] == "completed"
    assert state["result"]["data"] == [{"编号": "MLM456"}]
    assert calls == [(targets, {"recent_days": 45, "max_workers": 2})]
