"""Persist virtual EAN-13 assignments for explicitly enabled ERP publications.

These are format-only internal identifiers, not GS1 registrations. Never enable
this fallback implicitly in other marketplaces or generic follow-sell callers.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from erp.mercadolibre_attribute_rules import extract_listing_attributes_from_detail


def ean13(body):
    if len(body) != 12 or not body.isascii() or not body.isdigit():
        raise ValueError('EAN-13 主体必须是 12 位数字')
    check = -sum(int(value) * (1 if index % 2 == 0 else 3)
                 for index, value in enumerate(body)) % 10
    return body + str(check)


def _candidate(identity, attempt):
    number = int(hashlib.sha256(f'{identity}:{attempt}'.encode()).hexdigest(), 16) % 10**10
    return ean13(f'20{number:010d}')


def allocate(identity):
    # Production clients and workers share one registry through the existing
    # database API. Explicit SQLite storage is only for offline tests.
    offline_path = os.getenv('MERCADO_VIRTUAL_EAN_DB')
    if not offline_path:
        from bit import bit_db_api
        if bit_db_api.DB_MODE == 'mysql':
            return allocate_shared(identity)
        return bit_db_api._request('POST', '/api/db/mercado-products/virtual-ean',
                                   json={'identity': identity})['ean']
    path = Path(offline_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique constraint plus a write transaction protects concurrent workers.
    db = sqlite3.connect(str(path), timeout=30)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS assignments '
                       '(identity TEXT PRIMARY KEY, ean TEXT NOT NULL UNIQUE)')
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT ean FROM assignments WHERE identity = ?', (identity,)).fetchone()
            if previous:
                return previous[0]
            for attempt in range(1000):
                code = _candidate(identity, attempt)
                if db.execute('SELECT 1 FROM assignments WHERE ean = ?', (code,)).fetchone():
                    continue
                db.execute('INSERT INTO assignments VALUES (?, ?)', (identity, code))
                return code
            raise ValueError('虚拟 EAN 编码分配失败，请稍后重试')
    finally:
        db.close()


def allocate_shared(identity, *, connection_factory=None):
    if not isinstance(identity, str) or not identity or len(identity) > 10000:
        raise ValueError('无效的虚拟 EAN 商品标识')
    from erp.mercadolibre_collection_store import _connect
    identity_hash = hashlib.sha256(identity.encode()).hexdigest()
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            cursor.execute('''CREATE TABLE IF NOT EXISTS erp_meli_virtual_eans (
                identity_hash CHAR(64) PRIMARY KEY, ean CHAR(13) NOT NULL UNIQUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4''')
            for attempt in range(1000):
                code = _candidate(identity, attempt)
                # Lock existing assignments exclusively too; concurrent retries
                # must not upgrade shared duplicate-key locks and deadlock.
                cursor.execute('INSERT INTO erp_meli_virtual_eans (identity_hash, ean) VALUES (%s, %s) '
                               'ON DUPLICATE KEY UPDATE identity_hash = identity_hash',
                               (identity_hash, code))
                cursor.execute('SELECT ean FROM erp_meli_virtual_eans WHERE identity_hash = %s FOR UPDATE',
                               (identity_hash,))
                row = cursor.fetchone()
                if row:
                    connection.commit()
                    return row['ean']
            raise ValueError('虚拟 EAN 编码分配失败，请稍后重试')
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def product_identity(row):
    key = row.get('id') or row.get('source_item_id') or row.get('source_url')
    if not key:
        raise ValueError('自动生成 EAN 需要稳定的产品 ID')
    return json.dumps([str(row.get('organization_key') or ''), str(key)], ensure_ascii=False)


def _filled(attribute):
    return any(str(attribute.get(key) or '').strip() not in {'', '-1'}
               for key in ('value_name', 'value_id')) or bool(attribute.get('values'))


def _identifiers(record):
    attributes = extract_listing_attributes_from_detail(record)
    attributes += [a for a in record.get('attribute_combinations') or [] if isinstance(a, dict)]
    return [a for a in attributes if str(a.get('id') or '').upper() in
            {'GTIN', 'EAN', 'UPC', 'EMPTY_GTIN_REASON'} and _filled(a)]


def ensure_virtual_eans(source, identity):
    """Copy the listing, preserving existing identifiers and explicit exemptions."""
    result = deepcopy(source)
    generated = []

    def fill(record, key, scope):
        for container in ('attributes', 'attribute_combinations'):
            for attribute in record.get(container) or []:
                if str(attribute.get('id') or '').upper() in {'EAN', 'UPC'}:
                    attribute['id'] = 'GTIN'
        existing = _identifiers(record)
        if existing:
            # Recover codes saved in the collector's gtin/ean/upc scalar fields.
            attrs = record['attributes'] = [a for a in record.get('attributes') or []
                                           if str(a.get('id') or '').upper() not in
                                           {'GTIN', 'EMPTY_GTIN_REASON'} or _filled(a)]
            if not any(str(a.get('id') or '').upper() in {'GTIN', 'EAN', 'UPC', 'EMPTY_GTIN_REASON'}
                       and _filled(a) for a in attrs):
                attrs.extend(deepcopy(existing))
            return
        code = allocate(json.dumps([identity, key], ensure_ascii=False, sort_keys=True))
        record['attributes'] = [a for a in record.get('attributes') or []
                                if str(a.get('id') or '').upper() not in
                                {'GTIN', 'EAN', 'UPC', 'EMPTY_GTIN_REASON'}]
        record['attributes'].append({'id': 'GTIN', 'value_name': code})
        generated.append({'scope': scope, 'value': code})

    variations = result.get('variations') or []
    if _identifiers(result) or not variations:
        fill(result, 'product', '商品')
        for index, variation in enumerate(variations, 1):
            if _identifiers(variation):
                fill(variation, '', f'SKU {index}')
    else:
        keys = set()
        for index, variation in enumerate(variations, 1):
            if _identifiers(variation):
                fill(variation, '', f'SKU {index}')
                continue
            stable_id = next((str(variation[k]) for k in
                              ('sku_id', 'id', 'seller_sku', 'seller_custom_field') if variation.get(k)), '')
            combinations = sorted((str(a.get('id') or a.get('name') or ''),
                                   str(a.get('value_id') or a.get('value_name') or a.get('value') or ''))
                                  for a in variation.get('attribute_combinations') or [])
            key = ['sku', stable_id] if stable_id else ['options', combinations]
            signature = json.dumps(key, ensure_ascii=False)
            if signature in keys or (not stable_id and not combinations):
                raise ValueError('SKU 缺少唯一标识，请补充 SKU 或规格后生成 EAN')
            keys.add(signature)
            fill(variation, key, f'SKU {index}')
    if generated:
        result['_virtual_eans'] = generated
    return result
