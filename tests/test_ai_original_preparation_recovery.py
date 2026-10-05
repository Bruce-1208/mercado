"""Preparation checkpoints resume failed work without paid image/copy regeneration."""

import ast
import concurrent.futures
from copy import deepcopy
import json
import logging
from pathlib import Path
import threading

import pytest

from erp import ai_original_products as products


class PreparationClient:
    def __init__(self):
        self.calls = []
        self.discover = True
        self.schema = [
            {"id": "MODEL", "hierarchy": "PARENT_PK"},
            {"id": "SIZE", "hierarchy": "CHILD_PK"},
            {"id": "MATERIAL", "tags": {"required": True}},
        ]

    def request(self, method, path, **kwargs):
        self.calls.append((path, deepcopy(kwargs)))
        if "domain_discovery" in path:
            return [{"category_id": "CBT123", "category_name": "Costumes"}] if self.discover else []
        assert path == "/categories/CBT123/attributes"
        return deepcopy(self.schema)


@pytest.fixture
def preparation(monkeypatch, tmp_path):
    counts = dict(image=0, copy=0, attributes=0, variations=0, required=0)
    control = dict(bad_variations=False, missing_material=False, bad_attributes=False)
    row = {"id": 1, "source_item_id": "1688123", "source_snapshot_json": {
        "original_1688": {
            "source_1688_item_id": "123", "title": "儿童服装",
            "main_image_url": "https://example.com/source.jpg",
            "variations": [
                {"id": "s", "price": 10, "available_quantity": 4, "seller_sku": "small",
                 "image_url": "/s.jpg", "attribute_combinations": [{"name": "尺码", "value_name": "S"}]},
                {"id": "m", "price": 12, "available_quantity": 5, "seller_sku": "medium",
                 "image_url": "/m.jpg", "attribute_combinations": [{"name": "尺码", "value_name": "M"}]},
            ],
        },
        "marketplace_listings": {"amazon": {"title": "Other draft"}},
    }}
    copy = {"title_es": "Disfraz", "title_pt": "Fantasia", "description_es": "Un disfraz.",
            "description_pt": "Uma fantasia.", "product_type_en": "Children costume", "attributes": []}
    def make_image(*args, **kwargs):
        counts["image"] += 1
        path = tmp_path / "image.jpg"
        path.write_bytes(b"image")
        return path, "/api/ai-original-products/images/1688-123-ai-white.jpg"
    def make_copy(*args, **kwargs):
        counts["copy"] += 1
        return deepcopy(copy)
    def chat(messages, **kwargs):
        prompt = messages[0]["content"]
        if "选择与原始商品实体" in prompt:
            return json.dumps({"category_id": "", "search_query": "Children costume"})
        if "将1688规格选项" in prompt:
            counts["variations"] += 1
            if control["bad_variations"]:
                return '{"options": []}'
            return json.dumps({"options": [
                {"option_id": 0, "attributes": [{"id": "SIZE", "value_name": "S"}]},
                {"option_id": 1, "attributes": [{"id": "SIZE", "value_name": "M"}]},
            ]})
        if "补全遗漏" in prompt:
            counts["required"] += 1
            return '{"attributes": []}'
        counts["attributes"] += 1
        if control["bad_attributes"]:
            return "not JSON"
        attrs = [{"id": "MODEL", "value_name": "Costume"}]
        if not control["missing_material"]:
            attrs.append({"id": "MATERIAL", "value_name": "Synthetic"})
        return json.dumps({"attributes": attrs})
    monkeypatch.setattr(products, "_download_source_image", lambda *a, **k: b"image")
    monkeypatch.setattr(products, "create_white_background_image", make_image)
    monkeypatch.setattr(products, "generate_marketplace_copy", make_copy)
    return row, PreparationClient(), counts, control, chat


def retry_row(row, state):
    retry = deepcopy(row)
    retry["source_snapshot_json"]["ai_original"] = deepcopy(state)
    return retry


def test_category_failure_saves_copy_and_retries_without_new_image_or_copy(preparation):
    row, client, counts, _, chat = preparation
    client.discover = False
    checkpoints = []
    with pytest.raises(products.OriginalPreparationError) as failure:
        products.prepare_ai_original_product(row, category_client=client, chat=chat,
            on_preparation_checkpoint=lambda step, state: checkpoints.append((step, state)))
    error = failure.value
    assert error.step == "category"
    assert error.state["workflow_status"] == "needs_category"
    assert error.state["preparation"]["copy_ready"] is True
    assert error.state["title_es"] == "Disfraz"
    assert checkpoints[-1][1]["generated_copy"]["product_type_en"] == "Children costume"
    client.discover = True
    result = products.prepare_ai_original_product(retry_row(row, error.state), category_client=client, chat=chat)
    assert result["ai_original"]["status"] == "completed"
    assert counts["image"] == counts["copy"] == 1
    assert "ai_original" not in row["source_snapshot_json"]
    assert json.loads(result["source_snapshot_json"])["marketplace_listings"]["amazon"] == {"title": "Other draft"}


def test_variation_failure_reuses_category_attributes_but_fetches_live_schema(preparation):
    row, client, counts, control, chat = preparation
    control["bad_variations"] = True
    with pytest.raises(products.OriginalPreparationError) as failure:
        products.prepare_ai_original_product(row, category_client=client, chat=chat)
    error = failure.value
    assert error.step == "variations"
    assert error.state["workflow_status"] == "needs_variations"
    assert error.state["preparation"]["category_ready"] is True
    assert error.state["preparation"]["variations_ready"] is False
    assert counts["variations"] == 3
    control["bad_variations"] = False
    result = products.prepare_ai_original_product(retry_row(row, error.state), category_client=client, chat=chat)
    assert counts == dict(image=1, copy=1, attributes=1, variations=4, required=0)
    assert sum("domain_discovery" in path for path, _ in client.calls) == 1
    assert sum("/attributes" in path for path, _ in client.calls) == 2
    source = row["source_snapshot_json"]["original_1688"]["variations"]
    for actual, original in zip(result["ai_original"]["variations"], source):
        assert {key: actual[key] for key in ("id", "seller_sku", "price", "available_quantity", "image_url")} == {
            key: original[key] for key in ("id", "seller_sku", "price", "available_quantity", "image_url")}


def test_malformed_attributes_keep_selected_category_and_resume_at_attribute_generation(preparation):
    row, client, counts, control, chat = preparation
    control["bad_attributes"] = True
    with pytest.raises(products.OriginalPreparationError) as failure:
        products.prepare_ai_original_product(row, category_client=client, chat=chat)
    assert failure.value.step == "attributes"
    assert failure.value.state["preparation"]["category_ready"] is True
    assert failure.value.state["category_checkpoint"]["attributes_ready"] is False
    control["bad_attributes"] = False
    result = products.prepare_ai_original_product(retry_row(row, failure.value.state), category_client=client, chat=chat)
    assert result["ai_original"]["status"] == "completed"
    assert counts["image"] == counts["copy"] == 1
    assert counts["attributes"] == 2
    assert sum("domain_discovery" in path for path, _ in client.calls) == 1


def test_new_manual_lock_retains_corrected_copy_and_manual_attributes(preparation):
    row, client, counts, control, chat = preparation
    control["bad_variations"] = True
    with pytest.raises(products.OriginalPreparationError) as failure:
        products.prepare_ai_original_product(row, category_client=client, chat=chat)
    state = failure.value.state
    state["category_locked"] = True
    state["preparation"]["category_ready"] = False
    state["generated_copy"]["title_es"] = "Disfraz corregido"
    state["manual_attributes"] = [{"id": "MATERIAL", "value_name": "Polyester"}]
    control["bad_variations"] = False
    result = products.prepare_ai_original_product(retry_row(row, state), category_client=client, chat=chat)
    assert result["title"] == "Disfraz corregido"
    assert next(a for a in result["ai_original"]["attributes"] if a["id"] == "MATERIAL")["value_name"] == "Polyester"
    assert counts["image"] == counts["copy"] == 1


def test_manual_required_attribute_resumes_without_remapping_skus(preparation):
    row, client, counts, control, chat = preparation
    control["missing_material"] = True
    with pytest.raises(products.OriginalPreparationError) as failure:
        products.prepare_ai_original_product(row, category_client=client, chat=chat)
    state = failure.value.state
    assert failure.value.step == "attributes"
    assert state["preparation"]["variations_ready"] is True
    state["attributes"].append({"id": "MATERIAL", "value_name": "Synthetic"})
    result = products.prepare_ai_original_product(retry_row(row, state), category_client=client, chat=chat)
    assert result["ai_original"]["missing_required_attributes"] == []
    assert counts == dict(image=1, copy=1, attributes=1, variations=1, required=2)


def test_schema_drift_invalidates_cached_mapping(preparation):
    row, client, counts, control, chat = preparation
    control["bad_variations"] = True
    with pytest.raises(products.OriginalPreparationError) as failure:
        products.prepare_ai_original_product(row, category_client=client, chat=chat)
    client.schema.append({"id": "NEW_FIELD"})
    control["bad_variations"] = False
    products.prepare_ai_original_product(retry_row(row, failure.value.state), category_client=client, chat=chat)
    assert counts["image"] == counts["copy"] == 1
    assert counts["attributes"] == 2
    assert counts["variations"] == 4


def test_locked_category_is_retained_after_success_and_regeneration(preparation):
    row, client, _, _, chat = preparation
    row["category_id"] = "CBT123"
    row["category_name"] = "Manual costumes"
    row["source_snapshot_json"]["ai_original"] = {"category_locked": True}
    result = products.prepare_ai_original_product(row, category_client=client, chat=chat)
    assert result["ai_original"]["category_locked"] is True
    assert result["category_id"] == "CBT123"
    assert all("domain_discovery" not in path for path, _ in client.calls)
    result_row = deepcopy(row)
    result_row["source_snapshot_json"] = json.loads(result["source_snapshot_json"])
    products.prepare_ai_original_product(result_row, category_client=client, chat=chat)
    assert all("domain_discovery" not in path for path, _ in client.calls)


def test_manual_sku_override_used_without_modifying_collected_snapshot(preparation, monkeypatch):
    row, client, _, _, chat = preparation
    raw = deepcopy(row["source_snapshot_json"]["original_1688"])
    override = deepcopy(raw["variations"])
    override[0]["available_quantity"] = 90
    row["source_snapshot_json"]["ai_original"] = {"source_variations": override}
    result = products.prepare_ai_original_product(row, category_client=client, chat=chat)
    assert result["ai_original"]["variations"][0]["available_quantity"] == 90
    assert json.loads(result["source_snapshot_json"])["original_1688"] == raw


def test_worker_persists_partial_copy_and_actionable_failure(monkeypatch):
    node = next(n for n in ast.parse(Path("bit/bit_interface.py").read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == "_run_ai_original_task")
    saved = []
    state = {}
    def prepare(row, **kwargs):
        partial = {"category_id": "CBT123", "category_name": "Masks", "title_es": "Saved copy", "generated_copy": {"title_es": "Saved copy"},
                   "preparation": {"version": 1, "copy_ready": True}}
        kwargs["on_preparation_checkpoint"]("copy", partial)
        raise products.OriginalPreparationError("variations", "Check specifications", {
            **partial, "failed_step": "variations", "workflow_status": "needs_variations"})
    monkeypatch.setattr(products, "prepare_ai_original_product", prepare)
    namespace = dict(json=json, logging=logging, ThreadPoolExecutor=concurrent.futures.ThreadPoolExecutor,
                     as_completed=concurrent.futures.as_completed, _ai_original_task_lock=threading.Lock(),
                     _ai_original_task_state=state,
                     db_update_ai_original_product=lambda pid, changes: saved.append(deepcopy(changes)))
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<worker>", "exec"), namespace)
    namespace["_run_ai_original_task"]([{"id": 1}], workers=1)
    assert saved[-1]["category_id"] == "CBT123"
    failure = saved[-1]["source_snapshot_json"]["ai_original"]
    assert failure["title_es"] == "Saved copy"
    assert failure["preparation"]["copy_ready"] is True
    assert failure["workflow_status"] == "needs_variations"
    assert state["results"][0]["failed_step"] == "variations"
    assert state["failed_count"] == 1
