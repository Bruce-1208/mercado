"""Recover native attributes/variants from detail-page hydration data.

Never execute page scripts or construct a Cartesian product of picker options:
only variant rows actually supplied by the product page are retained.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy

from erp.mercadolibre_attribute_rules import canonical_attribute_id

_SCRIPT_MARKER = re.compile(
    r"(?:__PRELOADED_STATE__|__INITIAL_STATE__|__NEXT_DATA__|__APOLLO_STATE__|preloadedState|_n\.ctx\.r)\s*[:=]\s*", re.I
)
_VARIANTS = re.compile(r"^(?:variations?|variants?|hasVariant|skus?|sku_list|skuList|item_skus|itemSkus|sku_map|skuMap|sku_info_map|skuInfoMap)$", re.I)
_SKIP = re.compile(r"recommend|carousel|related|tracking|analytics", re.I)


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
        if isinstance(value, dict):
            value = list(value.values())
        if not isinstance(value, list) or not value or len(value) > 500:
            return
        rows = [v for v in value if isinstance(v, dict)]
        useful = [v for v in rows if any(v.get(k) for k in (
            'attribute_combinations', 'attributeCombinations', 'combinations', 'variation_attributes',
            'specifications', 'attributes', 'sku', 'sku_id', 'skuId', 'seller_sku', 'sellerSku', 'skuCode'
        )) or (any(v.get(k) for k in ('label', 'name', 'sku_name', 'skuName')) and any(k in v for k in ('price', 'available_quantity', 'stock')))]
        if useful:
            rich = sum(any(v.get(k) for k in ('attribute_combinations', 'attributeCombinations', 'combinations', 'variation_attributes', 'specifications', 'attributes')) for v in useful)
            candidates.append((rich * 1000 + len(useful) * 10 + min(len(rows), 9), rows))

    visited = 0
    has_variant_options = False

    def walk(value, depth=0, variant=False):
        nonlocal visited, has_variant_options
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
                if key == "pickers" and isinstance(child, list):
                    has_variant_options |= any(isinstance(picker, dict) and any(isinstance(picker.get(k), list) and len(picker[k]) > 1 for k in ("products", "options", "values")) for picker in child)
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
    variants = deepcopy(max(candidates, key=lambda v: v[0])[1]) if candidates else []
    for row in variants:
        combinations = next((row[k] for k in ('attribute_combinations', 'attributeCombinations', 'combinations', 'variation_attributes', 'specifications') if isinstance(row.get(k), list) and row[k]), None)
        if combinations:
            row['attribute_combinations'] = combinations
        if 'id' not in row:
            for key in ('variation_id', 'variationId'):
                if key in row:
                    row['id'] = row[key]
                    break
        if 'available_quantity' not in row:
            row['available_quantity'] = next((row[k] for k in ('availableQuantity', 'stock', 'quantity', 'available') if k in row), None)
        if 'price' not in row and 'price_amount' in row:
            row['price'] = row['price_amount']
        if 'picture_ids' not in row and isinstance(row.get('pictureIds'), list):
            row['picture_ids'] = row['pictureIds']
    result = {'attributes': attributes, 'variations': variants}
    if has_variant_options and not variants:
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
