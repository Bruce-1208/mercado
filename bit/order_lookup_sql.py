"""Fixed expression shared by order lookups and offline index maintenance."""
# Keep a bounded index key; the full JSON predicate below remains authoritative.
PACK_EXPRESSION = "(CAST(JSON_UNQUOTE(JSON_EXTRACT(`raw_json`, '$.pack_id')) AS CHAR(64) CHARACTER SET utf8mb4) COLLATE utf8mb4_bin)"


def context_lookup(identifiers, token_id):
    placeholders = ','.join(['%s'] * len(identifiers))
    branches = [
        f"SELECT `order_id` FROM `mercado_synced_orders` WHERE `token_id`=%s AND `order_id` IN ({placeholders})",
        f"SELECT `order_id` FROM `mercado_synced_orders` WHERE `token_id`=%s AND `shipping_id` IN ({placeholders})",
        f"SELECT `order_id` FROM `mercado_synced_orders` WHERE `token_id`=%s "
        f"AND {PACK_EXPRESSION} IN ({placeholders}) "
        f"AND JSON_UNQUOTE(JSON_EXTRACT(`raw_json`, '$.pack_id')) IN ({placeholders})",
    ]
    sql = (
        "SELECT orders.`order_id`, orders.`shop_name`, orders.`site_id`, orders.`country`, orders.`status`, "
        "orders.`status_label`, orders.`date_created`, orders.`last_updated`, orders.`shipping_id`, "
        "orders.`buyer_name`, orders.`title`, orders.`image_url`, orders.`raw_json` "
        "FROM (" + ' UNION '.join(branches) + ") AS matched "
        "STRAIGHT_JOIN `mercado_synced_orders` AS orders ON orders.`order_id`=matched.`order_id` "
        "ORDER BY orders.`last_updated` DESC, orders.`order_id` DESC"
    )
    params = [int(token_id), *identifiers, int(token_id), *identifiers,
              int(token_id), *(value[:64] for value in identifiers), *identifiers]
    return sql, params
