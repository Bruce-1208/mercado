"""Distinct order activity by the account that performed the operation."""
from datetime import datetime, timedelta


def build_query(start_date="", end_date="", allowed_token_ids=None):
    bounds = []
    for value in (start_date, end_date):
        try:
            bounds.append(datetime.strptime(value, "%Y-%m-%d") if value else None)
        except (ValueError, TypeError):
            raise ValueError("日期格式应为 YYYY-MM-DD") from None
    start, end = bounds
    if start and end and start > end:
        raise ValueError("开始日期不能晚于结束日期")
    where = ["logs.action_type IN ('label_printed', 'purchase_created')",
             "logs.operator_id IS NOT NULL", "logs.operator_id > 0"]
    params = []
    if allowed_token_ids is not None:
        ids = sorted({int(value) for value in allowed_token_ids})
        if ids:
            where.append("synced.token_id IN (" + ",".join(["%s"] * len(ids)) + ")")
            params.extend(ids)
        else:
            where.append("1 = 0")
    dates = []
    if start:
        dates.append("first_at >= %s")
        params.append(start.strftime("%Y-%m-%d %H:%M:%S"))
    if end:
        dates.append("first_at < %s")
        params.append((end + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
    # Apply dates AFTER lifetime deduplication, so reprints in another period
    # cannot turn into new work. IDs keep accounts with equal names separate.
    return f"""
        SELECT operator_id, MAX(operator_name) AS salesperson,
               SUM(CASE WHEN action_type = 'label_printed' THEN 1 ELSE 0 END) AS print_count,
               SUM(CASE WHEN action_type = 'purchase_created' THEN 1 ELSE 0 END) AS purchase_count
        FROM (
            SELECT logs.operator_id, MAX(logs.operator_name) AS operator_name,
                   logs.order_id, logs.action_type, MIN(logs.created_at) AS first_at
            FROM mercado_order_operation_logs AS logs
            INNER JOIN mercado_synced_orders AS synced ON synced.order_id = logs.order_id
            INNER JOIN mercado_store_tokens AS stores ON stores.id = synced.token_id
            WHERE {' AND '.join(where)}
            GROUP BY logs.operator_id, logs.order_id, logs.action_type
        ) AS activities
        {'WHERE ' + ' AND '.join(dates) if dates else ''}
        GROUP BY operator_id
        ORDER BY print_count DESC, purchase_count DESC, operator_id ASC
    """, params


def list_stats(start_date="", end_date="", *, allowed_token_ids=None):
    from bit import bit_mysql
    query, params = build_query(start_date, end_date, allowed_token_ids)
    connection = bit_mysql.pymysql.connect(**bit_mysql.config)
    try:
        with connection.cursor() as cursor:
            bit_mysql._ensure_mercado_synced_orders_table(cursor)
            bit_mysql._ensure_mercado_store_tokens_table(cursor)
            bit_mysql._ensure_mercado_order_logs_table(cursor)
            cursor.execute(query, params)
            rows = list(cursor.fetchall() or [])
        for row in rows:
            row['print_count'] = int(row['print_count'] or 0)
            row['purchase_count'] = int(row['purchase_count'] or 0)
            row['salesperson'] = row['salesperson'] or f"账号 {row['operator_id']}"
        return {"rows": rows, "print_count": sum(r['print_count'] for r in rows),
                "purchase_count": sum(r['purchase_count'] for r in rows)}
    finally:
        connection.close()
