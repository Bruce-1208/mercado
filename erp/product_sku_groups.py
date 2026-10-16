"""Identify synced SKU families and normalize their shared product titles."""
import hashlib
import json
import re
from functools import lru_cache

TABLE = 'erp_mercadolibre_product_sku_groups'
PRODUCTS = 'erp_mercadolibre_products'
VARIANT_ATTRIBUTES = {
    'COLOR', 'SIZE', 'MAIN_COLOR', 'SECONDARY_COLOR', 'SHOE_SIZE', 'VOLTAGE',
    'CAP_SIZE', 'LENS_STRENGTH', 'DIOPTER', 'OPTICAL_POWER', 'LENGTH', 'VOLUME',
    'DESIGN', 'PATTERN', 'STYLE', 'SIZE_GRID_ROW_ID',
}


def normalized(value):
    return re.sub(r'\s+', ' ', str(value or '').strip()).casefold()


def _facts(row):
    snapshot = row.get('source_snapshot_json') or row.get('source_json') or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (ValueError, TypeError):
            snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    source = snapshot.get('source') or snapshot
    plugin = snapshot.get('plugin_snapshot') or {}
    if not isinstance(source, dict):
        source = {}
    if not isinstance(plugin, dict):
        plugin = {}
    attrs = {str(a.get('id')): str(a.get('value_name') or '').strip()
             for a in source.get('attributes') or [] if isinstance(a, dict)}
    sku = str(attrs.get('SELLER_SKU') or source.get('seller_custom_field') or '').strip()
    return source, plugin, attrs, sku


@lru_cache(maxsize=8192)
def _suffix_pattern(value):
    # Sync sometimes compacts spaces in SKU values and drops Chinese characters
    # from the marketplace title; match those transformations, never title prefixes.
    return re.compile(r'(?:\s+|[-_]+)' + r'\s*'.join(re.escape(c) for c in value if not c.isspace()) + r'$', re.I)


def display_fields(row, facts=None):
    source, plugin, attrs, sku = facts or _facts(row)
    base_title = str(row.get('title') or source.get('title') or '').strip()
    family_name = str(source.get('family_name') or '').strip()
    variants = [v for k, v in attrs.items() if k in VARIANT_ATTRIBUTES and v]
    candidates = set(variants)
    for variant in source.get('variations') or []:
        if isinstance(variant, dict):
            for attribute in variant.get('attribute_combinations') or []:
                if isinstance(attribute, dict) and attribute.get('value_name'):
                    candidates.add(str(attribute['value_name']).strip())
    # This catalog uses the supplier product number followed by SKU values.
    supplier = re.fullmatch(r'([0-9]{8,20})[-_](.+)', sku)
    if supplier:
        candidates.update(part for part in re.split(r'[-_]', supplier[2]) if part)
    for value in list(candidates):
        ascii_value = re.sub(r'[^\x00-\x7f]', '', value).strip()
        if ascii_value and ascii_value != value:
            candidates.add(ascii_value)
    candidates = sorted(candidates, key=len, reverse=True)
    removed = []
    while True:
        match = next(((value, _suffix_pattern(value).search(base_title)) for value in candidates
                      if value and _suffix_pattern(value).search(base_title)), None)
        if not match or len(base_title[:match[1].start()].strip()) < 12:
            break
        value, suffix = match
        base_title = base_title[:suffix.start()].rstrip()
        removed.append(value)
        candidates.remove(value)
    label = ' / '.join(variants) or (' / '.join(re.split(r'[-_]', supplier[2])) if supplier else '')
    return family_name or base_title, label, removed


def identity(row):
    """Prefer explicit families, then the catalog's supplier-number SKU format."""
    if row.get('source_type') != 'pulled':
        return None
    saved = row.get('source_snapshot_json') or {}
    if isinstance(saved, str):
        try:
            saved = json.loads(saved)
        except ValueError:
            saved = {}
    family = saved.get('sku_family') or {}
    if family.get('group_key'):
        return family['group_key'], row.get('title') or '', ''
    source, plugin, attrs, sku = _facts(row)
    token = plugin.get('token_id') or source.get('seller_id')
    site = plugin.get('site_id') or source.get('site_id')
    if not token or not site:
        return None
    base_title, label, removed = display_fields(row, (source, plugin, attrs, sku))
    family = source.get('family_id') or source.get('siteless_family_id')
    supplier = re.fullmatch(r'([0-9]{8,20})[-_](.+)', sku)
    if family:
        key = ['family', str(family)]
    elif supplier:
        key = ['supplier', supplier[1], normalized(attrs.get('BRAND')), normalized(attrs.get('MODEL'))]
    else:
        base_sku = sku
        for value in removed:
            base_sku, count = _suffix_pattern(value).subn('', base_sku)
            if not count:
                return None
        if not removed or len(base_title) < 12 or len(base_sku) < 4 or base_sku == sku:
            return None
        key = ['sku', normalized(base_sku), normalized(base_title), normalized(attrs.get('BRAND')), normalized(attrs.get('MODEL'))]
    scope = [str(row.get('organization_key') or ''), str(token), str(site), str(row.get('category_id') or source.get('category_id') or '')]
    digest = hashlib.sha256(json.dumps(scope + key, ensure_ascii=False).encode()).hexdigest()
    return digest, base_title[:255], label[:255]


def public_titles(rows):
    """One variant-free display title per visible family; source titles stay intact."""
    grouped = {}
    for row in rows:
        title, label, _ = display_fields(row)
        row['display_title'] = title
        if label:
            row['sku_label'] = label
        grouped.setdefault(row.get('sku_group_key') or f"item:{row['id']}", []).append(row)
    for members in grouped.values():
        title = members[0]['display_title']
        if len(members) > 1:
            # Confirm the shared words across variants, retaining product descriptors.
            words = title.split()
            for member in members[1:]:
                other = member['display_title'].split()
                common = 0
                for left, right in zip(words, other):
                    if left.casefold() != right.casefold():
                        break
                    common += 1
                words = words[:common]
            common_title = ' '.join(words).rstrip(' ,-/')
            if len(common_title) >= 12:
                title = common_title
        for member in members:
            member['display_title'] = title
            member['sku_group_title'] = title
    return rows


def ensure_table(cursor):
    cursor.execute(f'''CREATE TABLE IF NOT EXISTS `{TABLE}` (
        product_id BIGINT NOT NULL PRIMARY KEY,
        group_key CHAR(64) NOT NULL,
        group_title VARCHAR(255) NOT NULL,
        sku_label VARCHAR(255) NOT NULL,
        KEY idx_sku_group (group_key, product_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')


def update_rows(cursor, rows):
    values, singles = [], []
    for row in rows:
        group = identity(row)
        if group:
            values.append((row['id'], *group))
        else:
            singles.append(row['id'])
    if values:
        cursor.executemany(f'''INSERT INTO `{TABLE}` (product_id,group_key,group_title,sku_label)
            VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE group_key=VALUES(group_key),
            group_title=VALUES(group_title),sku_label=VALUES(sku_label)''', values)
    if singles:
        cursor.execute(f"DELETE FROM `{TABLE}` WHERE product_id IN ({','.join(['%s'] * len(singles))})", tuple(singles))
    return len(values)


def grouped_page(cursor, where_sql, params, *, limit, offset, include_total, decode, sku_bounds=(None, None)):
    """Filter/authorize first, pick the newest matching member of each family."""
    base = f"`{PRODUCTS}` LEFT JOIN `{TABLE}` AS sku_groups ON sku_groups.product_id=`{PRODUCTS}`.id AND `{PRODUCTS}`.source_type='pulled'"
    family = f"COALESCE(sku_groups.group_key, CONCAT('item:', `{PRODUCTS}`.id))"
    if any(value is not None for value in sku_bounds):
        return _page_by_sku_count(cursor, base, family, where_sql, params,
            limit=limit, offset=offset, include_total=include_total, decode=decode, bounds=sku_bounds)
    total = None
    if include_total:
        try:
            index = 'FORCE INDEX (`idx_erp_meli_product_added`)' if not where_sql else ''
            cursor.execute(f"SELECT /*+ MAX_EXECUTION_TIME(5000) */ COUNT(*) AS total FROM `{PRODUCTS}` {index} {where_sql}", tuple(params))
            product_total = int((cursor.fetchone() or {}).get('total') or 0)
            cursor.execute(f"""SELECT /*+ MAX_EXECUTION_TIME(5000) */ COUNT(*) AS members,
                COUNT(DISTINCT sku_groups.group_key) AS families
                FROM `{TABLE}` AS sku_groups INNER JOIN `{PRODUCTS}`
                ON `{PRODUCTS}`.id=sku_groups.product_id AND `{PRODUCTS}`.source_type='pulled'
                {where_sql}""", tuple(params))
            counts = cursor.fetchone() or {}
            total = product_total - int(counts.get('members') or 0) + int(counts.get('families') or 0)
        except Exception as exc:
            if not exc.args or exc.args[0] != 3024:
                raise
            include_total = False
    # Index (group_key, product_id) bounds the sibling lookup. Unlike a global
    # GROUP BY/CTE this can stop as soon as one display page has been found.
    newer_where = where_sql.replace(f'`{PRODUCTS}`.', 'newer_products.')
    newer_filter = (' AND ' + newer_where[6:]) if newer_where else ''
    representative = f"""(sku_groups.product_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM `{TABLE}` AS newer_groups
        INNER JOIN `{PRODUCTS}` AS newer_products ON newer_products.id=newer_groups.product_id
        WHERE newer_groups.group_key=sku_groups.group_key
        AND newer_groups.product_id > `{PRODUCTS}`.id AND newer_products.source_type='pulled'
        {newer_filter}))"""
    page_where = where_sql + (' AND ' if where_sql else 'WHERE ') + representative
    cursor.execute(f"""SELECT /*+ MAX_EXECUTION_TIME(8000) */ `{PRODUCTS}`.id, {family} AS family_key
        FROM {base} {page_where} ORDER BY `{PRODUCTS}`.id DESC LIMIT %s OFFSET %s""",
        tuple(params + params + [limit + 1, offset]))
    page = cursor.fetchall()
    has_more = len(page) > limit
    page = page[:limit]
    if not page:
        return {'rows': [], 'total': total, 'group_count': 0, 'grouped_skus': True,
                'has_more': False, 'total_exact': include_total}
    keys = [row['family_key'] for row in page]
    identifiers = [row['id'] for row in page]
    placeholders = ','.join(['%s'] * len(keys))
    cursor.execute(f"SELECT product_id FROM `{TABLE}` WHERE group_key IN ({placeholders})", tuple(keys))
    member_ids = sorted(set(identifiers + [row['product_id'] for row in cursor.fetchall()]))
    member_filter = f"`{PRODUCTS}`.id IN ({','.join(['%s'] * len(member_ids))})"
    member_where = where_sql + (' AND ' if where_sql else 'WHERE ') + member_filter
    cursor.execute(f"""SELECT /*+ MAX_EXECUTION_TIME(8000) */ filtered.*,
        COALESCE(sku_groups.group_key, CONCAT('item:', filtered.id)) AS sku_group_key,
        sku_groups.group_title AS sku_group_title, sku_groups.sku_label,
        category.name AS management_category_name,
        category.listing_ratio_percent AS management_category_listing_ratio_percent
        FROM (SELECT * FROM `{PRODUCTS}` {member_where}) AS filtered
        LEFT JOIN `{TABLE}` AS sku_groups ON sku_groups.product_id=filtered.id AND filtered.source_type='pulled'
        LEFT JOIN erp_mercadolibre_management_categories AS category
        ON category.id=filtered.management_category_id""", tuple(params + member_ids))
    raw = cursor.fetchall()
    order = {key: index for index, key in enumerate(keys)}
    raw.sort(key=lambda row: (order[row['sku_group_key']], -row['id']))
    rows = []
    for row in raw:
        row['salesperson'] = row.get('owner_name') or '未分配'
        rows.append(decode(row))
    return {'rows': public_titles(rows), 'total': total, 'group_count': len(page), 'grouped_skus': True,
            'has_more': has_more, 'total_exact': include_total}


def paginate_rows(rows, *, limit, offset, sku_bounds=(None, None)):
    """Group after the risk service applies its in-memory filters."""
    groups = {}
    for row in rows:
        family = identity(row)
        key = family[0] if family else f"item:{row['id']}"
        row = dict(row, sku_group_key=key)
        if family:
            row.update(sku_group_title=family[1], sku_label=family[2])
        groups.setdefault(key, []).append(row)
    from erp.sku_matrix import sku_count
    grouped = list(groups.values())
    for operator, value in zip(('min', 'max'), sku_bounds):
        if value not in (None, ''):
            bound = int(value)
            if bound < 0 or str(value).strip() != str(bound):
                raise ValueError('SKU 个数必须是非负整数')
            def count(members):
                result = 0
                for row in members:
                    snapshot = row.get('source_snapshot_json') or {}
                    if isinstance(snapshot, str):
                        try:
                            snapshot = json.loads(snapshot)
                        except ValueError:
                            snapshot = {}
                    result += sku_count(snapshot)
                return result
            grouped = [members for members in grouped if (count(members) >= bound if operator == 'min' else count(members) <= bound)]
    page = grouped[offset:offset + limit if limit is not None else None]
    return {'rows': public_titles([r for g in page for r in g]), 'total': len(grouped), 'group_count': len(page), 'grouped_skus': True}


def refresh_products(cursor, ids):
    cursor.execute(f"SELECT id, source_type, organization_key, title, category_id, source_snapshot_json "
                   f"FROM `{PRODUCTS}` WHERE id IN ({','.join(['%s'] * len(ids))}) AND source_type='pulled'",
                   tuple(ids))
    update_rows(cursor, cursor.fetchall())


def _page_by_sku_count(cursor, base, family, where_sql, params, *, limit, offset, include_total, decode, bounds):
    from erp.sku_matrix import sku_count_sql
    conditions = ['SUM(member_skus) ' + operator + ' %s'
                  for operator, value in zip(('>=', '<='), bounds) if value is not None]
    cte = f"""WITH matching AS (
        SELECT `{PRODUCTS}`.id, {family} AS family_key,
        {sku_count_sql()} AS member_skus FROM {base} {where_sql}
    ), families AS (
        SELECT family_key, MAX(id) AS newest_id FROM matching GROUP BY family_key
        HAVING {' AND '.join(conditions)}
    )"""
    totals = '(SELECT COUNT(*) AS total FROM families)' if include_total else '(SELECT NULL AS total)'
    cursor.execute(cte + f""" SELECT /*+ MAX_EXECUTION_TIME(8000) */ `{PRODUCTS}`.*,
        page.family_key AS sku_group_key, sku_groups.group_title AS sku_group_title, sku_groups.sku_label,
        totals.total AS __group_total, category.name AS management_category_name,
        category.listing_ratio_percent AS management_category_listing_ratio_percent
        FROM {totals} AS totals
        LEFT JOIN (SELECT * FROM families ORDER BY newest_id DESC LIMIT %s OFFSET %s) AS page ON 1=1
        LEFT JOIN matching ON matching.family_key=page.family_key
        LEFT JOIN `{PRODUCTS}` ON `{PRODUCTS}`.id=matching.id
        LEFT JOIN `{TABLE}` AS sku_groups ON sku_groups.product_id=`{PRODUCTS}`.id
        LEFT JOIN erp_mercadolibre_management_categories AS category ON category.id=`{PRODUCTS}`.management_category_id
        ORDER BY page.newest_id DESC, `{PRODUCTS}`.id DESC""",
        tuple(params + [int(value) for value in bounds if value is not None] + [limit + 1, offset]))
    raw = cursor.fetchall()
    total = int((raw[0] if raw else {}).get('__group_total') or 0) if include_total else None
    keys = list(dict.fromkeys(row['sku_group_key'] for row in raw if row.get('id') is not None))
    keep = set(keys[:limit])
    rows = []
    for row in raw:
        if row.get('id') is not None and row['sku_group_key'] in keep:
            row.pop('__group_total', None)
            row['salesperson'] = row.get('owner_name') or '未分配'
            rows.append(decode(row))
    return {'rows': public_titles(rows), 'total': total, 'group_count': min(len(keys), limit), 'grouped_skus': True,
            'has_more': len(keys) > limit, 'total_exact': include_total}
