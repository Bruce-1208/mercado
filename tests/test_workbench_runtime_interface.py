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
        "Origin": "https://zeshun.nat100.top",
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
            "Origin": "https://zeshun.nat100.top",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
            "Access-Control-Request-Private-Network": "true",
        },
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert allowed.status_code == 200
    assert allowed.get_json()["data"]["execution_target"] == "local"
    assert allowed.headers["Access-Control-Allow-Origin"] == "https://zeshun.nat100.top"
    assert denied.status_code == 403
    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Private-Network"] == "true"


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
        "start_token_refresh_scheduler_bootstrap",
        "start_store_email_sync_scheduler_bootstrap",
    )
    for name in service_names:
        monkeypatch.setattr(
            bit_interface,
            name,
            lambda current=name: called.append(current),
        )

    bit_interface.start_interface_background_services()

    assert called == []


def test_server_mode_starts_order_sync_scheduler(monkeypatch):
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    for name in (
        "start_interrupted_collection_recovery",
        "start_store_link_scheduler_bootstrap",
        "start_prohibited_listing_scheduler_bootstrap",
        "start_official_infraction_scheduler_bootstrap",
        "start_token_refresh_scheduler_bootstrap",
        "start_store_email_sync_scheduler_bootstrap",
    ):
        monkeypatch.setattr(bit_interface, name, lambda: None)
    started = []
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

    assert started == ["order_sync"]


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
