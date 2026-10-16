"""Build the display-only SKU index in resumable batches (no marketplace writes).

python -m scripts.backfill_product_sku_groups --apply [--before-id ID]
"""
import argparse
import json
import time
from erp.product_sku_groups import ensure_table, update_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--before-id', type=int, default=2**63 - 1)
    parser.add_argument('--batch-size', type=int, default=2000)
    args = parser.parse_args()
    if not args.apply:
        print('Use --apply to build the display-only index. Product records are preserved.')
        return
    from erp.mercadolibre_collection_store import _connect
    connection = _connect()
    scanned = grouped = 0
    start = time.monotonic()
    before = args.before_id
    try:
        with connection.cursor() as cursor:
            ensure_table(cursor)
        connection.commit()
        while True:
            with connection.cursor() as cursor:
                cursor.execute('''SELECT id,source_type,organization_key,title,category_id,source_snapshot_json
                    FROM erp_mercadolibre_products WHERE source_type='pulled' AND id < %s
                    ORDER BY id DESC LIMIT %s FOR UPDATE''', (before, args.batch_size))
                rows = cursor.fetchall()
                if not rows:
                    break
                grouped += update_rows(cursor, rows)
                before = rows[-1]['id']
                scanned += len(rows)
            connection.commit()
            print(json.dumps({'scanned': scanned, 'indexed': grouped, 'before_id': before,
                              'seconds': round(time.monotonic()-start, 1)}), flush=True)
        print(json.dumps({'complete': True, 'scanned': scanned, 'indexed': grouped}), flush=True)
    finally:
        connection.close()


if __name__ == '__main__':
    main()
