"""Recover native attributes/variants from detail-page hydration data.

Never execute page scripts or construct a Cartesian product of picker options:
only variant rows actually supplied by the product page are retained.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from urllib.parse import parse_qs, urljoin, urlsplit

from erp.mercadolibre_attribute_rules import canonical_attribute_id

_SCRIPT_MARKER = re.compile(
    r"(?:__PRELOADED_STATE__|__INITIAL_STATE__|__NEXT_DATA__|__APOLLO_STATE__|preloadedState|_n\.ctx\.r)\s*[:=]\s*", re.I
)
_VARIANTS = re.compile(r"^(?:variations?|variants?|hasVariant|skus?|sku_list|skuList|item_skus|itemSkus|sku_map|skuMap|sku_info_map|skuInfoMap)$", re.I)
_SKIP = re.compile(r"recommend|carousel|related|tracking|analytics", re.I)


def _picker_text(value):
    if isinstance(value, dict):
        value = value.get('text') or value.get('name') or value.get('value')
    return value.strip() if isinstance(value, str) else ''


def _picker_variants(pickers, page_url=''):
    """Join dimension values only by explicit SKU identity, never by position."""
    dimensions = [p for p in pickers if isinstance(p, dict) and any(
        isinstance(p.get(k), list) and p[k] for k in ('products', 'options', 'values'))]
    rows = {}
    dimension_ids = []
    for picker in dimensions:
        dimension = picker.get('attribute_id') or picker.get('picker_id') or picker.get('id')
        if not isinstance(dimension, str) or not dimension or dimension in dimension_ids:
            return []
        dimension_ids.append(dimension)
        options = next(picker[k] for k in ('products', 'options', 'values')
                       if isinstance(picker.get(k), list) and picker[k])
        for option in options:
            if not isinstance(option, dict):
                return []
            label = next((text for key in ('value_name', 'picker_label', 'label', 'name', 'value')
                          if (text := _picker_text(option.get(key)))), '')
            identities = option.get('variation_ids', option.get('variationIds'))
            if identities is None:
                identity = option.get('variation_id', option.get('variationId'))
                identities = [identity] if identity is not None else None
            if identities is None and isinstance(option.get('variations'), list):
                identities = option['variations']
            if identities is None and page_url:
                # A plain option ID may identify a catalog product/value. Only
                # accept a URL's explicit variation parameter on this listing.
                link = option.get('permalink') or option.get('url')
                if isinstance(link, str):
                    try:
                        base, target = urlsplit(page_url), urlsplit(urljoin(page_url, link))
                    except ValueError:
                        return []
                    if (not re.search(r'/(?:p|up)/', base.path, re.I)
                            and (base.scheme, base.netloc, base.path) == (target.scheme, target.netloc, target.path)):
                        query = parse_qs(target.query)
                        linked_ids = list(dict.fromkeys(v for k in ('variation', 'variation_id', 'searchVariation')
                                                        for v in query.get(k, [])))
                        identities = linked_ids if len(linked_ids) == 1 else None
            if not label or not isinstance(identities, list) or not identities:
                return []
            for identity in identities:
                if isinstance(identity, bool) or not isinstance(identity, (int, str)) or not str(identity).isdigit():
                    return []
                key = str(identity)
                if key not in rows:
                    if len(rows) >= 500:
                        return []
                    rows[key] = {'id': identity, 'attribute_combinations': []}
                row = rows[key]
                previous = next((a for a in row['attribute_combinations'] if a['id'] == dimension), None)
                if previous and previous['value_name'] != label:
                    return []
                if not previous:
                    row['attribute_combinations'].append({'id': dimension, 'value_name': label})
                # Shared option-level stock/price is not per-SKU evidence.
                if len(identities) == 1:
                    normalized = normalize_variation(option)
                    for field in ('seller_sku', 'price', 'available_quantity', 'picture_ids', 'images', 'pictures'):
                        if normalized.get(field) is not None:
                            if field in row and row[field] != normalized[field]:
                                return []
                            row[field] = normalized[field]
    if any(len(row['attribute_combinations']) != len(dimension_ids) for row in rows.values()):
        return []
    return list(rows.values())


def normalize_variation(raw):
    """Keep source evidence while giving every collector the same SKU fields."""
    row = deepcopy(raw)
    aliases = {
        'id': ('variation_id', 'variationId'),
        'seller_sku': ('sellerSku', 'sku', 'skuCode'),
        'available_quantity': ('availableQuantity', 'stock', 'quantity', 'available'),
        'price': ('price_amount',),
        'picture_ids': ('pictureIds',),
        'attribute_combinations': ('attributeCombinations', 'combinations', 'variation_attributes', 'specifications'),
    }
    for key, names in aliases.items():
        if row.get(key) is None:
            for name in names:
                if row.get(name) is not None:
                    row[key] = row[name]
                    break
    # JSON-LD ProductGroup children describe real combinations, unlike picker
    # option lists. Do not manufacture combinations from independent options.
    if not row.get('attribute_combinations'):
        dimensions = [deepcopy(a) for a in row.get('attributes') or []
                      if isinstance(a, dict) and a.get('id') in ('COLOR', 'SIZE')]
        if dimensions:
            row['attribute_combinations'] = dimensions
            row['attributes'] = [a for a in row['attributes'] if a not in dimensions]
    if not row.get('attribute_combinations'):
        dimensions = [{'id': key.upper(), 'value_name': str(row[key])}
                      for key in ('color', 'size') if row.get(key) not in (None, '')]
        if dimensions:
            row['attribute_combinations'] = dimensions
    combinations = row.get('attribute_combinations')
    if isinstance(combinations, list):
        for attribute in combinations:
            if not isinstance(attribute, dict):
                continue
            if not attribute.get('id') and attribute.get('attribute_id'):
                attribute['id'] = attribute['attribute_id']
            if attribute.get('value_name') is None and attribute.get('value') is not None:
                value = attribute['value']
                if isinstance(value, dict):
                    attribute['value_name'] = value.get('name') or value.get('text')
                    if attribute.get('value_id') is None and value.get('id') is not None:
                        attribute['value_id'] = value['id']
                else:
                    attribute['value_name'] = str(value)
    row.setdefault('available_quantity', None)
    offers = row.get('offers')
    if isinstance(offers, dict) and row.get('price') is None:
        row['price'] = offers.get('price')
    # Keep one editable representation. Stale aliases otherwise resurrect a
    # price, stock or picture reference after the operator clears that field.
    for key, names in aliases.items():
        if key in row:
            for name in names:
                row.pop(name, None)
    return row


def _parse_script(text):
    if not isinstance(text, str) or len(text) > 5_000_000:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        marker = _SCRIPT_MARKER.search(text)
        if marker:
            start = text.find('{', marker.end())
            if start >= 0:
                try:
                    return json.JSONDecoder().raw_decode(text[start:])[0]
                except (ValueError, RecursionError):
                    pass
    return None


def page_facts(page):
    attributes, candidates = [], []
    picture_urls = {}
    variant_overflow = False

    def add(raw):
        if not isinstance(raw, dict):
            return
        name = str(raw.get('name') or raw.get('label') or raw.get('key') or raw.get('id') or '').strip()
        value = next((raw[k] for k in ('value_name', 'value', 'text') if raw.get(k) is not None), None)
        if not name or (value is None and not any(raw.get(k) is not None for k in ('value_id', 'values', 'value_struct'))):
            return
        row = dict(raw, name=name)
        if value is not None:
            row['value_name'] = str(value.get('text') or value.get('name') or '') if isinstance(value, dict) else str(value)
        if not row.get('id'):
            attribute_id = canonical_attribute_id({'name': name})
            if attribute_id:
                row['id'] = attribute_id
        old = next((a for a in attributes if (row.get('id') and a.get('id') == row['id']) or a['name'].casefold() == name.casefold()), None)
        if old is None:
            attributes.append(row)
        elif not any(old.get(k) for k in ('value_name', 'value_id', 'values', 'value_struct')):
            old.update(row)

    def add_variants(value):
        nonlocal variant_overflow
        if isinstance(value, dict):
            value = [dict(v, id=v.get('id', key)) for key, v in value.items() if isinstance(v, dict)]
        if isinstance(value, list) and len(value) > 500:
            variant_overflow = True
            return
        if not isinstance(value, list) or not value:
            return
        rows = [v for v in value if isinstance(v, dict) and not is_specification_section(v)]
        useful = [v for v in rows if any(v.get(k) for k in (
            'attribute_combinations', 'attributeCombinations', 'combinations', 'variation_attributes',
            'specifications', 'attributes', 'sku', 'sku_id', 'skuId', 'seller_sku', 'sellerSku', 'skuCode', 'color', 'size'
        )) or (any(v.get(k) for k in ('label', 'name', 'sku_name', 'skuName')) and any(k in v for k in ('price', 'available_quantity', 'stock')))]
        if useful:
            rich = sum(any(v.get(k) for k in ('attribute_combinations', 'attributeCombinations', 'combinations', 'variation_attributes', 'specifications', 'attributes')) for v in useful)
            candidates.append((rich * 1000 + len(useful) * 10 + min(len(rows), 9), rows))

    visited = 0
    minimum_variants = 0

    def walk(value, depth=0, variant=False):
        nonlocal visited, minimum_variants, variant_overflow
        visited += 1
        if depth > 22 or visited > 120000:
            return
        if isinstance(value, list):
            for child in value[:1000]:
                walk(child, depth + 1, variant)
        elif isinstance(value, dict):
            types = value.get('@type') or []
            if not variant and ('Product' in types or 'ProductGroup' in types):
                brand = value.get('brand')
                if brand:
                    add({'id': 'BRAND', 'name': 'Marca', 'value': brand.get('name') if isinstance(brand, dict) else brand})
            for key, child in value.items():
                if _SKIP.search(key):
                    continue
                if key in ('variation_ids', 'variationIds') and isinstance(child, list) and len(child) > 500:
                    variant_overflow = True
                if key == 'pictures' and isinstance(child, list):
                    for picture in child:
                        if isinstance(picture, dict) and picture.get('id') is not None:
                            url = picture.get('secure_url') or picture.get('url') or picture.get('source')
                            if isinstance(url, str) and url.startswith(('https://', 'http://')):
                                picture_urls[str(picture['id'])] = url
                if key == "pickers" and isinstance(child, list):
                    minimum_variants = max([minimum_variants, *[
                        len(picker[k]) for picker in child if isinstance(picker, dict)
                        for k in ('products', 'options', 'values') if isinstance(picker.get(k), list)
                    ]])
                    # A picker may contain the actual SKU rows rather than a
                    # separate variations array. Only use explicit combinations,
                    # or a single dimension with explicit variation IDs. Product
                    # IDs / value IDs can point at other listings, not SKUs.
                    dimensions = [p for p in child if isinstance(p, dict) and
                                  any(isinstance(p.get(k), list) and p[k]
                                      for k in ('products', 'options', 'values'))]
                    for picker in dimensions:
                        for container in ('products', 'options', 'values'):
                            options = picker.get(container)
                            if not isinstance(options, list):
                                continue
                            rows = []
                            for option in options:
                                if not isinstance(option, dict):
                                    continue
                                row = normalize_variation(option)
                                if not row.get('attribute_combinations') and len(dimensions) == 1:
                                    identity = option.get('variation_id', option.get('variationId'))
                                    label = _picker_text(option.get('value_name') or option.get('picker_label') or option.get('label') or option.get('name'))
                                    dimension = picker.get('attribute_id') or picker.get('picker_id') or picker.get('id')
                                    if identity is not None and dimension and isinstance(label, str) and label.strip():
                                        row['id'] = identity
                                        row['attribute_combinations'] = [{'id': dimension, 'value_name': label}]
                                if row.get('attribute_combinations'):
                                    rows.append(row)
                            if len(rows) == len(options):
                                add_variants(rows)
                    add_variants(_picker_variants(child, page.get('final_url') or ''))
                is_variant = bool(_VARIANTS.match(key))
                if is_variant:
                    add_variants(child)
                nested_variant = variant or is_variant or bool(re.search(r'picker|variation|variant|sku', key, re.I))
                if not nested_variant and key in ('attributes', 'additionalProperty', 'specs') and isinstance(child, list):
                    for raw in child:
                        add(raw)
                if isinstance(child, (dict, list)):
                    walk(child, depth + 1, nested_variant)

    for script in page.get('structured_scripts') or []:
        walk(_parse_script(script))
    for raw in page.get('attributes') or []:
        add(raw)
    for raw in page.get('specs') or []:
        add(raw)
    add_variants(page.get('variations'))
    variants = [normalize_variation(v) for v in max(candidates, key=lambda v: v[0])[1]] if candidates else []
    for row in variants:
        resolved = [picture_urls[str(pid)] for pid in row.get('picture_ids') or [] if str(pid) in picture_urls]
        if resolved:
            row['images'] = (row.get('images') if isinstance(row.get('images'), list) else []) + resolved
            row['pictures'] = [{'id': str(pid), 'source': picture_urls[str(pid)]}
                               for pid in row.get('picture_ids') or [] if str(pid) in picture_urls]
    result = {'attributes': attributes, 'variations': variants}
    if variant_overflow or (minimum_variants > 1 and len(variants) < minimum_variants) or any(not v.get('attribute_combinations') for v in variants):
        result['variation_collection_status'] = 'incomplete'
    return result


def is_specification_section(row):
    """Exact signature of native table sections misread by the old collector."""
    return (isinstance(row, dict) and row.get('type') == 'TITLE_STRIPED'
            and bool(row.get('title')) and isinstance(row.get('attributes'), list)
            and not any(key in row for key in ('id', 'sku', 'price', 'available_quantity', 'attribute_combinations')))


def repair_source_facts(source):
    """Remove proven table sections; do not guess the missing real variants."""
    source = deepcopy(source)
    variants = source.get('variations') or []
    sections = [v for v in variants if is_specification_section(v)]
    if not sections:
        return source
    facts = page_facts({'attributes': source.get('attributes') or [], 'specs': [
        a for section in sections for a in section['attributes']
    ]})
    source['attributes'] = facts['attributes']
    source['variations'] = [v for v in variants if not is_specification_section(v)]
    source['variation_collection_status'] = 'needs_recollection'
    return source
