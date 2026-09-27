"""Fill advertising thumbnails from the synchronized, store-scoped listing cache."""
import logging


def enrich(links):
    from bit.bit_ad_analysis import _product_image
    from erp.mercadolibre_store_link_store import _connect, STORE_LINK_TABLE

    missing = {}
    for row in links:
        row['thumbnail_url'] = _product_image(row)
        if not row['thumbnail_url'] and row.get('token_id') and row.get('item_id'):
            key = (int(row['token_id']), str(row['item_id']).strip().upper())
            missing.setdefault(key, []).append(row)
    if not missing:
        return
    connection = None
    try:
        connection = _connect()
        keys = list(missing)
        with connection.cursor() as cursor:
            for offset in range(0, len(keys), 500):
                batch = keys[offset:offset + 500]
                placeholders = ','.join(['(%s,%s)'] * len(batch))
                cursor.execute(
                    f'SELECT token_id, item_id, thumbnail_url FROM `{STORE_LINK_TABLE}` '
                    f'WHERE (token_id, item_id) IN ({placeholders})',
                    tuple(value for key in batch for value in key),
                )
                for item in cursor.fetchall():
                    image = _product_image(item)
                    if image:
                        key = (int(item['token_id']), str(item['item_id']).strip().upper())
                        for row in missing.get(key, []):
                            row['thumbnail_url'] = image
    except Exception as exc:
        logging.warning('广告商品本地图片读取失败：%s', exc)
    finally:
        if connection is not None:
            connection.close()
