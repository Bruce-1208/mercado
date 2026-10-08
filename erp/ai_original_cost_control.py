"""Scheduling and distributed exclusion for the three-cent listing workflow."""

from contextlib import contextmanager
from datetime import datetime, timezone
import time


def wait_for_low_cost_window(on_wait=None, *, now=None, sleep=None):
    from erp.ai_generation_budget import is_safe_offpeak, next_safe_offpeak

    clock = now or (lambda: datetime.now(timezone.utc))
    pause = sleep or time.sleep
    announced = None
    while True:
        current = clock()
        if is_safe_offpeak(current):
            return
        resume_at = next_safe_offpeak(current)
        if resume_at != announced and on_wait is not None:
            on_wait(resume_at.isoformat())
            announced = resume_at
        pause(min(30, max(0.1, (resume_at - current).total_seconds())))


@contextmanager
def product_generation_lock(product_id, *, connection_factory=None):
    """One paying worker per product, including different server processes.

    Use the database's advisory lock rather than a Python-only mutex. The
    worker must reread the product after acquisition. Connection loss releases
    the lock; every paid-call checkpoint still has to commit before dispatch.
    """
    from erp.mercadolibre_collection_store import _connect

    name = f"mercado-ai-budget:{int(product_id)}"
    connection = (connection_factory or _connect)()
    acquired = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (name,))
            row = cursor.fetchone() or {}
            acquired = row.get("acquired") == 1
        if not acquired:
            raise RuntimeError("该商品已有生成任务，未重复调用模型；请等待任务完成后刷新")

        def check_owner():
            with connection.cursor() as cursor:
                cursor.execute("SELECT IS_USED_LOCK(%s) = CONNECTION_ID() AS owned", (name,))
                if (cursor.fetchone() or {}).get("owned") != 1:
                    raise RuntimeError("商品生成锁已失效，已停止付费调用；请刷新后继续")

        yield check_owner
    finally:
        try:
            if acquired:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT RELEASE_LOCK(%s)", (name,))
        finally:
            connection.close()


def generation_cost_summary():
    """All original-product ledgers, independent of list filters/pagination."""
    import json
    from erp.mercadolibre_collection_store import _connect, PRODUCT_TABLE, ensure_collection_tables
    from bit import product_access

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            access_sql, access_params = product_access.predicate(PRODUCT_TABLE)
            cursor.execute(
                f"SELECT JSON_EXTRACT(source_snapshot_json, '$.ai_original.generation_budget') AS ledger "
                f"FROM `{PRODUCT_TABLE}` WHERE source_type = 'ai_original'"
                + (f" AND {access_sql}" if access_sql else ""), tuple(access_params))
            rows = cursor.fetchall()
    finally:
        connection.close()
    spent = reserved = tokens = recorded = 0
    for row in rows:
        ledger = row.get('ledger')
        if isinstance(ledger, str):
            ledger = json.loads(ledger)
        if not isinstance(ledger, dict):
            continue
        recorded += 1
        for call in ledger.get('calls', []):
            if call.get('state') == 'complete':
                spent += int(call.get('cost_nano', 0))
                tokens += int(call.get('prompt_tokens', 0)) + int(call.get('completion_tokens', 0))
            else:
                reserved += int(call.get('reserved_nano', 0))
    return dict(spent_yuan=spent / 1_000_000_000, reserved_yuan=reserved / 1_000_000_000,
                total_tokens=tokens, recorded_products=recorded, total_products=len(rows),
                budget_limit_enabled=False)
