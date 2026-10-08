"""Index-only reference pagination before loading live infringement snapshots."""
SOURCES = {
    'product_list': ('erp_mercadolibre_products', '1=1', 'idx_erp_meli_product_added'),
    'collection_list': ('erp_mercadolibre_collection_items', '`added_to_products` = 0', None),
    'pulled': ('erp_mercadolibre_store_links', '`is_current` = 1', None),
    'zying': ('zying_product', '1=1', None),
}


def references(cursor, sources, page, page_size, direction):
    """Bound each source before UNION; never read snapshots for skipped rows."""
    direction = 'ASC' if direction == 'asc' else 'DESC'
    offset = (page - 1) * page_size
    total = 0
    branches = []
    for source in sorted(set(sources)):
        table, predicate, count_index = SOURCES[source]
        hint = f'FORCE INDEX (`{count_index}`)' if count_index else ''
        cursor.execute(f'SELECT COUNT(*) AS total FROM `{table}` {hint} WHERE {predicate}')
        total += int((cursor.fetchone() or {}).get('total') or 0)
        branches.append(
            f"SELECT '{source}' AS source_type, id AS row_id FROM "
            f"(SELECT id FROM `{table}` WHERE {predicate} ORDER BY id {direction} "
            f"LIMIT {offset + page_size}) AS source_{source}"
        )
    if not branches:
        return [], 0
    cursor.execute('SELECT source_type, row_id FROM (' + ' UNION ALL '.join(branches)
                   + f') AS candidates ORDER BY row_id {direction}, source_type ASC LIMIT %s OFFSET %s',
                   (page_size, offset))
    return cursor.fetchall(), total
