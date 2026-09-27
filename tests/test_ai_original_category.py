import json

import pytest

from erp.ai_original_products import complete_marketplace_category


class Client:
    def __init__(self, predictions=None):
        self.calls = []
        self.predictions = predictions if predictions is not None else [
            {"category_id": "CBT123", "category_name": "Water bottles"}
        ]

    def request(self, method, path, **kwargs):
        self.calls.append((path, kwargs))
        if "domain_discovery" in path:
            return self.predictions
        return [
            {"id": "MATERIAL", "name": "Material", "value_type": "list",
             "values": [{"id": "12", "name": "Steel"}], "tags": {"required": True}},
            {"id": "CAPACITY", "tags": {"required": True}},
            {"id": "INTERNAL", "tags": {"read_only": True}},
        ]


def test_discovery_uses_product_identity_and_maps_only_live_schema():
    client = Client()
    def chat(messages, **kwargs):
        assert 'CAPACITY' in messages[0]['content']
        assert '水瓶' in messages[0]['content']
        return json.dumps({"attributes": [
            {"id": "MATERIAL", "value_name": "Steel"},
            {"id": "INVENTED", "value_name": "x"},
            {"id": "INTERNAL", "value_name": "x"},
        ]})
    result = complete_marketplace_category(
        {"title": "水瓶", "category_id": "wrong"},
        {"product_type_en": "Stainless steel water bottle"}, client, chat=chat,
    )
    assert client.calls[0] == ('/marketplace/domain_discovery/search',
                               {'params': {'q': 'Stainless steel water bottle'}})
    assert client.calls[1][0] == '/categories/CBT123/attributes'
    assert result['category_id'] == 'CBT123'
    assert result['attributes'] == [{'id': 'MATERIAL', 'name': 'Material', 'value_name': 'Steel', 'value_id': '12'}]
    assert result['missing_required_attributes'] == [{'id': 'CAPACITY', 'name': 'CAPACITY'}]


def test_empty_recommendation_does_not_keep_old_category():
    with pytest.raises(ValueError, match='未推荐对应分类'):
        complete_marketplace_category({}, {'product_type_en': 'Bottle'}, Client([]))


def test_missing_identity_does_not_query_with_marketing_title():
    client = Client()
    with pytest.raises(ValueError, match='未识别商品实际类型'):
        complete_marketplace_category({}, {'title_es': 'Oferta'}, client)
    assert client.calls == []


def test_missing_gender_is_retried_against_live_enum_without_overwriting_facts():
    from erp.ai_original_products import complete_required_attributes
    schema = [{'id': 'GENDER', 'tags': {'required': True}, 'value_type': 'list',
               'values': [{'id': 'u', 'name': 'Unisex'}]}]
    calls = []
    def chat(messages, **kwargs):
        calls.append(messages)
        return json.dumps({'attributes': [
            {'id': 'GENDER', 'value_name': 'unrecognized'} if len(calls) == 1 else
            {'id': 'GENDER', 'value_id': 'u', 'value_name': 'Unisex'},
            {'id': 'MATERIAL', 'value_name': 'Wrong'},
        ]})
    attrs, missing = complete_required_attributes(
        {'title': '男女通用服装'}, [{'id': 'MATERIAL', 'value_name': 'Cotton'}], schema, chat=chat)
    assert not missing
    assert attrs[0]['value_name'] == 'Cotton'
    assert attrs[1]['value_id'] == 'u'
    assert len(calls) == 2
    assert '未通过校验' in calls[1][-1]['content']


def test_required_completion_does_not_call_ai_for_fields_on_every_variation():
    from erp.ai_original_products import complete_required_attributes
    attrs, missing = complete_required_attributes({}, [], [
        {'id': 'SIZE', 'tags': {'required': True}}
    ], [{'attributes': [{'id': 'SIZE', 'value_name': 'S'}]},
        {'attribute_combinations': [{'id': 'SIZE', 'value_name': 'M'}]}],
        chat=lambda *a, **k: pytest.fail('No model call needed'))
    assert not missing


def test_required_completion_keeps_unknown_facts_missing_and_bounds_retries():
    from erp.ai_original_products import complete_required_attributes
    calls = []
    def chat(*a, **k):
        calls.append(1)
        return '{"attributes": []}'
    _, missing = complete_required_attributes({}, [], [
        {'id': 'GENDER', 'tags': {'required': True}}
    ], chat=chat)
    assert missing == [{'id': 'GENDER', 'name': 'GENDER'}]
    assert len(calls) == 2


def test_category_selection_rejects_spider_party_kits_and_refines_search():
    from erp.ai_original_products import _select_marketplace_category
    calls = []
    class Discovery:
        def request(self, method, path, **kwargs):
            assert kwargs['params']['q'] == 'costume'
            return [{'category_id': 'CBT24790', 'category_name': 'Costumes'}]
    def chat(messages, **kwargs):
        calls.append(messages)
        return json.dumps({'category_id': '' if len(calls) == 1 else 'CBT24790', 'search_query': 'costume'})
    result = _select_marketplace_category({'title': '蜘蛛侠儿童连体衣'}, 'Spider-Man costume', [
        {'category_id': 'CBT100778', 'category_name': 'Custom Kits'},
        {'category_id': 'CBT434745', 'category_name': 'For Washing Machines'},
    ], Discovery(), chat=chat)
    assert result['category_id'] == 'CBT24790'
    assert len(calls) == 2


def test_publication_repairs_legacy_gender_before_validation():
    from erp.mercadolibre_batch_publish import _complete_ai_publication_attributes
    class SchemaClient:
        def request(self, *a, **k):
            return [{'id': 'GENDER', 'tags': {'required': True}, 'value_type': 'list',
                     'values': [{'id': 'u', 'name': 'Unisex'}]}]
    source = {'category_id': 'CBT24790', 'attributes': []}
    row = {'source_snapshot_json': {'original_1688': {'title': '男女通用'}}}
    new, _ = _complete_ai_publication_attributes(row, (source, {}), SchemaClient(),
        chat=lambda *a, **k: json.dumps({'attributes': [{'id': 'GENDER', 'value_id': 'u'}]}))
    assert source['attributes'] == []
    assert next(a for a in new['attributes'] if a['id'] == 'GENDER')['value_id'] == 'u'


def test_default_structured_chat_disables_deepseek_v4_thinking(monkeypatch):
    from AI_Agent import deepseek
    from erp.ai_original_products import _listing_json_chat
    calls = []
    monkeypatch.setattr(deepseek, 'chat_deepseek', lambda *a, **k: calls.append(k))
    _listing_json_chat(model='deepseek-v4-pro', base_url='https://api.deepseek.com')([])
    assert calls[0]['thinking'] is False
