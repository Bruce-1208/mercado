from copy import deepcopy

import pytest

from erp.ai_category_reference import (
    compact_product_facts,
    product_type_reference,
    select_reference_category,
)


@pytest.mark.parametrize(("title", "query", "expected"), [
    ("万圣节儿童表演服", "Halloween costume", "costume"),
    ("万圣节南瓜灯串", "Halloween string lights", "string_lights"),
    ("不锈钢保温杯", "Insulated water bottle", "thermos"),
    ("入耳式耳机", "in-ear headphones", "earbuds"),
    ("手机保护壳", "phone_case", "phone_case"),
    ("大容量水瓶", "", "water_bottle"),
    ("儿童夜灯", "", "night_light"),
])
def test_identifies_concrete_types_and_prioritizes_specific_type(title, query, expected):
    assert product_type_reference({"title": title}, query)["key"] == expected


@pytest.mark.parametrize(("title", "query"), [
    ("万圣节派对装饰", "Halloween decorations"),
    ("儿童表演服和假发套装", "costume and wig set"),
    ("宠物项圈牵引绳套装", "pet collar and leash"),
    ("儿童夜灯", "water bottle"),
    ("假发支架", "wig stand"),
    ("假发支架", "wig"),
    ("儿童表演服和假发套装", "costume wig set"),
    ("水瓶刷", "water bottle brush"),
    ("派对贴纸", "Halloween costume decorations"),
    ("耳机收纳盒", "headphone case"),
    ("", ""),
])
def test_unknown_accessory_or_conflicting_identity_abstains(title, query):
    assert product_type_reference({"title": title}, query) is None


def test_does_not_treat_description_or_source_category_as_product_identity():
    assert product_type_reference({
        "title": "多用途商品", "category_name": "Costumes",
        "description_text": "Also useful with night lights and phone cases",
        "properties": [{"name": "适用", "value": "假发"}],
    }) is None


def test_uses_only_unique_compatible_live_id_and_copies_candidate():
    candidates = [
        {"category_id": "CBT111", "category_name": "Holiday Decorations"},
        {"category_id": "CBT222", "category_name": "Disfraces", "domain_name": "Costumes"},
        {"category_id": "CBT333", "category_name": "Party Printing Kits"},
    ]
    result = select_reference_category({"title": "万圣节儿童表演服"}, "costume", candidates)
    assert result == candidates[1]
    result["category_name"] = "changed"
    assert candidates[1]["category_name"] == "Disfraces"


@pytest.mark.parametrize("names", [
    [("Wigs", "Beauty wigs"), ("Wigs", "Party wigs")],
    [("Night Lights", "Lighting"), ("Luces Nocturnas", "Lighting")],
])
def test_multiple_live_categories_abstain_regardless_of_candidate_order(names):
    query = "wig" if names[0][0] == "Wigs" else "night light"
    candidates = [{"category_id": f"CBT{index}", "category_name": name, "domain_name": domain}
                  for index, (name, domain) in enumerate(names)]
    assert select_reference_category({}, query, candidates) is None
    assert select_reference_category({}, query, reversed(candidates)) is None


@pytest.mark.parametrize("candidate", [
    {"category_id": "CBT12", "category_name": "Wig Stands", "domain_name": "Wigs"},
    {"category_id": "CBT12", "category_name": "Accessories", "domain_name": "Wigs"},
    {"category_id": "CBT12", "category_name": "Other", "domain_name": "Wigs"},
    {"category_id": "CBT12", "category_name": "Costumes", "domain_name": "Wigs"},
    {"category_id": "MLM12", "category_name": "Wigs"},
    {"category_id": "", "category_name": "Wigs"},
])
def test_parent_domain_or_invalid_id_cannot_confirm_a_category(candidate):
    assert select_reference_category({"title": "假发"}, "wig", [candidate]) is None


def test_latin_aliases_handle_accents_and_domain_only_candidates():
    candidate = {"category_id": "CBT12", "domain_name": "Garrafas Térmicas"}
    assert select_reference_category({"title": "保温杯"}, "thermos", [candidate]) == candidate


def test_duplicate_ids_do_not_create_ambiguity_but_conflicting_duplicates_abstain():
    candidate = {"category_id": "CBT12", "category_name": "Wigs"}
    assert select_reference_category({}, "wig", [candidate, candidate]) == candidate
    assert select_reference_category({}, "wig", [
        candidate, {"category_id": "CBT12", "category_name": "Costumes"},
    ]) is None


def test_compaction_removes_only_envelope_noise_and_preserves_all_sku_facts():
    original = {
        "source_1688_item_id": "00123", "title": "灯串", "price": 10,
        "images": ["https://example.invalid/image"], "source_url": "https://example.invalid/item",
        "description_text": "规格：" + "长" * 10000, "weight_g": 85,
        "properties": [{"name": "包装数量", "value": 10, "price": "printed label price"}],
        "custom_fact": {"material": "copper"},
        "variations": [{
            "id": str(index), "sku_id": f"sku-{index}", "seller_custom_field": f"seller-{index}",
            "price": index + 1, "stock": 100, "picture_ids": ["a"], "quantity": 2,
            "attribute_combinations": [
                {"id": "STYLE", "value_id": f"v-{index}", "value_name": f"款式{index}"},
                {"name": "包装", "value_name": "2件装"},
            ], "source_remarks": [{"name": "备注", "value_name": "电池款"}],
        } for index in range(150)],
    }
    before = deepcopy(original)
    compact = compact_product_facts(original)
    assert "price" not in compact and "images" not in compact and "source_url" not in compact
    assert compact["description_text"] == original["description_text"]
    assert compact["properties"] == original["properties"]
    assert compact["custom_fact"] == original["custom_fact"]
    assert len(compact["variations"]) == 150
    for before_row, after_row in zip(original["variations"], compact["variations"]):
        assert after_row == {key: value for key, value in before_row.items()
                             if key not in {"price", "stock", "picture_ids"}}
    compact["variations"][0]["attribute_combinations"][0]["value_name"] = "changed"
    assert original == before


def test_compaction_keeps_malformed_sku_rows_for_caller_validation():
    assert compact_product_facts({"skus": [None, "bad", {"id": "01", "inventory": 5}]}) == {
        "skus": [None, "bad", {"id": "01"}],
    }
