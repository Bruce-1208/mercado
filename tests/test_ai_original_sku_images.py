"""SKU image processing, resumable repair and the final uploaded family payload."""
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from erp import ai_original_images as images
from erp import ai_original_products as products
from erp.mercadolibre_batch_publish import product_publish_issues, _prepared_listing_from_product_row
from erp.mercadolibre_follow_sell import follow_sell
from test_erp_mercadolibre_follow_sell import FamilyClient, family_source
from test_ai_original_products import _ai_row


@pytest.fixture
def local_images(monkeypatch, tmp_path):
    calls = []
    def download(url, **kwargs):
        calls.append(url)
        image = Image.new('RGB', (600, 600), '#dddddd')
        ImageDraw.Draw(image).rectangle((130, 80, 470, 520), fill='blue' if 'blue' in url else 'red')
        out = BytesIO()
        image.save(out, format='PNG')
        return SimpleNamespace(content=out.getvalue(), raise_for_status=lambda: None)
    def cutout(image):
        # Substitute only segmentation; exercise the real download, JPEG file,
        # sizing, white compositing and per-source filename generation.
        image = image.convert('RGBA')
        alpha = Image.new('L', image.size)
        ImageDraw.Draw(alpha).rectangle((130, 80, 470, 520), fill=255)
        image.putalpha(alpha)
        return image
    monkeypatch.setattr(products, '_remove_background_locally', cutout)
    return dict(http_get=download, image_dir=tmp_path), calls


def sku_rows():
    return [
        {'seller_sku': 'RED-S', 'price': 12, 'available_quantity': 5,
         'image_url': 'https://cbu01.alicdn.com/red.jpg_100x100.jpg'},
        {'seller_sku': 'BLUE-M', 'price': 15, 'available_quantity': 3,
         'images': [{'url': 'https://cbu01.alicdn.com/blue.jpg'}]},
        {'seller_sku': 'RED-L', 'price': 18, 'available_quantity': 9,
         'image': 'https://cbu01.alicdn.com/red.jpg'},
    ]


def test_each_sku_gets_correct_white_cover_and_shared_sources_are_processed_once(local_images):
    options, downloads = local_images
    original = sku_rows()
    before = deepcopy(original)
    rows, cache = images.prepare_variation_white_images(original, item_id='12345678', **options)
    assert original == before
    assert downloads == ['https://cbu01.alicdn.com/red.jpg', 'https://cbu01.alicdn.com/blue.jpg']
    assert rows[0]['white_image_url'] == rows[2]['white_image_url'] != rows[1]['white_image_url']
    assert len(cache) == 2 and all(images.variation_white_image_ready(row) for row in rows)
    for index, row in enumerate(rows):
        assert row['seller_sku'] == original[index]['seller_sku']
        assert row['price'] == original[index]['price']
        assert row['available_quantity'] == original[index]['available_quantity']
        with Image.open(row['white_image_path']) as image:
            assert image.size == (1024, 1024)
            assert all(channel > 250 for channel in image.getpixel((0, 0)))
            red, green, blue = image.getpixel((512, 512))
            assert (blue > red) == (index == 1)
    rows_again, _ = images.prepare_variation_white_images(rows, item_id='12345678', cache=cache, **options)
    assert rows_again == rows and len(downloads) == 2


def test_edited_source_and_deleted_file_invalidate_image_cache(local_images):
    options, downloads = local_images
    rows, cache = images.prepare_variation_white_images(sku_rows(), item_id='123', **options)
    rows[0]['image_url'] = 'https://cbu01.alicdn.com/blue-new.jpg'
    assert not images.variation_white_image_ready(rows[0])
    from pathlib import Path
    Path(rows[1]['white_image_path']).unlink()
    updated, _ = images.prepare_variation_white_images(rows, item_id='123', cache=cache, **options)
    assert len(downloads) == 4
    assert updated[0]['source_image_url'].endswith('blue-new.jpg')
    assert updated[0]['white_image_url'] != rows[0]['white_image_url']


def test_failure_checkpoints_completed_sku_and_retries_only_missing_source(monkeypatch, local_images):
    options, downloads = local_images
    real = products._prepare_original_image
    blocked = {'blue': True}
    def prepare(original, *args, **kwargs):
        if blocked['blue'] and 'blue' in original['main_image_url']:
            raise ValueError('模拟图片下载失败')
        return real(original, *args, **kwargs)
    monkeypatch.setattr(products, '_prepare_original_image', prepare)
    checkpoints = []
    with pytest.raises(ValueError, match='SKU 2.*下载失败'):
        images.prepare_variation_white_images(sku_rows(), item_id='123',
            on_checkpoint=lambda rows, cache: checkpoints.append((rows, cache)), **options)
    assert len(checkpoints) == len(downloads) == 1
    blocked['blue'] = False
    rows, _ = images.prepare_variation_white_images(checkpoints[-1][0], item_id='123', cache=checkpoints[-1][1], **options)
    assert len(downloads) == 2 and all(images.variation_white_image_ready(row) for row in rows)


def test_missing_sku_image_is_not_replaced_with_parent_or_other_sku(local_images):
    options, _ = local_images
    with pytest.raises(ValueError, match='SKU 2 缺少采集主图'):
        images.prepare_variation_white_images([sku_rows()[0], {'seller_sku': 'MISSING'}], item_id='123', **options)


def test_completed_legacy_draft_repairs_only_images_and_updates_publish_snapshot(monkeypatch, local_images):
    options, downloads = local_images
    row = _ai_row()
    snapshot = json.loads(row['source_snapshot_json'])
    snapshot['original_1688'] = {'source_1688_item_id': '123456', 'variations': sku_rows()}
    snapshot['ai_original']['variations'] = deepcopy(sku_rows())
    for index, variation in enumerate(snapshot['ai_original']['variations']):
        variation['attribute_combinations'] = [{'id': 'MODEL', 'value_name': str(index)}]
    snapshot['ai_original']['title_es'] = 'Título corregido manualmente'
    row['source_snapshot_json'] = snapshot
    original = deepcopy(snapshot['original_1688'])
    monkeypatch.setattr(products, 'generate_marketplace_copy', lambda *a, **kw: pytest.fail('must not regenerate copy'))
    monkeypatch.setattr(products, 'complete_marketplace_category', lambda *a, **kw: pytest.fail('must not regenerate category'))
    assert products.original_generation_skip_reason(row) == ''
    assert any('SKU 白底主图' in issue for issue in product_publish_issues(row))
    with pytest.raises(ValueError, match='每个 SKU'):
        _prepared_listing_from_product_row(row)
    result = products.prepare_ai_original_product(row, **options)
    saved = json.loads(result['source_snapshot_json'])
    assert saved['original_1688'] == original
    assert result['ai_original']['title_es'] == 'Título corregido manualmente'
    assert saved['source']['variations'] == saved['ai_original']['variations']
    assert len(downloads) == 2
    row['source_snapshot_json'] = saved
    assert products.original_generation_skip_reason(row)
    prepared, _ = _prepared_listing_from_product_row(row)
    assert all(images.variation_white_image_ready(sku) for sku in prepared['variations'])
    assert not any('SKU 白底主图' in issue for issue in product_publish_issues(row))


def test_final_family_upload_uses_different_sku_picture_ids(local_images):
    options, _ = local_images
    rows, _ = images.prepare_variation_white_images(sku_rows()[:2], item_id='123456', **options)
    source = family_source()
    source['pictures'].insert(0, {'source': '/api/ai-original-products/images/1688-parent-ai-white.jpg'})
    for variation, image in zip(source['variations'], rows):
        variation.update({key: image[key] for key in ('white_image_url', 'image_url', 'image_generation_method')})
    client = FamilyClient()
    with patch('erp.mercadolibre_source_store.record_publish_result'):
        follow_sell(client, source['id'], prepared_listing=(source, {}), net_proceeds=20,
                    publish=True, source_from_database=True)
    uploaded = {url: f'uploaded-{index}' for index, url in enumerate(client.uploads, 1)}
    assert [payload['pictures'][0]['id'] for payload in client.posts[0]] == [uploaded[row['white_image_url']] for row in rows]
    assert not any('parent-ai-white' in url for url in client.uploads)


def test_normalize_keeps_all_collected_gallery_images():
    urls = [f'https://cbu01.alicdn.com/image-{i}.jpg' for i in range(30)]
    result = products.normalize_1688_product({'source_url': 'https://detail.1688.com/offer/12345678.html', 'title': 'test', 'images': urls})
    assert result['images'] == urls


def test_restore_sources_uses_identity_preserves_manual_image_and_rejects_ambiguity():
    rows = [{'sku_id': '2'}, {'sku_id': '1'}, {'sku_id': '3', 'image_url': 'https://cbu01.alicdn.com/manual.jpg'}, {'sku_id': '4'}]
    sources = [{'sku_id': '1', 'image_url': 'https://cbu01.alicdn.com/red.jpg'},
               {'sku_id': '2', 'image_url': 'https://cbu01.alicdn.com/blue.jpg'},
               {'sku_id': '4', 'image_url': 'https://cbu01.alicdn.com/a.jpg'},
               {'sku_id': '4', 'image_url': 'https://cbu01.alicdn.com/b.jpg'}]
    result = images.restore_variation_image_sources(rows, sources)
    assert result[0]['image_url'].endswith('/blue.jpg')
    assert result[1]['image_url'].endswith('/red.jpg')
    assert result[2] == rows[2] and result[3] == rows[3]
    assert 'image_url' not in rows[0]


def test_completed_recollection_persists_only_missing_photos(monkeypatch):
    from erp import mercadolibre_collection_store as store
    from test_ai_original_net_persistence import ProductDB, existing_product, source_product
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    row = existing_product(status='completed', net=Decimal('22'))
    saved = {'original_1688': {'variations': [{'sku_id': 'small', 'price': 12}]},
             'ai_original': {'status': 'completed', 'title_es': 'Manual title', 'category_id': 'CBT123',
                             'variations': [{'sku_id': 'small', 'attribute_combinations': [{'id': 'COLOR', 'value_name': 'Red'}]}]},
             'purchase_net_proceeds': {'manual': True}}
    row['source_snapshot_json'] = json.dumps(saved)
    db = ProductDB(row)
    store.upsert_ai_original_product(source_product(variations=[{'sku_id': 'small', 'price': 19, 'image_url': 'https://cbu01.alicdn.com/red.jpg'}]), connection_factory=lambda: db)
    fresh = json.loads(db.row['source_snapshot_json'])
    assert fresh['ai_original']['title_es'] == 'Manual title'
    assert fresh['ai_original']['category_id'] == 'CBT123'
    assert fresh['ai_original']['variations'][0]['image_url'].endswith('/red.jpg')
    assert fresh['original_1688']['variations'][0]['price'] == 12
    assert fresh['purchase_net_proceeds'] == saved['purchase_net_proceeds']
    assert db.row['net_proceeds_usd'] == Decimal('22')
    assert fresh['ai_original']['edit_revision'] == 1
    assert db.committed


def test_recollection_supplements_missing_facts_without_replacing_existing_data():
    snapshot = {'original_1688': {'description_text': 'Reviewed source text', 'weight_g': 100,
        'images': [{'url': 'https://cbu01.alicdn.com/a.jpg'}],
        'properties': [{'name': '材质', 'value_name': 'Steel'}]},
        'ai_original': {'status': 'completed', 'title_es': 'Reviewed title'}}
    result = images.merge_recollected_sku_images(snapshot, {
        'description_text': 'New text', 'weight_g': 200, 'package_length_cm': 15,
        'images': ['https://cbu01.alicdn.com/b.jpg'],
        'properties': [{'name': '材质', 'value_name': 'Plastic'}, {'name': '颜色', 'value_name': 'Blue'}],
        'variations': [{'sku_id': '1', 'image_url': 'https://cbu01.alicdn.com/sku.jpg'}]})
    assert result['original_1688']['weight_g'] == 100
    assert result['original_1688']['package_length_cm'] == 15
    assert result['original_1688']['description_text'] == 'Reviewed source text'
    assert result['original_1688']['properties'] == [{'name': '材质', 'value_name': 'Steel'}, {'name': '颜色', 'value_name': 'Blue'}]
    assert result['original_1688']['variations'][0]['sku_id'] == '1'
    assert len(result['original_1688']['images']) == 2
    assert result['ai_original'] == snapshot['ai_original']
    assert 'package_length_cm' not in snapshot['original_1688']


@pytest.mark.parametrize('gallery_key', ['images', 'pictures'])
def test_product_gallery_does_not_substitute_missing_variant_photos(gallery_key):
    original = {gallery_key: [{'secure_url': 'https://cbu01.alicdn.com/first.jpg'},
                               {'url': 'https://cbu01.alicdn.com/second.jpg'}],
                'variations': [{'sku_id': 'red', 'images': ['https://cbu01.alicdn.com/red.jpg',
                                                         'https://cbu01.alicdn.com/red-detail.jpg']},
                               {'sku_id': 'blue'}]}
    before = deepcopy(original)
    rows = images.restore_product_variation_image_sources(
        [{'sku_id': 'red'}, {'sku_id': 'blue'}, {'seller_sku': 'legacy'}], original)
    assert rows[0]['image_url'].endswith('/red.jpg')
    assert all(not v.get('image_url') for v in rows[1:])
    assert original == before


def test_finished_white_cover_without_retained_source_can_resume(local_images):
    options, downloads = local_images
    ready = {'sku_id': '1', 'image_url': '/images/old-ai-white.jpg',
             'white_image_url': '/images/old-ai-white.jpg',
             'image_generation_method': 'local_background_removal'}
    rows, _ = images.prepare_variation_white_images([ready, sku_rows()[0]], item_id='1', **options)
    assert rows[0] == ready and len(downloads) == 1


@pytest.mark.parametrize('name,values', [('尺码', ['S', 'M']), ('颜色', ['Red', 'Blue']), ('款式', ['A', 'B'])])
def test_missing_variant_photos_require_explicit_confirmation(name, values, local_images):
    options, downloads = local_images
    source = {'main_image_url': 'https://cbu01.alicdn.com/cover.jpg',
              'images': ['https://cbu01.alicdn.com/detail.jpg'],
              'variations': [{'sku_id': str(i), 'attribute_combinations': [{'name': name, 'value_name': value}]}
                             for i, value in enumerate(values)]}
    restored = images.restore_product_variation_image_sources(source['variations'], source)
    with pytest.raises(ValueError, match='SKU 1 缺少采集主图'):
        images.prepare_variation_white_images(restored, item_id='123', **options)
    assert downloads == []


def test_gallery_cover_does_not_clear_multiple_sku_missing_source_blocker():
    from erp.ai_original_state import source_input_state
    draft = {'status': 'failed', 'failed_step': 'sku_images', 'variations': [{'sku_id': '1'}, {'sku_id': '2'}],
             'input_blockers': [{'field': 'sku_images', 'sku_indices': [1, 2]}]}
    assert source_input_state(draft, {'images': ['https://cbu01.alicdn.com/first.jpg']})['input_blockers'][0]['sku_indices'] == [1, 2]
    assert source_input_state(draft, {'images': ['/images/old-ai-white.jpg']})['input_blockers'][0]['sku_indices'] == [1, 2]


def test_failed_sku_image_retry_uses_confirmed_own_images_without_regenerating_copy(monkeypatch, local_images):
    options, downloads = local_images
    row = _ai_row()
    snapshot = json.loads(row['source_snapshot_json'])
    variations = [{'sku_id': str(i), 'image_url': f'https://cbu01.alicdn.com/{color}.jpg',
                   'attribute_combinations': [{'id': 'COLOR', 'value_name': color}]}
                  for i, color in enumerate(['Red', 'Blue'])]
    snapshot['original_1688'] = {'source_1688_item_id': '123456', 'variations': variations,
                               'images': ['https://cbu01.alicdn.com/first.jpg', 'https://cbu01.alicdn.com/detail.jpg']}
    snapshot['ai_original'].update(status='failed', failed_step='sku_images', variations=deepcopy(variations),
                                   preparation={'version': 1, 'sku_images_only': True})
    row['source_snapshot_json'] = snapshot
    monkeypatch.setattr(products, 'generate_marketplace_copy', lambda *a, **kw: pytest.fail('must not regenerate copy'))
    monkeypatch.setattr(products, 'complete_marketplace_category', lambda *a, **kw: pytest.fail('must not regenerate category'))
    result = products.prepare_ai_original_product(row, **options)
    saved = json.loads(result['source_snapshot_json'])
    assert result['ai_original']['status'] == 'completed'
    assert downloads == ['https://cbu01.alicdn.com/Red.jpg', 'https://cbu01.alicdn.com/Blue.jpg']
    assert all(images.variation_white_image_ready(v) for v in saved['source']['variations'])
    assert saved['original_1688'] == snapshot['original_1688']
