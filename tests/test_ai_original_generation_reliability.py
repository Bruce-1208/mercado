"""Regression cases from the 2026-10-07 batch; no paid model calls."""
import json
from copy import deepcopy

import pytest

from erp import ai_original_products as p


def required_schema():
    return [
        {'id': 'BRAND', 'value_type': 'string', 'tags': {'required': True}},
        {'id': 'GENDER', 'value_type': 'list', 'tags': {'required': True},
         'values': [{'id': 'girls', 'name': 'Girls'}]},
    ]


@pytest.mark.parametrize('envelope', ['array', 'object', 'fenced'])
def test_actual_attribute_answers_are_accepted_in_equivalent_envelopes(envelope):
    rows = [{'id': 'BRAND', 'value_name': 'Generic'}, {'id': 'GENDER', 'value_id': 'girls'}]
    response = json.dumps({'attributes': rows} if envelope == 'object' else rows)
    if envelope == 'fenced':
        response = '```json\n' + response + '\n```'
    calls = []
    def chat(*a, **k):
        calls.append(1)
        return response
    attrs, missing = p.complete_required_attributes({}, [], required_schema(), chat=chat)
    assert missing == [] and len(calls) == 1
    assert {a['id']: a['value_name'] for a in attrs} == {'BRAND': 'Generic', 'GENDER': 'Girls'}


def test_invalid_enum_does_not_discard_valid_sibling_and_retry_is_targeted():
    calls = []
    def chat(messages, **kwargs):
        calls.append(deepcopy(messages))
        return json.dumps({'attributes': [
            {'id': 'BRAND', 'value_name': 'Generic'},
            {'id': 'GENDER', 'value_id': 'nonexistent', 'value_name': 'Impossible'},
        ]})
    attrs, missing = p.complete_required_attributes({}, [], required_schema(), chat=chat)
    assert [a['id'] for a in attrs] == ['BRAND']
    assert [a['id'] for a in missing] == ['GENDER']
    assert 'GENDER:' in calls[1][-1]['content']


def test_partial_sku_completion_receives_its_original_facts():
    original = {'variations': [
        {'id': 'a', 'label': '服装 / 儿童 S'}, {'id': 'b', 'label': '假发 / 均码'}]}
    variants = [{'id': 'a', 'attributes': [{'id': 'GENDER', 'value_id': 'girls'}]}, {'id': 'b'}]
    prompts = []
    def chat(messages, **kwargs):
        prompts.append(messages[0]['content'])
        return '[{"id":"GENDER","value_id":"girls"}]'
    _, missing = p.complete_required_attributes(original, [], required_schema()[1:], variants, chat=chat)
    assert not missing and len(prompts) == 1
    assert '"source_sku":{"id":"b","label":"假发 / 均码"}' in prompts[0]
    assert variants[1]['attributes'][0]['id'] == 'GENDER'


def test_children_and_adults_do_not_collapse_even_when_model_loses_audience():
    original = {'variations': [
        {'id': 'child', 'price': 10, 'available_quantity': 3,
         'attribute_combinations': [{'name': '款式', 'value_name': '红衣服儿童'}, {'name': '尺码', 'value_name': 'S'}]},
        {'id': 'adult', 'price': 20, 'available_quantity': 4,
         'attribute_combinations': [{'name': '款式', 'value_name': '红衣服成人'}, {'name': '尺码', 'value_name': 'S'}]},
    ]}
    before = deepcopy(original)
    schema = [{'id': 'COLOR', 'hierarchy': 'PARENT_PK'},
              {'id': 'SIZE', 'value_type': 'string', 'hierarchy': 'CHILD_PK'}]
    def chat(*a, **k):
        return json.dumps({'options': [
            {'option_id': 0, 'attributes': [{'id': 'COLOR', 'value_name': 'Red'}]},
            {'option_id': 1, 'attributes': [{'id': 'SIZE', 'value_name': 'S'}]},
            {'option_id': 2, 'attributes': [{'id': 'COLOR', 'value_name': 'Red'}]},
        ]})
    rows = p.normalize_marketplace_variations(original, schema, chat=chat)
    assert original == before
    assert [r['id'] for r in rows] == ['child', 'adult']
    assert [r['price'] for r in rows] == [10, 20]
    assert [r['available_quantity'] for r in rows] == [3, 4]
    assert [next(a['value_name'] for a in r['attribute_combinations'] if a['id'] == 'SIZE') for r in rows] == ['Kids S', 'Adult S']


def test_enum_sizes_cannot_be_relabelled_to_bypass_identity_check():
    attrs = {'SIZE': {'id': 'SIZE', 'value_id': 'small', 'value_name': 'S'}}
    p._preserve_source_size_audience(attrs, {'attribute_combinations': [{'value_name': '儿童 S'}]},
                                   [{'id': 'SIZE', 'value_type': 'list', 'values': [{'id': 'small', 'name': 'S'}]}])
    assert attrs['SIZE']['value_name'] == 'S'


def test_missing_actual_fact_still_blocks_completion():
    attrs, missing = p.complete_required_attributes({}, [], required_schema(), chat=lambda *a, **k: '[]')
    assert attrs == [] and {a['id'] for a in missing} == {'BRAND', 'GENDER'}


def test_explicit_source_audience_corrects_model_gender_without_using_character_sex():
    schema = [{'id': 'GENDER', 'value_type': 'list', 'values': [
        {'id': 'boy', 'name': 'Boys'}, {'id': 'man', 'name': 'Man'}, {'id': 'girl', 'name': 'Girls'}]}]
    original = {'title': '七龙珠孙悟空服装 成人儿童男龟派cosplay'}
    for label, expected in [('儿童 S', 'Boys'), ('成人 S', 'Man')]:
        result = p._source_gender_attribute(original, schema, {'attribute_combinations': [{'value_name': label}]})
        assert result['value_name'] == expected
    assert p._source_gender_attribute({'title': '孙悟空假发'}, schema) is None
    assert p._source_gender_attribute({'title': '万圣节女巫服装'}, schema) is None


def test_conflicting_repeated_attribute_is_not_silently_last_wins():
    response = '[{"id":"BRAND","value_name":"A"},{"id":"BRAND","value_name":"B"}]'
    attrs, missing = p.complete_required_attributes({}, [], required_schema()[:1], chat=lambda *a, **k: response)
    assert not attrs and missing[0]['id'] == 'BRAND'


def test_generic_original_can_assign_a_traceable_seller_model_without_inventing_factory_facts():
    schema = [{'id': 'MODEL', 'name': 'Model', 'value_type': 'string', 'tags': {'required': True}},
              {'id': 'BRAND', 'name': 'Brand', 'value_type': 'string'}]
    class Client:
        def request(self, *a, **k): return deepcopy(schema)
    def chat(*a, **k): pytest.fail('Deterministic seller model should not need a model call')
    chat.cost_control = True
    result = p.complete_marketplace_category(
        {'source_1688_item_id': '856899155378'},
        {'attributes': [{'id': 'BRAND', 'value_name': 'Generic'}]}, Client(),
        category_override={'category_id': 'CBT7787'}, chat=chat)
    assert result['missing_required_attributes'] == []
    assert result['assigned_model']['value'] == 'AO-856899155378'
    assert result['assigned_model']['basis'] == 'seller_catalog_identifier'


@pytest.mark.parametrize('cached_brand', [None, 'Generated Brand'])
def test_original_brand_policy_overrides_generated_and_cached_brands(cached_brand):
    schema = [{'id': 'BRAND', 'name': 'Brand', 'value_type': 'string', 'tags': {'required': True}}]
    class Client:
        def request(self, *a, **k): return deepcopy(schema)
    cached = {'category_id': 'CBT123', 'schema_fingerprint': p._preparation_fingerprint(schema),
              'attributes_ready': True, 'attributes': [{'id': 'BRAND', 'value_name': cached_brand}],
              'variations_ready': True, 'variations': []} if cached_brand else None
    def chat(*a, **k): return '{"attributes":[{"id":"BRAND","value_name":"Model Brand"}]}'
    result = p.complete_marketplace_category({}, {}, Client(), category_override={'category_id': 'CBT123'},
                                             cached_result=cached, chat=chat)
    assert result['attributes'] == [{'id': 'BRAND', 'name': 'Brand', 'value_name': 'Generic'}]
    assert not result['missing_required_attributes']
