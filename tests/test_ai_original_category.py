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
