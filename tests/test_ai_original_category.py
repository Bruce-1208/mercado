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


def test_wig_category_search_does_not_accept_a_single_cosplay_kit_suggestion():
    from erp.ai_original_products import _select_marketplace_category

    class Discovery:
        calls = []

        def request(self, method, path, **kwargs):
            self.calls.append(kwargs['params']['q'])
            return [
                {'category_id': 'CBT455862', 'category_name': 'Cosplay Kits', 'domain_name': 'Costumes'},
                {'category_id': 'CBT7787', 'category_name': 'Wigs', 'domain_name': 'Beauty wigs'},
                {'category_id': 'CBT457501', 'category_name': 'Wigs', 'domain_name': 'Party wigs'},
            ]

    client = Discovery()
    prompts = []

    def chat(messages, **kwargs):
        prompts.append(messages[0]['content'])
        return json.dumps({'category_id': 'CBT7787', 'search_query': ''})

    result = _select_marketplace_category(
        {'title': '假发 Jinx cosplay'}, 'cosplay wig',
        [{'category_id': 'CBT455862', 'category_name': 'Cosplay Kits', 'domain_name': 'Costumes'}],
        client, chat=chat,
    )

    assert result['category_id'] == 'CBT7787'
    assert client.calls == ['wig']
    assert 'CBT455862' not in prompts[0]


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


def test_variation_enum_id_only_and_empty_optional_are_normalized():
    from erp.ai_original_products import normalize_marketplace_variations
    schema = [{'id': 'POWER_SUPPLY_TYPE', 'values': [{'id': 'bat', 'name': 'Battery'}]},
              {'id': 'PROTECTION_DEGREE'}]
    result = normalize_marketplace_variations(
        {'variations': [{'attribute_combinations': [{'name': '规格', 'value_name': '电池款'}]}]},
        schema, chat=lambda *a, **k: json.dumps({'options': [{'option_id': 0, 'attributes': [
            {'id': 'POWER_SUPPLY_TYPE', 'value_id': 'bat'},
            {'id': 'PROTECTION_DEGREE', 'value_name': ''}]}]}))
    assert result[0]['attribute_combinations'] == [
        {'id': 'POWER_SUPPLY_TYPE', 'name': 'POWER_SUPPLY_TYPE', 'value_name': 'Battery', 'value_id': 'bat'}]


@pytest.mark.parametrize('shared', [True, False])
def test_neutral_placeholder_only_omitted_if_shared_by_every_sku(shared):
    from erp.ai_original_products import normalize_marketplace_variations
    original = {'variations': [
        {'id': 'a', 'price': 2, 'attribute_combinations': [
            {'name': '规格1', 'value_name': '黑色'}, {'name': '规格2', 'value_name': '常规'}]},
        {'id': 'b', 'price': 3, 'attribute_combinations': [
            {'name': '规格1', 'value_name': '红色'},
            {'name': '规格2', 'value_name': '常规' if shared else '特殊'}]}]}
    response = {'options': [
        {'option_id': 0, 'attributes': [{'id': 'COLOR', 'value_name': 'Black'}]},
        {'option_id': 1, 'attributes': []},
        {'option_id': 2, 'attributes': [{'id': 'COLOR', 'value_name': 'Red'}]}]}
    if not shared:
        response['options'].append({'option_id': 3, 'attributes': []})
        with pytest.raises(ValueError, match='规格无法映射'):
            normalize_marketplace_variations(original, [{'id': 'COLOR'}], chat=lambda *a, **k: json.dumps(response))
    else:
        result = normalize_marketplace_variations(original, [{'id': 'COLOR'}], chat=lambda *a, **k: json.dumps(response))
        assert [r['price'] for r in result] == [2, 3]
        assert len(original['variations'][0]['attribute_combinations']) == 2
        assert len(result[0]['attribute_combinations']) == 1


def test_category_search_includes_history_and_does_not_repeat_query():
    from erp.ai_original_products import _select_marketplace_category
    class Discovery:
        def request(self, *a, **k):
            pytest.fail('Repeated discovery query must not be sent')
    prompts = []
    def chat(messages, **kwargs):
        prompts.append(messages[0]['content'])
        return json.dumps({'category_id': '', 'search_query': 'pumpkin lantern'})
    with pytest.raises(ValueError, match='无法从平台候选'):
        _select_marketplace_category({}, 'pumpkin lantern', [
            {'category_id': 'CBT1'}, {'category_id': 'CBT2'}], Discovery(), chat=chat)
    assert len(prompts) == 5
    assert 'searched_queries' in prompts[-1]


def test_invalid_optional_enum_cannot_satisfy_required_completion():
    from erp.ai_original_products import complete_required_attributes
    result, missing = complete_required_attributes({}, [], [
        {'id': 'POWER', 'tags': {'required': True}, 'value_type': 'list',
         'values': [{'id': 'bat', 'name': 'Battery'}]}],
        chat=lambda *a, **k: '{"attributes": [{"id": "POWER", "value_name": "invalid"}]}')
    assert not result
    assert missing == [{'id': 'POWER', 'name': 'POWER'}]


def test_partial_sku_required_field_is_completed_per_sku_not_globally():
    from erp.ai_original_products import complete_required_attributes
    variations = [
        {'attribute_combinations': [{'id': 'POWER', 'value_name': 'Battery'}]},
        {'attribute_combinations': [{'id': 'MODEL', 'value_name': 'USB version'}]}]
    result, missing = complete_required_attributes({}, [], [
        {'id': 'POWER', 'tags': {'required': True}}], variations,
        chat=lambda *a, **k: '{"attributes": [{"id": "POWER", "value_name": "USB"}]}')
    assert not result and not missing
    assert variations[0]['attribute_combinations'][0]['value_name'] == 'Battery'
    assert variations[1]['attributes'][0]['value_name'] == 'USB'


@pytest.mark.parametrize('field', ['MODEL', 'COLOR', 'SIZE', 'SEASON', 'GTIN', 'PACKAGE_WEIGHT'])
def test_generation_never_completes_with_required_omissions(monkeypatch, field):
    import erp.ai_original_products as module
    monkeypatch.setattr(module, '_download_source_image', lambda *a, **k: b'image')
    monkeypatch.setattr(module, 'create_white_background_image', lambda *a, **k: ('path', '/image.jpg'))
    monkeypatch.setattr(module, 'generate_marketplace_copy', lambda *a, **k: {'attributes': []})
    monkeypatch.setattr(module, 'complete_marketplace_category', lambda *a, **k: {
        'attributes': [], 'missing_required_attributes': [{'id': field}]})
    with pytest.raises(ValueError, match=field):
        module.prepare_ai_original_product({'source_snapshot_json': {
            'original_1688': {'main_image_url': 'https://example.com/image.jpg'}}}, category_client=object())


def test_collapsed_option_mapping_retries_with_whole_sku_facts():
    from erp.ai_original_products import normalize_marketplace_variations
    original = {'variations': [
        {'id': 'battery', 'price': 2, 'attribute_combinations': [
            {'name': '款式', 'value_name': '南瓜'}, {'name': '版本', 'value_name': '电池闪烁款'}]},
        {'id': 'usb', 'price': 3, 'attribute_combinations': [
            {'name': '款式', 'value_name': '南瓜'}, {'name': '版本', 'value_name': 'USB常亮款'}]}]}
    calls = []
    def chat(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps({'options': [
                {'option_id': 0, 'attributes': [{'id': 'MODEL', 'value_name': 'Pumpkin'}]},
                {'option_id': 1, 'attributes': [{'id': 'POWER', 'value_name': 'Battery'}]},
                {'option_id': 2, 'attributes': [{'id': 'POWER', 'value_name': 'USB'}]}]})
        assert '完整SKU规格' in messages[0]['content']
        return json.dumps({'options': [
            {'option_id': 0, 'attributes': [{'id': 'MODEL', 'value_name': 'Pumpkin battery flashing'}]},
            {'option_id': 1, 'attributes': [{'id': 'MODEL', 'value_name': 'Pumpkin USB steady'}]}]})
    result = normalize_marketplace_variations(original, [
        {'id': 'MODEL', 'hierarchy': 'PARENT_PK'}, {'id': 'POWER', 'hierarchy': 'ITEM'}], chat=chat)
    assert len(calls) == 2
    assert [r['id'] for r in result] == ['battery', 'usb']
    assert [r['price'] for r in result] == [2, 3]
    assert original['variations'][0]['attribute_combinations'][0]['value_name'] == '南瓜'


@pytest.mark.parametrize('second,valid', [('7 cm', True), ('11.2 cm', False)])
def test_repeated_identical_attributes_are_deduplicated_but_conflicts_rejected(second, valid):
    from erp.ai_original_products import normalize_marketplace_variations
    call = lambda: normalize_marketplace_variations(
        {'variations': [{'attribute_combinations': [{'name': '规格', 'value_name': '7cm'}]}]},
        [{'id': 'LENGTH'}], chat=lambda *a, **k: json.dumps({'options': [
            {'option_id': 0, 'attributes': [{'id': 'LENGTH', 'value_name': '7 cm'},
                                           {'id': 'LENGTH', 'value_name': second}]}]}))
    if valid:
        assert len(call()[0]['attribute_combinations']) == 1
    else:
        with pytest.raises(ValueError, match='属性ID重复'):
            call()


def test_required_completion_leaves_package_dimensions_for_manual_review():
    from erp.ai_original_products import complete_required_attributes
    existing = [{'id': 'PACKAGE_LENGTH', 'value_name': '20 cm'}]
    attrs, missing = complete_required_attributes({}, existing, [
        {'id': prefix + axis, 'tags': {'required': True}}
        for prefix in ('PACKAGE_', 'SELLER_PACKAGE_')
        for axis in ('LENGTH', 'WIDTH', 'HEIGHT')
    ], chat=lambda *a, **k: pytest.fail('Packaging dimensions must not invoke AI'))
    assert attrs == existing
    assert missing == []


def test_category_generates_unmapped_required_values_and_ignores_package_dimensions():
    class PackagingClient(Client):
        def request(self, method, path, **kwargs):
            result = super().request(method, path, **kwargs)
            if 'domain_discovery' not in path:
                result += [{'id': 'PACKAGE_HEIGHT', 'tags': {'required': True}}]
            return result

    def chat(messages, **kwargs):
        prompt = messages[0]['content']
        assert '允许自行编造' in prompt
        assert 'PACKAGE_HEIGHT' not in prompt
        return json.dumps({'attributes': [
            {'id': 'MATERIAL', 'value_id': '12'},
            {'id': 'CAPACITY', 'value_name': '500 ml'},
            {'id': 'PACKAGE_HEIGHT', 'value_name': '99 cm'},
        ]})

    result = complete_marketplace_category(
        {'title': '水瓶'}, {'product_type_en': 'Water bottle'}, PackagingClient(), chat=chat)
    assert result['missing_required_attributes'] == []
    assert {a['id'] for a in result['attributes']} == {'MATERIAL', 'CAPACITY'}
