import json
import threading
import time

import pytest

from bit import browser_extension_appeal as appeal


@pytest.fixture
def service(tmp_path, monkeypatch):
    from bit import bit_runtime_lock
    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path / "locks")
    rows = [
        {"id": 11, "meli_user_id": "100", "display_name": "当前店铺", "nickname": "shop-a",
         "enabled": True, "organization_key": "tenant-a", "site_settings": [
             {"site_id": "MLM", "appeal_enabled": True},
             {"site_id": "MLB", "appeal_enabled": "true"},
             {"site_id": "MLC", "appeal_enabled": "false"}]},
        {"id": 22, "meli_user_id": "200", "display_name": "其他企业店铺", "nickname": "shop-b",
         "enabled": True, "organization_key": "tenant-b", "site_settings": [
             {"site_id": "MLM", "appeal_enabled": True}]},
    ]
    loaded, written, identifier_calls = [], [], []
    def load(token_id):
        loaded.append(token_id)
        return next({**row, "access_token": "oauth-never-return"} for row in rows if row["id"] == token_id)
    def identifiers(identity, form, site):
        identifier_calls.append((identity["token_id"], form, site))
        return [f"{site}{n}" for n in range(1, 14)]
    value = appeal.AppealService(
        scoped_tokens=lambda user: [row for row in rows if row["organization_key"] == user["organization_key"]],
        token_loader=load, api_key=lambda user: "saved-account-key",
        record_writer=lambda record: written.append(record) or 55,
        root=tmp_path / "appeals", accounts_loader=lambda token: [
            {"user_id": "101" if token["id"] == 11 else "201", "site_id": "MLM"},
            {"user_id": "102" if token["id"] == 11 else "202", "site_id": "MLB"},
            {"user_id": "103" if token["id"] == 11 else "203", "site_id": "MLC"}],
        packages_loader=lambda identity, site, ids: [str(9000000000000 + int(value)) for value in ids],
        identifiers_loader=identifiers, phrase_loader=lambda form: "{nickname}：请核查{order_ids}")
    value.user = {"id": 7, "organization_key": "tenant-a", "access_version": 1}
    value.rows, value.loaded, value.written, value.identifier_calls = rows, loaded, written, identifier_calls
    return value


def params(**extra):
    return {"seller_id": "100", "forms": ["侵权"], "sites": ["MLM"], "mode": "AI客服",
            "appeal_copy_mode": "普通模式", "loop_count": 10, **extra}


def test_async_preparation_returns_before_slow_reads_and_deduplicates(service):
    from bit import browser_extension_appeal_jobs as jobs
    entered, release = threading.Event(), threading.Event()
    original = service.identifiers_loader
    def slow(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    service.identifiers_loader = slow
    payload = params(request_id="request-one")
    try:
        first = jobs.start(service, service.user, payload)
        assert first["state"] == "preparing"
        assert entered.wait(2)
        assert jobs.start(service, service.user, payload)["state"] == "preparing"
        assert jobs.status(service, service.user, payload)["state"] == "preparing"
        with pytest.raises(appeal.AppealError, match="参数已改变"):
            jobs.start(service, service.user, {**payload, "sites": ["MLB"]})
        with pytest.raises(appeal.AppealError, match="不存在"):
            jobs.status(service, {**service.user, "id": 8}, payload)
    finally:
        release.set()
    for _ in range(200):
        result = jobs.status(service, service.user, payload)
        if result["state"] == "ready":
            break
        time.sleep(.01)
    assert result["state"] == "ready"
    assert len(service.identifier_calls) == 1
    assert jobs.start(service, service.user, payload)["plan"]["task_id"] == result["plan"]["task_id"]
    service.rows[0]["enabled"] = False
    with pytest.raises(appeal.AppealError, match="权限已变更"):
        jobs.status(service, service.user, payload)


def test_async_preparation_failure_is_readable_without_replaying(service):
    from bit import browser_extension_appeal_jobs as jobs
    calls = []
    def failing(*args):
        calls.append(1)
        raise appeal.AppealError("上游限频", 429)
    service.identifiers_loader = failing
    payload = params(request_id="request-failed")
    jobs.start(service, service.user, payload)
    for _ in range(200):
        try:
            jobs.status(service, service.user, payload)
        except appeal.AppealError as exc:
            assert exc.status == 429
            break
        time.sleep(.01)
    else:
        pytest.fail("preparation did not fail")
    with pytest.raises(appeal.AppealError, match="上游限频"):
        jobs.start(service, service.user, payload)
    assert len(calls) == 1


def test_preparation_timeout_and_restart_do_not_launch_again(service, monkeypatch):
    from bit import browser_extension_appeal_jobs as jobs
    payload = params(request_id="interrupted")
    path = jobs._path(service, service.user, payload["request_id"])
    value = {"state": "preparing", "pid": 123, "created_at": time.time() - 601}
    jobs._write(path, value)
    monkeypatch.setattr(jobs, "_pid_is_running", lambda pid: True)
    with pytest.raises(appeal.AppealError, match="准备超时"):
        jobs.status(service, service.user, payload)
    monkeypatch.setattr(jobs, "_pid_is_running", lambda pid: False)
    with pytest.raises(appeal.AppealError, match="重启"):
        jobs.status(service, service.user, payload)
    assert service.identifier_calls == []


def test_context_binds_root_or_verified_child_to_scoped_store(service):
    root = service.context(service.user, {"seller_id": "100"})
    assert root["shop"] == {"id": 11, "name": "当前店铺", "seller_id": "100"}
    assert [row["site_id"] for row in root["sites"]] == ["MLM", "MLB"]
    child = service.context(service.user, {"seller_id": "101"})
    assert [row["site_id"] for row in child["sites"]] == ["MLM"]
    assert child["identity"]["allowed_seller_ids"] == ["100", "101"]
    with pytest.raises(appeal.AppealError, match="本人授权店铺"):
        service.context(service.user, {"seller_id": "201"})
    assert 22 not in service.loaded
    assert "oauth-never-return" not in json.dumps(root)


def test_disabled_store_or_site_cannot_be_selected(service):
    with pytest.raises(appeal.AppealError, match="授权站点"):
        service.plan(service.user, params(sites=["MLC"]))
    with pytest.raises(appeal.AppealError, match="授权站点"):
        service.plan(service.user, params(seller_id="101", sites=["MLB"]))
    service.rows[0]["enabled"] = "false"
    with pytest.raises(appeal.AppealError, match="本人授权店铺"):
        service.context(service.user, {"seller_id": "100"})


def test_context_reports_missing_store_with_browser_account_id(service):
    with pytest.raises(appeal.AppealError, match="ID：999.*未匹配到本人授权店铺") as error:
        service.context(service.user, {"seller_id": "999", "nickname": "shop-a"})
    assert error.value.status == 403


def test_context_distinguishes_unreadable_authorization_from_missing_store(service):
    def unavailable(token):
        raise RuntimeError("private upstream response")
    service.accounts_loader = unavailable
    with pytest.raises(appeal.AppealError, match="授权资料读取失败") as error:
        service.context(service.user, {"seller_id": "101"})
    assert error.value.status == 503
    assert "private upstream response" not in str(error.value)
    with pytest.raises(appeal.AppealError, match="授权账号校验失败") as direct_error:
        service.context(service.user, {"seller_id": "100"})
    assert direct_error.value.status == 503


def test_context_reports_duplicate_authorized_store_without_selecting_one(service):
    service.rows.append({**service.rows[0], "id": 33})
    with pytest.raises(appeal.AppealError, match="匹配到多个本人授权店铺") as error:
        service.context(service.user, {"seller_id": "100"})
    assert error.value.status == 403
    assert service.written == []


def test_context_reports_empty_account_scope_without_loading_other_stores(service):
    service.scoped_tokens = lambda user: []
    with pytest.raises(appeal.AppealError, match="没有可用的本人授权店铺") as error:
        service.context(service.user, {"seller_id": "100"})
    assert error.value.status == 403
    assert service.loaded == []


def test_ai_plan_grouping_and_fixed_agent_business_order(service):
    plan = service.plan(service.user, params(forms=["投诉", "侵权", "取消率", "禁限售", "延误"]))
    assert [row["form"] for row in plan["browser_collections"]] == ["延误", "取消率", "投诉"]
    plan = service.plan(service.user, {"task_id": plan["task_id"], "round_number": 1,
        "reputation_identifiers": {"MLM": {"延误": [str(100000 + n) for n in range(6)],
        "取消率": [str(200000 + n) for n in range(11)], "投诉": [str(300000 + n) for n in range(3)]}}})
    assert [group["form"] for group in plan["groups"]] == ["延误", "延误", "侵权", "侵权", "禁限售", "禁限售", "取消率", "取消率", "投诉", "投诉"]
    assert [len(group["identifiers"]) for group in plan["groups"]] == [5, 1, 10, 3, 10, 3, 10, 1, 2, 1]
    assert plan["browser_collections"] == []
    assert service.identifier_calls == [(11, "侵权", "MLM"), (11, "禁限售", "MLM")]
    repeated = service.plan(service.user, {"task_id": plan["task_id"]})
    assert repeated["groups"] == plan["groups"]
    assert service.identifier_calls == [(11, "侵权", "MLM"), (11, "禁限售", "MLM")]


def test_manual_mode_matches_single_agent_selection_and_custom_copy(service):
    plan = service.plan(service.user, params(mode="人工客服", forms=["投诉", "侵权", "禁限售"]))
    assert [len(group["identifiers"]) for group in plan["groups"]] == [10, 10]
    assert [item["form"] for item in plan["browser_collections"]] == ["投诉"]
    assert plan["groups"][1]["identifiers"] == [f"MLM{n}" for n in range(1, 11)]
    assert service.plan(service.user, {"task_id": plan["task_id"]})["groups"] == plan["groups"]
    service.identifier_calls.clear()
    custom = service.plan(service.user, params(mode="人工客服", forms=list(appeal.FORMS), message="自定义正文"))
    assert len(custom["groups"]) == 2
    assert all(group["message"] == "自定义正文" and group["identifiers"] == [] for group in custom["groups"])
    assert [item["form"] for item in custom["browser_collections"]] == ["延误", "取消率", "投诉"]
    assert service.identifier_calls == []


def test_ai_copy_reuses_shared_generator_and_never_returns_or_persists_credentials(service):
    calls = []
    def generate(form, ids, key, *, product_loader):
        calls.append((form, ids, key, product_loader(ids)))
        return {"message": "产品编号：" + "、".join(ids) + "\n请人工复核"}
    service.copy_generator = generate
    service.products_loader = lambda identity, group: [{"product_id": value, "title": "通用商品", "description": "事实"} for value in group["identifiers"]]
    plan = service.plan(service.user, params(appeal_copy_mode="AI话术模式"))
    assert [len(group["identifiers"]) for group in plan["groups"]] == [3, 3, 3, 3, 1]
    request = {"task_id": plan["task_id"], "group_id": plan["groups"][0]["group_id"]}
    first = service.copy(service.user, request)
    assert first == service.copy(service.user, request)
    assert len(calls) == 1 and calls[0][2] == "saved-account-key"
    serialized = json.dumps(plan) + service._path(plan["task_id"]).read_text()
    assert "saved-account-key" not in serialized and "oauth-never-return" not in serialized


def test_ai_reputation_forms_use_normal_copy_without_model_key(service):
    service.api_key = lambda user: ""
    plan = service.plan(service.user, params(forms=["投诉"], appeal_copy_mode="AI话术模式"))
    assert plan["browser_collections"] == [{"site_id": "MLM", "form": "投诉"}]
    with pytest.raises(appeal.AppealError, match="阿里云百炼"):
        service.plan(service.user, params(appeal_copy_mode="AI话术模式"))


def test_uncollected_reputation_data_cannot_be_reported_completed_or_skipped(service):
    plan = service.plan(service.user, params(forms=["延误"]))
    assert plan["groups"] == []
    with pytest.raises(appeal.AppealError, match="采集"):
        service.finish(service.user, {"task_id": plan["task_id"], "status": "completed"})
    with pytest.raises(appeal.AppealError, match="上一轮"):
        service.plan(service.user, {"task_id": plan["task_id"], "round_number": 2})
    ready = service.plan(service.user, {"task_id": plan["task_id"], "reputation_identifiers": {"MLM": {"延误": []}}})
    assert ready["browser_collections"] == []
    assert service.finish(service.user, {"task_id": plan["task_id"], "status": "completed"})["status"] == "completed"
    assert len(service.written) == 1 and service.written[0]["execution"]["status"] == "no_data"
    service.finish(service.user, {"task_id": plan["task_id"], "status": "completed"})
    assert len(service.written) == 1


def test_task_identity_permissions_and_issued_ids_are_immutable(service):
    plan = service.plan(service.user, params(forms=["投诉"]))
    with pytest.raises(appeal.AppealError, match="本人"):
        service.status({**service.user, "id": 8}, plan["task_id"])
    with pytest.raises(appeal.AppealError, match="不一致"):
        service.plan(service.user, {"task_id": plan["task_id"], "seller_id": "200"})
    with pytest.raises(appeal.AppealError, match="任务参数"):
        service.plan(service.user, {"task_id": plan["task_id"], "sites": ["MLB"]})
    with pytest.raises(appeal.AppealError, match="未选择"):
        service.plan(service.user, {"task_id": plan["task_id"], "reputation_identifiers": {"MLB": {"投诉": ["123456"]}}})
    service.plan(service.user, {"task_id": plan["task_id"], "reputation_identifiers": {"MLM": {"投诉": ["123456"]}}})
    with pytest.raises(appeal.AppealError, match="不能修改"):
        service.plan(service.user, {"task_id": plan["task_id"], "reputation_identifiers": {"MLM": {"投诉": ["654321"]}}})
    service.rows[0]["site_settings"][0]["appeal_enabled"] = False
    with pytest.raises(appeal.AppealError, match="关闭"):
        service.status(service.user, plan["task_id"])


def test_results_share_agent_record_fields_and_are_idempotent_after_restart(service):
    plan = service.plan(service.user, params())
    group = plan["groups"][0]
    payload = {"task_id": plan["task_id"], "group_id": group["group_id"],
               "shop_name": "伪造其他店铺", "identifiers": ["MLB99"],
               "log_records": [{"event": "send_appeal", "message": group["message"],
                                "response": "正在复核", "extra": {"ids": ["MLB99"]},
                                "chat": [{"id": "1", "role": "user", "text": group["message"]}]},
                               {"event": "group_result", "message": group["message"], "response": "正在复核",
                                "extra": {"result": {"status": "sent", "sent": True, "acknowledged": True}}}],
               "execution": {"status": "sent", "sent": True, "acknowledged": True, "response": "正在复核"}}
    first = service.result(service.user, payload)
    second = service.result(service.user, payload)
    assert first == second and len(service.written) == 1
    record = first["record"]
    assert record["shop_name"] == "当前店铺" and record["site"] == "墨西哥"
    assert record["identifiers"] == group["identifiers"]
    assert record["appeal_content"] == group["message"] and record["ai_replies"] == ["正在复核"]
    assert record["executor"]["execution_target"] == "browser_extension"
    assert record["execution"]["metrics"]["groups"] == 1
    assert record["execution"]["metrics"]["sent_confirmed"] == 1
    assert record["success_ids"] == [] and record["failed_ids"] == []
    remaining = service.plan(service.user, {"task_id": plan["task_id"]})
    assert group["group_id"] not in {pending["group_id"] for pending in remaining["groups"]}
    with pytest.raises(appeal.AppealError, match="分组不存在"):
        service.result(service.user, {**payload, "group_id": "0" * 32})
    recreated = appeal.AppealService(scoped_tokens=service.scoped_tokens, token_loader=service.token_loader,
        api_key=service.api_key, record_writer=service.record_writer, root=service.root)
    assert recreated.result(service.user, payload) == first and len(service.written) == 1


def test_next_round_refreshes_only_bound_store_and_requires_previous_results(service):
    plan = service.plan(service.user, params(loop_count=1))
    with pytest.raises(appeal.AppealError, match="超出"):
        service.plan(service.user, {"task_id": plan["task_id"], "round_number": 2})
    plan = service.plan(service.user, params())
    with pytest.raises(appeal.AppealError, match="上一轮"):
        service.plan(service.user, {"task_id": plan["task_id"], "round_number": 2})
    for group in plan["groups"]:
        service.result(service.user, {"task_id": plan["task_id"], "group_id": group["group_id"],
                                    "execution": {"status": "no_data"}})
    next_plan = service.plan(service.user, {"task_id": plan["task_id"], "round_number": 2})
    assert next_plan["round_number"] == 2
    assert all(token_id == 11 for token_id, _, _ in service.identifier_calls)


def test_busy_interprocess_lock_does_not_mutate_task(service):
    plan = service.plan(service.user, params())
    original = service._path(plan["task_id"]).read_text()
    from bit.bit_runtime_lock import InterProcessLock
    lock = InterProcessLock("browser_extension_appeal_" + plan["task_id"])
    assert lock.acquire()
    try:
        with pytest.raises(appeal.AppealError, match="另一个请求"):
            service.plan(service.user, {"task_id": plan["task_id"]})
    finally:
        lock.release()
    assert service._path(plan["task_id"]).read_text() == original


def test_live_product_collection_targets_only_bound_token_site(service, monkeypatch):
    from bit import mercado_infraction_sync, bit_db_api
    calls = []
    def live(targets, **kwargs):
        calls.append((targets, kwargs))
        return {"data": [
            {"站点": "MLM", "编号": "MLM1", "类型": "侵权", "侵权原因": "Counterfeit product"},
            {"站点": "MLM", "编号": "MLM2", "类型": "权利人举报", "侵权原因": "Counterfeit product"},
            {"站点": "MLM", "编号": "MLM3", "类型": "侵权", "侵权原因": "The product is prohibited."},
            {"站点": "MLM", "编号": "MLM4", "类型": "侵权", "侵权原因": "The product's brand is not generic."},
            {"站点": "MLB", "编号": "MLB5", "类型": "侵权", "侵权原因": "Counterfeit product"}], "failed_stores": []}
    monkeypatch.setattr(mercado_infraction_sync, "collect_live_detection_infractions", live)
    identity = service.context(service.user, {"seller_id": "100"})["identity"]
    assert appeal.store_identifiers(identity, "侵权", "MLM") == ["MLM1"]
    assert calls == [([{"token_id": 11, "name": "当前店铺", "aliases": ["当前店铺"], "site_ids": ["MLM"]}],
                      {"recent_days": 100, "max_workers": 1})]
    monkeypatch.setattr(bit_db_api, "list_mercado_prohibited_listings", lambda **kwargs: calls.append(kwargs) or {
        "rows": [{"site_id": "MLM", "item_id": "MLM9"}, {"site_id": "MLB", "item_id": "MLB9"}], "pages": 1})
    assert appeal.store_identifiers(identity, "禁限售", "MLM") == ["MLM9"]
    assert calls[-1]["token_id"] == 11 and calls[-1]["site_id"] == "MLM"


def test_database_record_read_preserves_immutable_scope_metadata(monkeypatch):
    from bit import bit_mysql
    record = {"token_id": 0, "binding_mode": "browser", "organization_key": "tenant-a", "seller_id": "100", "task_id": "task", "user_id": 7}
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def execute(self, *_args): pass
        def fetchall(self): return [{"id": 55, "raw_json": json.dumps(record)}]
    class Connection:
        def cursor(self): return Cursor()
        def close(self): pass
    monkeypatch.setattr(bit_mysql, "initialize_appeal_storage", lambda: None)
    monkeypatch.setattr(bit_mysql, "_appeal_connection", Connection)
    row = bit_mysql.get_ai_appeal_records()["rows"][0]
    assert all(row[key] == value for key, value in record.items())


def test_extension_routes_require_permission_and_use_bearer_identity(service, monkeypatch, tmp_path):
    from erp.ai_weight_price import service as weight_price
    original_service = weight_price.Service
    monkeypatch.setenv("BIT_RUNTIME_ROLE", "client")
    monkeypatch.setattr(weight_price, "Service", lambda *args, **kwargs: original_service(
        tmp_path / "ai-weight-price", storage_backend="sqlite", migrate_legacy_state=False))
    from bit import bit_interface as workbench
    workbench.app.config.update(TESTING=True)
    monkeypatch.setattr(workbench, "_browser_extension_appeal_service", lambda: service)
    monkeypatch.setattr(workbench, "_browser_extension_user_from_token", lambda token: service.user if token == "test" else None)
    monkeypatch.setattr(workbench, "_workbench_billing_blocked", lambda user: None)
    monkeypatch.setattr(workbench, "workbench_user_has_permission", lambda user, permission: False)
    client = workbench.app.test_client()
    headers = {"Authorization": "Bearer test"}
    assert client.post("/api/browser-extension/appeal/context", json={"seller_id": "100"}).status_code == 401
    assert client.post("/api/browser-extension/appeal/context", headers=headers, json={"seller_id": "100"}).status_code == 403
    monkeypatch.setattr(workbench, "workbench_user_has_permission", lambda user, permission: permission == "appeal.execute")
    response = client.post("/api/browser-extension/appeal/prepare", headers=headers, json=params())
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    task_id = response.get_json()["data"]["task_id"]
    assert client.get("/api/browser-extension/appeal/status?task_id=" + task_id, headers=headers).status_code == 200
    from flask import request
    original_loader = service.identifiers_loader
    def contextual_loader(*args):
        assert request.path == "/api/browser-extension/appeal/plan-async"
        return original_loader(*args)
    service.identifiers_loader = contextual_loader
    receipt = client.post("/api/browser-extension/appeal/plan-async", headers=headers,
                          json=params(request_id="route-preparation"))
    assert receipt.status_code == 200
    assert receipt.get_json()["data"]["state"] == "preparing"
    for _ in range(200):
        progress = client.post("/api/browser-extension/appeal/plan-status", headers=headers,
                               json={"request_id": "route-preparation"})
        assert progress.status_code == 200
        if progress.get_json()["data"]["state"] == "ready":
            break
        time.sleep(.01)
    else:
        pytest.fail("async route did not complete")
    service.user["access_version"] = 0
    assert client.post("/api/browser-extension/appeal/context", headers=headers, json={"seller_id": "100"}).status_code == 401
    assert client.get("/api/browser-extension/appeal/status?task_id=" + task_id, headers=headers).status_code == 401


def browser_params(**extra):
    return params(context={"binding_mode": "browser", "seller_id": "999", "nickname": "vngbjkk",
                           "sites": [{"site_id": "MLM", "seller_id": "998"}]},
                  seller_id="999", **extra)


def browser_only(service):
    def forbidden(*args):
        pytest.fail("browser runs must not access ERP OAuth stores or credentials")
    service.scoped_tokens = service.token_loader = service.accounts_loader = forbidden
    service.identifiers_loader = service.products_loader = forbidden
    return service


def test_browser_run_collects_all_forms_without_any_oauth_access(service):
    browser_only(service)
    plan = service.plan(service.user, browser_params(forms=list(appeal.FORMS)))
    assert plan["shop"] == {"id": 0, "name": "vngbjkk", "seller_id": "999"}
    assert plan["identity"]["allowed_seller_ids"] == ["998", "999"]
    assert plan["groups"] == []
    assert [c["form"] for c in plan["browser_collections"]] == list(appeal.FORMS)
    plan = service.plan(service.user, {"task_id": plan["task_id"], "browser_identifiers": {
        "MLM": {"侵权": ["MLM1234567"], "禁限售": ["MLM1234568"],
                "延误": {"pack_ids": ["2000000012345678"]}, "取消率": [], "投诉": []}}})
    assert plan["browser_collections"] == []
    assert [g["form"] for g in plan["groups"]] == ["延误", "侵权", "禁限售"]
    assert service.status(service.user, plan["task_id"])["shop"]["id"] == 0
    service.finish(service.user, {"task_id": plan["task_id"], "status": "stopped"})
    assert all(r["token_id"] == 0 and r["user_id"] == 7 for r in service.written)


def test_browser_tasks_remain_bound_to_user_organization_sites_and_ids(service):
    browser_only(service)
    plan = service.plan(service.user, browser_params())
    task_id = plan["task_id"]
    with pytest.raises(appeal.AppealError, match="本人创建"):
        service.status({**service.user, "id": 8}, task_id)
    with pytest.raises(appeal.AppealError, match="企业已变更"):
        service.status({**service.user, "organization_key": "tenant-b"}, task_id)
    with pytest.raises(appeal.AppealError, match="站点"):
        service.plan(service.user, {"task_id": task_id, "browser_identifiers": {"MLB": {"侵权": ["MLB1234567"]}}})
    with pytest.raises(appeal.AppealError, match="站点无效"):
        service.plan(service.user, {"task_id": task_id, "browser_identifiers": {"MLM": {"侵权": ["MLB1234567"]}}})
    with pytest.raises(appeal.AppealError, match="尚未完成采集"):
        service.finish(service.user, {"task_id": task_id})
    assert service.written == []


def test_browser_ai_copy_uses_only_task_bound_page_product_facts(service):
    browser_only(service)
    plan = service.plan(service.user, browser_params(appeal_copy_mode="AI话术模式"))
    task_id = plan["task_id"]
    plan = service.plan(service.user, {"task_id": task_id, "browser_identifiers": {"MLM": {"侵权": ["MLM1234567"]}}})
    request = {"task_id": task_id, "group_id": plan["groups"][0]["group_id"]}
    with pytest.raises(appeal.AppealError, match="完整商品标题"):
        service.copy(service.user, request)
    facts = {"MLM1234567": {"title": "Plain cotton hat", "description": ""}}
    service.plan(service.user, {"task_id": task_id, "browser_products": facts})
    def generate(form, ids, key, *, product_loader):
        assert product_loader(ids) == [{"product_id": "MLM1234567", **facts["MLM1234567"]}]
        return {"message": "产品编号：MLM1234567，请复核"}
    service.copy_generator = generate
    assert "MLM1234567" in service.copy(service.user, request)["message"]
    with pytest.raises(appeal.AppealError, match="不能修改"):
        service.plan(service.user, {"task_id": task_id, "browser_products": {"MLM1234567": {"title": "other"}}})
    with pytest.raises(appeal.AppealError, match="不属于本轮"):
        service.plan(service.user, {"task_id": task_id, "browser_products": {"MLM0000000": {"title": "other"}}})


def test_browser_empty_round_can_finish_without_oauth_and_records_no_data(service):
    browser_only(service)
    plan = service.plan(service.user, browser_params())
    service.plan(service.user, {"task_id": plan["task_id"], "browser_identifiers": {"MLM": {"侵权": []}}})
    result = service.finish(service.user, {"task_id": plan["task_id"]})
    assert result["status"] == "completed"
    assert len(service.written) == 1
    assert service.written[0]["shop_name"] == "vngbjkk"
    assert service.written[0]["token_id"] == 0


def test_browser_records_are_visible_only_to_creator_in_same_organization(service, monkeypatch):
    from bit import bit_interface as workbench
    monkeypatch.setattr(workbench.bit_db_api, "list_mercado_store_tokens", lambda: {"rows": []})
    row = {"id": 1, "token_id": 0, "binding_mode": "browser", "organization_key": "tenant-a", "user_id": 7,
           "shop_name": "vngbjkk"}
    rows = [row, {**row, "id": 2, "user_id": 8}, {**row, "id": 3, "organization_key": "tenant-b"},
            {**row, "id": 4, "binding_mode": "oauth"}, {**row, "id": 5, "token_id": 11}]
    assert workbench._filter_store_rows_for_user({"rows": rows}, service.user)["rows"] == [row]
