from copy import deepcopy
import json
import pytest
from erp.sku_matrix import sync_single_sku_cover, sync_supplier_skus, supplier_skus
from erp import ai_original_products as products
from erp.mercadolibre_collection_store import _mirror_zying_snapshot_fields
from test_ai_original_sku_images import local_images, sku_rows
from test_ai_original_products import _ai_row


def test_cover_preserves_picture_ids_and_other_images():
    item = {'variations': [{'picture_ids': ['B']}], 'pictures': [
        {'id': 'A', 'url': 'https://example.com/a.jpg'}, {'id': 'B', 'url': 'https://example.com/b.jpg'}]}
    assert sync_single_sku_cover(item) == 'https://example.com/b.jpg'
    assert [p['id'] for p in item['pictures']] == ['B', 'A']
    before = deepcopy(item)
    item['variations'].append({'image_url': 'https://example.com/c.jpg'})
    assert sync_single_sku_cover(item) == ''
    assert item['pictures'] == before['pictures']


def test_supplier_edit_updates_single_cover():
    rows = supplier_skus([{'id': 'sku1', 'image_url': 'https://example.com/new.jpg'}])
    snapshot = {'original_1688': {'main_image_url': 'https://example.com/old.jpg'},
                'ai_original': {'status': 'completed', 'variations': [{'id': 'sku1', 'image_url': 'https://example.com/old.jpg'}]}}
    sync_supplier_skus(snapshot, rows)
    assert snapshot['original_1688']['main_image_url'] == rows[0]['image_url']
    assert snapshot['ai_original']['main_image_url'] == rows[0]['image_url']
    assert snapshot['ai_original']['failed_step'] == 'sku_images'


def test_legacy_list_uses_single_sku_cover_without_mutating_input():
    snapshot = {'original_1688': {'variations': sku_rows()[:1]},
                'ai_original': {'variations': [{'image_url': '/api/ai-original-products/images/sku1-ai-white.jpg'}]}}
    row = {'source_type': 'ai_original', 'main_image_url': 'https://example.com/old.jpg', 'source_snapshot_json': json.dumps(snapshot)}
    before = deepcopy(row)
    result = _mirror_zying_snapshot_fields(row)
    assert result['main_image_url'].endswith('/sku1-ai-white.jpg')
    assert result['original_1688']['main_image_url'].endswith('/red.jpg')
    assert row == before


@pytest.mark.parametrize('platform', ['mercadolibre', 'amazon'])
def test_single_sku_repair_reuses_cover(platform, monkeypatch, local_images):
    options, downloads = local_images
    row = _ai_row()
    snapshot = json.loads(row['source_snapshot_json'])
    snapshot['original_1688'] = {'source_1688_item_id': '123456', 'main_image_url': 'https://example.com/wrong.jpg', 'variations': sku_rows()[:1]}
    snapshot['ai_original']['variations'] = deepcopy(sku_rows()[:1])
    snapshot['ai_original']['variations'][0]['attribute_combinations'] = [{'id': 'COLOR', 'value_name': 'Red'}]
    snapshot['ai_original'].update(title='Title', description='Description')
    snapshot['marketplace_listings'] = {platform: deepcopy(snapshot['ai_original'])}
    row['source_snapshot_json'] = snapshot
    monkeypatch.setattr(products, 'generate_marketplace_copy', lambda *a, **kw: pytest.fail('must not regenerate copy'))
    monkeypatch.setattr(products, 'generate_platform_copy', lambda *a, **kw: pytest.fail('must not regenerate copy'))
    result = products.prepare_ai_original_product(row, target_platform=platform, **options)
    draft = result['ai_original']
    assert result['main_image_url'] == draft['main_image_url'] == draft['variations'][0]['white_image_url']
    assert downloads == ['https://cbu01.alicdn.com/red.jpg']
    if platform == 'mercadolibre':
        assert json.loads(result['source_snapshot_json'])['source']['pictures'][0]['source'] == draft['main_image_url']


def test_fresh_single_sku_generation_does_not_generate_a_second_cover(monkeypatch, local_images):
    options, downloads = local_images
    row = _ai_row()
    row['source_snapshot_json'] = {'original_1688': {'source_1688_item_id': '123456',
        'main_image_url': 'https://example.com/wrong.jpg', 'variations': sku_rows()[:1]}}
    monkeypatch.setattr(products, 'generate_platform_copy', lambda *a, **kw: {'title': 'Title', 'description': 'Description'})
    result = products.prepare_ai_original_product(row, target_platform='amazon', **options)
    draft = result['ai_original']
    assert draft['main_image_url'] == draft['variations'][0]['white_image_url']
    assert downloads == ['https://cbu01.alicdn.com/red.jpg']
