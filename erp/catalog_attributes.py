"""Shared catalog attribute inspection and explicit user-requested completion."""
from copy import deepcopy
import re

from erp.mercadolibre_collection_quality import parsed
from erp.mercadolibre_collection_attributes import check_required_attributes, collection_category_id, _attributes, _present
from erp.mercadolibre_attribute_rules import is_read_only_attribute, resolve_schema_attribute_id

KINDS = {'collection', 'products', 'supplier', 'original'}


def facts(row, kind):
    if kind not in KINDS:
        raise ValueError('不支持的产品列表')
    snapshot = parsed(row.get('source_snapshot_json'))
    if kind == 'collection':
        source = parsed(row.get('source_json'))
    elif kind == 'supplier':
        source = deepcopy(snapshot.get('original_1688') or row.get('original_1688') or {})
    elif kind == 'original' or row.get('source_type') == 'ai_original':
        source = deepcopy(snapshot.get('ai_original') or row.get('ai_original') or snapshot.get('source') or {})
    else:
        source = deepcopy(snapshot.get('source') or {})
    category = str(source.get('category_id') or row.get('category_id') or '').strip().upper()
    if kind == 'collection':
        category = collection_category_id(row)
    row_category = str(row.get('category_id') or '').strip().upper()
    if not re.fullmatch(r'(?:ML[A-Z]|MCO|CBT)\d+', category) and re.fullmatch(r'(?:ML[A-Z]|MCO|CBT)\d+', row_category):
        category = row_category
    return {**row, 'category_id': category, 'source_json': source, 'source': source}


def fields_for(source, schema):
    definitions = {str(d['id']): deepcopy(d) for d in schema if not is_read_only_attribute(d)}
    # Existing empty attributes also need attention, even when optional.
    containers = [source] + [v for v in source.get('variations') or [] if isinstance(v, dict)]
    for container in containers:
        for attribute in _attributes(container.get('attributes')) + _attributes(container.get('attribute_combinations')):
            key = resolve_schema_attribute_id(attribute, schema) or str(attribute.get('id') or '')
            if key and key not in definitions and not any(d.get('id') == key for d in schema):
                definitions[key] = {'id': key, 'name': attribute.get('name') or key}
    return definitions


def inspect(row, kind, schema):
    normalized = facts(row, kind)
    report = check_required_attributes(normalized, schema)
    report['kind'] = kind
    report['can_operate'] = row.get('can_operate', True)
    if report['status'] == 'unavailable':
        report['fields'] = []
        return report
    source = normalized['source_json']
    definitions = fields_for(source, schema)
    missing = report['missing']
    for entry in missing:
        entry['sku_index'] = int(entry['scope'].split()[-1]) - 1 if entry['scope'].startswith('规格 ') else None
    existing = {(entry['sku_index'], entry['attribute_id']) for entry in missing}
    for index, container in [(None, source)] + list(enumerate(source.get('variations') or [])):
        if not isinstance(container, dict):
            continue
        for attribute in _attributes(container.get('attributes')) + _attributes(container.get('attribute_combinations')):
            key = resolve_schema_attribute_id(attribute, schema) or str(attribute.get('id') or '')
            if key not in definitions or _present(attribute) or (index, key) in existing:
                continue
            existing.add((index, key))
            missing.append({'attribute_id': key, 'name': definitions[key].get('name_zh') or definitions[key].get('name') or key,
                            'scope': '商品' if index is None else f'规格 {index + 1}', 'sku_index': index,
                            'sku': str(container.get('seller_sku') or '') if index is not None else ''})
    fields = []
    for key, definition in definitions.items():
        choices = [{'id': str(v.get('id') or ''), 'name': str(v.get('name') or v.get('value_name') or v.get('id') or '')}
                   for v in definition.get('values') or [] if isinstance(v, dict)]
        fields.append({'attribute_id': key, 'name': definition.get('name_zh') or definition.get('name') or key,
                       'choices': choices, 'is_choice': bool(choices) or definition.get('value_type') in {'list', 'boolean'},
                       'value_type': definition.get('value_type') or 'string'})
    field_map = {f['attribute_id']: f for f in fields}
    for entry in missing:
        entry['is_choice'] = field_map.get(entry['attribute_id'], {}).get('is_choice', False)
    report.update(fields=fields, missing_count=len(missing))
    if missing:
        report.update(status='missing', message=f'缺少 {len(missing)} 项属性')
    elif report['status'] == 'complete':
        report['message'] = '必填及已有属性齐全'
    return report


def apply(row, kind, schema, operation):
    """Re-evaluate current locked facts. Never replace another product's attributes."""
    report = inspect(row, kind, schema)
    source = deepcopy(facts(row, kind)['source_json'])
    if report['status'] == 'unavailable':
        return source, 0, report
    mode = operation.get('mode')
    if mode not in {'generic', 'set'}:
        raise ValueError('不支持的属性操作')
    field_map = {f['attribute_id']: f for f in report['fields']}
    writes = []
    if mode == 'generic':
        writes = [(e['sku_index'], e['attribute_id'], {'value_name': 'generic'}) for e in report['missing'] if not e['is_choice']]
    else:
        if operation.get('category_id') != report['category_id']:
            return source, 0, report
        key = str(operation.get('attribute_id') or '')
        field = field_map.get(key)
        if not field:
            raise ValueError('所选类目不支持该属性，请重新检查')
        raw = str(operation.get('value') if operation.get('value') is not None else '').strip()
        if not raw or len(raw) > 255:
            raise ValueError('属性值不能为空且不能超过 255 个字符')
        if field['is_choice']:
            choice = next((c for c in field['choices'] if (c['id'] or c['name']) == raw), None)
            if not choice:
                raise ValueError('属性值必须是当前类目的有效选项')
            value = {'value_name': choice['name'], **({'value_id': choice['id']} if choice['id'] else {})}
        else:
            value = {'value_name': raw}
        if operation.get('only_missing', True):
            writes = [(e['sku_index'], key, value) for e in report['missing'] if e['attribute_id'] == key]
            def has_value(container):
                return any((resolve_schema_attribute_id(a, schema) or a.get('id')) == key and _present(a)
                           for a in _attributes(container.get('attributes')) + _attributes(container.get('attribute_combinations')))
            variants = source.get('variations') or []
            if not has_value(source) and (not variants or not all(has_value(v) for v in variants)) and not writes:
                writes = [(None, key, value)]
        else:
            writes = [(None, key, value)]
            for index, variant in enumerate(source.get('variations') or []):
                if any((resolve_schema_attribute_id(a, schema) or a.get('id')) == key for a in
                       _attributes(variant.get('attributes')) + _attributes(variant.get('attribute_combinations'))):
                    writes.append((index, key, value))
    count = 0
    # Shared omissions go on the product; explicit blank SKU overrides are filled in place.
    for index, key, value in writes:
        container = source if index is None else source['variations'][index]
        targets = []
        for name in ('attributes', 'attribute_combinations'):
            attrs = _attributes(container.get(name))
            if name in container or name == 'attributes':
                container[name] = attrs
            targets.extend(a for a in attrs if (resolve_schema_attribute_id(a, schema) or a.get('id')) == key)
        if not targets and index is not None:
            container = source
            container['attributes'] = _attributes(container.get('attributes'))
            targets = [a for a in container['attributes'] if (resolve_schema_attribute_id(a, schema) or a.get('id')) == key]
        if not targets:
            target = {'id': key, 'name': field_map[key]['name']}
            container.setdefault('attributes', []).append(target)
            targets = [target]
        for target in targets:
            if mode == 'generic' or operation.get('only_missing', True):
                if _present(target):
                    continue
            updated = {k: v for k, v in target.items() if k not in {'value', 'value_name', 'value_id', 'value_struct', 'values', 'value_name_es', 'value_name_pt'}}
            updated.update(id=key, **value)
            if updated != target:
                target.clear(); target.update(updated); count += 1
    # Inspection of the modified source uses the same normalized checker for every list.
    after = inspect({**facts(row, kind), 'source_json': source}, 'collection', schema)
    after.update(kind=kind, id=row.get('id'))
    return source, count, after
