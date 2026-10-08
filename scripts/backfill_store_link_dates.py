"""Fill missing listing dates from saved platform responses, in small batches."""
import json
from erp.mercadolibre_store_link_store import _ensure_column, listing_start_time
from bit.db_pool import default_connection_config
import pymysql


def main():
    config = default_connection_config()
    config["read_timeout"] = 3600
    connection = pymysql.connect(**config)
    scanned = filled = last_id = 0
    try:
        with connection.cursor() as cursor:
            _ensure_column(cursor, 'listed_at', 'DATETIME NULL')
        connection.commit()
        while True:
            with connection.cursor() as cursor:
                cursor.execute('SELECT id, JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(remote_json), remote_json, "{}"), "$.start_time")) start_time, JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(remote_json), remote_json, "{}"), "$.date_created")) date_created FROM erp_mercadolibre_store_links WHERE id > %s AND listed_at IS NULL ORDER BY id LIMIT 1000', (last_id,))
                rows = cursor.fetchall()
                if not rows:
                    break
                updates = []
                for row in rows:
                    value = listing_start_time(row)
                    if value:
                        updates.append((value, row['id']))
                if updates:
                    cursor.executemany('UPDATE erp_mercadolibre_store_links SET listed_at=%s, updated_at=updated_at WHERE id=%s AND listed_at IS NULL', updates)
                    filled += cursor.rowcount
                last_id = rows[-1]['id']
                scanned += len(rows)
            connection.commit()
            if scanned % 50000 == 0:
                print(json.dumps(dict(scanned=scanned, filled=filled)), flush=True)
        print(json.dumps(dict(scanned=scanned, filled=filled, missing=scanned-filled)), flush=True)
    finally:
        connection.close()


def backfill_remote():
    """Only read platform dates and update missing dates; resumable on rerun."""
    from concurrent.futures import ThreadPoolExecutor
    from bit.bit_store_link_sync import _token_records, _client_and_token
    config = default_connection_config()
    config["read_timeout"] = 3600
    connection = pymysql.connect(**config)
    total = filled = failed = 0
    try:
        for token in _token_records():
            last_id = 0
            while True:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id,item_id FROM erp_mercadolibre_store_links WHERE token_id=%s AND is_current=1 AND listed_at IS NULL AND id>%s ORDER BY id LIMIT 100", (token['id'], last_id))
                    rows = cursor.fetchall()
                connection.commit()
                if not rows:
                    break
                try:
                    client, token = _client_and_token(token)
                except Exception as exc:
                    print(json.dumps(dict(token_id=token['id'], error=type(exc).__name__)), flush=True)
                    break
                def fetch(row):
                    try:
                        item = client.get_marketplace_item(row['item_id'], attributes=('id', 'start_time', 'date_created'))
                        return listing_start_time(item), row['id']
                    except Exception:
                        return None, row['id']
                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(fetch, rows))
                updates = [(value, link_id) for value, link_id in results if value]
                with connection.cursor() as cursor:
                    if updates:
                        cursor.executemany('UPDATE erp_mercadolibre_store_links SET listed_at=%s, updated_at=updated_at WHERE id=%s AND listed_at IS NULL', updates)
                        filled += cursor.rowcount
                connection.commit()
                total += len(rows)
                failed += len(rows) - len(updates)
                last_id = rows[-1]['id']
                print(json.dumps(dict(token_id=token['id'], checked=total, filled=filled, unavailable=failed)), flush=True)
        print(json.dumps(dict(status='completed', checked=total, filled=filled, unavailable=failed)), flush=True)
    finally:
        connection.close()


if __name__ == '__main__':
    import sys
    if "--remote" in sys.argv:
        backfill_remote()
    else:
        main()
