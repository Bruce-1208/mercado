"""Official payment-release snapshots. No platform writes or profit mutations."""
from collections import defaultdict
from datetime import date
import logging

TABLE = "mercado_order_payment_releases"
STATES = {"", "released", "unreleased", "partial", "unknown"}


def validate_filter(value):
    value = str(value or "").strip()
    if value not in STATES:
        raise ValueError("资金释放筛选参数无效")
    return value


def release_snapshot(entries):
    """Missing/novel states never mean unreleased; scheduled dates aren't actuals."""
    payments = {}
    for entry in entries:
        for payment in entry.get("payment_info") or []:
            if not isinstance(payment, dict) or not str(payment.get("payment_id") or "").strip():
                return {"state": "unknown", "release_date": None}
            key = str(payment["payment_id"])
            value = (str(payment.get("money_release_status") or "").lower(),
                     str(payment.get("money_release_date") or ""))
            if key in payments and payments[key] != value:
                return {"state": "unknown", "release_date": None}
            payments[key] = value
    states = [value[0] for value in payments.values()]
    if not states or any(value not in {"released", "pending", "blocked"} for value in states):
        return {"state": "unknown", "release_date": None}
    released = states.count("released")
    state = "released" if released == len(states) else "partial" if released else "unreleased"
    dates = []
    for status, raw in payments.values():
        if status != "released":
            continue
        try:
            # The billing API may omit an offset. Preserve the platform calendar
            # date instead of inventing a UTC/Beijing timezone conversion.
            dates.append(date.fromisoformat(raw[:10]).isoformat())
        except ValueError:
            dates.append(None)
    return {"state": state, "release_date": max(dates) if dates and all(dates) else None}


def ensure_schema(cursor):
    cursor.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        token_id BIGINT NOT NULL, order_id VARCHAR(64) NOT NULL,
        release_state VARCHAR(16) NOT NULL DEFAULT 'unknown', release_date DATE NULL,
        checked_at DATETIME NULL, attempted_at DATETIME NOT NULL,
        sync_error VARCHAR(255) NOT NULL DEFAULT '',
        PRIMARY KEY (token_id, order_id),
        KEY release_due (release_state, attempted_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")


def _connect():
    from bit import bit_mysql
    return bit_mysql.pymysql.connect(**bit_mysql.config)


def pending_orders(limit=120):
    """Read bounded candidates and return the DB connection before HTTP calls."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_schema(cursor)
            cursor.execute(f"""SELECT o.token_id, o.order_id,
                JSON_UNQUOTE(JSON_EXTRACT(CASE WHEN JSON_VALID(o.raw_json)
                    THEN o.raw_json ELSE '{{}}' END, '$.seller.id')) AS seller_id
                FROM mercado_synced_orders o
                INNER JOIN mercado_store_tokens t ON t.id=o.token_id AND t.enabled=1
                LEFT JOIN {TABLE} r ON r.token_id=o.token_id AND r.order_id=o.order_id
                WHERE o.site_id <> 'YANDEX'
                  AND (r.order_id IS NULL OR ((r.release_state <> 'released' OR r.release_date IS NULL)
                       AND r.attempted_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 6 HOUR)))
                ORDER BY r.attempted_at ASC, o.date_created DESC, o.order_id
                LIMIT %s""", (max(1, min(600, int(limit))),))
            rows = list(cursor.fetchall())
        connection.commit()
        return rows
    finally:
        connection.close()


def save_results(token_id, results, *, error=""):
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_schema(cursor)
            for order_id, snapshot in results.items():
                if error:
                    # Failed reads preserve the last verified state and date.
                    cursor.execute(f"""INSERT INTO {TABLE}
                        (token_id,order_id,attempted_at,sync_error)
                        VALUES (%s,%s,UTC_TIMESTAMP(),%s)
                        ON DUPLICATE KEY UPDATE attempted_at=UTC_TIMESTAMP(),sync_error=VALUES(sync_error)""",
                        (token_id, order_id, error[:255]))
                else:
                    cursor.execute(f"""INSERT INTO {TABLE}
                        (token_id,order_id,release_state,release_date,checked_at,attempted_at,sync_error)
                        VALUES (%s,%s,%s,%s,UTC_TIMESTAMP(),UTC_TIMESTAMP(),'')
                        ON DUPLICATE KEY UPDATE release_state=VALUES(release_state),
                        release_date=VALUES(release_date),checked_at=VALUES(checked_at),
                        attempted_at=VALUES(attempted_at),sync_error=''""",
                        (token_id, order_id, snapshot['state'], snapshot['release_date']))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    from bit.order_read_cache import invalidate
    invalidate()


def sync_pending(limit=120):
    """Called under the existing maintenance-worker financial lock, in batches."""
    from bit import bit_mysql, bit_order_sync
    rows = pending_orders(limit)
    groups = defaultdict(list)
    for row in rows:
        groups[(int(row['token_id']), str(row.get('seller_id') or ''))].append(str(row['order_id']))
    failed = 0
    clients = {}
    for (token_id, seller_id), ids in groups.items():
        for start in range(0, len(ids), 60):
            batch = ids[start:start + 60]
            try:
                bit_order_sync._raise_if_interpreter_shutting_down()
                # Billing expects the local order seller, never the parent
                # merchant stored in the token's meli_user_id field.
                if not seller_id.isdigit() or int(seller_id) <= 0:
                    raise ValueError("订单缺少站点卖家编号")
                if token_id not in clients:
                    token = bit_mysql.get_mercado_store_token(token_id)
                    if not token or not bool(token.get('enabled', 1)):
                        raise ValueError("店铺授权已停用")
                    clients[token_id], _ = bit_order_sync._client_and_token(token)
                payload = clients[token_id].get_order_billing(batch, seller_id=seller_id)
                entries = payload.get('results')
                if not isinstance(entries, list) or int(payload.get('total', len(entries))) > len(entries):
                    raise ValueError("订单账单响应不完整")
                by_order = defaultdict(list)
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError("订单账单格式无效")
                    order_id = str(entry.get('order_id') or '')
                    if order_id in batch:
                        by_order[order_id].append(entry)
                snapshots = {key: release_snapshot(by_order[key]) for key in batch}
                save_results(token_id, snapshots)
            except Exception as exc:
                if isinstance(exc, bit_order_sync._InterpreterShutdownRequested) or bit_order_sync._is_interpreter_shutdown_error(exc):
                    raise
                logging.exception("订单资金释放核对失败，店铺 %s", token_id)
                failed += len(batch)
                save_results(token_id, dict.fromkeys(batch), error="核对失败，保留上次结果；后台将重试")
    return {"requested": len(rows), "failed": failed}
