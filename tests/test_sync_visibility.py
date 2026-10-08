from copy import deepcopy

import pytest

from bit.sync_visibility import scope_sync_state


TOKENS = [
    {"id": 7, "display_name": "徐家强的店铺", "nickname": "xu-shop",
     "organization_key": "wuhan-zeshun", "site_settings": [{"salesperson": "徐家强"}]},
    {"id": 9, "display_name": "李超宇的店铺", "nickname": "li-shop",
     "organization_key": "wuhan-zeshun", "site_settings": [{"salesperson": "李超宇"}]},
    {"id": 11, "display_name": "其他企业店铺", "organization_key": "other-company"},
]


def shared_state():
    return {
        "running": True, "status": "running", "task_id": "shared-task",
        "message": "李超宇的店铺正在同步，已新增 999 条",
        "target_token_ids": [7, 9, 11], "active_token_ids": [7, 9, 11],
        "current_store": "李超宇的店铺", "active_stores": [row["display_name"] for row in TOKENS],
        "total_stores": 3, "processed_stores": 2, "inserted_count": 1002,
        "failed_count": 1, "failed_stores": 1,
        "results": [
            {"token_id": 7, "store": "徐家强的店铺", "status": "success", "inserted": 3},
            {"token_id": 9, "store": "李超宇的店铺", "status": "error", "inserted": 999},
        ],
        "logs": ["徐家强的店铺完成；李超宇的店铺失败", "其他企业店铺已同步"],
        "auto_print": {"results": [{"store": "李超宇的店铺"}]},
        "daily_financials": {"logs": ["李超宇的店铺"]},
        "lock_owner": {"host": "internal-host"},
        "auto_sync_enabled": True, "auto_sync_days": 3,
    }


def test_member_state_contains_only_own_stores_and_counters():
    state = shared_state()
    original = deepcopy(state)
    visible = scope_sync_state(state, TOKENS, {7})
    assert "李超宇" not in str(visible)
    assert "其他企业" not in str(visible)
    assert visible["current_store"] == "徐家强的店铺"
    assert visible["target_token_ids"] == [7]
    assert visible["total_stores"] == visible["processed_stores"] == 1
    assert visible["inserted_count"] == 3
    assert visible["failed_count"] == visible["failed_stores"] == 0
    assert visible["auto_sync_days"] == 3
    assert "lock_owner" not in visible
    assert "auto_print" not in visible
    assert "daily_financials" not in visible
    assert state == original
    assert scope_sync_state(visible, TOKENS, {7}) == visible


def test_foreign_task_is_idle_for_member_even_when_globally_running():
    state = shared_state()
    state.update(target_token_ids=[9, 11], active_token_ids=[9, 11], results=state["results"][1:])
    visible = scope_sync_state(state, TOKENS, {7})
    assert visible["running"] is False
    assert visible["status"] == "idle"
    assert visible["results"] == visible["logs"] == visible["active_stores"] == []
    assert visible["inserted_count"] == visible["total_stores"] == 0
    assert "task_id" not in visible


def test_member_finishes_while_other_members_still_sync():
    state = shared_state()
    state["active_token_ids"] = [9, 11]
    visible = scope_sync_state(state, TOKENS, {7})
    assert visible["running"] is False
    assert visible["status"] == "completed"
    assert visible["failed_count"] == 0


def test_queued_own_store_is_visible_before_worker_starts():
    state = shared_state()
    state.update(status="starting", active_token_ids=[], results=[])
    visible = scope_sync_state(state, TOKENS, {7})
    assert visible["running"] is True
    assert visible["status"] == "starting"
    assert visible["total_stores"] == 1
    assert visible["processed_stores"] == 0


def test_nested_rights_holder_sync_is_scoped_independently():
    state = shared_state()
    state.update(target_token_ids=[9], active_token_ids=[9], results=[])
    state["rights_holder_sync"] = shared_state()
    visible = scope_sync_state(state, TOKENS, {7})
    assert "李超宇" not in str(visible)
    assert visible["prohibited_running"] is False
    assert visible["running"] is True
    assert visible["rights_holder_sync"]["target_token_ids"] == [7]


def test_legacy_names_must_be_unique_and_token_id_takes_precedence():
    tokens = deepcopy(TOKENS)
    tokens[1]["display_name"] = tokens[0]["display_name"]
    state = {
        "running": True, "current_store": tokens[0]["display_name"],
        "results": [
            {"store": tokens[0]["display_name"], "status": "success"},
            {"store": "xu-shop", "status": "error"},
            {"store": "xu-shop", "token_id": 9, "status": "error"},
        ],
    }
    visible = scope_sync_state(state, tokens, {7})
    assert visible["active_stores"] == []
    assert len(visible["results"]) == 1
    assert visible["results"][0]["token_id"] == 7


def test_no_stores_means_no_task_data_even_for_unidentifiable_lock_owner():
    visible = scope_sync_state(shared_state(), TOKENS, set())
    assert visible["results"] == visible["logs"] == []
    assert visible["running"] is False
    assert visible["total_stores"] == 0
    assert "shared-task" not in str(visible)


@pytest.fixture
def member_client(monkeypatch, subscribed_workbench_account):
    from bit import bit_interface as web
    user = {
        "id": 71, "username": "xujiaqiang", "display_name": "徐家强",
        "role_key": "member", "own_store_only": False, "access_version": 1,
        "permissions": ["*"], "organization_key": "wuhan-zeshun",
    }
    monkeypatch.setattr(web, "get_current_workbench_user", lambda: user)
    monkeypatch.setattr(web.bit_db_api, "list_mercado_store_tokens", lambda: {"rows": TOKENS})
    return web, web.app.test_client(), user


@pytest.mark.parametrize("path,backend", [
    ("/api/order-sync/status", "get_order_sync_status"),
    ("/api/store-links/sync/status", "get_store_link_sync_status"),
    ("/api/prohibited-listings/sync/status", "get_prohibited_listing_sync_status"),
    ("/api/official-infractions/sync/status", "get_official_infraction_sync_status"),
])
@pytest.mark.parametrize("remote_backend", [True, False])
def test_status_routes_filter_server_and_client_responses(member_client, monkeypatch, path, backend, remote_backend):
    web, client, _ = member_client
    state = shared_state()
    state["rights_holder_sync"] = shared_state()
    monkeypatch.setattr(web, "USE_DB_API", remote_backend)
    monkeypatch.setattr(web.bit_db_api, backend, lambda: state)
    monkeypatch.setattr(web.mercado_infraction_sync, "official_infraction_sync_status", lambda: state)
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "李超宇" not in str(response.get_json())
    assert response.get_json()["data"]["total_stores"] == 1


@pytest.mark.parametrize("path,backend", [
    ("/api/order-sync/start", "start_order_sync"),
    ("/api/store-links/sync/start", "start_store_link_sync"),
    ("/api/prohibited-listings/sync/start", "start_prohibited_listing_sync"),
    ("/api/official-infractions/sync", "start_official_infraction_sync"),
])
@pytest.mark.parametrize("started", [True, False])
def test_start_and_already_running_responses_are_also_scoped(member_client, monkeypatch, path, backend, started):
    web, client, _ = member_client
    monkeypatch.setattr(web, "USE_DB_API", True)
    def start(*args, **kwargs):
        ids = args[0] if args else kwargs["token_ids"]
        assert ids == [7]
        return (started, shared_state()) if backend == "start_official_infraction_sync" else {
            "started": started, "state": shared_state(),
        }
    monkeypatch.setattr(web.bit_db_api, backend, start)
    response = client.post(path, json={"token_ids": [7]})
    assert response.status_code == (202 if started else 409)
    assert "李超宇" not in str(response.get_json())
    assert response.get_json()["data"]["target_token_ids"] == [7]


def test_enterprise_admin_sees_company_stores_and_platform_admin_can_see_all(member_client, monkeypatch):
    web, client, user = member_client
    monkeypatch.setattr(web.bit_db_api, "get_store_link_sync_status", shared_state)
    user.update(role_key="enterprise_admin", own_store_only=False)
    response = client.get("/api/store-links/sync/status")
    assert response.get_json()["data"]["target_token_ids"] == [7, 9]
    assert "其他企业" not in str(response.get_json())
    # Existing unrestricted compatibility sessions preserve the full payload.
    user.update(is_platform_admin=True)
    response = client.get("/api/store-links/sync/status")
    assert response.get_json()["data"]["target_token_ids"] == [7, 9, 11]


def test_failed_permission_lookup_never_returns_global_state(member_client, monkeypatch):
    web, client, _ = member_client
    monkeypatch.setattr(web.bit_db_api, "get_store_link_sync_status", shared_state)
    def unavailable():
        raise RuntimeError("catalog unavailable")
    monkeypatch.setattr(web.bit_db_api, "list_mercado_store_tokens", unavailable)
    response = client.get("/api/store-links/sync/status")
    assert response.status_code == 502
    assert "data" not in response.get_json()
    assert "李超宇" not in str(response.get_json())


@pytest.mark.parametrize("module_name,runner", [
    ("bit_order_sync", "run_order_sync"),
    ("bit_store_link_sync", "run_store_link_sync"),
    ("bit_prohibited_listing_sync", "run_prohibited_listing_sync"),
    ("mercado_infraction_sync", "run_official_infraction_sync"),
])
def test_workers_identify_targets_active_stores_and_failed_results(monkeypatch, module_name, runner):
    import importlib
    module = importlib.import_module("bit." + module_name)
    monkeypatch.setattr(module, "_sync_state", deepcopy(module._sync_state))
    monkeypatch.setattr(module, "_token_records", lambda _: TOKENS[:2])
    monkeypatch.setattr(module, "get_lock_owner", lambda _: None)
    monkeypatch.setattr(module, "run_capacity_store", lambda _kind, _id, fn, record: fn(record))
    for name in ("mark_store_link_sync_started", "mark_store_link_sync_finished",
                 "mark_prohibited_sync_started", "mark_prohibited_sync_finished",
                 "mark_infraction_sync_started", "mark_infraction_sync_finished",
                 "invalidate_store_link_metadata_cache"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda *a, **kw: None)
    if hasattr(module, "get_overview_sync_settings"):
        monkeypatch.setattr(module, "get_overview_sync_settings", lambda _: {"enabled": True})
    if hasattr(module, "seed_legacy_infraction_snapshot"):
        monkeypatch.setattr(module, "seed_legacy_infraction_snapshot", lambda: {})
        monkeypatch.setattr(module, "backfill_infraction_images", lambda **_: {})
        monkeypatch.setattr(module, "get_infraction_sync_context", lambda _: {})
    def sync(record, *_, **kwargs):
        assert module._sync_state["target_token_ids"] == [7, 9]
        assert record["id"] in module._sync_state["active_token_ids"]
        if record["id"] == 9:
            raise RuntimeError("other member failed")
        return {
            "store": record["display_name"], "token_id": record["id"], "status": "success",
            "fetched": 1, "inserted": 1, "updated": 0, "discovered": 1,
            "details": 1, "products": 1, "failed": 0, "scanned": 1,
            "reason_matched": 0, "prohibited": 0, "detail_failed": 0,
            "detection_scanned": 1, "detection_matched": 0, "rights_holder": 0,
        }
    monkeypatch.setattr(module, "_sync_store", sync)
    kwargs = {"mode": "automatic"} if module_name == "bit_order_sync" else {}
    state = getattr(module, runner)(token_ids=[7, 9], **kwargs)
    assert state["target_token_ids"] == [7, 9]
    assert state["active_token_ids"] == []
    assert {row["token_id"] for row in state["results"]} == {7, 9}
    visible = scope_sync_state(state, TOKENS, {7})
    assert visible["status"] == "completed"
    assert "other member failed" not in str(visible)
