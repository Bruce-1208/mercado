import copy
import json

import pytest

from erp.ai_original_products import normalize_marketplace_variations


SHARED_REMARK = {"name": "←注", "value_name": "此款是(皮料,好质量)"}
COLOR_SCHEMA = [{"id": "COLOR", "hierarchy": "CHILD_PK"}]


def source_options(messages):
    return json.loads(messages[0]["content"].rsplit("\n", 1)[1])["options"]


def color_response(options):
    colors = {"黑色": "Black", "红色": "Red"}
    return json.dumps({"options": [
        {"option_id": option["option_id"], "attributes": [
            {"id": "COLOR", "value_name": colors[option["source"]["value_name"]]}
        ] if option["source"].get("name") == "颜色" else []}
        for option in options
    ]})


def original_with_remarks(remarks):
    return {"title": "Costume", "variations": [
        {"id": f"sku-{index}", "seller_sku": f"SUP-{index}", "price": index + 2,
         "available_quantity": index + 7, "image_url": f"https://example.com/{index}.jpg",
         "images": [{"url": f"https://example.com/{index}-detail.jpg"}],
         "supplier_data": {"warehouse": "CN", "codes": [index]},
         "attribute_combinations": [{"name": "颜色", "value_name": color}, copy.deepcopy(remark)]}
        for index, (color, remark) in enumerate(zip(["黑色", "红色"], remarks))
    ]}


def test_shared_material_remark_removed_from_identity_and_preserved_with_all_sku_facts():
    original = original_with_remarks([SHARED_REMARK, SHARED_REMARK])
    before = copy.deepcopy(original)
    calls = []

    def chat(messages, **kwargs):
        options = source_options(messages)
        calls.append(options)
        assert [option["source"]["name"] for option in options] == ["颜色", "颜色"]
        return color_response(options)

    result = normalize_marketplace_variations(original, COLOR_SCHEMA, chat=chat)

    assert len(calls) == 1
    assert original == before
    assert len(result) == len(before["variations"])
    for source, mapped in zip(before["variations"], result):
        assert mapped["source_remarks"] == [SHARED_REMARK]
        assert {key: value for key, value in mapped.items()
                if key not in {"attribute_combinations", "source_remarks"}} == {
                    key: value for key, value in source.items() if key != "attribute_combinations"}
        assert [attribute["id"] for attribute in mapped["attribute_combinations"]] == ["COLOR"]
    result[0]["source_remarks"][0]["value_name"] = "changed"
    result[0]["supplier_data"]["codes"].append(100)
    assert original == before


def test_shared_remark_matches_normalized_dimension_and_value():
    remarks = [{"name": "← 注：", "value_name": "此款是(皮料,好质量)"},
               {"name": "注", "value_name": "  此款是（皮料，好质量）  "}]
    original = original_with_remarks(remarks)
    before = copy.deepcopy(original)
    result = normalize_marketplace_variations(
        original, COLOR_SCHEMA, chat=lambda messages, **kwargs: color_response(source_options(messages)))
    assert [variation["source_remarks"] for variation in result] == [[remarks[0]], [remarks[1]]]
    assert original == before


def test_differing_material_remarks_are_not_omitted():
    remarks = [{"name": "←注", "value_name": "此款是皮料"},
               {"name": "←注", "value_name": "此款是网纱"}]
    original = original_with_remarks(remarks)
    before = copy.deepcopy(original)
    calls = []

    def chat(messages, **kwargs):
        options = source_options(messages)
        calls.append(options)
        assert [option["source"] for option in options
                if option["source"]["name"] == "←注"] == remarks
        return color_response(options)

    with pytest.raises(ValueError, match="自动映射已尝试 3 次：规格无法映射"):
        normalize_marketplace_variations(original, COLOR_SCHEMA, chat=chat)
    assert len(calls) == 3
    assert original == before


def test_remarks_without_canonical_source_values_are_not_assumed_shared():
    remarks = [{"name": "←注", "value": "此款是皮料"},
               {"name": "←注", "value": "此款是网纱"}]
    original = original_with_remarks(remarks)
    before = copy.deepcopy(original)

    def chat(messages, **kwargs):
        options = source_options(messages)
        assert [option["source"] for option in options
                if option["source"]["name"] == "←注"] == remarks
        return color_response(options)

    with pytest.raises(ValueError, match="规格无法映射"):
        normalize_marketplace_variations(original, COLOR_SCHEMA, chat=chat)
    assert original == before


def test_note_only_rows_cannot_be_cleaned_into_empty_sku_identity():
    original = {"variations": [
        {"id": "a", "attribute_combinations": [copy.deepcopy(SHARED_REMARK)]},
        {"id": "b", "attribute_combinations": [copy.deepcopy(SHARED_REMARK)]},
    ]}
    before = copy.deepcopy(original)

    def chat(messages, **kwargs):
        options = source_options(messages)
        assert [option["source"] for option in options] == [SHARED_REMARK]
        return color_response(options)

    with pytest.raises(ValueError, match="规格无法映射"):
        normalize_marketplace_variations(original, COLOR_SCHEMA, chat=chat)
    assert original == before


def test_shared_remark_not_removed_when_remaining_source_combinations_collapse():
    original = original_with_remarks([SHARED_REMARK, SHARED_REMARK])
    original["variations"][1]["attribute_combinations"][0]["value_name"] = "黑色"
    before = copy.deepcopy(original)

    def chat(messages, **kwargs):
        options = source_options(messages)
        assert SHARED_REMARK in [option["source"] for option in options]
        return color_response(options)

    with pytest.raises(ValueError, match="规格无法映射"):
        normalize_marketplace_variations(original, COLOR_SCHEMA, chat=chat)
    assert original == before


def test_cleanup_keeps_final_marketplace_identity_collision_check():
    original = original_with_remarks([SHARED_REMARK, SHARED_REMARK])
    before = copy.deepcopy(original)

    def chat(messages, **kwargs):
        options = source_options(messages)
        assert SHARED_REMARK not in [option["source"] for option in options]
        return json.dumps({"options": [
            {"option_id": option["option_id"], "attributes": [{"id": "COLOR", "value_name": "Black"}]}
            for option in options
        ]})

    with pytest.raises(ValueError, match="自动映射已尝试 3 次：AI 映射后存在无法区分的变体"):
        normalize_marketplace_variations(original, COLOR_SCHEMA, chat=chat)
    assert original == before


def test_whole_sku_retry_uses_cleaned_facts_and_keeps_raw_remarks():
    original = {"variations": [
        {"id": "battery", "price": 2, "attribute_combinations": [
            {"name": "款式", "value_name": "南瓜"}, {"name": "版本", "value_name": "电池闪烁款"},
            copy.deepcopy(SHARED_REMARK)]},
        {"id": "usb", "price": 3, "attribute_combinations": [
            {"name": "款式", "value_name": "南瓜"}, {"name": "版本", "value_name": "USB常亮款"},
            copy.deepcopy(SHARED_REMARK)]},
    ]}
    before = copy.deepcopy(original)
    calls = []

    def chat(messages, **kwargs):
        options = source_options(messages)
        calls.append(options)
        assert "此款是" not in messages[0]["content"]
        if len(calls) == 1:
            return json.dumps({"options": [
                {"option_id": 0, "attributes": [{"id": "MODEL", "value_name": "Pumpkin"}]},
                {"option_id": 1, "attributes": [{"id": "POWER", "value_name": "Battery"}]},
                {"option_id": 2, "attributes": [{"id": "POWER", "value_name": "USB"}]},
            ]})
        assert all(option["source"]["name"] == "完整SKU规格" for option in options)
        return json.dumps({"options": [
            {"option_id": 0, "attributes": [{"id": "MODEL", "value_name": "Pumpkin battery flashing"}]},
            {"option_id": 1, "attributes": [{"id": "MODEL", "value_name": "Pumpkin USB steady"}]},
        ]})

    result = normalize_marketplace_variations(original, [
        {"id": "MODEL", "hierarchy": "PARENT_PK"}, {"id": "POWER", "hierarchy": "ITEM"}], chat=chat)
    assert len(calls) == 2
    assert [variation["id"] for variation in result] == ["battery", "usb"]
    assert [variation["price"] for variation in result] == [2, 3]
    assert all(variation["source_remarks"] == [SHARED_REMARK] for variation in result)
    assert original == before


def test_shoulder_to_hem_size_preserves_real_lengths_without_guessing_standard_size():
    original = {'title': '披风', 'variations': [
        {'id': str(length), 'price': 20, 'attribute_combinations': [
            {'name': '规格2', 'value_name': f'肩到裙摆{length}cm'}]}
        for length in [130, 180]]}
    schema = [{'id': 'SIZE', 'value_type': 'string', 'hierarchy': 'CHILD_PK',
               'values': [{'id': 'large', 'name': 'L'}]},
              {'id': 'LENGTH', 'value_type': 'number_unit', 'hierarchy': 'CHILD_DEPENDENT',
               'allowed_units': [{'id': 'cm', 'name': 'cm'}]}]
    result = normalize_marketplace_variations(original, schema, chat=lambda messages, **kw: json.dumps({
        'options': [{'option_id': row['option_id'], 'attributes': []} for row in source_options(messages)]}))
    for length, sku in zip([130, 180], result):
        attrs = {a['id']: a['value_name'] for a in sku['attribute_combinations']}
        assert attrs == {'SIZE': f'Shoulder to hem {length} cm', 'LENGTH': f'{length} cm'}
    assert original['variations'][0]['attribute_combinations'][0]['value_name'] == '肩到裙摆130cm'


def test_two_tone_colors_use_one_field_and_remain_distinct():
    original = {'variations': [{'id': str(i), 'attribute_combinations': [
        {'name': '规格1', 'value_name': raw}]} for i, raw in enumerate(['黑加绿', '黑加宝蓝'])]}
    result = normalize_marketplace_variations(original, [{**COLOR_SCHEMA[0], 'value_type': 'string',
        'values': [{'id': 'black', 'name': 'Black'}, {'id': 'green', 'name': 'Green'}]}],
        chat=lambda messages, **kw: json.dumps({
        'options': [{'option_id': o['option_id'], 'attributes': [
            {'id': 'COLOR', 'value_name': 'Black'}, {'id': 'COLOR', 'value_name': 'Green'}]}
            for o in source_options(messages)]}))
    assert [v['attribute_combinations'][0]['value_name'] for v in result] == ['Black / Green', 'Black / Royal blue']


def test_full_sku_translation_supplies_model_identity_when_gender_is_only_family():
    original = {'variations': [{'id': str(i), 'attribute_combinations': [
        {'name': '完整SKU规格', 'value_name': raw}]} for i, raw in enumerate([
            '女款全套: 连体衣+眼罩 / 身高150cm', '男款全套: 连体衣+眼罩 / 身高150cm'])]}
    schema = [{'id': 'SIZE', 'hierarchy': 'CHILD_PK'}, {'id': 'MODEL', 'hierarchy': 'PARENT_PK'},
              {'id': 'GENDER', 'hierarchy': 'FAMILY', 'value_type': 'list', 'values': [
                  {'id': 'woman', 'name': 'Woman'}, {'id': 'man', 'name': 'Man'}]}]
    result = normalize_marketplace_variations(original, schema, chat=lambda messages, **kw: json.dumps({
        'options': [{'option_id': o['option_id'],
                     'source_value_en': ['Women set, bodysuit and eye mask, 150 cm', 'Men set, bodysuit and eye mask, 150 cm'][o['option_id']],
                     'attributes': [{'id': 'SIZE', 'value_name': '150 cm'}]}
                    for o in source_options(messages)]}))
    attrs = [{a['id']: a['value_name'] for a in v['attribute_combinations']} for v in result]
    assert attrs[0]['GENDER'] == 'Woman'
    assert attrs[1]['GENDER'] == 'Man'
    assert attrs[0]['MODEL'] != attrs[1]['MODEL']


def test_unmapped_numbered_options_retry_full_sku_with_model_identity():
    original = {'title': '汽车卡扣', 'variations': [
        {'id': str(i), 'price': i / 100, 'available_quantity': 100 + i,
         'attribute_combinations': [{'name': '规格1', 'value_name': f'{i}号'}]}
        for i in range(1, 7)
    ]}
    before = copy.deepcopy(original)
    calls = []

    def chat(messages, **kwargs):
        options = source_options(messages)
        calls.append(options)
        if len(calls) > 1:
            assert all(o['source']['name'] == '完整SKU规格' for o in options)
        return json.dumps({'options': [
            {'option_id': o['option_id'], 'source_value_en': f"No. {o['option_id'] + 1}",
             'attributes': []} for o in options
        ]})

    result = normalize_marketplace_variations(original, [
        {'id': 'MODEL', 'hierarchy': 'PARENT_PK', 'value_type': 'string', 'tags': {'hidden': True}}
    ], chat=chat)
    assert len(calls) == 2
    assert original == before
    for i, row in enumerate(result, 1):
        assert row['attribute_combinations'] == [{'id': 'MODEL', 'name': 'Model', 'value_name': f'No. {i}'}]
        assert {k: v for k, v in row.items() if k != 'attribute_combinations'} == {
            k: v for k, v in before['variations'][i - 1].items() if k != 'attribute_combinations'}
