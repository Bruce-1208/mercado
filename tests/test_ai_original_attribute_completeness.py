"""All writable attributes are considered even when required fields are satisfied."""
import json
from copy import deepcopy

import pytest
from erp import ai_original_products as p


def test_optional_fields_are_sent_to_metered_ai_even_with_required_complete():
    seen = []
    schema = [
        {'id': 'BRAND', 'tags': {'required': True}},
        {'id': 'MATERIAL', 'values': [{'id': 'r', 'name': 'Resin'}]},
        {'id': 'FINISH'},
        {'id': 'INTERNAL', 'tags': {'read_only': True}},
        {'id': 'PACKAGE_WEIGHT', 'tags': {'required': True}},
        {'id': 'HEIGHT'}, {'id': 'NET_PROCEEDS_USD'},
    ]
    def chat(messages, **kwargs):
        data = json.loads(messages[0]['content'].split('\n', 1)[1])
        seen.append(data['schema'])
        return json.dumps({'attributes': [
            {'id': 'MATERIAL', 'value_name': 'Resin'},
            {'id': 'BRAND', 'value_name': 'Wrong'},
            {'id': 'HEIGHT', 'value_name': '10 cm'},
        ]})
    chat.cost_control = True
    original = {'title': '滴胶键帽'}
    existing = [{'id': 'BRAND', 'value_name': 'Generic'}]
    before = deepcopy(existing)
    result, missing = p.complete_required_attributes(original, existing, schema, chat=chat)
    assert [[a['id'] for a in call] for call in seen] == [['MATERIAL', 'FINISH']]
    assert result == before + [{'id': 'MATERIAL', 'name': 'MATERIAL', 'value_name': 'Resin', 'value_id': 'r'}]
    assert existing == before
    assert not missing


def test_partial_optional_values_are_filled_per_sku_without_promoting_shared_value():
    variations = [{'attributes': [{'id': 'MATERIAL', 'value_name': 'Steel'}]}, {'attributes': []}]
    def chat(messages, **kwargs):
        data = json.loads(messages[0]['content'].split('\n', 1)[1])
        assert data['original']['source_sku']['label'] == 'Plastic'
        return '{"attributes":[{"id":"MATERIAL","value_name":"Plastic"}]}'
    result, missing = p.complete_required_attributes(
        {'variations': [{'label': 'Steel'}, {'label': 'Plastic'}]}, [],
        [{'id': 'MATERIAL'}], variations, chat=chat)
    assert result == [] and missing == []
    assert variations[0]['attributes'][0]['value_name'] == 'Steel'
    assert variations[1]['attributes'][0]['value_name'] == 'Plastic'


@pytest.mark.parametrize('key', ['WEIGHT', 'PACKAGE_WEIGHT', 'SELLER_PACKAGE_HEIGHT',
    'PRODUCT_LENGTH', 'WIDTH', 'DEPTH', 'DIAMETER', 'NET_INCOME_USD', 'NET_PROCEEDS_USD'])
def test_manual_fields_do_not_trigger_ai_even_if_required(key):
    result, missing = p.complete_required_attributes({}, [], [{'id': key, 'tags': {'required': True}}],
        chat=lambda *a, **k: pytest.fail('manual fields must never be generated'))
    assert result == missing == []


def test_missing_required_fields_remain_blockers_after_optional_completion():
    calls = []
    def chat(messages, **kwargs):
        calls.append(json.loads(messages[0]['content'].split('\n', 1)[1])['schema'])
        return '{"attributes":[{"id":"MATERIAL","value_name":"Resin"}]}'
    result, missing = p.complete_required_attributes({}, [], [
        {'id': 'MODEL', 'tags': {'required': True}}, {'id': 'MATERIAL'}], chat=chat)
    assert [[a['id'] for a in call] for call in calls] == [['MODEL', 'MATERIAL'], ['MODEL']]
    assert missing == [{'id': 'MODEL', 'name': 'MODEL'}]


def test_more_than_fifty_explicit_attributes_are_not_silently_truncated():
    raw = [{'name': f'Fact {i}', 'value_name': str(i)} for i in range(75)]
    normalized = p._normalize_ai_attributes(raw)
    assert len(normalized) == 75
    assert len(p._merge_explicit_source_attributes([], {'properties': raw})) == 75


def test_keycap_cannot_accept_complete_keyboard_even_as_only_candidate():
    class Client:
        def request(self, method, path, **kwargs):
            assert path == '/categories/CBT418448'
            return {'id': 'CBT418448', 'name': 'Physical Keyboards',
                    'children_categories': [], 'settings': {'listing_allowed': True}}
    with pytest.raises(p.OriginalPreparationError):
        p._select_marketplace_category(
            {'title': '单个醒狮键帽'}, 'keycaps',
            [{'category_id': 'CBT418448', 'category_name': 'Physical Keyboards'}], Client(),
            chat=lambda *a, **k: '{"category_id":"CBT418448","search_query":"keycaps"}')
