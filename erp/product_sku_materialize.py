"""Persist synced siblings as the SKU matrix of one ordinary product.

Raw sync records remain available for provenance; membership hides only the
children from catalog lists. Parent snapshots and edited matrices survive sync.
"""
import copy
import json
from collections import defaultdict
from erp.product_sku_groups import TABLE as GROUPS, PRODUCTS, VARIANT_ATTRIBUTES, _facts, public_titles

TABLE = 'erp_mercadolibre_product_sku_members'


def snapshot(row):
    value = row.get('source_snapshot_json') or {}
    return json.loads(value) if isinstance(value, str) else copy.deepcopy(value)


def ensure_table(cursor):
    cursor.execute(f'''CREATE TABLE IF NOT EXISTS `{TABLE}` (
        product_id BIGINT NOT NULL PRIMARY KEY,
        parent_id BIGINT NOT NULL,
        original_snapshot_json LONGTEXT NULL,
        KEY idx_sku_parent (parent_id, product_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')


def variants(row):
    source, _, attrs, sku = _facts(row)
    native = source.get('variations') or [{}]
    result = []
    for index, raw in enumerate(native):
        variant = copy.deepcopy(raw)
        variant['source_product_item_id'] = row['id']
        variant['source_item_id'] = row.get('source_item_id')
        # Remote variation IDs belong to their original listings, not the parent.
        if 'id' in variant:
            variant['source_variation_id'] = variant.pop('id')
        variant.setdefault('sku_id', f"product-{row['id']}-{index}")
        variant.setdefault('seller_sku', sku)
        variant.setdefault('attribute_combinations', [copy.deepcopy(a) for a in source.get('attributes') or []
            if a.get('id') in VARIANT_ATTRIBUTES and a.get('value_name')])
        for field in ('price', 'net_proceeds_usd', 'weight_g', 'package_length_cm', 'package_width_cm', 'package_height_cm'):
            if variant.get(field) is None and row.get(field) is not None:
                variant[field] = row[field]
        variant.setdefault('available_quantity', source.get('available_quantity', 0))
        pictures = source.get('pictures') or []
        ids = set(map(str, variant.get('picture_ids') or []))
        urls = [p.get('secure_url') or p.get('url') or p.get('source') for p in pictures
                if isinstance(p, dict) and (not ids or str(p.get('id')) in ids)]
        variant.setdefault('images', [u for u in urls if u] or [row.get('main_image_url')])
        variant['images'] = [u for u in variant['images'] if u]
        variant['sku_images_customized'] = True
        result.append(variant)
    return result


def merge(parent, rows, group_key):
    """Idempotent merge: preserve edits/deletions; append newly discovered SKUs."""
    saved = snapshot(parent)
    family = saved.get('sku_family') or {}
    known = set(family.get('member_ids') or [])
    fresh = [r for r in rows if r['id'] not in known]
    if not fresh:
        return None
    source = saved.setdefault('source', {})
    matrix = copy.deepcopy(source.get('variations') or []) if family else []
    for row in fresh:
        matrix.extend(variants(row))
    source['variations'] = matrix
    source['available_quantity'] = sum(int(v.get('available_quantity') or 0) for v in matrix)
    if not family:
        source['attributes'] = [a for a in source.get('attributes') or []
                                if a.get('id') not in VARIANT_ATTRIBUTES | {'SELLER_SKU'}]
        source.pop('seller_custom_field', None)
        titled = public_titles([dict(r, sku_group_key=group_key) for r in rows])
        title = titled[0]['display_title']
        source['title'] = title
    else:
        title = parent['title']
    saved['sku_family'] = {'group_key': group_key, 'member_ids': sorted(known | {r['id'] for r in rows})}
    return title, saved


def materialize(cursor, keys):
    changed = 0
    for key in sorted(set(keys)):
        cursor.execute(f'''SELECT p.* FROM `{GROUPS}` g
            JOIN `{PRODUCTS}` p ON p.id=g.product_id
            WHERE g.group_key=%s ORDER BY p.id FOR UPDATE''', (key,))
        group_rows = cursor.fetchall()
        if not group_rows:
            continue
        cursor.execute(f"SELECT product_id,parent_id FROM `{TABLE}` WHERE product_id IN ({','.join(['%s'] * len(group_rows))})",
                       tuple(r['id'] for r in group_rows))
        parents_by_id = {r['product_id']: r['parent_id'] for r in cursor.fetchall()}
        # Deleted catalog parents stay hidden until an explicit store sync
        # reintroduces the family. Do not let stale membership block that sync.
        referenced = sorted(set(parents_by_id.values()))
        if referenced:
            cursor.execute(f"SELECT id FROM `{PRODUCTS}` WHERE id IN ({','.join(['%s'] * len(referenced))})",
                           tuple(referenced))
            live_parents = {r['id'] for r in cursor.fetchall()}
            parents_by_id = {item: parent for item, parent in parents_by_id.items() if parent in live_parents}
        partitions = defaultdict(list)
        for row in group_rows:
            row['parent_id'] = parents_by_id.get(row['id'])
            # Never promote another owner's record into an editable SKU.
            scope = tuple(row.get(k) for k in ('organization_key', 'owner_user_id', 'owner_name', 'visibility', 'currency_id'))
            partitions[scope].append(row)
        for rows in partitions.values():
            if len(rows) < 2:
                continue
            parents = [r for r in rows if snapshot(r).get('sku_family')]
            if len(parents) > 1:
                continue  # Separate already edited products must not be merged implicitly.
            parent = parents[0] if parents else rows[-1]
            if any(r.get('parent_id') not in (None, parent['id']) for r in rows):
                continue
            result = merge(parent, rows, key)
            if result is None:
                # Repair a missing membership row without touching edited SKUs.
                missing = [r for r in rows if parents_by_id.get(r['id']) != parent['id']]
                if missing:
                    cursor.executemany(f'''INSERT INTO `{TABLE}` (product_id,parent_id,original_snapshot_json)
                        VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE parent_id=VALUES(parent_id)''',
                        [(r['id'], parent['id'], None) for r in missing])
                continue
            title, saved = result
            backup = json.dumps(snapshot(parent), ensure_ascii=False, default=str)
            cursor.executemany(f'''INSERT INTO `{TABLE}` (product_id,parent_id,original_snapshot_json)
                VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE parent_id=VALUES(parent_id)''',
                [(r['id'], parent['id'], backup if r['id'] == parent['id'] else None) for r in rows])
            cursor.execute(f'UPDATE `{PRODUCTS}` SET title=%s, source_snapshot_json=%s WHERE id=%s',
                           (title, json.dumps(saved, ensure_ascii=False, default=str), parent['id']))
            changed += 1
    return changed
