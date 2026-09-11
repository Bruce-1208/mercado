import pymysql

from bit.bit_mysql import config


SAMPLE_IDS = [148980004, 140509583, 120193561, 97986667, 77980457]
SAMPLE_ORDER_NOS = [
    "2000014971169645",
    "2000014199784765",
    "2000012315178535",
    "2000010125990443",
    "2000008346021185",
]


connection = pymysql.connect(**config, autocommit=False)
try:
    with connection.cursor() as cursor:
        for table, id_col, date_col in (
            ("orders", "id", "时间"),
            ("mercado_synced_orders", "order_id", "date_created"),
        ):
            cursor.execute(
                f"SELECT COUNT(*) AS n, MIN(`{id_col}`) AS min_id, MAX(`{id_col}`) AS max_id, "
                f"MIN(`{date_col}`) AS min_date, MAX(`{date_col}`) AS max_date FROM `{table}`"
            )
            print(table, cursor.fetchone())
        placeholders = ",".join(["%s"] * len(SAMPLE_IDS))
        cursor.execute(
            f"""
            SELECT order_id, date_created, shop_name, country, total_amount,
                   sale_fee, freight
            FROM mercado_synced_orders
            WHERE order_id IN ({placeholders})
            ORDER BY order_id DESC
            """,
            SAMPLE_IDS,
        )
        print("mercado_synced_orders")
        for row in cursor.fetchall():
            print(row)
        order_placeholders = ",".join(["%s"] * len(SAMPLE_ORDER_NOS))
        cursor.execute(
            f"""
            SELECT order_id, date_created, shop_name, country, total_amount,
                   sale_fee, freight,
                   JSON_UNQUOTE(JSON_EXTRACT(raw_json, '$.pack_id')) AS pack_id
            FROM mercado_synced_orders
            WHERE order_id IN ({order_placeholders})
               OR JSON_UNQUOTE(JSON_EXTRACT(raw_json, '$.pack_id')) IN ({order_placeholders})
            ORDER BY date_created DESC, order_id DESC
            """,
            [*SAMPLE_ORDER_NOS, *SAMPLE_ORDER_NOS],
        )
        print("mercado_synced_orders by order number")
        for row in cursor.fetchall():
            print(row)
        cursor.execute(
            f"""
            SELECT id, 编号, 时间, 业务员, 来源, 金额, 费用, 退款
            FROM orders
            WHERE id IN ({placeholders})
            ORDER BY CAST(id AS UNSIGNED) DESC
            """,
            SAMPLE_IDS,
        )
        print("orders")
        for row in cursor.fetchall():
            print(row)
finally:
    connection.rollback()
    connection.close()
