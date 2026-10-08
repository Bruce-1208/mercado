"""Offline checks for idempotent, parallel and independently failing batches."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest
from flask import Flask

from erp import ai_original_products as products
from erp import ai_original_cost_control as scheduling
from test_ai_original_budget_routes import isolated_function


def row(pid, status="pending", platform="mercadolibre", serialized=False):
    draft = {"status": status, "title_es": "Saved title", "title": "Saved title",
             "main_image_url": "/saved-ai-white.jpg", "description_es": "Saved description"}
    snapshot = {"original_1688": {"title": "供应商商品"},
                "ai_original": draft if platform == "mercadolibre" else {},
                "marketplace_listings": {platform: draft}}
    return {"id": pid, "source_type": "ai_original",
            "source_snapshot_json": json.dumps(snapshot) if serialized else snapshot}


def worker(monkeypatch, rows):
    saved, locks, attempted = [], [], []
    state = {"running": True}
    by_id = {item["id"]: item for item in rows}
    @contextmanager
    def lock(pid):
        locks.append(pid)
        yield lambda: None
    monkeypatch.setattr(scheduling, "product_generation_lock", lock)
    monkeypatch.setattr(products, "_listing_json_chat", lambda **kwargs: lambda *a, **k: "{}")
    def save(pid, changes):
        saved.append((pid, deepcopy(changes)))
        return {}
    run = isolated_function("_run_ai_original_task", ThreadPoolExecutor=ThreadPoolExecutor,
        as_completed=as_completed, _ai_original_task_lock=threading.Lock(),
        _ai_original_task_state=state, db_update_ai_original_product=save,
        db_get_mercado_product_items_by_ids=lambda ids: [deepcopy(by_id[pid]) for pid in ids])
    def prepare(item, **kwargs):
        attempted.append(item["id"])
        draft = {"status": "completed", "title_es": "New title", "title_pt": "Novo titulo"}
        return {"ai_original": draft, "source_snapshot_json": item["source_snapshot_json"],
                "title": "New title", "description_text": "New description", "main_image_url": "/new-ai-white.jpg"}
    monkeypatch.setattr(products, "prepare_ai_original_product", prepare)
    return SimpleNamespace(run=run, state=state, saved=saved, locks=locks, attempted=attempted, prepare=prepare)


@pytest.mark.parametrize("platform", ["mercadolibre", "amazon", "yandex"])
@pytest.mark.parametrize("serialized", [False, True])
def test_skip_rule_is_per_platform_and_does_not_depend_on_template(platform, serialized):
    item = row(1, "completed", platform, serialized)
    assert products.original_generation_skip_reason(item, platform)
    if platform != "mercadolibre":
        assert not products.original_generation_skip_reason(item, "mercadolibre")
    assert not products.original_generation_skip_reason(row(2, "failed", platform), platform)
    assert products.original_generation_skip_reason(row(3, "processing", platform), platform)


@pytest.mark.parametrize("platform", ["mercadolibre", "amazon", "yandex"])
def test_completed_preparation_never_calls_ai_even_after_template_changes(platform):
    item = row(1, "completed", platform)
    # Legacy completed records can have no checkpoint ledger.
    result = products.prepare_ai_original_product(item, target_platform=platform, template_id="features",
        chat=lambda *a, **k: pytest.fail("successful draft must not call AI"),
        image_generate=lambda *a, **k: pytest.fail("successful image must not be regenerated"))
    assert result["ai_original"]["status"] == "completed"
    assert json.loads(result["source_snapshot_json"]) == item["source_snapshot_json"]


def test_all_skipped_route_never_checks_credentials_or_starts_thread(monkeypatch):
    items = [row(1, "completed"), row(2, "processing", serialized=True)]
    def forbidden(*a, **k): pytest.fail("all-skipped batches must not initialize generation")
    route = isolated_function("api_process_ai_original_products",
        db_get_mercado_product_items_by_ids=lambda ids: items,
        _resolve_ai_original_text_api_key=forbidden, _ai_original_category_client=forbidden,
        threading=SimpleNamespace(Thread=forbidden))
    with Flask(__name__).test_request_context(json={"product_item_ids": [1, 2]}):
        response, status = route()
    data = response.get_json()["data"]
    assert status == 200 and data["skipped_count"] == 2
    assert data["failed_count"] == 0 and not data["running"]


def test_mixed_batch_skips_success_and_processing_and_deduplicates(monkeypatch):
    items = [row(1, "completed"), row(2, "processing"), row(3, "failed"), row(4)]
    fixture = worker(monkeypatch, items)
    fixture.run(items + [items[2]], model="qwen3.7-flash", workers=3)
    assert sorted(fixture.attempted) == [3, 4]
    assert sorted(fixture.locks) == [3, 4]
    assert {pid for pid, _ in fixture.saved} == {3, 4}
    assert fixture.state["skipped_count"] == 2
    assert fixture.state["failed_count"] == 0
    assert fixture.state["requested_count"] == fixture.state["processed_count"] == 4


def test_reread_under_lock_skips_product_completed_by_another_batch(monkeypatch):
    fixture = worker(monkeypatch, [row(1, "completed")])
    fixture.run([row(1)], model="qwen3.7-flash")
    assert fixture.locks == [1]
    assert fixture.attempted == fixture.saved == []
    assert fixture.state["skipped_count"] == 1


def test_products_really_run_concurrently_and_failure_does_not_stop_queue(monkeypatch):
    items = [row(pid) for pid in range(1, 5)]
    fixture = worker(monkeypatch, items)
    barrier = threading.Barrier(3, timeout=5)
    def prepare(item, **kwargs):
        if item["id"] <= 3:
            barrier.wait()  # A serial implementation cannot pass this.
        if item["id"] == 1:
            raise products.OriginalPreparationError("attributes", "属性缺失", {"failed_step": "attributes"})
        return fixture.prepare(item, **kwargs)
    monkeypatch.setattr(products, "prepare_ai_original_product", prepare)
    fixture.run(items, model="qwen3.7-flash", workers=3)
    assert fixture.state["completed_count"] == 3 and fixture.state["failed_count"] == 1
    assert fixture.state["status"] == "partial" and not fixture.state["running"]
    assert sorted(fixture.attempted) == [2, 3, 4]
    failures = [r for r in fixture.state["results"] if r["status"] == "failed"]
    assert failures[0]["failed_step"] == "attributes"


def test_single_account_error_does_not_skip_remaining_products(monkeypatch):
    items = [row(1), row(2)]
    fixture = worker(monkeypatch, items)
    def prepare(item, **kwargs):
        if item["id"] == 1:
            raise products.OriginalModelAccountError("该商品调用失败")
        return fixture.prepare(item, **kwargs)
    monkeypatch.setattr(products, "prepare_ai_original_product", prepare)
    fixture.run(items, workers=1)
    assert fixture.attempted == [2]
    assert fixture.state["completed_count"] == fixture.state["failed_count"] == 1
    assert fixture.state["skipped_count"] == 0


def test_uncaught_future_exception_is_isolated(monkeypatch):
    from erp import ai_usage_billing
    items = [row(1), row(2)]
    fixture = worker(monkeypatch, items)
    def bind(fn, **kwargs):
        def dispatch(item):
            if item["id"] == 1:
                raise RuntimeError("worker dispatch failed")
            return fn(item)
        return dispatch
    monkeypatch.setattr(ai_usage_billing, "bind_usage", bind)
    fixture.run(items, workers=2)
    assert fixture.state["processed_count"] == 2
    assert fixture.state["completed_count"] == fixture.state["failed_count"] == 1
    assert not fixture.state["running"]


def test_parallel_workers_keep_initiating_catalog_account(monkeypatch):
    from bit import product_access
    fixture = worker(monkeypatch, [row(1), row(2)])
    accounts = []
    def prepare(item, **kwargs):
        accounts.append(product_access.actor())
        return fixture.prepare(item, **kwargs)
    monkeypatch.setattr(products, "prepare_ai_original_product", prepare)
    user = {"access_version": 1, "id": 17, "organization_key": "test-org", "username": "tester"}
    with product_access.scope(user):
        fixture.run([row(1), row(2)], workers=2)
    assert accounts == [user, user]
    assert fixture.state["completed_count"] == 2
    assert product_access.actor() is None
