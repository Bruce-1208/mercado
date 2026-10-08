"""Add a covering index for substring searches without rebuilding the data table.

Run as python -m scripts.optimize_product_search_index [--apply].
The complete columns preserve existing LIKE/collation semantics, including
short words and Chinese. No FULLTEXT token-length restrictions are introduced.
"""
import argparse
import json
import time

TABLE = 'erp_mercadolibre_products'
INDEX = 'idx_product_search_cover'
COLUMNS = ['source_item_id', 'title', 'organization_key']


def inspect(cursor, name=INDEX, columns=COLUMNS):
    cursor.execute(f'SHOW INDEX FROM `{TABLE}`')
    indexes = {}
    for row in cursor.fetchall():
        indexes.setdefault(row['Key_name'], []).append(row)
    for existing_name, rows in indexes.items():
        rows.sort(key=lambda row: row['Seq_in_index'])
        if ([row['Column_name'] for row in rows[:len(columns)]] == columns
                and all(row.get('Sub_part') is None
                        and row.get('Visible', 'YES') == 'YES'
                        and row.get('Index_type') == 'BTREE' for row in rows)):
            return existing_name
    if name in indexes:
        raise ValueError('Existing search index has an incompatible definition')
    return None


def optimize(connection, *, apply=False):
    with connection.cursor() as cursor:
        cursor.execute('SET SESSION lock_wait_timeout=5')
        reports = []
        specs = [(INDEX, COLUMNS), ('idx_product_scope_search',
                 ['organization_key', 'source_item_id', 'title', 'visibility', 'owner_user_id', 'owner_name'])]
        for name, columns in specs:
            before = inspect(cursor, name, columns)
            start = time.monotonic()
            if apply and not before:
                definition = ', '.join(f'`{column}`' for column in columns)
                cursor.execute(f'ALTER TABLE `{TABLE}` ADD INDEX `{name}` ({definition}), '
                               'ALGORITHM=INPLACE, LOCK=NONE')
            reports.append({'index': name, 'before': before, 'after': inspect(cursor, name, columns),
                            'elapsed_seconds': round(time.monotonic() - start, 3)})
        return reports



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from bit.db_pool import default_connection_config
    import pymysql
    config = default_connection_config()
    config.update(read_timeout=1800, autocommit=True)
    connection = pymysql.connect(**config)
    try:
        print(json.dumps(optimize(connection, apply=args.apply)))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
