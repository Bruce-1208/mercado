"""Supplier SKU economics and explicitly ordered SKU photographs."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_CEILING


def supplier_net_proceeds(price):
    try:
        value = Decimal(str(price))
        if not value.is_finite() or value <= 0:
            return None
        return int(((value + Decimal('5')) / Decimal('6.7')).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, TypeError):
        return None


def supplier_skus(rows, *, validate=False):
    from erp.ai_original_images import variation_image_sources
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError('SKU 必须是数组，最多 500 项')
    result = deepcopy(rows)
    identities = set()
    for index, row in enumerate(result, 1):
        if not isinstance(row, dict):
            raise ValueError(f'SKU {index} 格式无效')
        identity = str(row.get('sku_id') or row.get('id') or row.get('seller_sku') or '')
        if validate and (not identity or identity in identities):
            raise ValueError(f'SKU {index} 编号缺失或重复')
        identities.add(identity)
        if validate:
            if row.get('images') is not None and not isinstance(row['images'], list):
                raise ValueError(f'SKU {index} 图片必须是链接数组')
            if row.get('seller_sku') is not None and (not isinstance(row['seller_sku'], str) or len(row['seller_sku']) > 255):
                raise ValueError(f'SKU {index} 自定义 SKU 必须是最多 255 字的文本')
            for key in ('price', 'available_quantity', 'weight_g'):
                value = row.get(key)
                if value in (None, ''):
                    continue
                try:
                    number = Decimal(str(value))
                    if (not number.is_finite() or number < 0 or number > Decimal('9999999999999999.9999') or
                            (key != 'available_quantity' and number == 0) or
                            (key == 'available_quantity' and number != number.to_integral_value())):
                        raise ValueError()
                except (InvalidOperation, ValueError):
                    raise ValueError(f'SKU {index} 的 {key} 数值无效') from None
                row[key] = int(number) if key == 'available_quantity' else float(number)
            for value in [row.get('image_url'), *(row.get('images') or [])]:
                if value and (not isinstance(value, str) or not value.startswith(('https://', 'http://')) or len(value) > 1500):
                    raise ValueError(f'SKU {index} 图片必须是 http/https 链接')
        urls = variation_image_sources(row)
        row.update(image_url=urls[0] if urls else '', images=urls,
                   image_status='confirmed' if urls else 'missing',
                   net_proceeds_usd=supplier_net_proceeds(row.get('price')),
                   domestic_freight_cny=5, net_exchange_rate=6.7)
    return result


def sku_identity(row):
    return str(row.get('sku_id') or row.get('id') or row.get('seller_sku') or '')


def sync_supplier_skus(snapshot, rows):
    """Apply reviewed supplier fields by identity, preserving translated attributes."""
    original = snapshot.setdefault('original_1688', {})
    legacy_single = len(rows) == 1 and not original.get('variations')
    old_cover = original.get('main_image_url') or original.get('image_url') or ''
    old_images = deepcopy(original.get('images') or ([old_cover] if old_cover else []))
    original['variations'] = deepcopy(rows)
    prices = [row['price'] for row in rows if row.get('price') not in (None, '')]
    original['price'] = min(prices) if prices else None
    sync_single_sku_cover(original)
    by_id = {sku_identity(row): row for row in rows}
    states = [snapshot.get('ai_original') or {}, *((snapshot.get('marketplace_listings') or {}).values()),
              snapshot.get('source') or {}]
    for state in states:
        if not state:
            continue
        changed_images = False
        if legacy_single:
            source = rows[0]
            existing = state.get('variations') or []
            if not existing:
                # Keep a prepared single product's white cover when its source
                # pictures have not changed; supplier identity is now explicit.
                cover = state.get('main_image_url') or old_cover
                state['variations'] = [{
                    **deepcopy(source), 'image_url': cover,
                    'images': [cover] if cover else [],
                    'source_image_url': old_cover, 'source_images': old_images,
                    **{key: state[key] for key in ('image_generation_method', 'white_image_url', 'white_image_path') if key in state},
                }]
            elif len(existing) == 1:
                existing[0]['sku_id'] = sku_identity(source)
            state['source_variations'] = [{**deepcopy(source),
                'source_image_url': old_cover, 'source_images': old_images}]
        for field in ('variations', 'source_variations'):
            for variation in state.get(field) or []:
                source = by_id.get(sku_identity(variation))
                if source is None:
                    continue
                old_cover = variation.get('source_image_url') or variation.get('image_url') or ''
                if old_cover != source['image_url'] or variation.get('source_images', variation.get('images', [])) != source['images']:
                    for key in ('white_image_url', 'white_image_path', 'image_generation_method', 'image_source_fingerprint',
                                'picture_ids', 'pictures', 'main_image_url', 'imageUrl', 'imgUrl', 'pictureUrl'):
                        variation.pop(key, None)
                    variation.update(image_url=source['image_url'], image=source['image_url'],
                                     source_image_url=source['image_url'], source_images=deepcopy(source['images']),
                                     images=deepcopy(source['images']), image_source_basis='supplier_sku')
                    changed_images = True
                for key in ('price', 'net_proceeds_usd', 'available_quantity', 'weight_g', 'seller_sku'):
                    if key in source:
                        variation[key] = source[key]
        sync_single_sku_cover(state)
        if changed_images and state.get('status') in ('completed', 'failed'):
            state.update(status='failed', failed_step='sku_images', workflow_status='needs_sku_images',
                         error='SKU 图片已修改，请重新处理白底主图', input_blockers=[])
            state.setdefault('preparation', {}).update(version=1, sku_images_ready=False, sku_images_only=True)
            state['edit_revision'] = int(state.get('edit_revision') or 0) + 1


SKU_PATHS = ('source.variations', 'original_1688.variations', 'ai_original.variations', 'variations')


def sku_count(snapshot):
    counts = [1]
    for path in SKU_PATHS:
        value = snapshot
        for part in path.split('.'):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, list):
            counts.append(len(value))
    return max(counts)


def sku_count_sql(column='`source_snapshot_json`'):
    document = f"IF(JSON_VALID({column}), {column}, '{{}}')"
    arrays = [f"JSON_EXTRACT({document}, '$.{path}')" for path in SKU_PATHS]
    return 'GREATEST(1, ' + ', '.join(f"CASE WHEN JSON_TYPE({a}) = 'ARRAY' THEN JSON_LENGTH({a}) ELSE 0 END" for a in arrays) + ')'


def sync_single_sku_cover(product):
    """A single variant owns its product cover; retain the remaining gallery."""
    from erp.ai_original_images import image_url
    rows = product.get('variations') or []
    if len(rows) != 1 or not isinstance(rows[0], dict):
        return ''
    sku = rows[0]
    cover = next((image_url(sku.get(key)) for key in ('image_url', 'image', 'main_image_url')
                  if image_url(sku.get(key))), '')
    if not cover:
        pictures = product.get('pictures') or []
        lookup = {str(p.get('id')): image_url(p) for p in pictures if isinstance(p, dict)}
        cover = next((lookup.get(str(key), '') for key in sku.get('picture_ids') or []
                      if lookup.get(str(key))), '')
    if not cover:
        return ''
    product['main_image_url'] = cover
    for field in ('images', 'pictures'):
        if field in product:
            first = next((p for p in product[field] or [] if image_url(p) == cover),
                         {'source': cover} if field == 'pictures' else cover)
            product[field] = [first, *(p for p in product[field] or [] if image_url(p) != cover)]
    if sku.get('image_generation_method'):
        product['image_generation_method'] = sku['image_generation_method']
    return cover
