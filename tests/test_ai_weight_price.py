import copy
import csv
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from flask import Flask, jsonify

from erp.ai_weight_price.browser import CircuitOpen
from erp.ai_weight_price.config import Config, validate as validate_config
from erp.ai_weight_price.models import Models, parse_price, validate_weight
from erp.ai_weight_price.service import Service
from erp.ai_weight_price.store import CHINA, Store
from erp.ai_weight_price.web import create_blueprint

def validate(value):
    """Legacy consultation coverage; image-first behavior has dedicated tests."""
    return validate_config({"workflow_mode": "legacy_consult", **value})


@pytest.fixture
def service(tmp_path, monkeypatch):
    service = Service(tmp_path)
    service.config.save(validate({}))
    monkeypatch.setattr(service, "exchange_rate", lambda config: {"cny_per_usd": config.get("usd_cny_rate") or "7.2", "date": "2026-09-07", "source": "manual"})
    return service


def task(store, key="g1", merchant="m1", matched=False, **extra):
    store.add({"erp_goods_id": key, "title": "水杯", "erp_sku": "蓝色500ml一只",
               "reference_weight_g": "450", "erp_edit_url": "https://meli.zying.net/#/product/g1"})
    data = {"merchant_id": merchant, **extra}
    if matched:
        data.update(supplier_sku_id="s1", supplier_sku="蓝色500ml一只", supplier_url="https://detail.1688.com/offer/1.html",
                    cost_price="12.50", match_confidence=.97, match_evidence=[{"review": "matched"}])
    return store.update(key, **data)


def test_status_exposes_current_product_category_name(service):
    task(service.store, source_category="202170568", source_category_label="202170568")
    service.store.set_state("categories", [
        {"value": "202170568", "label": "家居 / 家电类", "name": "家电类"},
    ])
    service.store.set_state("run", {"run_id": "run-1", "current_task_id": "g1"})

    current = service.status()["current_product"]

    assert current == {
        "erp_goods_id": "g1",
        "title": "水杯",
        "zying_category_id": "202170568",
        "zying_category_name": "家居 / 家电类",
    }


@pytest.mark.parametrize("value", [0,.95,.9499,1,True,float("nan"),".96"])
def test_invalid_config_confidence(value):
    if value == .95 and type(value) is float:
        assert validate({"match_threshold": value})["match_threshold"] == .95
    else:
        with pytest.raises(ValueError):
            validate({"match_threshold": value})


def test_image_candidate_count_defaults_to_ten_and_remains_configurable():
    assert validate({})["max_candidates"] == 10
    assert validate({"max_candidates": 17})["max_candidates"] == 17
    with pytest.raises(ValueError):
        validate({"max_candidates": 21})


@pytest.mark.parametrize("setting,value", [("consult_interval_seconds",59),("max_waiting",3),("daily_limit",0),
                                          ("small_tolerance_g",51),("large_tolerance_g",31),
                                          ("poll_minutes",float("nan")),("phrases",["一样","一样"]),
                                          ("cdp_url","http://remote:9222"),("api_base_url","http://remote/v1")])
def test_config_cannot_bypass_limits(setting, value):
    with pytest.raises(ValueError):
        validate({setting: value})


@pytest.mark.parametrize("weight,ref,passes", [(500,550,True),(500,550.01,False),(500.01,530.01,True),
                                             (501,532,False),(450,400,True),(450,399,False),
                                             ("NaN",450,False),(0,450,False)])
def test_weight_boundaries(weight, ref, passes):
    record = {"weight_g": weight, "reference_weight_g": ref}
    if passes:
        assert validate_weight(record, validate({}))["passed"]
    else:
        with pytest.raises(ValueError):
            validate_weight(record, validate({}))


def test_weight_never_compares_with_itself():
    with pytest.raises(ValueError):
        validate_weight({"weight_g": 450}, validate({}))
    with pytest.raises(ValueError):
        validate_weight({"weight_g": 450, "reference_weight_g": 450}, validate({"reference_mode":"disabled"}))
    assert validate_weight({"weight_g":450,"measured_weight_g":480},validate({"reference_mode":"manual"}))["passed"]


@pytest.mark.parametrize("raw",["10-20","￥5起","2件 ¥3.2","¥12.345","NaN","0","9.9/件"])
def test_ambiguous_prices_are_not_cost(raw):
    with pytest.raises(ValueError):
        parse_price(raw)


def test_price_exact():
    assert parse_price("￥ 12.50 元") == "12.50"


def test_strict_matching_and_review_same_sku(monkeypatch):
    model = Models(validate({}), lambda *a: None)
    approved = {"same_product":True,"specs_confirmed":True,"sku_id":"s1","confidence":.96}
    assert model.accepted(approved)
    assert model.accepted({**approved, "confidence": .95})
    for confidence in (.9499,1.1,float("nan"),True,".99"):
        assert not model.accepted({**approved,"confidence":confidence})
    replies = iter([approved,{**approved,"sku_id":"s2"}])
    monkeypatch.setattr(model,"call",lambda *a:next(replies))
    result, evidence = model.match({"erp_sku":"blue","main_image_url":"https://example.com/a.jpg"},
                                  {"skus":[{"id":"s1","price":"12.50"}],"main_image_url":"https://example.com/b.jpg"})
    assert result is None and len(evidence) == 2


def test_image_match_scores_tolerate_common_json_serialization_quirks(monkeypatch):
    model = Models(validate({}), lambda *a: None)
    monkeypatch.setattr(model, "call", lambda *a: {
        "matches": [
            {"index": "1", "same_product": "true", "confidence": "96%", "reason": "同款"},
            {"index": "2", "same_product": False, "confidence": "0.2", "reason": "非同款"},
            {"index": "bad", "same_product": True, "confidence": 0.99},
        ]
    })
    approved, evidence = model.match_images(
        {"main_image_url": "https://example.com/target.jpg"},
        [
            {"url": "https://detail.1688.com/offer/1.html", "main_image_url": "https://example.com/1.jpg"},
            {"url": "https://detail.1688.com/offer/2.html", "main_image_url": "https://example.com/2.jpg"},
        ],
    )
    assert [item["url"] for item in approved] == ["https://detail.1688.com/offer/1.html"]
    assert len(evidence) == 2
    assert evidence[0]["review"]["confidence"] == .96


@pytest.mark.parametrize("answer,expected",[("450","450"),("0.5kg",None),("null",None),("450g",None),("400-500",None),("NaN",None)])
def test_model_weight_strict_output(monkeypatch,answer,expected):
    model=Models(validate({}),lambda *a:None)
    monkeypatch.setattr(model,"call",lambda *a:answer)
    assert model.weight("含包装450克") == expected


def test_persistent_dedup_resume_and_csv(tmp_path):
    store = Store(tmp_path)
    row=task(store,title='=HYPERLINK("bad")')
    store.exception("g1","人工处理")
    assert store.add({"erp_goods_id":"g1","title":"new"}) == 0
    again=Store(tmp_path)
    assert again.get("g1")["status"] == "exception"
    assert again.get("g1")["title"] == row["title"]
    exported=list(csv.DictReader(io.StringIO(again.csv().decode("utf-8-sig"))))
    assert exported[0]["title"].startswith("'=HYPERLINK")
    assert exported[0]["exception_reason"] == "人工处理"


def test_quota_dedup_interval_concurrency_and_next_day(service):
    s=service.store;c=validate({"daily_limit":2})
    one=task(s,"g1","m1");two=task(s,"g2","m2");three=task(s,"g3","m3")
    reserve=lambda row,now:s.reserve(row,c,"hello","https://air.1688.com/chat",[],now)
    assert reserve(one,1000)=="ok"
    assert reserve(two,1059)=="interval"
    assert reserve(two,1060)=="ok"
    assert reserve(three,1120)=="daily"
    s.update("g1",status="success")
    assert reserve(three,1000+86400)=="ok"
    dup=task(s,"g4","m1")
    assert reserve(dup,1000+86400+60)=="duplicate"
    # A separate database connection sees exactly the same blacklist.
    assert Store(s.root).quota(1000+86400)["today"] == 1


def test_atomic_reservation_cannot_exceed_waiting_limit(service):
    s=service.store;c=validate({})
    rows=[task(s,"g"+str(i),"m"+str(i)) for i in range(5)]
    for i in range(2):
        assert s.reserve(rows[i],c,"msg","https://air.1688.com/chat",[],1000+i*60)=="ok"
    assert s.reserve(rows[2],c,"msg","https://air.1688.com/chat",[],1200)=="waiting"
    s.update("g0",status="success");s.update("g1",status="success")
    def reserve(_):
        return Store(s.root).reserve(rows[2],c,"msg","https://air.1688.com/chat",[],1300)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(reserve,range(4)))
    assert results.count("ok")==1
    assert results.count("duplicate")==3


def test_circuit_persists_and_uncertain_actions_recover(service):
    s=service.store;c=validate({});row=task(s)
    assert s.reserve(row,c,"msg","https://air.1688.com/chat",[],1000)=="ok"
    task(s,"g2",stage="writing")
    s.recover()
    assert s.get("g1")["status"]==s.get("g2")["status"]=="exception"
    service.circuit(CircuitOpen("验证码"))
    row=task(s,"g3","m3")
    assert Store(s.root).reserve(row,c,"msg","url",[],2000)=="circuit"


def test_legacy_unknown_domain_false_pause_is_cleared_without_losing_current_item(tmp_path):
    store = Store(tmp_path)
    store.set_state("pipeline_current", {"scope": "selected-scope", "task_id": "848332340"})
    store.set_state("circuit", {"kind": "browser_attention",
                    "reason": "1688搜图进入登录或人机审核页面（未知域名）；请处理后返回控制台继续执行"})
    store.set_state("run", {"mode": "pipeline", "outcome": "blocked", "current_task_id": "848332340"})

    service = Service(tmp_path)

    assert service.store.state("circuit") is None
    assert service.store.state("pipeline_current")["task_id"] == "848332340"
    assert service.store.state("run")["outcome"] == "stopped"
    assert "开始按钮已恢复" in service.store.logs()[-1]["message"]


def test_agent_can_construct_api_service_without_remote_database_access(tmp_path, monkeypatch):
    from erp.ai_weight_price.store import RemoteStore

    def forbidden(*args, **kwargs):
        raise AssertionError("Agent import must not access the remote AI weight-price store")

    monkeypatch.setattr(RemoteStore, "_call", forbidden)
    service = Service(
        tmp_path,
        storage_backend="api",
        migrate_legacy_state=False,
    )

    assert service.store.backend == "api"


class FakeBrowser:
    def __init__(self):
        self.sent=[];self.written=[];self.reply=[];self.fail_write=False;self.risk=False
    def prepare_chat(self,task):
        return object(),"https://air.1688.com/chat/"+task["merchant_id"],[]
    def supplier_page(self,task):
        return ""
    def send(self,page,task,text):
        if self.risk:
            raise CircuitOpen("验证码")
        self.sent.append(text)
    def release(self,page):
        pass
    def replies(self,task):
        return self.reply
    def write(self,task,before_save):
        before_save({"net_income_usd":"10","weight_g":"400"})
        if self.fail_write:
            raise ValueError("保存失败")
        self.written.append(task)
        return {"net_income_usd":task["net_income_usd"],"weight_g":task["weight_g"]}


class FakeModel:
    def __init__(self,weight="450"):
        self.result=weight
    def weight(self,text):
        return self.result


def test_manual_execute_writes_operator_values_and_marks_manual_verification(service, monkeypatch):
    row = task(service.store, matched=True, weight_g="450", net_income_usd="9")

    class ManualBrowser:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write_patch(self, current, changes, before_save):
            before_save({"weight_g": "400", "net_income_usd": "8", "review_status": "待审核"})
            return {"weight_g": changes["weight_g"], "net_income_usd": changes["net_income_usd"],
                    "review_status": changes["review_status"]}

    monkeypatch.setattr(service, "require_login", lambda config: None)
    monkeypatch.setattr(service, "browser_factory", lambda *args: ManualBrowser())
    result = service.manual_execute("g1", {"weight_g": "480", "net_income_usd": "12", "note": "人工复核"}, "tester")

    assert result["status"] == "success"
    assert result["verification_mode"] == "manual"
    assert result["manual_verification"]["note"] == "人工复核"
    assert result["write_verified"] is True
    assert result["erp_after"] == {"weight_g": "480", "net_income_usd": "12", "review_status": "通过"}
    assert result["write_intent"]["verification_mode"] == "manual"


def test_manual_execute_locates_product_when_collector_has_no_edit_url(service, monkeypatch):
    task(service.store, matched=True, weight_g="450", net_income_usd="9")
    service.store.update("g1", erp_edit_url=None, source_page=2, source_index=7)
    received = []

    class ManualBrowser:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write_patch(self, current, changes, before_save):
            received.append(current)
            before_save({"weight_g": "400", "net_income_usd": "8", "review_status": "待审核"})
            return dict(changes)

    monkeypatch.setattr(service, "require_login", lambda config: None)
    monkeypatch.setattr(service, "browser_factory", lambda *args: ManualBrowser())

    result = service.manual_execute(
        "g1", {"weight_g": "480", "net_income_usd": "12", "note": "人工复核"}, "tester"
    )

    assert received[0]["erp_edit_url"] is None
    assert received[0]["source_page"] == 2
    assert received[0]["source_index"] == 7
    assert result["status"] == "success"


def test_flow_matched_send_poll_validate_save(service):
    row=task(service.store,matched=True);browser=FakeBrowser();model=FakeModel();config=validate({"writeback_enabled":True})
    service.process(row,browser,model,config)
    waiting=service.store.get("g1")
    assert waiting["status"]=="waiting_merchant_reply" and len(browser.sent)==1
    assert "蓝色500ml" in browser.sent[0]
    browser.reply=[{"id":"r1","text":"这一只带包装450g","at":waiting["sent_at"]+1}]
    service.poll(waiting,browser,model,config,waiting["deadline"])
    saved=service.store.get("g1")
    assert saved["status"]=="success" and saved["weight_g"]=="450"
    assert len(browser.written)==1 and saved["erp_before"]["net_income_usd"]=="10"
    assert saved["cost_price"] == "12.50" and saved["net_income_usd"] == "10"
    assert "net_income_usd" not in saved["write_intent"]


def test_selected_variant_final_price_is_used_instead_of_minimum(service):
    row = task(service.store, weight_g="450")
    browser = FakeBrowser()
    candidate = {"merchant_id": "m1", "url": "https://detail.1688.com/offer/1.html", "skus": [
        {"id": "cheap", "label": "small", "price": "1"},
        {"id": "s1", "label": "blue 500ml", "price": "22", "base_price_cny": "20", "variant_surcharge_cny": "2"}]}
    browser.candidates = lambda task: [candidate]
    model = FakeModel()
    model.match = lambda *args: ({**candidate, "selected_sku": candidate["skus"][1], "confidence": .97}, [{"same_product": True}])
    service.process(row, browser, model, validate({"writeback_enabled": True}))
    saved = service.store.get("g1")
    assert saved["status"] == "success"
    assert saved["cost_price"] == "22" and saved["net_income_usd"] == "10"
    assert saved["supplier_price_evidence"]["variant_surcharge_cny"] == "2"
    assert len(browser.written) == 1


def test_lower_calculated_net_income_keeps_original_and_only_writes_weight(service):
    row = task(service.store, matched=True, weight_g="450")
    browser = FakeBrowser()
    service.finish(row, browser, validate({"writeback_enabled": True, "usd_cny_rate": "7.2"}))
    saved = service.store.get("g1")
    assert saved["status"] == "success"
    assert saved["net_income_usd"] == "10"
    assert saved["weight_g"] == "450"
    assert saved["pricing"]["calculated_net_income_usd"] == "3"
    assert saved["pricing"]["net_income_writeback_usd"] == "10"
    assert saved["pricing"]["net_income_retained_original"] is True
    assert "net_income_usd" not in saved["write_intent"]
    assert "仅修改重量" in saved["pricing"]["net_income_adjustment"]
    assert saved["decision_reason"] == saved["pricing"]["net_income_adjustment"]


def test_exchange_failure_never_writes_and_edit_clears_old_conversion(service, monkeypatch):
    row = task(service.store, matched=True, weight_g="450", net_income_usd="999", pricing={"old": True})
    browser = FakeBrowser()
    def fail(config): raise RuntimeError("exchange rate unavailable")
    monkeypatch.setattr(service, "exchange_rate", fail)
    service.finish(row, browser, validate({"writeback_enabled": True}))
    saved = service.store.get("g1")
    assert saved["exception_reason"] == "美元成本换算失败" and saved["net_income_usd"] is None
    assert not browser.written
    service.store.update("g1", net_income_usd="2", pricing={"old": True})
    service.edit("g1", {"cost_price": "22"}, "tester")
    saved = service.store.get("g1")
    assert saved["cost_price"] == "22" and saved["pricing"] is None and saved["net_income_usd"] is None


@pytest.mark.parametrize("weight,reference,writeback,fail_write,reason",[
    (None,"450",True,False,"AI无法识别包装重量"),
    ("600","450",True,False,"重量校验误差超出阈值"),
    ("450",None,True,False,"重量校验误差超出阈值"),
    ("450","450",False,False,"校验通过，等待启用ERP回写后手动重试"),
    ("450","450",True,True,"ERP回写保存失败"),
])
def test_exception_branches_never_report_success(service,weight,reference,writeback,fail_write,reason):
    row=task(service.store,matched=True,reference_weight_g=reference)
    browser=FakeBrowser();browser.fail_write=fail_write;model=FakeModel(weight);config=validate({"writeback_enabled":writeback})
    service.process(row,browser,model,config)
    waiting=service.store.get("g1")
    browser.reply=[{"id":"r","text":"包装450g","at":waiting["sent_at"]+1}]
    service.poll(waiting,browser,model,config,waiting["deadline"])
    assert service.store.get("g1")["exception_reason"]==reason
    assert not browser.written


def test_timeout_checked_before_next_scheduled_poll(service):
    row=task(service.store,matched=True);b=FakeBrowser();c=validate({"poll_minutes":15,"timeout_minutes":1})
    service.process(row,b,FakeModel(),c)
    wait=service.store.get("g1")
    service.poll(wait,b,FakeModel(),c,wait["deadline"])
    assert service.store.get("g1")["exception_reason"]=="商家超时未回复重量咨询"


def test_retry_preserves_blacklist_and_raw_reply(service):
    row=task(service.store,matched=True);b=FakeBrowser();c=validate({})
    service.process(row,b,FakeModel(),c)
    service.store.exception("g1","商家超时未回复重量咨询")
    service.retry("g1")
    assert service.store.get("g1")["status"]=="waiting_merchant_reply"
    assert service.store.quota()["today"]==1
    service.store.exception("g1","AI无法识别包装重量")
    service.edit("g1",{"weight_g":"450","note":"人工核对"},"tester")
    service.retry("g1")
    assert service.store.get("g1")["status"]=="pending"
    service.process(service.store.get("g1"),b,FakeModel(),validate({"writeback_enabled":True}))
    assert len(b.sent)==1 and len(b.written)==1


def test_dom_preflight_no_browser_side_effect(service):
    with pytest.raises(ValueError,match="DOM"):
        service.preflight(validate({"supplier_auto_adapt": False}), "process")
    assert service.thread is None


def test_auto_adaptation_preflight_keeps_upload_writeback_and_key_checks(service, monkeypatch):
    config = validate({"api_base_url": "http://localhost:11434/v1"})
    service.preflight(config, "pipeline")
    config["selectors"]["image_search_upload"] = ""
    with pytest.raises(ValueError, match="image_search_upload"):
        service.preflight(config, "pipeline")
    config = validate({"api_base_url": "http://localhost:11434/v1", "writeback_enabled": True})
    with pytest.raises(ValueError, match="erp_edit_sku"):
        service.preflight(config, "pipeline")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr("erp.ai_weight_price.credentials.windows_user_environment", lambda name: "")
    with pytest.raises(ValueError, match="模型密钥"):
        service.preflight(validate({}), "process")


def test_supplier_adaptation_switch_requires_boolean():
    with pytest.raises(ValueError, match="自动适配"):
        validate({"supplier_auto_adapt": "true"})


def test_supplier_adaptation_failure_skips_item_and_preserves_report_reason(service, monkeypatch):
    from erp.ai_weight_price.supplier_adapter import SupplierAdaptationError
    monkeypatch.setattr("erp.ai_weight_price.service.socket.gethostname", lambda: "OPS-PC-01")
    class UnreadableSupplier:
        def candidates(self, row):
            raise SupplierAdaptationError("1688详情页自动适配失败：SKU缺少稳定ID")
    task(service.store)
    config = validate({})
    config["run_id"] = "adapt-failure"
    service.store.save_run({"run_id": config["run_id"]})
    service.complete_one("g1", UnreadableSupplier(), None, config)
    _, rows = service.store.run_report(config["run_id"])
    assert rows[0]["status"] == "skipped"
    assert rows[0]["execution_reason"].startswith("自动跳过异常：")
    assert rows[0]["exception_reason"].endswith("SKU缺少稳定ID")
    assert rows[0]["execution_terminal"] == "OPS-PC-01"
    assert any("SKU缺少稳定ID" in log["message"] for log in service.store.logs())


def test_mutations_locked_while_running(service):
    task(service.store);service.store.exception("g1","待复核")
    lock=service.lock()
    assert lock.acquire()
    try:
        with pytest.raises(ValueError,match="运行中"):
            service.edit("g1",{"weight_g":450},"tester")
        with pytest.raises(ValueError,match="运行中"):
            service.retry("g1")
    finally:
        lock.release()


@pytest.fixture
def client(service):
    app=Flask(__name__);app.config.update(TESTING=True)
    app.register_blueprint(create_blueprint(service))
    return app.test_client()


def test_api_visual_config_reports_and_invalid_requests(client,service):
    headers={"X-AWP-Request":"1"}
    page = client.get("/ai-weight-price")
    assert page.status_code==200
    assert page.text.count('class="login-box"') == 2
    assert 'id="open-supplier-login"' not in page.text
    assert 'id="terminate"' in page.text
    assert 'bestSupplierUrl' in page.text
    assert '打开最佳匹配' in page.text
    assert 'id="manual-execute"' in page.text
    assert 'id="detail-product-image"' in page.text
    assert '智赢分类 / 执行终端' in page.text
    assert 'id="detail-product-runtime"' in page.text
    assert 'renderDetailProduct(currentTask)' in page.text
    assert 'product-thumb:hover' in page.text
    assert '/manual-execute' in page.text
    status = client.get("/api/ai-weight-price/status").json
    assert status["counts"]["pending"] == 0
    assert status["execution_terminal"] == status["computer"]
    result=client.put("/api/ai-weight-price/config",json={"daily_limit":12},headers=headers)
    assert result.status_code==200 and result.json["daily_limit"]==12
    assert service.config.load()["daily_limit"]==12
    assert client.put("/api/ai-weight-price/config",json={"max_waiting":3},headers=headers).status_code==400
    assert client.post("/api/ai-weight-price/start",json={}).status_code==403
    assert client.post("/api/ai-weight-price/start",json={},headers={**headers,"Origin":"https://evil.com"}).status_code==403
    assert client.get("/api/ai-weight-price/tasks?status=bogus").status_code==400
    assert client.get("/api/ai-weight-price/tasks/missing").status_code==404
    assert client.get("/api/ai-weight-price/export?status=success").data.startswith(b"\xef\xbb\xbf")
    assert client.get("/api/ai-weight-price/status",base_url="https://zeshun.example.com").status_code==403
    assert "本机控制台" in client.get("/ai-weight-price",base_url="https://zeshun.example.com").text


def test_continue_endpoint_requires_switch_and_resumes(service, client, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "continue_after_human", lambda: calls.append("continued"))
    headers = {"X-AWP-Request": "1"}
    denied = client.post("/api/ai-weight-price/continue", json={"acknowledged": False}, headers=headers)
    assert denied.status_code == 400 and not calls
    accepted = client.post("/api/ai-weight-price/continue", json={"acknowledged": True}, headers=headers)
    assert accepted.status_code == 200 and calls == ["continued"]


def test_readonly_role_cannot_edit(service):
    app=Flask(__name__)
    def authorize(permission):
        return None if permission.endswith(".view") else (jsonify(message="forbidden"),403)
    app.register_blueprint(create_blueprint(service,authorize))
    client=app.test_client()
    assert client.get("/api/ai-weight-price/tasks").status_code==200
    assert client.put("/api/ai-weight-price/config",json={},headers={"X-AWP-Request":"1"}).status_code==403


def test_malformed_requests_fail_without_internal_error(client):
    headers={"X-AWP-Request":"1"}
    assert client.put("/api/ai-weight-price/config",json={"selectors":[]},headers=headers).status_code==400
    assert client.post("/api/ai-weight-price/start",json=[],headers=headers).status_code==400


def test_retry_endpoint_starts_only_selected_task(service,client,monkeypatch):
    task(service.store,matched=True,weight_g="450")
    service.store.exception("g1","ERP回写保存失败")
    started=[]
    monkeypatch.setattr(service,"require_login",lambda *args:None)
    monkeypatch.setattr(service,"start",lambda *args:started.append(args))
    response=client.post("/api/ai-weight-price/tasks/g1/retry",json={},headers={"X-AWP-Request":"1"})
    assert response.status_code==200
    assert started==[("process","g1")]


def test_retry_endpoint_can_start_an_interrupted_pending_task(service, client, monkeypatch):
    task(service.store, "pending-one")
    started = []
    monkeypatch.setattr(service, "require_login", lambda *args: None)
    monkeypatch.setattr(service, "preflight", lambda *args: None)
    monkeypatch.setattr(service, "start", lambda *args: started.append(args))

    response = client.post("/api/ai-weight-price/tasks/pending-one/retry", json={},
                           headers={"X-AWP-Request": "1"})

    assert response.status_code == 200
    assert response.json["message"] == "已启动此待处理商品"
    assert started == [("process", "pending-one")]


def test_skip_current_exception_preserves_reason_and_resumes_same_batch(service, monkeypatch):
    task(service.store, "blocked-one")
    service.store.exception("blocked-one", "ERP回写保存失败", "未找到唯一保存按钮")
    service.store.set_state("pipeline_current", {"scope": "scope", "task_id": "blocked-one"})
    service.store.set_state("run", {"run_id": "run-skip", "mode": "pipeline", "selection": {"category": "", "start_page": 1, "end_page": 1},
                                     "max_items": 10, "processed_items": 2, "current_item_index": 3,
                                     "current_task_id": "blocked-one", "outcome": "blocked"})
    started = []
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: started.append((args, kwargs)))

    service.skip_current_exception()

    saved = service.store.get("blocked-one")
    assert saved["status"] == "skipped"
    assert saved["exception_reason"] == "ERP回写保存失败"
    assert saved["exception_detail"] == "未找到唯一保存按钮"
    assert saved["skip_reason"].startswith("人工跳过异常：")
    assert service.store.state("pipeline_current") is None
    assert started == [(("pipeline", None, {"category": "", "start_page": 1, "end_page": 1}, 10), {"resume": True})]


def test_current_run_items_endpoint_shows_only_that_run_with_live_task_status(service, client):
    task(service.store, "current")
    task(service.store, "historical")
    service.store.exception("current", "上传控件异常")
    service.store.save_run({"run_id": "run-now", "mode": "pipeline", "outcome": "running"})
    service.store.record_run_item("run-now", "current", execution_result="异常", execution_reason="上传控件异常")
    service.store.set_state("run", {"run_id": "run-now", "mode": "pipeline", "outcome": "running"})

    response = client.get("/api/ai-weight-price/run-items")

    assert response.status_code == 200
    assert response.json["total"] == 1
    assert response.json["rows"][0]["erp_goods_id"] == "current"
    assert response.json["rows"][0]["execution_result"] == "异常"
    assert response.json["rows"][0]["run_sequence"] == 1


def test_queue_never_skips_deferred_current_product(service,monkeypatch):
    for i in range(101):
        task(service.store,"g"+str(i),next_attempt_at=9999999999 if i<100 else 0)
    seen=[]
    monkeypatch.setattr(service,"process",lambda row,*args:seen.append(row["erp_goods_id"]))
    service.tick(FakeBrowser(),FakeModel(),validate({}))
    assert seen==[]


def test_sent_lower_bound_does_not_lose_immediate_reply(service):
    row=task(service.store)
    assert service.store.reserve(row,validate({}),"msg","https://air.1688.com/chat",[],1000)=="ok"
    service.store.sent("g1",1005)
    assert service.store.get("g1")["sent_at"]==1000
    assert service.store.quota(1005)["last"]==1005


def test_browser_replies_filters_history_self_and_late_messages(monkeypatch):
    from erp.ai_weight_price.browser import Browser
    browser=Browser(validate({}),threading.Event(),lambda *a:None)
    monkeypatch.setattr(browser,"page",lambda *a:object())
    monkeypatch.setattr(browser,"delay",lambda *a:None)
    monkeypatch.setattr(browser,"release",lambda *a:None)
    monkeypatch.setattr(browser,"messages",lambda *a:[
        {"id":"old","at":1001,"text":"旧消息"},
        {"id":"early","at":999,"text":"更早消息"},
        {"id":"new","at":1002,"text":"包装450克"},
        {"id":"late","at":2801,"text":"逾期"}])
    result=browser.replies({"conversation_url":"https://air.1688.com/chat","merchant_id":"m1",
                            "reply_baseline":["old"],"sent_at":1000,"deadline":2800})
    assert [m["id"] for m in result]==["new"]


def test_mismatched_supplier_and_ambiguous_identity_never_send(monkeypatch):
    from erp.ai_weight_price.browser import Browser
    browser=Browser(validate({}),threading.Event(),lambda *a:None)
    monkeypatch.setattr(browser,"chat_root",lambda *a:object())
    monkeypatch.setattr(browser,"value",lambda *a:"wrong-merchant")
    with pytest.raises(ValueError,match="商家ID"):
        browser.verify_chat(object(),"expected-merchant")


@pytest.mark.parametrize("selection", [None,{}, {"start_page":0,"end_page":1}, {"start_page":3,"end_page":2},
    {"start_page":1,"end_page":101}, {"start_page":1.5,"end_page":2},
    {"start_page":True,"end_page":2}, {"start_page":1,"end_page":10001},
    {"category":[],"start_page":1,"end_page":2}])
def test_run_selection_requires_valid_explicit_pages(selection):
    from erp.ai_weight_price.config import selection_params
    with pytest.raises(ValueError):
        selection_params(selection,validate({}))


def test_no_category_is_valid_and_scope_includes_range():
    from erp.ai_weight_price.config import selection_params,selection_key
    config=validate({})
    choice=selection_params({"start_page":3,"end_page":5},config)
    assert choice=={"category":"","start_page":3,"end_page":5}
    assert selection_key(choice,config)!=selection_key({**choice,"category":"1/2"},config)
    assert selection_key(choice,config)!=selection_key({**choice,"end_page":6},config)


def test_page_item_start_is_one_based_and_part_of_scope():
    from erp.ai_weight_price.config import selection_params,selection_key
    config=validate({})
    choice=selection_params({"start_page":3,"end_page":5,"start_item":4},config)
    assert choice=={"category":"","start_page":3,"end_page":5,"start_item":4}
    assert selection_key(choice,config)!=selection_key({**choice,"start_item":1},config)
    with pytest.raises(ValueError,match="本页起始商品序号"):
        selection_params({"start_page":1,"end_page":1,"start_item":0},config)


def test_product_cursor_is_optional_and_part_of_scope():
    from erp.ai_weight_price.config import selection_params,selection_key
    config=validate({})
    choice=selection_params({"category":"17","start_product_id":" 848332340 "},config)
    assert choice=={"category":"17","start_product_id":"848332340"}
    assert selection_key(choice,config)!=selection_key({**choice,"start_product_id":"848332341"},config)
    assert selection_params({"category":"17","start_product_id":""},config)["start_product_id"]==""
    with pytest.raises(ValueError,match="起始产品编号"):
        selection_params({"category":"17","start_product_id":"bad-id"},config)


def test_no_login_or_no_selection_cannot_start(service,client,monkeypatch):
    headers={"X-AWP-Request":"1"}
    with pytest.raises(ValueError,match="成功登录"):
        service.start("collect",selection={"start_page":1,"end_page":1})
    assert client.post("/api/ai-weight-price/start",json={"mode":"collect"},headers=headers).status_code==400
    assert service.thread is None
    monkeypatch.setattr(service,"require_login",lambda *args:None)
    with pytest.raises(ValueError,match="分类"):
        service.start("collect",selection={"category":"not-real","start_page":1,"end_page":2})
    with pytest.raises(ValueError,match="尚无采集"):
        service.start("process",selection={"category":"","start_page":1,"end_page":2})


def test_login_confirm_requires_real_logged_in_browser(service,client,monkeypatch):
    import erp.ai_weight_price.service as module
    calls=[]
    class LoginBrowser:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def confirm_login(self):
            calls.append("confirm")
            raise ValueError("仍在登录页")
    service.browser_factory=LoginBrowser
    monkeypatch.setattr(module,"debugger_identity",lambda *args:"edge1")
    headers={"X-AWP-Request":"1"}
    assert client.post("/api/ai-weight-price/login/confirm",json={},headers=headers).status_code==400
    assert calls==[]
    assert client.post("/api/ai-weight-price/login/confirm",json={"acknowledged":True},headers=headers).status_code==400
    assert not service.store.state("login")["confirmed"]
    monkeypatch.setattr(LoginBrowser,"confirm_login",lambda self:{"confirmed_at":123})
    result=client.post("/api/ai-weight-price/login/confirm",json={"acknowledged":True},headers=headers)
    assert result.status_code==200 and result.json["login"]["confirmed"]
    service.require_login(service.config.load())
    monkeypatch.setattr(module,"debugger_identity",lambda *args:"edge2")
    with pytest.raises(ValueError,match="会话"):
        service.require_login(service.config.load())
    assert not service.store.state("login")["confirmed"]


@pytest.mark.parametrize("include_supplier", [True, False])
def test_open_login_opens_requested_login_pages(service, monkeypatch, include_supplier):
    import erp.ai_weight_price.service as module
    calls = []

    class LoginPagesBrowser:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def open_login(self):
            calls.append("zying")

        def open_supplier_login(self):
            calls.append("1688")

    service.browser_factory = LoginPagesBrowser
    monkeypatch.setattr(module, "open_edge", lambda *args: "edge1")

    service.open_login(include_supplier=include_supplier)

    assert calls == (["zying", "1688"] if include_supplier else ["zying"])
    assert service.store.state("login")["confirmed"] is False


def test_terminate_current_clears_only_current_batch_and_keeps_login_and_history(service, client):
    task(service.store, "current")
    task(service.store, "historical")
    service.store.save_run({"run_id": "old-run", "mode": "pipeline", "started_at": 1})
    service.store.record_run_item("old-run", "historical")
    service.store.save_run({"run_id": "current-run", "mode": "pipeline", "started_at": 2})
    service.store.record_run_item("current-run", "current")
    service.store.set_state("run", {"run_id": "current-run", "mode": "pipeline", "outcome": "completed"})
    service.store.set_state("login", {"confirmed": True, "confirmed_at": 123})

    response = client.post("/api/ai-weight-price/terminate", json={}, headers={"X-AWP-Request": "1"})

    assert response.status_code == 200
    assert response.json["cleared"] is True
    assert service.store.state("run") == {}
    assert service.store.state("latest_run_id") is None
    assert service.status()["current_counts"] == dict.fromkeys(service.status()["current_counts"], 0)
    assert service.store.state("login")["confirmed"] is True
    assert service.store.get("current")["erp_goods_id"] == "current"
    assert service.store.get("historical")["erp_goods_id"] == "historical"
    with pytest.raises(ValueError, match="不存在"):
        service.store.run_report("current-run")
    old_batch, old_rows = service.store.run_report("old-run")
    assert old_batch["run_id"] == "old-run"
    assert [row["erp_goods_id"] for row in old_rows] == ["historical"]


def test_open_edge_launches_visible_installed_app_with_exact_login_url(tmp_path,monkeypatch):
    from erp.ai_weight_price import edge
    calls=[];ready=iter([None,"edge1"])
    monkeypatch.setattr(edge,"debugger_identity",lambda *args:next(ready))
    monkeypatch.setattr(edge,"edge_executable",lambda:"C:/Edge/msedge.exe")
    monkeypatch.setattr(edge.subprocess,"Popen",lambda *args,**kwargs:calls.append((args,kwargs)))
    assert edge.open_edge("http://127.0.0.1:9222",tmp_path)=="edge1"
    command=calls[0][0][0]
    assert command[0]=="C:/Edge/msedge.exe"
    assert command[-1]=="https://meli.zying.net/#/login"
    assert "--remote-debugging-address=127.0.0.1" in command
    assert not any("headless" in arg for arg in command)


def test_run_scope_filters_processing_and_keeps_duplicate_task_history(service,monkeypatch):
    for key in ("g1","g2"):
        task(service.store,key)
    service.store.include_in_scope("chosen","g2",3)
    service.store.exception("g1","保留其他范围异常")
    seen=[]
    monkeypatch.setattr(service,"process",lambda row,*args:seen.append(row["erp_goods_id"]))
    service.tick(FakeBrowser(),FakeModel(),{**validate({}),"run_scope":"chosen"})
    assert seen==["g2"]
    assert service.store.counts("chosen")["pending"]==1
    service.store.reset_scope("chosen")
    assert service.store.get("g2")["status"]=="pending"
    assert service.store.get("g1")["exception_reason"]=="保留其他范围异常"


@pytest.mark.parametrize("resume,same_scope,expected",[(False,True,[3,4]),(True,True,[4]),(True,False,[3,4])])
def test_collection_only_reads_selected_pages_and_resumes_matching_scope(service,monkeypatch,resume,same_scope,expected):
    from erp.ai_weight_price.browser import Browser
    from erp.ai_weight_price.config import selection_key
    config=validate({})
    selection={"category":"a/b","start_page":3,"end_page":4}
    config["run_selection"]=selection
    scope=selection_key(selection,config)
    if resume:
        service.store.set_state("collection",{"scope":scope if same_scope else "other","page":3,"complete":False})
    else:
        # Terminating a previous batch persists JSON null. A fresh collection
        # must treat it exactly like a missing checkpoint, not call .get on it.
        service.store.set_state("collection",None)
    browser=Browser(config,threading.Event(),lambda *a:None)
    class Row:
        def __init__(self,n):self.n=n
        def inner_text(self):return f"product {self.n}"
    class Rows:
        @property
        def first(self):return self
        def wait_for(self,**kwargs):pass
        def all_inner_texts(self):return [f"product {page.n}"]
        def all(self):return [Row(page.n)]
    class Page:
        n=1
        url=config["erp_list_url"]
        def locator(self,*args):return Rows()
    page=Page();read=[];categories=[]
    monkeypatch.setattr(browser,"page",lambda *args:page)
    monkeypatch.setattr(browser,"release",lambda *args:None)
    monkeypatch.setattr(browser,"check",lambda *args:None)
    monkeypatch.setattr(browser,"apply_category",lambda p,c:categories.append(c) or "分类B")
    monkeypatch.setattr(browser,"first_page",lambda *args:None)
    def next_page(p,current):p.n+=1;return True
    monkeypatch.setattr(browser,"next_page",next_page)
    def value(row,key,*args,**kwargs):
        if key=="erp_id":read.append(row.n);return str(row.n)
        return {"erp_title":f"product {row.n}","erp_image":"https://image.example/1.jpg"}.get(key,"")
    monkeypatch.setattr(browser,"value",value)
    monkeypatch.setattr(browser,"erp_goods_id",lambda page,row,record:value(row,"erp_id"))
    browser.collect(service.store)
    assert read==expected and categories==["a/b"]
    assert service.store.list(scope=scope)["total"]==len(expected)
    assert service.store.state("collection")["complete"]


def test_collection_product_cursor_is_inclusive_and_stops_at_limit(service, monkeypatch):
    from erp.ai_weight_price.browser import Browser
    from erp.ai_weight_price.config import selection_key
    config = validate({"max_pages": 10})
    selection = {"category": "a/b", "start_product_id": "3"}
    config.update(run_selection=selection, max_items=2)
    scope = selection_key(selection, config)
    browser = Browser(config, threading.Event(), lambda *args: None)

    class Row:
        def __init__(self, number): self.number = number
        def inner_text(self): return f"product {self.number}"

    class Rows:
        @property
        def first(self): return self
        def wait_for(self, **_kwargs): pass
        def all_inner_texts(self): return [f"product {page.number}"]
        def all(self): return [Row(page.number)]

    class Page:
        number = 1
        url = config["erp_list_url"]
        def locator(self, *_args): return Rows()

    page = Page()
    monkeypatch.setattr(browser, "page", lambda *_args: page)
    monkeypatch.setattr(browser, "release", lambda *_args: None)
    monkeypatch.setattr(browser, "check", lambda *_args: None)
    monkeypatch.setattr(browser, "focus", lambda *_args: None)
    monkeypatch.setattr(browser, "apply_category", lambda *_args: "分类B")
    monkeypatch.setattr(browser, "first_page", lambda *_args: None)
    monkeypatch.setattr(browser, "next_page", lambda _page, _current: setattr(page, "number", page.number + 1) or True)
    monkeypatch.setattr(browser, "value", lambda row, key, *_args, **_kwargs: {
        "erp_title": f"product {row.number}", "erp_image": "https://image.example/1.jpg"
    }.get(key, ""))
    monkeypatch.setattr(browser, "erp_goods_id", lambda _page, row, _record: str(row.number))

    browser.collect(service.store)

    assert [row["erp_goods_id"] for row in service.store.list(scope=scope)["rows"]] == ["3", "4"]
    assert service.store.state("collection")["complete"] is True


def test_clear_category_does_not_leave_previous_filter(monkeypatch):
    from erp.ai_weight_price.browser import Browser,CATEGORY_READ
    browser=Browser(validate({}),threading.Event(),lambda *args,**kwargs:None)
    class Control:
        selected=["a"]
        @property
        def first(self):return self
        def count(self):return 1
        def evaluate(self,script,*args):
            if script==CATEGORY_READ:return {"options":[{"value":"a","label":"分类A","children":[]}],"value":self.selected}
            self.selected=args[0];return True
    control=Control();clicks=[]
    class Search:
        def count(self):return 1
        def click(self):clicks.append("search")
    class Page:
        url="https://meli.zying.net/#/product"
        def locator(self,*args):return control
        def get_by_role(self,*args,**kwargs):return Search()
    monkeypatch.setattr(browser,"delay",lambda *args:None)
    monkeypatch.setattr(browser,"check",lambda *args:None)
    monkeypatch.setattr(browser,"category_control",lambda *args:control)
    monkeypatch.setattr(browser,"category_search",lambda *args:Search())
    assert browser.apply_category(Page(),"")=="全部分类"
    assert control.selected==[] and clicks==["search"]


def test_category_tree_preserves_depth_order_and_duplicate_names():
    from erp.ai_weight_price.browser import category_control_score,category_paths
    paths=category_paths([
        {"value":"a","label":"服饰","children":[{"value":"b","label":"配件","children":[{"value":"c","label":"帽子"}]}]},
        {"value":"d","label":"家居","disabled":True,"children":[{"value":"b","label":"配件"}]}])
    assert [p["value"] for p in paths]==["a","a/b","a/b/c","d","d/b"]
    assert paths[2]["label"]=="服饰 / 配件 / 帽子"
    assert paths[2]["depth"]==3 and paths[2]["parent_value"]=="a/b"
    assert paths[4]["disabled"] and not paths[1]["disabled"]
    product={"label":"商品分类","placeholder":"请选择分类","text":""}
    shop={"label":"店铺 / 国家","placeholder":"","text":"武汉泽顺跟卖 / 巴西"}
    assert category_control_score(product,[{"value":"a","label":"服饰"}]) > category_control_score(shop,[{"value":"br","label":"巴西"}])


def test_config_only_accepts_mercado_zying_product_page():
    assert validate({})["erp_list_url"]=="https://meli.zying.net/#/product"
    with pytest.raises(ValueError):
        validate({"erp_list_url":"https://seller.zying.net/#/product/myproduct/productlist"})


def test_category_reader_waits_for_async_tree(monkeypatch):
    from erp.ai_weight_price.browser import Browser
    from types import SimpleNamespace
    browser=Browser(validate({}),threading.Event(),lambda *args,**kwargs:None)
    monkeypatch.setattr(browser,"check",lambda *args:None)
    monkeypatch.setattr(browser.stop,"wait",lambda *args:False)
    tree=[{"value":"1","label":"家居","children":[{"value":"2","label":"厨房"}]}]
    values=iter([[],[],tree,tree])
    control=SimpleNamespace(evaluate=lambda *args:{"options":next(values),"value":[]})
    assert browser.read_categories(SimpleNamespace(url="https://meli.zying.net/#/product"),control)["options"]==tree
    timeout_control=SimpleNamespace(evaluate=lambda *args:{"tag":"DIV","classes":"ant-cascader","react":True,
                                                            "vue":False,"placeholder":"分类","text":""})
    with pytest.raises(ValueError,match="未更新"):
        browser.read_categories(SimpleNamespace(url="https://meli.zying.net/#/product"),timeout_control,timeout=0)


def test_category_refresh_failure_keeps_snapshot_then_replaces_with_new_tree(service,client,monkeypatch):
    from erp.ai_weight_price.browser import category_paths
    monkeypatch.setattr(service,"require_login",lambda *args:None)
    old=category_paths([{"value":"a","label":"旧分类"}])
    new=category_paths([{"value":"b","label":"新分类","children":[{"value":"c","label":"二级"}]}])
    service.store.set_state("categories",old)
    class CategoriesBrowser:
        fail=True
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def confirm_login(self):pass
        def categories(self):
            if self.fail:raise ValueError("加载超时")
            return new
    service.browser_factory=CategoriesBrowser
    headers={"X-AWP-Request":"1"}
    assert client.post('/api/ai-weight-price/categories/refresh',json={},headers=headers).status_code==400
    saved=client.get('/api/ai-weight-price/categories')
    assert saved.json['options']==old and saved.json['meta']['error']=='加载超时'
    assert saved.headers['Cache-Control']=='no-store'
    CategoriesBrowser.fail=False
    refreshed=client.post('/api/ai-weight-price/categories/refresh',json={},headers=headers)
    assert refreshed.status_code==200 and refreshed.json['options']==new
    assert refreshed.json['meta']['refreshed_at'] and not refreshed.json['meta']['error']


def test_category_scripts_use_committed_react_tree_and_custom_fields():
    import json,shutil,subprocess
    from erp.ai_weight_price.browser import CATEGORY_READ,CATEGORY_SET
    node=shutil.which('node')
    if not node:pytest.skip('Node required to test browser adapter JavaScript')
    script="""
const assert=require('node:assert/strict');
const read=READ, set=SET;
const root1={stateNode:{}},root2={stateNode:root1.stateNode};root1.stateNode.current=root2;
const old={memoizedProps:{options:[],onChange:()=>{}},return:root1};
let selected;
const current={memoizedProps:{fieldNames:{value:'id',label:'name',children:'nodes'},
 options:[{id:1,name:'Parent',nodes:[{id:2,name:'Child',nodes:[{id:3,name:'Leaf'}]}]}],value:[],
 onChange:(values)=>{selected=values;current.memoizedProps.value=values;}},return:root2};
old.alternate=current;current.alternate=old;
const element={__reactFiber$fixture:old};
assert.equal(read(element).options[0].children[0].children[0].label,'Leaf');
assert.equal(set(element,['1','2','3']),true);assert.deepEqual(selected,[1,2,3]);
assert.deepEqual(read(element).value,[1,2,3]);
assert.equal(set(element,['1','404']),false);assert.deepEqual(selected,[1,2,3]);
assert.equal(set(element,[]),true);assert.deepEqual(selected,[]);
""".replace('READ',CATEGORY_READ).replace('SET',CATEGORY_SET)
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert result.returncode==0,result.stderr


def test_start_rejection_is_persistent_and_visible_in_status(service, client, monkeypatch):
    monkeypatch.setattr(service, "require_login", lambda *args: None)
    response = client.post("/api/ai-weight-price/start", json={
        "mode": "process", "selection": {"start_page": 1, "end_page": 2}}, headers={"X-AWP-Request": "1"})
    assert response.status_code == 400
    reason = response.json["message"]
    assert "尚无采集任务" in reason
    status = client.get("/api/ai-weight-price/status")
    assert status.json["action_error"]["message"] == reason
    assert status.headers["Cache-Control"] == "no-store"
    assert any(reason in row["message"] and row["level"] == "ERROR" for row in Store(service.store.root).logs())
    assert not status.json["running"] and service.thread is None


def test_legacy_worker_error_returned_even_without_outcome(service, client):
    service.store.set_state("run", {"mode": "collect", "message": "已停止"})
    service.store.set_state("run_error", "未找到唯一的智赢分类搜索按钮")
    assert client.get("/api/ai-weight-price/status").json["run_error"] == "未找到唯一的智赢分类搜索按钮"


def test_rejected_cross_site_request_does_not_change_visible_error(service, client):
    previous = {"message": "keep local error", "at": 1}
    service.store.set_state("action_error", previous)
    response = client.post("/api/ai-weight-price/start", json={},
                           headers={"X-AWP-Request": "1", "Origin": "https://evil.example"})
    assert response.status_code == 403
    assert service.store.state("action_error") == previous
    assert not service.store.logs()


def test_corrected_config_clears_last_action_error(service, client):
    headers = {"X-AWP-Request": "1"}
    assert client.put("/api/ai-weight-price/config", json={"max_waiting": 3}, headers=headers).status_code == 400
    assert service.store.state("action_error")
    assert client.put("/api/ai-weight-price/config", json={"max_waiting": 2}, headers=headers).status_code == 200
    assert service.store.state("action_error") is None


@pytest.mark.parametrize("failure", [None, "分类搜索失败"])
def test_worker_start_finish_logs_and_preserved_selection(service, monkeypatch, failure):
    entered, release = threading.Event(), threading.Event()
    class CollectBrowser:
        def __init__(self, *args): pass
        def __enter__(self):
            entered.set()
            if not release.wait(5): raise RuntimeError("test timed out")
            return self
        def __exit__(self, *args): pass
        def confirm_login(self): pass
        def collect(self, store):
            if failure: raise ValueError(failure)
    service.browser_factory = CollectBrowser
    monkeypatch.setattr(service, "require_login", lambda *args: None)
    selection = {"category": "", "start_page": 1, "end_page": 2}
    service.start("collect", selection=selection)
    try:
        assert entered.wait(2)
        assert service.status()["running"]
        assert any("采集已启动" in row["message"] for row in service.store.logs())
    finally:
        release.set()
        service.thread.join(5)
    status = service.status()
    assert not service.thread.is_alive() and not status["running"]
    assert status["run"]["selection"] == selection and status["run"]["started_at"]
    assert status["run"]["outcome"] == ("failed" if failure else "completed")
    assert status["run_error"] == failure
    assert (failure or "采集完成") in service.store.logs()[-1]["message"]


def test_failed_thread_start_releases_lock_and_reports_failure(service, monkeypatch):
    def fail_start(self): raise RuntimeError("cannot start thread")
    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="cannot start"):
        service.start("probe")
    assert not service.status()["running"]
    assert service.status()["run"]["outcome"] == "failed"
    assert "cannot start" in service.store.logs()[-1]["message"]
