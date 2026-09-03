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
