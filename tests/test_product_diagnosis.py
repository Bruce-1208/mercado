from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import Flask

from bit import product_diagnosis as service
from bit.product_diagnosis_routes import register_routes, validate_payload
from erp.mercadolibre_diagnosis_rules import evaluate
from erp.mercadolibre_diagnosis_store import DiagnosisStore

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def base_item(link_id=1, token_id=1):
    return {"link_id": link_id, "token_id": token_id, "site_id": "MLM", "item_id": f"MLM{link_id}", "title": "测试商品", "store_name": "测试店"}


def good_sources():
    return {"item": {"status": "ok", "data": {"status": "active", "available_quantity": 3}},
            "quality": {"status": "ok", "data": {"score": 85, "level": "Good", "buckets": []}},
            "experience": {"status": "ok", "data": {"reputation": {"color": "green", "value": 100}}}}


def test_nested_quality_rules_preserve_platform_evidence_and_deduplicate():
    sources = good_sources()
    rule = {"key": "HAS_GTIN", "status": "PENDING", "mode": "WARNING",
            "wordings": {"title": "<b>Falta código</b>", "link": "javascript:alert(1)"}}
    sources["quality"]["data"]["buckets"] = [{"variables": [{"rules": [rule, rule, {**rule, "status": "COMPLETED"}]}]}]
    row = evaluate(base_item(), sources, checked_at=NOW)
    assert row["risk"] == "medium"
    assert row["issue_count"] == 1
    assert row["issues"][0]["evidence"] == "Falta código"
    assert row["issues"][0]["official_url"] == ""
    assert row["quality_score"] == 85


def test_missing_source_cannot_produce_a_healthy_diagnosis():
    sources = good_sources()
    sources["experience"] = {"status": "restricted"}
    row = evaluate(base_item(), sources)
    assert row["risk"] == "pending"
    assert row["data_status"] == "partial"
    sources["item"]["data"]["available_quantity"] = 0
    row = evaluate(base_item(), sources)
    assert row["risk"] == "high"
    assert row["issues"][0]["source"] == "local_rule"
    sources["item"] = {"status": "error"}
    assert evaluate(base_item(), sources)["issues"] == []


def test_shopping_experience_is_not_guessed_from_score():
    sources = good_sources()
    sources["experience"]["data"] = {"reputation": {"color": "red", "value": 30},
        "title": {"text": "Problemas del producto"}, "freeze": {"text": "Sin penalización"},
        "reasoning": {"subtitles": [{"text": "Wrong size"}]},
        "recommendations": {"subtitles": [{"text": "Check size chart"}]}}
    row = evaluate(base_item(), sources)
    assert row["risk"] == "high"
    assert row["experience_score"] == 30
    issue = row["issues"][0]
    assert "Wrong size" in issue["evidence"]
    assert "Check size chart" in issue["official_suggestion"]
    assert issue["freeze_notice"] == "Sin penalización"


class Client:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        result = self.payloads[path]
        if isinstance(result, Exception):
            raise result
        return result


def test_user_product_uses_new_resource_and_local_locale():
    client = Client({"/marketplace/items/MLM1": {"id": "MLM1", "status": "active", "available_quantity": 3, "user_product_id": "MLMU25"},
        "/item/MLM1/performance": {"score": 95, "level": "Good"},
        "/marketplace/user_products/MLMU25/purchase_experience": {"up_id": "MLMU25", "reputation": {"value": 100, "color": "green"}}})
    row = service.collect_product(client, base_item(), {"id": 1})
    assert row["risk"] == "healthy"
    assert client.calls[-1][2]["params"] == {"locale": "es_MX"}


@pytest.mark.parametrize("value,expected_status", [(100, "ok"), (0, "ok"), (-1, "no_data")])
def test_user_product_array_matches_up_identity(value, expected_status):
    client = Client({
        "/marketplace/items/MLM1": {"id": "MLM1", "status": "active", "available_quantity": 3, "user_product_id": "MLMU25"},
        "/item/MLM1/performance": {"score": 95},
        "/marketplace/user_products/MLMU25/purchase_experience": [
            {"up_id": "MLBU25", "reputation": {"value": 30, "color": "red"}},
            {"up_id": "MLMU25", "item_id": "MLM2", "reputation": {"value": 30, "color": "red"}},
            {"up_id": "MLMU25", "reputation": {"value": value, "color": "green"}},
        ],
    })
    row = service.collect_product(client, base_item(), {})
    assert row["sources"]["experience"]["status"] == expected_status
    assert row["experience_score"] == (value if value >= 0 else None)
    assert not row["issues"]


def test_user_product_array_never_uses_another_products_score():
    client = Client({
        "/marketplace/items/MLM1": {"id": "MLM1", "status": "active", "available_quantity": 3, "user_product_id": "MLMU25"},
        "/item/MLM1/performance": {"score": 95},
        "/marketplace/user_products/MLMU25/purchase_experience": [
            {"up_id": "MLMU26", "reputation": {"value": 100, "color": "green"}},
        ],
    })
    row = service.collect_product(client, base_item(), {})
    assert row["experience_score"] is None
    assert row["risk"] == "pending"


@pytest.mark.parametrize("error,status,message", [
    ("失败 (404): Not found entity: User Product performance not found", "no_data", "平台尚未生成此商品的刊登质量评分"),
    ("失败 (HTTP 404): not_found", "no_data", "平台尚未生成此商品的刊登质量评分"),
    ("失败 (400): Entity not calculated: Product items are not supported", "unsupported", "此商品模型不支持该接口"),
    ("失败 (HTTP 400): Entity not calculated: Product items are not supported", "unsupported", "此商品模型不支持该接口"),
    ("失败 (HTTP 403): denied", "restricted", "店铺模式或应用权限不支持此接口"),
    ("access token 已失效，且未配置完整的 refresh token/client id/client secret", "error", "店铺授权已失效，请刷新授权后重新诊断"),
])
def test_actual_api_errors_keep_specific_safe_reason(error, status, message):
    path = "/item/MLM1/performance"
    client = Client({path: RuntimeError(error + " SECRET")})
    assert service._read(client, path) == {"status": status, "message": message}


def test_legacy_experience_selects_exact_local_item_and_keeps_sample_insufficiency():
    client = Client({"/marketplace/items/MLM1": {"id": "MLM1", "status": "active", "cbt_item_id": "CBT10"},
        "/item/MLM1/performance": {"score": 80},
        "/marketplace/items/CBT10/purchase_experience": [
            {"item_id": "MLB2", "reputation": {"value": 10, "color": "red"}},
            {"item_id": "MLM1", "reputation": {"value": -1, "color": "gray"}}]})
    row = service.collect_product(client, base_item(), {})
    assert row["experience_score"] is None
    assert row["sources"]["experience"]["status"] == "no_data"
    assert not row["issues"]


def test_failed_sources_never_expose_exception_credentials():
    client = Client({"/marketplace/items/MLM1": RuntimeError("secret_token=TOKEN"),
                     "/item/MLM1/performance": RuntimeError("(403) forbidden TOKEN")})
    row = service.collect_product(client, base_item(), {})
    assert row["risk"] == "pending"
    assert "TOKEN" not in str(row)


def test_missing_inventory_and_mismatched_experience_cannot_be_healthy():
    client = Client({"/marketplace/items/MLM1": {"id": "MLM1", "status": "active", "cbt_item_id": "CBT10"},
        "/item/MLM1/performance": {"score": 90},
        "/marketplace/items/CBT10/purchase_experience": {"item_id": "MLB2", "reputation": {"value": 100, "color": "green"}}})
    row = service.collect_product(client, base_item(), {})
    assert row["sources"]["item"]["status"] == "partial"
    assert row["sources"]["experience"]["status"] == "no_data"
    assert row["risk"] == "pending"


def test_store_scope_pagination_category_and_stale_normality(tmp_path):
    store = DiagnosisStore(tmp_path / "diagnosis.sqlite3")
    sources = good_sources()
    sources["item"]["data"]["available_quantity"] = 0
    for index in range(1, 56):
        store.save(evaluate(base_item(index), sources))
    store.save(evaluate(base_item(99, 2), sources))
    store.save(evaluate(base_item(100), good_sources(), checked_at=datetime.now(timezone.utc) - timedelta(days=2)))
    result = store.list_page([1], category="inventory", page=2, page_size=50)
    assert len(result["rows"]) == 5
    assert result["total"] == 55
    assert result["summary"]["high"] == 55
    result = store.list_page([1], risk="pending")
    assert [v["link_id"] for v in result["rows"]] == [100]
    assert store.get(100, [1])["stale"]
    with pytest.raises(KeyError):
        store.get(99, [1])
    assert store.list_page([])["rows"] == []


def test_resumable_run_and_scoped_retirement(tmp_path):
    store = DiagnosisStore(tmp_path / "diagnosis.sqlite3")
    store.save(evaluate(base_item(1), good_sources()))
    store.save(evaluate({**base_item(2), "site_id": "MLB", "item_id": "MLB2"}, good_sources()))
    run_id = store.create_run([{"token_id": 1, "site_id": "MLM"}], "operator")
    with pytest.raises(ValueError, match="正在执行"):
        store.create_run([{"token_id": 2}], "other")
    store.save(evaluate(base_item(3), good_sources()), run_id)
    restarted = DiagnosisStore(store.path)
    run = restarted.next_run()
    assert (run["cursor_token"], run["cursor_link"], run["processed"]) == (1, 3, 1)
    restarted.save(evaluate(base_item(3), good_sources()), run_id)
    assert restarted.next_run()["processed"] == 1
    restarted.finish_scope(run_id, 1, "MLM")
    assert {row["link_id"] for row in restarted.list_page([1])["rows"]} == {2, 3}
    assert restarted.runs([2]) == []


def test_worker_resumes_without_skipping_items_and_fails_closed_for_revoked_store(tmp_path, monkeypatch):
    from bit import clearance_sources, sync_capacity
    store = DiagnosisStore(tmp_path / "diagnosis.sqlite3")
    run_id = store.create_run([{"token_id": 1, "site_id": "MLM"}], "operator")
    store.save(evaluate(base_item(3), good_sources()), run_id)
    monkeypatch.setattr(service, "current_stores", lambda ids: {1: {"id": 1}})
    monkeypatch.setattr(clearance_sources, "_client_for_store", lambda token: (object(), {}))
    cursors = []
    def links(token, site, after):
        cursors.append(after)
        yield base_item(4)
    monkeypatch.setattr(service, "load_links", links)
    monkeypatch.setattr(service, "collect_product", lambda *args: evaluate(base_item(4), good_sources()))
    monkeypatch.setattr(sync_capacity, "run_store", lambda name, token, action: action())
    service.run_scan(store.next_run(), store)
    assert cursors == [3]
    assert store.runs([1])[0]["status"] == "succeeded"
    assert store.runs([1])[0]["processed"] == 2
    second = store.create_run([{"token_id": 1}], "operator")
    monkeypatch.setattr(service, "current_stores", lambda ids: {})
    service.run_scan(store.next_run(), store)
    assert store.runs([1])[0]["status"] == "failed"
    assert store.list_page([1])["total"] == 2


def test_long_scan_renews_expiring_token_before_next_product(tmp_path, monkeypatch):
    from bit import clearance_sources, sync_capacity
    store = DiagnosisStore(tmp_path / "diagnosis.sqlite3")
    store.create_run([{"token_id": 1}], "operator")
    monkeypatch.setattr(service, "current_stores", lambda ids: {1: {"id": 1}})
    clients = iter([("old", {"expires_at": datetime.now() - timedelta(minutes=1)}),
                    ("renewed", {"expires_at": datetime.now() + timedelta(hours=6)})])
    monkeypatch.setattr(clearance_sources, "_client_for_store", lambda token: next(clients))
    monkeypatch.setattr(service, "load_links", lambda *args: iter([base_item(1), base_item(2)]))
    used = []
    def collect(client, row, summary):
        used.append(client)
        return evaluate(row, good_sources())
    monkeypatch.setattr(service, "collect_product", collect)
    monkeypatch.setattr(sync_capacity, "run_store", lambda name, token, action: action())
    service.run_scan(store.next_run(), store)
    assert used == ["renewed", "renewed"]
    assert store.runs([1])[0]["status"] == "succeeded"


def test_today_task_idempotency_and_internal_deadline(tmp_path, monkeypatch):
    from bit import bit_db_api
    calls = []
    monkeypatch.setattr(bit_db_api, "upsert_mercado_operator_task", lambda row: calls.append(row) or {"id": 5})
    sources = good_sources()
    sources["item"]["data"]["available_quantity"] = 0
    row = evaluate(base_item(), sources)
    service.add_today_task(row, {"organization_key": "enterprise-a"}, "operator")
    service.add_today_task(row, {"organization_key": "enterprise-a"}, "operator")
    assert calls[0]["task_key"] == calls[1]["task_key"]
    assert calls[0]["source"] == "product_diagnosis"
    assert "非平台截止时间" in calls[0]["details"]
    row["stale"] = True
    with pytest.raises(ValueError):
        service.add_today_task(row, {"organization_key": "enterprise-a"}, "operator")


def test_one_failed_store_does_not_block_other_stores_or_delete_previous_results(tmp_path, monkeypatch):
    from bit import clearance_sources, sync_capacity
    store = DiagnosisStore(tmp_path / "diagnosis.sqlite3")
    store.save(evaluate(base_item(1), good_sources()))
    run_id = store.create_run([{"token_id": 1}, {"token_id": 2}], "operator")
    monkeypatch.setattr(service, "current_stores", lambda ids: {v: {"id": v} for v in ids})
    def client(token):
        if token == 1:
            raise RuntimeError('authorization unavailable')
        return object(), {}
    monkeypatch.setattr(clearance_sources, "_client_for_store", client)
    monkeypatch.setattr(service, "load_links", lambda token,site,after: iter([base_item(2, token)]))
    monkeypatch.setattr(service, "collect_product", lambda client,row,summary: evaluate(row, good_sources()))
    monkeypatch.setattr(sync_capacity, "run_store", lambda name,token,action: action())
    service.run_scan(store.next_run(), store)
    saved = store.runs([1, 2])[0]
    assert saved["id"] == run_id
    assert saved["status"] == "partial"
    assert saved["scope_errors"][0]["token_id"] == 1
    assert {v['link_id'] for v in store.list_page([1, 2])["rows"]} == {1, 2}


@pytest.fixture
def route_client(monkeypatch):
    from bit import bit_db_api
    app = Flask(__name__, template_folder="../bit/templates", static_folder="../bit/static")
    permission = {"product_diagnosis.view", "product_diagnosis.execute", "tasks.execute"}
    user = {"username": "operator", "permissions": permission}
    calls = []
    def dispatch(op, payload, **kwargs):
        calls.append((op, payload, kwargs))
        return {"operation": op}
    monkeypatch.setattr(bit_db_api, "product_diagnosis", dispatch)
    register_routes(app, login_required=lambda func: func, internal_api_required=lambda func: func,
                    current_user=lambda: user, authorized_token_ids=lambda: {1},
                    has_permission=lambda u,p: p in u["permissions"], reject_client_mode=lambda: None)
    return app.test_client(), permission, calls


def test_route_permissions_and_authorized_scope_pass_through(route_client):
    client, permissions, calls = route_client
    assert client.get("/api/product-diagnosis?token_id=1").status_code == 200
    assert calls[0][2]["allowed_token_ids"] == [1]
    assert client.get("/api/product-diagnosis?page_size=201").status_code == 400
    assert client.get("/api/product-diagnosis?token_id=-1").status_code == 400
    permissions.remove("product_diagnosis.execute")
    assert client.post("/api/product-diagnosis/start", json={}).status_code == 403
    assert client.post("/api/product-diagnosis/1/today").status_code == 403
    permissions.remove("product_diagnosis.view")
    assert client.get("/product-diagnosis").status_code == 403
    assert client.get("/api/product-diagnosis").status_code == 403


def test_server_dispatch_rejects_cross_store_and_filters_disabled_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("BIT_PRODUCT_DIAGNOSIS_DB_PATH", str(tmp_path / "diagnosis.sqlite3"))
    monkeypatch.setattr(service, "current_stores", lambda ids: {1: {"id": 1, "display_name": "本人店铺"}})
    store = DiagnosisStore()
    store.save(evaluate(base_item(1), good_sources()))
    store.save(evaluate(base_item(2, 2), good_sources()))
    assert service.dispatch("list", {}, allowed_token_ids=[1])["total"] == 1
    with pytest.raises(PermissionError):
        service.dispatch("start", {"token_ids": [2]}, allowed_token_ids=[1])
    with pytest.raises(KeyError):
        service.dispatch("detail", {"link_id": 2}, allowed_token_ids=[1])


def test_client_database_transport_does_not_create_local_store(monkeypatch):
    from bit import bit_db_api
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(bit_db_api, "_request", lambda *a,**k: calls.append((a,k)) or {})
    bit_db_api.product_diagnosis("start", {"token_ids": [1]}, allowed_token_ids=[], actor="operator")
    assert calls[0][0] == ("POST", "/api/db/product-diagnosis")
    assert calls[0][1]["json"]["allowed_token_ids"] == []
    assert calls[0][1]["max_attempts"] == 1


def test_worker_service_ownership_and_parameter_bounds():
    from bit.service_split import STATEFUL_PREFIXES
    from bit.service_topology import request_group
    assert any('/api/product-diagnosis'.startswith(v) for v in STATEFUL_PREFIXES)
    assert request_group('/api/product-diagnosis/start') == 'stores'
    assert request_group('/api/db/product-diagnosis') == 'stores'
    with pytest.raises(ValueError):
        validate_payload({"site_id": "../../other"})
    with pytest.raises(ValueError):
        validate_payload({"link_id": True})


def test_console_registration_permissions_and_today_entry():
    from bit import bit_interface
    paths = {str(rule) for rule in bit_interface.app.url_map.iter_rules()}
    assert {"/product-diagnosis", "/api/product-diagnosis", "/api/product-diagnosis/start", "/api/db/product-diagnosis"} <= paths
    assert bit_interface._required_workbench_permissions('/api/product-diagnosis/42', 'GET') == ('product_diagnosis.view',)
    assert bit_interface._required_workbench_permissions('/api/product-diagnosis/start', 'POST') == ('product_diagnosis.execute',)
    assert bit_interface._validate_workbench_permissions(['product_diagnosis.execute']) == ['product_diagnosis.execute', 'product_diagnosis.view']
