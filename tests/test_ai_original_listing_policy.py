"""The reviewed and published AI SKU snapshots use the same names and photos."""
from copy import deepcopy
import json

import pytest

from erp.mercadolibre_batch_publish import _prepared_listing_from_product_row
from test_ai_original_products import _ai_row


def product():
    row = _ai_row()
    snapshot = json.loads(row['source_snapshot_json'])
    variants = []
    for color, label in [('red', '红色'), ('blue', '蓝色')]:
        cover = f'/api/ai-original-products/images/{color}-ai-white.jpg'
        variants.append({'sku_id': color, 'price': 12,
            'label': label, 'label_es': 'No usar', 'label_pt': 'Não usar',
            'image_url': cover, 'white_image_url': cover,
            'source_image_url': f'https://cbu01.alicdn.com/{color}.jpg',
            'image_generation_method': 'local_background_removal',
            'images': ['https://cbu01.alicdn.com/blue.jpg'],
            'attribute_combinations': [{'id': 'COLOR', 'value_name': label,
                'value_name_es': 'No usar', 'value_name_pt': 'Não usar'}]})
    snapshot['ai_original']['variations'] = variants
    snapshot['original_1688'] = {
        'main_image_url': 'https://cbu01.alicdn.com/red.jpg',
        'images': ['https://cbu01.alicdn.com/red.jpg', 'https://cbu01.alicdn.com/blue.jpg',
                   'https://cbu01.alicdn.com/usage.jpg', {'url':'https://cbu01.alicdn.com/usage.jpg'}],
        'detail_images': ['https://cbu01.alicdn.com/details.jpg'],
        'variations': [{'sku_id': color, 'image_url': f'https://cbu01.alicdn.com/{color}.jpg'}
                       for color in ['red', 'blue']]}
    row['source_snapshot_json'] = snapshot
    return row


@pytest.mark.parametrize('site', ['MLM', 'MLB'])
def test_publication_translates_missing_names_and_filters_sku_covers(monkeypatch, site):
    calls = []
    def translate(values, source, target):
        calls.append((values, source, target))
        return [{'红色':'Red', '蓝色':'Blue'}[value] for value in values]
    monkeypatch.setattr('erp.mercadolibre_translation.translate_texts', translate)
    row = product()
    before = deepcopy(row)
    source, _ = _prepared_listing_from_product_row(row, site)
    assert row == before
    assert calls == [(['红色','蓝色'], 'zh-CN', 'en')]
    assert [v['name'] for v in source['variations']] == ['Red', 'Blue']
    for variant in source['variations']:
        assert variant['images'] == [variant['white_image_url'],
            'https://cbu01.alicdn.com/usage.jpg', 'https://cbu01.alicdn.com/details.jpg']
        assert variant['image'] == variant['white_image_url']
        assert variant['pictures'] == [{'source':url} for url in variant['images']]


def test_failed_translation_blocks_preparation(monkeypatch):
    monkeypatch.setattr('erp.mercadolibre_translation.translate_texts', lambda *a: ['红色','Blue'])
    with pytest.raises(ValueError, match='英文翻译未完成'):
        _prepared_listing_from_product_row(product(), 'MLB')


def test_existing_english_options_fill_blank_names_without_translation(monkeypatch):
    row = product()
    for variant, value in zip(row['source_snapshot_json']['ai_original']['variations'], ['Red','Blue']):
        variant['label'] = ''
        variant['attribute_combinations'][0]['value_name_en'] = value
    monkeypatch.setattr('erp.mercadolibre_translation.translate_texts', lambda *a: pytest.fail('already English'))
    source, _ = _prepared_listing_from_product_row(row, 'MLB')
    assert [v['name'] for v in source['variations']] == ['Red','Blue']


def test_final_family_payload_uploads_own_white_cover_and_shared_photos(monkeypatch):
    from unittest.mock import patch
    from erp.mercadolibre_follow_sell import follow_sell
    from test_erp_mercadolibre_follow_sell import FamilyClient, family_source
    monkeypatch.setattr('erp.mercadolibre_translation.translate_texts', lambda *a: ['Red','Blue'])
    prepared, _ = _prepared_listing_from_product_row(product(), 'MLM')
    source = family_source()
    for target, variant in zip(source['variations'], prepared['variations']):
        target.pop('picture_ids', None)
        for key in ('image_url','image','images','pictures','white_image_url','image_generation_method','attribute_combinations'):
            target[key] = variant[key]
    client = FamilyClient()
    with patch('erp.mercadolibre_source_store.record_publish_result'):
        follow_sell(client, source['id'], prepared_listing=(source, {}), net_proceeds=20,
                    publish=True, source_from_database=True)
    uploaded = {url: f'uploaded-{index}' for index, url in enumerate(client.uploads, 1)}
    for payload, variant in zip(client.posts[0], prepared['variations']):
        assert payload['pictures'] == [{'id':uploaded[url]} for url in variant['images']]
    assert all(not url.endswith(('/red.jpg','/blue.jpg')) for url in client.uploads)
