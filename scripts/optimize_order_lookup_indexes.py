"""Inspect missing order indexes; use --apply for bounded online-only DDL.

No business module invokes this maintenance script. Never falls back to COPY
or blocking DDL. Equivalent existing indexes are reused, regardless of name.
"""
import argparse
import json
import re

from bit.order_lookup_sql import PACK_EXPRESSION, context_lookup

TABLE = 'mercado_synced_orders'
SPECS = {
    'idx_orders_token_created': ('`token_id`, `date_created`', ['token_id', 'date_created']),
    'idx_orders_token_shipping': ('`token_id`, `shipping_id`', ['token_id', 'shipping_id']),
    'idx_orders_token_pack': ('`token_id`, `_pack_lookup_key`', ['token_id', '_pack_lookup_key']),
}


def normalized(value):
    value = str(value).lower().replace('character set', 'charset').replace('_utf8mb4', '').replace('\\', '')
    return re.sub(r'[\s`()]+', '', value)


def inspect(cursor):
    cursor.execute(f'SHOW INDEX FROM `{TABLE}`')
    indexes = {}
    names = set()
    for row in cursor.fetchall():
        names.add(row['Key_name'])
        if row.get('Visible', 'YES') != 'YES' or row.get('Sub_part') is not None:
            continue
        if row.get('Index_type') != 'BTREE':
            continue
        indexes.setdefault(row['Key_name'], {})[int(row['Seq_in_index'])] = row.get('Column_name') or row.get('Expression')
    result = {}
    for name, (_, columns) in SPECS.items():
        matches = [existing for existing, parts in indexes.items()
                   if [normalized(parts.get(i + 1)) for i in range(len(columns))]
                   == [normalized(c) for c in columns]]
        if name in names and name not in matches:
            raise ValueError(f'Index definition mismatch: {name}')
        result[name] = next(iter(matches), None)
    return result


def optimize(connection, *, apply=False):
    with connection.cursor() as cursor:
        cursor.execute('SELECT DATABASE() AS db, VERSION() AS version')
        server = cursor.fetchone()
        if not server['db'] or server['db'] in {'mysql', 'sys', 'performance_schema', 'information_schema'}:
            raise ValueError('Application database required')
        cursor.execute('SET SESSION lock_wait_timeout=5')
        cursor.execute('SET SESSION max_execution_time=15000')
        cursor.execute("SELECT GENERATION_EXPRESSION, COLUMN_TYPE, COLLATION_NAME "
                       "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                       "AND TABLE_NAME=%s AND COLUMN_NAME='_pack_lookup_key'", (TABLE,))
        pack_column = cursor.fetchone()
        if pack_column and (normalized(pack_column['GENERATION_EXPRESSION']) != normalized(PACK_EXPRESSION)
                            or pack_column['COLUMN_TYPE'] != 'varchar(64)'
                            or pack_column['COLLATION_NAME'] != 'utf8mb4_bin'):
            raise ValueError('Existing pack lookup column has an incompatible definition')
        if not pack_column and apply:
            # MySQL cannot add a virtual column AND its index in one LOCK=NONE
            # operation. Separate online operations avoid a shared write lock.
            cursor.execute(f"ALTER TABLE `{TABLE}` ADD COLUMN `_pack_lookup_key` "
                           f"VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin "
                           f"GENERATED ALWAYS AS ({PACK_EXPRESSION}) VIRTUAL, "
                           "ALGORITHM=INPLACE, LOCK=NONE")
        before = inspect(cursor)
        report = {'server': server, 'before': before, 'applied': []}
        for name, (definition, _) in SPECS.items():
            if not before[name] and apply:
                cursor.execute(f'ALTER TABLE `{TABLE}` ADD INDEX `{name}` ({definition}), ALGORITHM=INPLACE, LOCK=NONE')
                report['applied'].append(name)
        report['after'] = inspect(cursor)
        sql, params = context_lookup(['0'], 1)
        cursor.execute('EXPLAIN ' + sql, params)
        report['lookup_plan'] = cursor.fetchall()
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from bit.db_pool import default_connection_config
    import pymysql
    config = default_connection_config()
    config.update(read_timeout=900, autocommit=True)
    connection = pymysql.connect(**config)
    try:
        print(json.dumps(optimize(connection, apply=args.apply), default=str, indent=2))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
