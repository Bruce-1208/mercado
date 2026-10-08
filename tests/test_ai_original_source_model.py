"""Regression for lossy style mapping on the real 39-SKU 1688 source."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from erp.ai_original_products import normalize_marketplace_variations


STYLE_TRANSLATIONS = {
    '悟字款五件套': 'Wu emblem 5-piece costume set',
    '龟字款五件套': 'Gui emblem 5-piece costume set',
    '悟字款六件套': 'Wu emblem 6-piece costume set',
    '龟字款六件套': 'Gui emblem 6-piece costume set',
    '悟空鞋套': 'Goku shoe covers',
    '悟空假发蓝色': 'Goku blue wig',
    '龟头戴': 'Gui headwear',
    '悟空假发红色': 'Goku red wig',
    '悟空假发宝蓝色': 'Goku royal blue wig',
    '悟空假发粉彩色': 'Goku pastel-colored wig',
    '悟空假发黑色': 'Goku black wig',
    '悟空假发金色': 'Goku golden wig',
    '悟空头戴': 'Goku headwear',
    '悟空假发混色': 'Goku mixed-color wig',
}


def test_real_source_uses_full_translation_when_model_summarizes_styles():
    fixture = json.loads((Path(__file__).parent / 'fixtures/ai_original_625324015225.json').read_text())
    original, schema = fixture['original'], fixture['schema']
    before = deepcopy(original)
    calls = []
    def chat(messages, **kwargs):
        calls.append(messages)
        prompt = messages[0]['content']
        payload = json.loads(prompt[prompt.index('\n') + 1:])
        options = []
        for option in payload['options']:
            raw = option['source']['value_name']
            if raw in STYLE_TRANSLATIONS:
                # Reproduce the bad summary while supplying the literal source translation.
                attrs = [{'id': 'MODEL', 'value_name': 'Son Goku costume accessory'}]
                translated = STYLE_TRANSLATIONS[raw]
            else:
                translated = 'One size' if raw == '均码' else ('Kids ' if '儿童' in raw else 'Adult ') + raw.split('-')[-1].strip().removesuffix('码')
                attrs = [{'id': 'SIZE', 'value_name': translated}]
            options.append({'option_id': option['option_id'], 'source_value_en': translated, 'attributes': attrs})
        return json.dumps({'options': options})
    result = normalize_marketplace_variations(original, schema, chat=chat)
    assert len(calls) == 1 and len(result) == 39
    assert original == before
    signatures = set()
    for source, mapped in zip(original['variations'], result):
        assert {k:v for k,v in source.items() if k != 'attribute_combinations'} == {
            k:v for k,v in mapped.items() if k != 'attribute_combinations'}
        attrs = {a['id']: a['value_name'] for a in mapped['attribute_combinations']}
        assert attrs['MODEL'] == STYLE_TRANSLATIONS[source['attribute_combinations'][0]['value_name']]
        signatures.add((attrs['MODEL'], attrs['SIZE']))
    assert len(signatures) == 39
    assert result[-1]['available_quantity'] == 0


@pytest.mark.parametrize('definition,expected', [
    ({'id':'MODEL', 'hierarchy':'PARENT_PK', 'value_type':'string'}, 'Gui headwear'),
    ({'id':'MODEL', 'hierarchy':'ITEM', 'value_type':'string'}, 'Headwear'),
    ({'id':'MODEL', 'hierarchy':'PARENT_PK', 'value_type':'list', 'values':[{'id':'h','name':'Headwear'}]}, 'Headwear'),
])
def test_source_translation_only_replaces_a_free_text_model_identity(definition, expected):
    mapped = normalize_marketplace_variations(
        {'variations':[{'attribute_combinations':[{'name':'款式','value_name':'龟头戴'}]}]},
        [definition], chat=lambda *a, **k: json.dumps({'options':[
            {'option_id':0, 'source_value_en':'Gui headwear',
             'attributes':[{'id':'MODEL','value_name':'Headwear'}]}]}))
    assert mapped[0]['attribute_combinations'][0]['value_name'] == expected


@pytest.mark.parametrize('translation', ['', None, [], '龟 headwear', '123', 'x' * 256])
def test_invalid_or_overlong_literal_model_is_never_silently_truncated(translation):
    with pytest.raises(ValueError):
        normalize_marketplace_variations(
            {'variations':[{'attribute_combinations':[{'name':'款式','value_name':'龟头戴'}]}]},
            [{'id':'MODEL','value_type':'string','value_max_length':255}],
            chat=lambda *a, **k: json.dumps({'options':[
                {'option_id':0, 'source_value_en':translation,
                 'attributes':[{'id':'MODEL','value_name':'Headwear'}]}]}))


def test_collapsed_literal_translations_still_fail_without_fake_suffixes():
    with pytest.raises(ValueError, match='无法区分'):
        normalize_marketplace_variations(
            {'variations':[{'attribute_combinations':[{'name':'款式','value_name':value}]}
                           for value in ['龟头戴','悟空头戴']]},
            [{'id':'MODEL','hierarchy':'PARENT_PK'}],
            chat=lambda *a, **k: json.dumps({'options':[
                {'option_id':i, 'source_value_en':'Headwear',
                 'attributes':[{'id':'MODEL','value_name':'Headwear'}]} for i in range(2)]}))


def test_actual_recovered_model_response_keeps_all_39_sku_identities():
    from erp.ai_original_products import _normalize_marketplace_variations_once
    from erp.mercadolibre_follow_sell import _user_product_identity_signature
    fixture = json.loads((Path(__file__).parent / 'fixtures/ai_original_625324015225.json').read_text())
    original = deepcopy(fixture['original'])
    for variation in original['variations']:
        variation['attribute_combinations'] = [{'name':'完整SKU规格', 'value_name':' / '.join(
            f"{a['name']}: {a['value_name']}" for a in variation['attribute_combinations'])}]
    result = _normalize_marketplace_variations_once(original, fixture['schema'],
        chat=lambda *a, **k: json.dumps(fixture['recovered_model_response']))
    assert len(result) == 39
    assert len({_user_product_identity_signature(v['attribute_combinations'],fixture['schema']) for v in result}) == 39
    models = {v['label']: next(a['value_name'] for a in v['attribute_combinations'] if a['id']=='MODEL') for v in result}
    assert 'Gui' in models['龟头戴 / 均码']
    assert 'Wukong' in models['悟空头戴 / 均码']
    assert 'pastel' in models['悟空假发粉彩色 / 均码']
    assert 'mixed' in models['悟空假发混色 / 均码']
    for old, new in zip(fixture['original']['variations'], result):
        for key in ('id','sku_id','price','available_quantity','image_url','weight_g'):
            assert new[key] == old[key]
