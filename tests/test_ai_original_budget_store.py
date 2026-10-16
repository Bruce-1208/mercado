"""Offline regressions for durable cost accounting across writers/recollection."""

from copy import deepcopy
from datetime import datetime
import json

import pytest

from erp import mercadolibre_collection_store as store
from erp.ai_generation_budget import BEIJING, BudgetedChat, GenerationBudgetError
from test_1688_product_management_backend import ProductConnection
from test_ai_original_platform_routes import SnapshotDB


def ledger(revision=3, state="complete"):
    record = BudgetedChat().snapshot()
    record["revision"] = revision
    record["calls"] = [{
        "state": state, "reserved_nano": 30_000_000, "request_hash": "request-one",
        **({"cost_nano": 29_000_000} if state == "complete" else {}),
    }]
    record["cache"] = {"prior-response-hash": '{"title":"Existing"}'}
    return record


def snapshot(budget):
    return {"ai_original": {"generation_budget": deepcopy(budget), "status": "processing",
                            "edit_revision": 4}, "original_1688": {"title": "货源"}}


@pytest.fixture(autouse=True)
def no_schema_ddl(monkeypatch):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)


def saved_budget(db):
    return json.loads(db.row["source_snapshot_json"])["ai_original"]["generation_budget"]


def test_two_workers_reserving_from_same_revision_only_first_can_commit():
    old = ledger(revision=3)
    db = SnapshotDB(snapshot(old))
    first = deepcopy(old)
    first["revision"] = 4
    first["calls"].append({"state": "pending", "reserved_nano": 1_000_000,
                            "request_hash": "worker-one"})
    second = deepcopy(first)
    second["calls"][-1]["request_hash"] = "worker-two"
    store.update_ai_original_product(1, {
        "source_snapshot_json": snapshot(first), "expected_generation_budget_revision": 3,
        "expected_ai_original_revision": 4,
    }, connection_factory=lambda: db)
    assert saved_budget(db) == first
    assert db.row["review_status"] == "approved"
    with pytest.raises(ValueError, match="费用记录已变化"):
        store.update_ai_original_product(1, {
            "source_snapshot_json": snapshot(second), "expected_generation_budget_revision": 3,
            "expected_ai_original_revision": 4,
        }, connection_factory=lambda: db)
    assert saved_budget(db) == first
    assert sum(query.startswith("UPDATE") for query, _ in db.queries) == 1
    assert all("FOR UPDATE" in query for query, _ in db.queries if "SELECT `source_snapshot_json`" in query)


@pytest.mark.parametrize("replacement", [None, {}, ledger(revision=0), ledger(revision=999)])
def test_ordinary_snapshot_updates_cannot_remove_or_replace_budget(replacement):
    old = ledger()
    db = SnapshotDB(snapshot(old))
    incoming = {"ai_original": {"status": "failed", "title_es": "Manual edit"}}
    if replacement is not None:
        incoming["ai_original"]["generation_budget"] = replacement
    store.update_ai_original_product(1, {"source_snapshot_json": incoming}, connection_factory=lambda: db)
    assert saved_budget(db) == old
    saved = json.loads(db.row["source_snapshot_json"])
    assert saved["ai_original"]["title_es"] == "Manual edit"
    assert saved["marketplace_listings"]["mercadolibre"]["generation_budget"] == old


def test_matching_cas_reconciles_reservation_to_confirmed_usage():
    old = ledger(revision=1, state="pending")
    db = SnapshotDB(snapshot(old))
    actual = deepcopy(old)
    actual["revision"] = 2
    actual["calls"][0].update(state="complete", cost_nano=12_000_000,
                             prompt_tokens=8000, completion_tokens=1000)
    store.update_ai_original_product(1, {
        "source_snapshot_json": snapshot(actual), "expected_generation_budget_revision": 1,
    }, connection_factory=lambda: db)
    assert saved_budget(db) == actual
    assert BudgetedChat(ledger=saved_budget(db)).remaining_yuan == 0.018


def test_first_reservation_checks_absent_ledger_as_revision_zero():
    db = SnapshotDB({"ai_original": {"status": "pending"}})
    first = ledger(revision=1, state="pending")
    store.update_ai_original_product(1, {
        "source_snapshot_json": snapshot(first), "expected_generation_budget_revision": 0,
    }, connection_factory=lambda: db)
    assert saved_budget(db) == first
    with pytest.raises(ValueError, match="费用记录已变化"):
        store.update_ai_original_product(1, {
            "source_snapshot_json": snapshot(first), "expected_generation_budget_revision": 0,
        }, connection_factory=lambda: db)


@pytest.mark.parametrize("status,call_state", [
    ("failed", "complete"), ("failed", "unknown"), ("pending", "pending"),
    ("processing", "pending"), ("completed", "complete"),
])
def test_recollection_keeps_cost_ledger_and_prevents_fresh_allowance(status, call_state):
    old = ledger(revision=8, state=call_state)
    prior = snapshot(old)
    prior["ai_original"].update(status=status, edit_revision=4,
                                preparation={"version": 1, "copy_ready": True,
                                             "copy_fingerprint": "old-source"})
    connection = ProductConnection({
        "id": 7, "source_type": "ai_original", "source_item_id": "1688_907173727091",
        "currency_id": "CNY", "source_snapshot_json": json.dumps(prior),
    })
    store.upsert_ai_original_product({
        "source_url": "https://detail.1688.com/offer/907173727091.html",
        "title": "重新采集商品", "price": 12,
        "images": ["https://cbu01.alicdn.com/product.jpg"],
    }, connection_factory=lambda: connection)
    saved = json.loads(connection.cursor_object.row["source_snapshot_json"])
    assert saved["ai_original"]["generation_budget"] == old
    assert saved["ai_original"]["preparation"] == prior["ai_original"]["preparation"]
    if status != "completed":
        assert saved["original_1688"]["title"] == "重新采集商品"
        assert saved["ai_original"]["edit_revision"] == 5
    restored = BudgetedChat(
        ledger=saved["ai_original"]["generation_budget"],
        now=lambda: datetime(2026, 10, 3, 10, tzinfo=BEIJING),
        transport=lambda **kwargs: pytest.fail("recollection must not reset paid allowance"),
    )
    with pytest.raises(GenerationBudgetError) as caught:
        restored([{"role": "user", "content": "new product copy"}])
    assert caught.value.kind == ("budget_exhausted" if call_state == "complete" else "usage_unknown")
    assert connection.committed
