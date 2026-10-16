"""Three-cent API/worker regressions with isolated routes and no live services."""

import ast
import concurrent.futures
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from flask import Flask, Response, jsonify, request

from AI_Agent import deepseek
from erp import ai_original_cost_control as scheduling
from erp import ai_original_products as products


def isolated_function(name, **dependencies):
    node = next(node for node in ast.parse(Path("bit/bit_interface.py").read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    namespace = dict(request=request, Response=Response, jsonify=jsonify,
                     json=json, logging=logging, **dependencies)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<budget-route>", "exec"), namespace)
    return namespace[name]


@pytest.fixture
def process_route(monkeypatch):
    started, preflights, resolved = [], [], []
    state = {}

    class Thread:
        def __init__(self, **kwargs): started.append(kwargs)
        def start(self): pass

    original_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *args, **kwargs:
                        object() if name == "rembg" else original_find_spec(name, *args, **kwargs))
    monkeypatch.setattr(products, "check_original_model_account", lambda **kwargs: preflights.append(kwargs))

    def credentials(base_url):
        resolved.append(base_url)
        return "dashscope", "saved-test-key"

    route = isolated_function("api_process_ai_original_products",
        db_get_mercado_product_items_by_ids=lambda ids: [
            {"id": value, "source_type": "ai_original"} for value in ids],
        _ai_original_category_client=lambda *args: (None, None),
        _resolve_ai_original_text_api_key=credentials,
        _account_api_key=lambda provider: pytest.fail("must not resolve paid-image credentials"),
        threading=SimpleNamespace(Thread=Thread), _run_ai_original_task=lambda: None,
        _ai_original_task_lock=threading.Lock(), _ai_original_task_state=state,
        _ai_original_task_snapshot=lambda: deepcopy(state), workbench_user_salesperson=lambda: "业务员")

    def call(payload):
        with Flask(__name__).test_request_context(json={"product_item_ids": [1], **payload}):
            response, status = route()
            return response.get_json(), status

    return SimpleNamespace(call=call, started=started, preflights=preflights, resolved=resolved, state=state)


@pytest.mark.parametrize("options", [
    {},
    {"model": "qwen3.7-flash", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"model": "qwen3.7-flash", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/"},
])
def test_mercado_route_fixes_official_flash_and_enables_worker_budget(process_route, options):
    payload, status = process_route.call(options)
    assert status == 202
    worker = process_route.started[0]
    assert worker["kwargs"] == {"cost_control": True}
    assert worker["args"][1:4] == ("saved-test-key", "qwen3.7-flash", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert worker["args"][5:8] == ("", "", "")
    assert worker["args"][-2:] == ("mercadolibre", "standard")
    assert process_route.resolved == ["https://dashscope.aliyuncs.com/compatible-mode/v1"]
    assert process_route.preflights == [{"api_key": "saved-test-key", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}]
    assert payload["data"]["cost_cap_yuan"] == 0.03
    assert payload["data"]["resume_at"] is None
    assert payload["data"]["running"] is True


@pytest.mark.parametrize("options, message", [
    ({"model": "deepseek-v4-pro"}, "仅支持 qwen3.7-flash"),
    ({"base_url": "https://gateway.example/v1"}, "官方接口"),
    ({"base_url": "https://api.deepseek.com.evil.example"}, "官方接口"),
    ({"base_url": "https://api.deepseek.com/v1?redirect=other"}, "官方接口"),
    ({"base_url": "http://api.deepseek.com"}, "官方接口"),
    ({"image_model": "paid-image", "image_api_key": "test"}, "本地白底图"),
])
def test_mercado_route_rejects_unbudgeted_provider_model_and_paid_images(process_route, options, message):
    payload, status = process_route.call(options)
    assert status == 400
    assert message in payload["message"]
    assert process_route.started == []
    assert process_route.preflights == []
    assert process_route.resolved == []


@pytest.mark.parametrize("platform", ["amazon", "yandex"])
def test_other_platforms_keep_existing_model_and_no_three_cent_cap(process_route, platform):
    payload, status = process_route.call({
        "target_platform": platform, "api_key": "custom-key", "model": "custom-model",
        "base_url": "https://gateway.example/v1", "image_model": "paid-image", "image_api_key": "image-key",
    })
    assert status == 202
    worker = process_route.started[0]
    assert worker["kwargs"] == {"cost_control": False}
    assert worker["args"][1:4] == ("custom-key", "custom-model", "https://gateway.example/v1")
    assert worker["args"][5:7] == ("image-key", "paid-image")
    assert payload["data"]["cost_cap_yuan"] is None


def test_waiting_task_rejects_duplicate_batch(process_route):
    process_route.state.update(running=True, status="waiting_off_peak", resume_at="2026-10-05T12:00:00+08:00")
    payload, status = process_route.call({"api_key": "test"})
    assert status == 409
    assert "已有" in payload["message"]
    assert process_route.started == []
    assert process_route.state["status"] == "waiting_off_peak"


def make_worker(monkeypatch, fresh_row, *, lock_error=False, reject_budget_revision=False,
                lose_lock_on_check=None):
    saved, events, queue_states = [], [], []
    state = {"running": True, "status": "running"}
    held = [False]
    resume = "2026-10-05T12:00:00+08:00"

    def wait(on_wait, *, sleep=None):
        on_wait(resume)
        queue_states.append(deepcopy(state))
        events.append("wait")

    @contextmanager
    def lock(product_id):
        events.append(("lock", product_id))
        if lock_error:
            raise RuntimeError("该商品已有生成任务")
        held[0] = True
        try:
            def check_owner():
                assert held[0]
                events.append("check_owner")
                if events.count("check_owner") == lose_lock_on_check:
                    raise RuntimeError("商品生成锁已失效，已停止付费调用")
            yield check_owner
        finally:
            held[0] = False
            events.append("unlock")

    def reload(ids):
        assert held[0], "budget snapshot must be reread under the per-product lock"
        events.append(("reload", ids))
        return [deepcopy(fresh_row)]

    def save(product_id, changes):
        assert held[0]
        saved.append(deepcopy(changes))
        if reject_budget_revision and "expected_generation_budget_revision" in changes:
            raise ValueError("商品费用记录已变化，已停止付费调用")
        return {}

    def transport(*args, **kwargs):
        assert held[0]
        events.append("model")
        return {"content": "{}", "usage": {"prompt_tokens": 2, "completion_tokens": 2}}

    monkeypatch.setattr(scheduling, "wait_for_low_cost_window", wait)
    monkeypatch.setattr(scheduling, "product_generation_lock", lock)
    monkeypatch.setattr(products, "_listing_json_chat", lambda **kwargs:
                        lambda *args, **kw: pytest.fail("cost-controlled worker must use the usage transport"))
    monkeypatch.setattr(deepseek, "chat_deepseek_with_usage", transport)
    worker = isolated_function("_run_ai_original_task",
        ThreadPoolExecutor=concurrent.futures.ThreadPoolExecutor, as_completed=concurrent.futures.as_completed,
        _ai_original_task_lock=threading.Lock(), _ai_original_task_state=state,
        db_get_mercado_product_items_by_ids=reload, db_update_ai_original_product=save)
    return SimpleNamespace(run=worker, saved=saved, events=events, state=state, queue_states=queue_states)


@pytest.mark.parametrize("serialized", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_worker_restores_latest_budget_preserves_checkpoints_and_reports_cost(monkeypatch, serialized, fail):
    old_budget = {"version": 1, "revision": 4, "spent_yuan": 0.012, "reserved_yuan": 0,
                  "cap_yuan": 0.03, "status": "ready", "calls": [{"state": "complete"}]}
    budget = {**deepcopy(old_budget), "revision": 6, "spent_yuan": 0.019,
              "status": "blocked" if fail else "ready", "reserved_yuan": 0.005 if fail else 0}
    snapshot = {"original_1688": {"title": "货源"}, "ai_original": {
        "status": "failed", "edit_revision": 3, "generation_budget": old_budget, "title_es": "Saved copy"},
        "marketplace_listings": {"amazon": {"title": "Other platform"}}}
    row = {"id": 1, "source_type": "ai_original", "source_item_id": "1688001",
           "source_snapshot_json": json.dumps(snapshot) if serialized else deepcopy(snapshot)}
    fixture = make_worker(monkeypatch, row)

    def prepare(actual_row, **kwargs):
        actual_snapshot = actual_row["source_snapshot_json"]
        if isinstance(actual_snapshot, str): actual_snapshot = json.loads(actual_snapshot)
        assert actual_snapshot["ai_original"]["generation_budget"] == old_budget
        assert kwargs["cost_control"] is True
        kwargs["before_model_request"]()
        reservation = {**deepcopy(old_budget), "revision": 5, "reserved_yuan": 0.01}
        kwargs["on_preparation_checkpoint"]("copy", {"generation_budget": reservation})
        kwargs["budget_transport"]([])
        partial = {"generation_budget": budget, "title_es": "Saved copy",
                   "preparation": {"version": 1, "copy_ready": True}}
        kwargs["on_preparation_checkpoint"]("attributes", partial)
        if fail:
            raise products.OriginalPreparationError("attributes", "预算不足，请人工完成", {
                **partial, "failed_step": "attributes", "workflow_status": "needs_budget_review"})
        draft = {**partial, "status": "completed", "title_pt": "Texto salvo"}
        return {"ai_original": draft, "source_snapshot_json": actual_snapshot,
                "title": "Saved copy", "description_text": "Saved description", "main_image_url": "/local-ai-white.jpg"}

    monkeypatch.setattr(products, "prepare_ai_original_product", prepare)
    stale_row = {"id": 1, "source_type": "ai_original", "source_snapshot_json": {"ai_original": {"generation_budget": {"spent_yuan": 0}}}}
    fixture.run([stale_row], workers=1, cost_control=True,
                billing_actor={"actor_id": 7, "actor_name": "业务员",
                               "salesperson": "业务员", "organization_key": "test-org"})
    assert fixture.events == ["wait", ("lock", 1), ("reload", [1]), "wait", "check_owner", "check_owner", "model", "check_owner", "unlock"]
    guarded_writes = [change["expected_generation_budget_revision"] for change in fixture.saved
                      if "expected_generation_budget_revision" in change]
    assert guarded_writes == [4, 5]
    assert all(waiting["status"] == "waiting_off_peak" and waiting["running"] is True
               and waiting["resume_at"] == "2026-10-05T12:00:00+08:00" for waiting in fixture.queue_states)
    assert fixture.saved[0]["source_snapshot_json"]["ai_original"]["generation_budget"] == old_budget
    assert all(change["expected_ai_original_revision"] == 3 for change in fixture.saved)
    final = fixture.saved[-1]["source_snapshot_json"]["ai_original"]
    assert final["generation_budget"] == budget
    assert final["title_es"] == "Saved copy"
    assert final["preparation"]["copy_ready"] is True
    assert fixture.state["results"][0]["generation_budget"] == budget
    assert fixture.state["results"][0]["status"] == ("failed" if fail else "completed")
    assert fixture.state["running"] is False
    assert fixture.state["resume_at"] is None
    if fail:
        assert final["workflow_status"] == "needs_budget_review"
        assert fixture.state["results"][0]["failed_step"] == "attributes"


def test_worker_busy_product_never_reads_or_writes_stale_budget_and_never_calls_model(monkeypatch):
    fixture = make_worker(monkeypatch, {"id": 1}, lock_error=True)
    monkeypatch.setattr(products, "prepare_ai_original_product", lambda *a, **kw: pytest.fail("must not prepare locked product"))
    fixture.run([{"id": 1}], workers=1, cost_control=True)
    assert fixture.saved == []
    assert fixture.events == ["wait", ("lock", 1)]
    assert fixture.state["results"][0]["status"] == "failed"
    assert "已有生成任务" in fixture.state["results"][0]["message"]
    assert fixture.state["failed_count"] == 1


def test_worker_budget_compare_and_swap_failure_prevents_model_dispatch(monkeypatch):
    old_budget = {"version": 1, "revision": 7, "cap_yuan": 0.03,
                  "spent_yuan": 0.014, "reserved_yuan": 0, "status": "ready"}
    row = {"id": 1, "source_type": "ai_original", "source_snapshot_json": {
        "ai_original": {"generation_budget": old_budget, "edit_revision": 2}}}
    fixture = make_worker(monkeypatch, row, reject_budget_revision=True)

    def prepare(actual_row, **kwargs):
        kwargs["before_model_request"]()
        pending = {**old_budget, "revision": 8, "reserved_yuan": 0.01}
        # A paying client saves its reservation before dispatching a request.
        kwargs["on_preparation_checkpoint"]("copy", {"generation_budget": pending})
        kwargs["budget_transport"]([])
        pytest.fail("failed durable reservation must prevent dispatch")

    monkeypatch.setattr(products, "prepare_ai_original_product", prepare)
    fixture.run([row], workers=1, cost_control=True)
    reservations = [change for change in fixture.saved if "expected_generation_budget_revision" in change]
    assert len(reservations) == 1
    assert reservations[0]["expected_generation_budget_revision"] == 7
    assert reservations[0]["source_snapshot_json"]["ai_original"]["generation_budget"]["revision"] == 8
    assert "model" not in fixture.events
    assert fixture.state["results"][0]["status"] == "failed"
    assert "费用记录已变化" in fixture.state["results"][0]["message"]


def test_worker_lost_lock_after_reservation_save_prevents_model_dispatch(monkeypatch):
    row = {"id": 1, "source_type": "ai_original", "source_snapshot_json": {
        "ai_original": {"generation_budget": {"revision": 1, "spent_yuan": 0.01}}}}
    fixture = make_worker(monkeypatch, row, lose_lock_on_check=2)

    def prepare(actual_row, **kwargs):
        kwargs["before_model_request"]()
        kwargs["on_preparation_checkpoint"]("copy", {"generation_budget": {
            "revision": 2, "spent_yuan": 0.01, "reserved_yuan": 0.005}})
        kwargs["budget_transport"]([])
        pytest.fail("lost lock after durable reservation must prevent dispatch")

    monkeypatch.setattr(products, "prepare_ai_original_product", prepare)
    fixture.run([row], workers=1, cost_control=True)
    reservation = next(change for change in fixture.saved if "expected_generation_budget_revision" in change)
    assert reservation["expected_generation_budget_revision"] == 1
    assert reservation["source_snapshot_json"]["ai_original"]["generation_budget"]["reserved_yuan"] == 0.005
    assert "model" not in fixture.events
    assert fixture.state["results"][0]["status"] == "failed"
    assert "锁已失效" in fixture.state["results"][0]["message"]


def test_qwen_worker_does_not_wait_for_deepseek_offpeak(monkeypatch):
    fixture = make_worker(monkeypatch, {'id': 1, 'source_type': 'ai_original'})
    def prepare(row, **kwargs):
        kwargs['before_model_request']()
        raise ValueError('stop before paid request')
    monkeypatch.setattr(products, 'prepare_ai_original_product', prepare)
    fixture.run([{'id': 1}], model='qwen3.7-flash', workers=1, cost_control=True)
    assert 'wait' not in fixture.events
    assert fixture.queue_states == []


def test_qwen_credentials_do_not_fall_back_to_deepseek(monkeypatch):
    import os
    monkeypatch.setattr(deepseek, '_load_local_env', lambda: None)
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'wrong-provider-key')
    resolver = isolated_function('_resolve_ai_original_text_api_key', os=os, Path=Path,
                                 _account_api_key=lambda provider: '')
    assert resolver('https://dashscope.aliyuncs.com/compatible-mode/v1') == ('dashscope', '')
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'correct-provider-key')
    assert resolver('https://dashscope.aliyuncs.com/compatible-mode/v1') == ('dashscope', 'correct-provider-key')
