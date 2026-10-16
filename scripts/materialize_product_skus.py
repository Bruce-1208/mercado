"""Consolidate indexed store SKUs into editable product matrices, resumably."""
import argparse
import json
import time
from erp.product_sku_materialize import ensure_table, materialize
from erp.product_sku_groups import TABLE
from erp.mercadolibre_collection_store import _connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--missing-only', action='store_true')
    parser.add_argument('--after-key', default='')
    parser.add_argument('--before-key', default='z')
    args = parser.parse_args()
    if not args.apply:
        parser.error('--apply required')
    connection = _connect()
    count = 0
    after = args.after_key
    start = time.monotonic()
    try:
        with connection.cursor() as cursor:
            ensure_table(cursor)
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        failures = 0
        while True:
            try:
                connection.begin()
                with connection.cursor() as cursor:
                    having = "COUNT(*) > 1"
                    if args.missing_only:
                        having += " AND SUM(NOT EXISTS (SELECT 1 FROM erp_mercadolibre_product_sku_members m WHERE m.product_id=g.product_id)) > 0"
                    cursor.execute(f'''SELECT group_key FROM `{TABLE}` g WHERE group_key > %s AND group_key < %s
                        GROUP BY group_key HAVING {having} ORDER BY group_key LIMIT 100''', (after, args.before_key))
                    keys = [r['group_key'] for r in cursor.fetchall()]
                    if not keys:
                        break
                    changed = materialize(cursor, keys)
                connection.commit()
            except Exception as exc:
                connection.rollback()
                failures += 1
                if not exc.args or exc.args[0] not in (1205, 1213) or failures > 10:
                    raise
                time.sleep(min(failures, 3))
                continue
            failures = 0
            count += changed
            after = keys[-1]
            print(json.dumps({'products': count, 'after_key': after, 'seconds': round(time.monotonic()-start, 1)}), flush=True)
        print(json.dumps({'complete': True, 'products': count}), flush=True)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    main()
