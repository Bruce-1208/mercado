"""Per-SKU white covers, retaining supplier photos and resumable image work."""
from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


WHITE_METHODS = {'ai_image_edit', 'local_background_removal'}


def restore_variation_image_sources(variations, source_variations):
    """Restore missing photos by supplier SKU identity, never by list position."""
    rows = deepcopy(list(variations or []))
    sources = list(source_variations or [])
    for row in rows:
        if variation_image_sources(row) or variation_white_image_ready(row):
            continue
        identity = str(row.get('sku_id') or row.get('id') or '')
        matches = [s for s in sources if identity and
                   str(s.get('sku_id') or s.get('id') or '') == identity]
        urls = list(dict.fromkeys(url for s in matches for url in variation_image_sources(s)))
        if len(urls) == 1:
            row['image_url'] = urls[0]
            row['source_image_url'] = urls[0]
    return rows


def merge_recollected_sku_images(snapshot, collected):
    """Supplement completed drafts without replacing reviewed listing fields."""
    result = deepcopy(snapshot)
    original = result.setdefault('original_1688', {})
    source_rows = collected.get('variations') or []
    original['variations'] = restore_variation_image_sources(original.get('variations'), source_rows)
    original['images'] = list(dict.fromkeys([*(original.get('images') or []),
                                            *(collected.get('images') or [])]))
    states = [result.get('ai_original') or {},
              *((result.get('marketplace_listings') or {}).values())]
    for state in states:
        for field in ('variations', 'source_variations'):
            if isinstance(state.get(field), list):
                state[field] = restore_variation_image_sources(state[field], source_rows)
        checkpoint = state.get('category_checkpoint') or {}
        if isinstance(checkpoint.get('variations'), list):
            checkpoint['variations'] = restore_variation_image_sources(checkpoint['variations'], source_rows)
    source = result.get('source') or {}
    if isinstance(source.get('variations'), list):
        source['variations'] = restore_variation_image_sources(source['variations'], source_rows)
    return result


def image_url(value):
    if isinstance(value, Mapping):
        value = next((value.get(key) for key in ('secure_url', 'url', 'source', 'src') if value.get(key)), '')
    from erp.ai_original_products import normalize_1688_image_url
    return normalize_1688_image_url(value)


def is_white_image(value):
    return urlparse(str(value or '')).path.endswith('-ai-white.jpg')


def variation_image_sources(variation):
    """Prefer an explicitly edited picture; generated covers retain their source."""
    values = [variation.get('image_url'), variation.get('image'), variation.get('main_image_url'),
              variation.get('imageUrl'), variation.get('imgUrl'), variation.get('pictureUrl'),
              variation.get('source_image_url')]
    for key in ('source_images', 'images', 'pictures'):
        if isinstance(variation.get(key), list):
            values.extend(variation[key])
    for key in ('attribute_combinations', 'attributes', 'properties'):
        for attribute in variation.get(key) or []:
            if isinstance(attribute, Mapping):
                values.extend(attribute.get(name) for name in ('image_url', 'image', 'imageUrl'))
    urls = []
    for value in values:
        url = image_url(value)
        if url and not is_white_image(url) and url not in urls:
            urls.append(url)
    return urls


def variation_white_image_ready(variation):
    if not isinstance(variation, Mapping):
        return False
    cover = variation.get('white_image_url')
    return bool(cover and is_white_image(cover)
                and variation.get('image_generation_method') in WHITE_METHODS
                and variation.get('image_url') == cover)


def missing_variation_white_images(draft, original=None):
    variations = draft.get('variations') or []
    source = draft.get('source_variations')
    if not isinstance(source, list):
        source = (original or {}).get('variations') or []
    if len(variations) < len(source):
        return list(range(1, len(source) + 1))
    return [index for index, variation in enumerate(variations, 1)
            if not variation_white_image_ready(variation)]


def prepare_variation_white_images(variations, *, item_id, target_platform='mercadolibre',
                                   cache=None, on_checkpoint=None, image_dir=None, http_get=None):
    """Process each distinct supplier cover once; checkpoint each successful image."""
    from erp.ai_original_products import _prepare_original_image
    rows = deepcopy(list(variations or []))
    cached = deepcopy(dict(cache or {}))
    for index, variation in enumerate(rows, 1):
        sources = variation_image_sources(variation)
        if not sources:
            raise ValueError(f'SKU {index} 缺少采集主图，请补充该 SKU 的图片链接后重新生成')
        fingerprint = hashlib.sha256((str(item_id) + '\n' + target_platform + '\n' + sources[0]).encode()).hexdigest()
        saved = cached.get(fingerprint) or {}
        valid = (saved.get('source_image_url') == sources[0]
                 and saved.get('image_generation_method') in WHITE_METHODS
                 and is_white_image(saved.get('white_image_url'))
                 and (not saved.get('white_image_path') or Path(saved['white_image_path']).is_file()))
        if not valid:
            try:
                url, method, path = _prepare_original_image(
                    {'source_1688_item_id': f'sku-{fingerprint[:24]}', 'main_image_url': sources[0], 'images': sources},
                    {}, target_platform=target_platform, api_key='', base_url='',
                    image_api_key='', image_base_url='', image_model='', image_generate=None,
                    image_dir=image_dir, http_get=http_get,
                )
                if not is_white_image(url) or method not in WHITE_METHODS:
                    raise ValueError('未返回有效的白底主图')
            except Exception as exc:
                raise ValueError(f'SKU {index} 白底主图处理失败：{exc}') from exc
            saved = {'white_image_url': url, 'white_image_path': path,
                     'image_generation_method': method, 'source_image_url': sources[0]}
            cached[fingerprint] = saved
        variation.update(saved, image_url=saved['white_image_url'], image=saved['white_image_url'],
                         image_source_fingerprint=fingerprint, source_images=sources,
                         images=[saved['white_image_url']])
        variation.pop('picture_ids', None)
        if on_checkpoint:
            on_checkpoint(deepcopy(rows), deepcopy(cached))
    return rows, cached
