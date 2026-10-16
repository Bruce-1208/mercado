"""Platform routing and snapshot isolation, without live database/model calls."""
from contextlib import nullcontext
import ast
from copy import deepcopy
import concurrent.futures
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from flask import Flask, Response, jsonify, request

from bit import product_access
from erp import ai_original_products as original
from erp import mercadolibre_collection_store as store


def load_route(name, **dependencies):
    node = next(node for node in ast.parse(Path("bit/bit_interface.py").read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    namespace = dict(request=request, Response=Response, jsonify=jsonify,
                     json=json, logging=logging, **dependencies)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<isolated-route>", "exec"), namespace)
    return namespace[name]


def test_process_rejects_unknown_platform_before_loading_products():
    route = load_route("api_process_ai_original_products")
    with Flask(__name__).test_request_context(json={"product_item_ids": [1], "target_platform": "unknown"}):
        response, status = route()
    assert status == 400
    assert response.get_json()["status"] == "error"


@pytest.mark.parametrize("platform", ["yandex", "amazon"])
def test_process_new_platform_needs_no_mercado_account(monkeypatch, platform):
    started = []
    monkeypatch.setattr(original, "check_original_model_account", lambda **kwargs: None)
    state = {}
    class Thread:
        def __init__(self, **kwargs): started.append(kwargs)
        def start(self): pass
    route = load_route("api_process_ai_original_products",
        db_get_mercado_product_items_by_ids=lambda ids: [{"id": 1, "source_type": "ai_original"}],
        _ai_original_category_client=lambda *a: pytest.fail("must not read Mercado credentials"),
        threading=SimpleNamespace(Thread=Thread), _run_ai_original_task=lambda: None,
        _ai_original_task_lock=threading.Lock(), _ai_original_task_state=state,
        _ai_original_task_snapshot=lambda: state, workbench_user_salesperson=lambda: "业务员")
    with Flask(__name__).test_request_context(json={
        "product_item_ids": [1], "target_platform": platform, "template_id": "features",
        "api_key": "test", "image_api_key": "test", "image_model": "image-test",
    }):
        response, status = route()
    assert status == 202
    assert started[0]["args"][-2:] == (platform, "features")
    assert started[0]["args"][8] is None
    assert response.get_json()["data"]["target_platform"] == platform


@pytest.mark.parametrize("fail", [False, True])
def test_worker_saves_only_target_platform_on_success_image_and_failure(monkeypatch, fail):
    saved = []
    state = {}
    snapshot = {"ai_original": {"status": "completed", "title_es": "Existente"},
                "original_1688": {"title": "货源"}, "source": {"title": "Existente"}}
    def prepare(row, **kwargs):
        assert kwargs["target_platform"] == "yandex"
        assert kwargs["category_client"] is None
        kwargs["on_image_ready"]("/api/ai-original-products/images/1688-1yandex-ai-white.jpg", "local_background_removal")
        if fail:
            raise ValueError("生成失败")
        return {"ai_original": {"title": "Товар", "description": "Описание", "status": "completed"},
                "source_snapshot_json": deepcopy(snapshot)}
    from erp import ai_original_cost_control as scheduling
    monkeypatch.setattr(scheduling, "product_generation_lock", lambda pid: nullcontext(lambda: None))
    monkeypatch.setattr(original, "prepare_ai_original_product", prepare)
    monkeypatch.setattr(original, "_listing_json_chat", lambda **kwargs: lambda *a, **kw: "{}")
    worker = load_route("_run_ai_original_task",
        ThreadPoolExecutor=concurrent.futures.ThreadPoolExecutor, as_completed=concurrent.futures.as_completed,
        _ai_original_task_lock=threading.Lock(), _ai_original_task_state=state,
        db_get_mercado_product_items_by_ids=lambda ids: [{"id": ids[0], "source_type": "ai_original", "source_snapshot_json": snapshot}],
        db_update_ai_original_product=lambda pid, changes: saved.append(deepcopy(changes)) or {})
    worker([{"id": 1, "source_snapshot_json": snapshot}], workers=1,
           target_platform="yandex", generated_by="业务员")
    assert len(saved) == 3
    assert all(set(change) == {"marketplace_listing"} for change in saved)
    assert saved[-1]["marketplace_listing"]["status"] == ("failed" if fail else "completed")
    if not fail:
        assert saved[-1]["marketplace_listing"]["review_status"] == "unreviewed"
    assert snapshot["ai_original"] == {"status": "completed", "title_es": "Existente"}
    assert state["completed_count"] == (0 if fail else 1)


class SnapshotDB:
    def __init__(self, snapshot):
        self.row = {"id": 1, "source_type": "ai_original", "title": "Mercado title",
                    "review_status": "approved", "currency_id": "USD", "weight_g": 250,
                    "package_length_cm": 20, "package_width_cm": 10, "package_height_cm": 5,
                    "net_proceeds_usd": 8, "weight_basis": "ai_original_manual",
                    "source_snapshot_json": json.dumps(snapshot)}
        self.queries = []
        self.rowcount = 1
        self.committed = False
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, query, params=None):
        self.queries.append((query, params))
        if query.startswith("UPDATE"):
            fields = query.split(" SET ")[1].split(" WHERE ")[0].split(", ")
            for index, field in enumerate(fields):
                self.row[field.split("`")[1]] = params[index]
    def fetchone(self): return dict(self.row)
    def commit(self): self.committed = True
    def rollback(self): pass
    def close(self): pass


def test_nonmercado_snapshot_write_merges_latest_locked_data(monkeypatch):
    snapshot = {"ai_original": {"title_es": "Existente", "status": "completed"},
                "source": {"title": "Existente"}, "original_1688": {"title": "货源"},
                "source_1688_overrides": {"weight_g": 200},
                "marketplace_listings": {"amazon": {"title": "Latest edit"}}}
    db = SnapshotDB(snapshot)
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    row = store.update_ai_original_product(1, {
        "marketplace_listing": {"target_platform": "yandex", "template_id": "standard", "title": "Товар",
                                "review_status": "approved"},
        "title": "Should never replace Mercado", "review_status": "unreviewed",
    }, connection_factory=lambda: db)
    saved = json.loads(db.row["source_snapshot_json"])
    assert all(saved[key] == value for key, value in snapshot.items() if key != "marketplace_listings")
    assert saved["marketplace_listings"]["amazon"]["title"] == "Latest edit"
    assert row["marketplace_listings"]["yandex"]["title"] == "Товар"
    assert row["marketplace_listings"]["yandex"]["review_status"] == "unreviewed"
    assert db.row["title"] == "Mercado title"
    assert db.row["review_status"] == "approved"
    assert "FOR UPDATE" in db.queries[0][0]
    assert db.committed


def test_mercado_worker_stale_snapshot_preserves_other_platform_edits(monkeypatch):
    db = SnapshotDB({"marketplace_listings": {"yandex": {"title": "Latest edit"}},
                     "source_1688_overrides": {"weight_g": 250}})
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    row = store.update_ai_original_product(1, {"source_snapshot_json": {
        "ai_original": {"title_es": "Nuevo"},
        "marketplace_listings": {
            "yandex": {"title": "Stale version"},
            "amazon": {"title": "Injected approval", "review_status": "approved"},
        },
    }}, connection_factory=lambda: db)
    assert row["marketplace_listings"]["yandex"]["title"] == "Latest edit"
    assert row["marketplace_listings"]["mercadolibre"]["title_es"] == "Nuevo"
    assert row["marketplace_listings"]["amazon"]["review_status"] == "unreviewed"
    assert json.loads(db.row["source_snapshot_json"])["source_1688_overrides"]["weight_g"] == 250


@pytest.mark.parametrize("edit_same_platform", [False, True])
def test_translation_does_not_overwrite_changes_made_during_remote_call(monkeypatch, edit_same_platform):
    from erp import mercadolibre_translation

    db = SnapshotDB({"ai_original": {"title_es": "Old title"}, "original_1688": {"title": "收纳盒"}})
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    def translate(*args):
        current = json.loads(db.row["source_snapshot_json"])
        current["marketplace_listings"] = {"yandex": {"title": "Новый товар"}}
        if edit_same_platform:
            current["ai_original"]["title_es"] = "Concurrent edit"
        db.row["source_snapshot_json"] = json.dumps(current)
        return ["Organizador"]
    monkeypatch.setattr(mercadolibre_translation, "translate_texts", translate)
    if edit_same_platform:
        with pytest.raises(ValueError, match="已更新"):
            store.translate_ai_original_listing(1, "es", connection_factory=lambda: db)
        assert json.loads(db.row["source_snapshot_json"])["ai_original"]["title_es"] == "Concurrent edit"
    else:
        row = store.translate_ai_original_listing(1, "es", connection_factory=lambda: db)
        assert row["title_es"] == "Organizador"
        assert row["marketplace_listings"]["yandex"]["title"] == "Новый товар"
        assert row["review_status"] == "unreviewed"
        assert any("FOR UPDATE" in query for query, _ in db.queries)


def test_edit_platform_listing_does_not_touch_other_listing(monkeypatch):
    draft = {"target_platform": "yandex", "template_id": "standard", "title": "Товар",
             "description": "Описание товара", "status": "completed", "attributes": [], "bullet_points": [],
             "main_image_url": "/api/ai-original-products/images/1688-1yandex-ai-white.jpg",
             "review_status": "approved"}
    db = SnapshotDB({"ai_original": {"title_es": "Existente"}, "marketplace_listings": {"yandex": draft}})
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    row = store.update_ai_original_listing(1, {"target_platform": "yandex", "title": "Контейнер"}, connection_factory=lambda: db)
    assert row["marketplace_listings"]["yandex"]["title"] == "Контейнер"
    assert row["marketplace_listings"]["yandex"]["review_status"] == "unreviewed"
    assert row["title_es"] == "Existente"
    assert db.row["title"] == "Mercado title"
    assert db.row["review_status"] == "approved"
    assert db.committed


def test_export_route_rejects_non_ai_product_and_uses_attachment():
    app = Flask(__name__)
    route = load_route("api_export_ai_original_listing", db_get_mercado_product_items_by_ids=lambda ids: [{"source_type": "collected"}])
    with app.test_request_context("/?target_platform=yandex"):
        _, status = route(1)
    assert status == 404
    route = load_route("api_export_ai_original_listing", db_get_mercado_product_items_by_ids=lambda ids: [{
        "id": 1, "source_type": "ai_original", "source_snapshot_json": {"marketplace_listings": {"yandex": {
            "title": "Товар", "description": "Описание", "status": "completed", "main_image_url": "/image.jpg",
        }}},
    }])
    with app.test_request_context("/?target_platform=yandex"):
        response = route(1)
    assert response.get_json()["draft_only"] is True
    assert response.get_json()["ready_for_submission"] is False
    assert response.get_json()["listing"]["title"] == "Товар"
    assert response.headers["Content-Disposition"] == 'attachment; filename="ai-original-1-yandex.json"'


@pytest.mark.parametrize('status', ['approved', 'unreviewed', 'failed', 'suspected'])
def test_platform_manual_review_is_saved_only_in_requested_draft(monkeypatch, status):
    draft = {'target_platform': 'yandex', 'template_id': 'standard', 'title': 'Товар',
             'description': 'Описание товара', 'status': 'completed', 'main_image_url': '/image.jpg'}
    db = SnapshotDB({'ai_original': {'title_es': 'Existente'}, 'marketplace_listings': {'yandex': draft}})
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    row = store.update_ai_original_listing(1, {'target_platform': 'yandex', 'review_status': status}, connection_factory=lambda: db)
    assert row['marketplace_listings']['yandex']['review_status'] == status
    assert db.row['review_status'] == 'approved'
    assert row['title_es'] == 'Existente'
    assert db.committed


@pytest.mark.parametrize('change', [{'status': 'failed'}, {'main_image_url': ''}, {'title': ''}])
def test_platform_cannot_approve_unfinished_draft(monkeypatch, change):
    draft = {'target_platform': 'yandex', 'template_id': 'standard', 'title': 'Товар',
             'description': 'Описание товара', 'status': 'completed', 'main_image_url': '/image.jpg', **change}
    db = SnapshotDB({'marketplace_listings': {'yandex': draft}})
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    with pytest.raises(ValueError, match='需完成 AI 制作'):
        store.update_ai_original_listing(1, {'target_platform': 'yandex', 'review_status': 'approved'}, connection_factory=lambda: db)
    assert not db.committed


def test_platform_cannot_approve_when_shared_manual_facts_are_missing(monkeypatch):
    draft = {'target_platform': 'yandex', 'template_id': 'standard', 'title': 'Товар',
             'description': 'Описание товара', 'status': 'completed', 'attributes': [],
             'bullet_points': [], 'main_image_url': 'https://cdn.example.com/a.jpg'}
    db = SnapshotDB({'marketplace_listings': {'yandex': draft}})
    db.row['weight_g'] = None
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)

    with pytest.raises(ValueError, match='实重'):
        store.update_ai_original_listing(
            1, {'target_platform': 'yandex', 'review_status': 'approved'},
            connection_factory=lambda: db,
        )

    assert not db.committed


def test_platform_cannot_approve_while_row_has_infringement_risk(monkeypatch):
    draft = {'target_platform': 'yandex', 'template_id': 'standard', 'title': 'Товар',
             'description': 'Описание товара', 'status': 'completed',
             'main_image_url': 'https://cdn.example.com/a.jpg'}
    db = SnapshotDB({'marketplace_listings': {'yandex': draft}})
    db.row['review_status'] = 'infringing'
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)

    with pytest.raises(ValueError, match='侵权风险'):
        store.update_ai_original_listing(
            1, {'target_platform': 'yandex', 'review_status': 'approved'},
            connection_factory=lambda: db,
        )

    assert not db.committed


def test_platform_content_edit_and_approval_must_be_separate(monkeypatch):
    draft = {'target_platform': 'yandex', 'template_id': 'standard', 'title': 'Товар',
             'description': 'Описание товара', 'status': 'completed', 'attributes': [],
             'bullet_points': [], 'main_image_url': 'https://cdn.example.com/a.jpg',
             'review_status': 'unreviewed'}
    db = SnapshotDB({'marketplace_listings': {'yandex': draft}})
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)

    with pytest.raises(ValueError, match='审核通过必须单独保存'):
        store.update_ai_original_listing(
            1,
            {'target_platform': 'yandex', 'title': 'Новый товар',
             'review_status': 'approved'},
            connection_factory=lambda: db,
        )

    assert not db.committed
    assert json.loads(db.row['source_snapshot_json'])['marketplace_listings']['yandex'] == draft


def test_platform_edit_rejects_product_outside_actor_scope(monkeypatch):
    draft = {'target_platform': 'yandex', 'template_id': 'standard', 'title': 'Товар',
             'description': 'Описание товара', 'status': 'completed',
             'main_image_url': 'https://cdn.example.com/a.jpg'}
    db = SnapshotDB({'marketplace_listings': {'yandex': draft}})
    db.row.update(organization_key='wuhan-zeshun', owner_user_id=99,
                  owner_name='别人', visibility='enterprise')
    actor = {'access_version': 1, 'id': 7, 'display_name': '当前用户',
             'role_key': 'member', 'organization_key': 'wuhan-zeshun'}
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)

    with product_access.scope(actor), pytest.raises(PermissionError, match='操作权限'):
        store._update_ai_platform_listing(
            1, {'target_platform': 'yandex', 'title': 'Новый товар'}, 'yandex',
            connection_factory=lambda: db,
        )

    assert not db.committed
