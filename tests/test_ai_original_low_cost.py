"""End-to-end preparation with a mocked metered transport, never paid APIs."""

from copy import deepcopy
from datetime import datetime
import json

import pytest

from erp import ai_generation_budget as budgets
from erp import ai_original_products as products


class LiveCategories:
    def __init__(self):
        self.calls = []
        self.candidates = [
            {"category_id": "CBT123", "category_name": "Water Bottles"},
            {"category_id": "CBT456", "category_name": "Costumes"},
        ]
        self.schema = [{
            "id": "MATERIAL", "name": "Material", "value_type": "list",
            "tags": {"required": True}, "values": [{"id": "steel", "name": "Steel"}],
        }]

    def request(self, method, path, **kwargs):
        self.calls.append((path, deepcopy(kwargs)))
        if "domain_discovery" in path:
            return deepcopy(self.candidates)
        assert path == "/categories/CBT123/attributes"
        return deepcopy(self.schema)


@pytest.fixture
def flow(monkeypatch, tmp_path):
    real_budget = budgets.BudgetedChat
    monkeypatch.setattr(budgets, "BudgetedChat", lambda **kwargs: real_budget(
        now=lambda: datetime(2026, 10, 3, 10, tzinfo=budgets.BEIJING), **kwargs))
    image_path = tmp_path / "white.jpg"
    image_path.write_bytes(b"local image stub")
    state = {"calls": [], "checkpoints": [], "images": 0, "extra_attributes": []}

    def make_image(*args, **kwargs):
        state["images"] += 1
        return "/api/ai-original-products/images/white.jpg", "local_background_removal", str(image_path)

    monkeypatch.setattr(products, "_prepare_original_image", make_image)
    generated = {
        "title_es": "Botella de acero", "title_pt": "Garrafa de aço",
        "description_es": "Botella de acero para llevar agua.",
        "description_pt": "Garrafa de aço para levar água.",
        "attributes": [{"name": "Material", "value_name": "Steel"}],
        "product_type_en": "water bottle", "brand_terms": [],
    }

    def save(step, checkpoint):
        state["checkpoints"].append((step, deepcopy(checkpoint)))

    def transport(**kwargs):
        # Verify the preparation integration saves a pending reservation through
        # its checkpoint callback before handing control to a billed transport.
        ledger = state["checkpoints"][-1][1]["generation_budget"]
        assert ledger["calls"][-1]["state"] == "pending"
        assert ledger["reserved_yuan"] > 0
        assert ledger["spent_yuan"] + ledger["reserved_yuan"] <= 0.03
        assert kwargs["thinking"] is False
        assert kwargs["model"] == "qwen3.7-flash"
        assert kwargs["max_retries"] == 0
        state["calls"].append(deepcopy(kwargs))
        prompt = kwargs["messages"][0]["content"]
        if "将1688规格选项" in prompt:
            content = {"options": [
                {"option_id": 0, "attributes": [{"id": "COLOR", "value_name": "Red"}]},
                {"option_id": 1, "attributes": [{"id": "COLOR", "value_name": "Blue"}]},
            ]}
        elif "补全遗漏" in prompt:
            content = {"attributes": deepcopy(state["extra_attributes"])}
        else:
            assert "一次生成美客多JSON文案" in prompt, "unexpected duplicate paid category/attribute pass"
            content = deepcopy(generated)
        return {"content": json.dumps(content, ensure_ascii=False), "model": "qwen3.7-flash",
                "finish_reason": "stop", "usage": {"prompt_tokens": 100, "completion_tokens": 100}}

    row = {"id": 1, "source_item_id": "1688001", "source_snapshot_json": {"original_1688": {
        "source_1688_item_id": "001", "title": "不锈钢水瓶", "description_text": "材质不锈钢",
        "properties": [{"name": "材质", "value_name": "不锈钢"}],
        "main_image_url": "https://example.invalid/source.jpg", "variations": [],
    }}}
    options = {"api_key": "test-dashscope-key", "cost_control": True, "budget_transport": transport, "on_preparation_checkpoint": save}
    return row, LiveCategories(), state, options


def saved_row(row, result):
    return {**deepcopy(row), "source_snapshot_json": json.loads(result["source_snapshot_json"])}


def test_one_paid_copy_reuses_reference_and_translations_against_live_schema(flow):
    row, client, state, options = flow
    result = products.prepare_ai_original_product(row, category_client=client, **options)
    assert result["ai_original"]["status"] == "completed"
    assert len(state["calls"]) == 1
    assert result["ai_original"]["attributes"] == [
        {"id": "MATERIAL", "name": "Material", "value_name": "Steel", "value_id": "steel"}]
    assert [path for path, _ in client.calls] == [
        "/marketplace/domain_discovery/search", "/categories/CBT123/attributes"]
    ledger = result["ai_original"]["generation_budget"]
    assert ledger["spent_yuan"] == 0.0001
    assert ledger["reserved_yuan"] == 0
    assert ledger["calls"][0]["state"] == "complete"


def test_completed_result_skips_all_generation_and_schema_calls(flow):
    row, client, state, options = flow
    first = products.prepare_ai_original_product(row, category_client=client, **options)
    second = products.prepare_ai_original_product(saved_row(row, first), category_client=client, **options)
    assert second["ai_original"]["status"] == "completed"
    assert len(state["calls"]) == state["images"] == 1
    assert [path for path, _ in client.calls].count("/categories/CBT123/attributes") == 1
    assert [path for path, _ in client.calls].count("/marketplace/domain_discovery/search") == 1
    assert second["ai_original"]["generation_budget"]["spent_yuan"] == 0.0001


def test_changed_product_facts_invalidate_copy_without_resetting_budget(flow):
    row, client, state, options = flow
    first = products.prepare_ai_original_product(row, category_client=client, **options)
    changed = saved_row(row, first)
    changed["source_snapshot_json"]["ai_original"]["status"] = "failed"
    changed["source_snapshot_json"]["original_1688"]["title"] = "不锈钢水瓶750ml"
    second = products.prepare_ai_original_product(changed, category_client=client, **options)
    assert len(state["calls"]) == 2 and state["images"] == 1
    assert "750ml" in state["calls"][-1]["messages"][0]["content"]
    assert second["ai_original"]["generation_budget"]["spent_yuan"] == 0.0002
    assert len(second["ai_original"]["generation_budget"]["calls"]) == 2


def test_live_schema_drift_invalidates_old_attribute_ids_without_new_copy(flow):
    row, client, state, options = flow
    first = products.prepare_ai_original_product(row, category_client=client, **options)
    client.schema[0]["id"] = "BODY_MATERIAL"
    retry = saved_row(row, first)
    retry["source_snapshot_json"]["ai_original"]["status"] = "failed"
    second = products.prepare_ai_original_product(retry, category_client=client, **options)
    assert len(state["calls"]) == 1
    assert [item["id"] for item in second["ai_original"]["attributes"]] == ["BODY_MATERIAL"]


def test_old_budget_exhaustion_resumes_without_cap(flow):
    row, client, state, options = flow
    ledger = budgets.BudgetedChat(cap_yuan="0.000001").snapshot()
    ledger.update(blocked_kind="budget_exhausted", blocked_reason="old limit")
    row["source_snapshot_json"]["ai_original"] = {"generation_budget": ledger}
    result = products.prepare_ai_original_product(row, category_client=client, **options)
    assert result["ai_original"]["generation_budget"]["budget_limit_enabled"] is False
    assert state["calls"]


def test_failed_reservation_checkpoint_prevents_paid_dispatch(flow):
    row, client, state, options = flow
    def failed_save(step, checkpoint):
        if any(call["state"] == "pending" for call in checkpoint["generation_budget"]["calls"]):
            raise OSError("storage unavailable")
    options["on_preparation_checkpoint"] = failed_save
    with pytest.raises(products.OriginalPreparationError) as failure:
        products.prepare_ai_original_product(row, category_client=client, **options)
    assert failure.value.state["generation_budget"]["blocked_kind"] == "ledger_save_failed"
    assert state["calls"] == []


def test_sku_identity_inventory_prices_and_media_are_preserved_outside_prompt(flow):
    row, client, state, options = flow
    source_skus = [{
        "id": f"sku-{index}", "seller_sku": f"seller-{index}", "price": 10 + index,
        "available_quantity": 8 + index, "image_url": f"https://example.invalid/{index}.jpg",
        "attribute_combinations": [{"id": "source-color", "name": "颜色", "value_name": color}],
    } for index, color in enumerate(("红色", "蓝色"))]
    row["source_snapshot_json"]["original_1688"]["variations"] = deepcopy(source_skus)
    client.schema.append({"id": "COLOR", "name": "Color", "hierarchy": "CHILD_PK"})
    result = products.prepare_ai_original_product(row, category_client=client, **options)
    assert len(state["calls"]) == 2
    for actual, original in zip(result["ai_original"]["variations"], source_skus):
        assert {key: value for key, value in actual.items() if key != "attribute_combinations"} == {
            key: value for key, value in original.items() if key != "attribute_combinations"}
    copy_prompt = state["calls"][0]["messages"][0]["content"]
    assert "sku-0" in copy_prompt and "sku-1" in copy_prompt
    assert "available_quantity" not in copy_prompt and "image_url" not in copy_prompt
    assert json.loads(result["source_snapshot_json"])["original_1688"]["variations"] == source_skus


def test_supplier_enum_id_collision_cannot_override_the_translated_fact():
    schema = [{"id": "MATERIAL", "name": "Material", "value_type": "list", "tags": {"required": True},
               "values": [{"id": "source-42", "name": "Plastic"}, {"id": "ml-steel", "name": "Steel"}]}]
    mapped = products._copy_attributes_for_schema({"attributes": [
        {"name": "Material", "value_id": "source-42", "value_name": "Steel"},
    ]}, schema)
    assert mapped[0]["value_id"] == "ml-steel"
    assert mapped[0]["value_name"] == "Steel"


def test_low_cost_required_retry_never_authorizes_invented_facts():
    prompts = []
    def chat(messages, **kwargs):
        prompts.append(deepcopy(messages))
        return '{"attributes": []}'
    chat.cost_control = True
    products.complete_required_attributes({}, [], [
        {"id": "MATERIAL", "tags": {"required": True}},
    ], chat=chat)
    assert len(prompts) == 2
    assert all("允许自行编造" not in message["content"] for call in prompts for message in call)


def test_known_product_reference_rejection_does_not_accept_wrong_single_candidate():
    class Discovery:
        def request(self, *args, **kwargs):
            return [{"category_id": "CBT123", "category_name": "Holiday Decorations"}]
    calls = []
    def chat(messages, **kwargs):
        calls.append(messages)
        return '{"category_id":"", "search_query":"costume"}'
    chat.cost_control = True
    with pytest.raises(products.OriginalPreparationError):
        products._select_marketplace_category(
            {"title": "万圣节儿童表演服"}, "costume",
            [{"category_id": "CBT123", "category_name": "Holiday Decorations"}], Discovery(), chat=chat)
    assert calls
