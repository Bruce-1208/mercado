"""Yandex orders in the shared order-management store.

Negative store IDs and yandex:<campaign>:<order> keys isolate platform identities.
Only platform fields are upserted; procurement and workflow fields remain local.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bit import bit_mysql
from bit.bit_runtime_lock import InterProcessLock, get_lock_owner

STATUS = {'UNPAID': ('payment_required', '待付款'), 'RESERVED': ('confirmed', '审核'),
          'PROCESSING': ('paid', '找货'), 'DELIVERY': ('shipped', '已发'),
          'PICKUP': ('shipped', '已发'), 'DELIVERED': ('delivered', '交付'),
          'CANCELLED': ('cancelled', '取消'), 'PARTIALLY_RETURNED': ('partially_refunded', '退货'),
          'RETURNED': ('refunded', '退货')}
_guard = threading.Lock()
_state = {'running': False, 'status': 'idle', 'fetched': 0, 'inserted': 0, 'updated': 0, 'errors': []}


def ensure_tables(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS unified_yandex_stores (
        id BIGINT PRIMARY KEY, name VARCHAR(255) NOT NULL,
        active TINYINT NOT NULL DEFAULT 1
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')


def store_source_sql(cursor):
    ensure_tables(cursor)
    return '(SELECT id FROM mercado_store_tokens UNION ALL SELECT id FROM unified_yandex_stores WHERE active = 1)'


def registered_store_ids():
    connection = bit_mysql.pymysql.connect(**bit_mysql.config)
    try:
        with connection.cursor() as cursor:
            ensure_tables(cursor)
            cursor.execute('SELECT id FROM unified_yandex_stores WHERE active = 1')
            return {int(row['id']) for row in cursor.fetchall()}
    finally:
        connection.close()


def _utc(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=3)))
    return parsed.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def normalize(store, order):
    from yandex.app.order_finance import build_order_finance
    finance = build_order_finance(order)
    payment = finance.get('buyer_payment') or {}
    currency = payment.get('currency') or 'UNKNOWN'
    currency = 'RUB' if currency == 'RUR' else currency
    items = [{'item': {'id': str(item.get('offerId') or ''),
                       'title': str(item.get('offerName') or item.get('offerId') or ''),
                       'thumbnail': item.get('picture') or ''},
              'quantity': item.get('count') or 0} for item in order.get('items') or []]
    remote_id = str(order.get('orderId') or order.get('id') or '')
    if not remote_id:
        raise ValueError('Yandex 订单缺少订单号')
    status, label = STATUS.get(order.get('status'), ('confirmed', str(order.get('status') or '审核')))
    return {
        'order_id': f"yandex:{int(store['campaign_id'])}:{remote_id}",
        'token_id': -int(store['id']), 'shop_name': store.get('alias') or store.get('store_name') or str(store['id']),
        'seller_id': str(store['business_id']), 'site_id': 'YANDEX', 'country': '俄罗斯',
        'status': status, 'status_label': label, 'status_detail': str(order.get('substatus') or ''),
        'date_created': _utc(order.get('creationDate')), 'last_updated': _utc(order.get('updateDate')),
        'currency_id': currency, 'amount_currency_id': currency, 'total_amount': payment.get('value'),
        'product_id': items[0]['item']['id'] if items else '',
        'title': ' / '.join(item['item']['title'] for item in items),
        'quantity': sum(int(item['quantity']) for item in items),
        'raw_json': json.dumps({'platform': 'yandex', 'pack_id': remote_id, 'order_items': items,
                                'yandex_order': order}, ensure_ascii=False),
    }


def upsert_orders(store, orders):
    rows = [normalize(store, order) for order in orders]
    if not rows:
        return {'inserted': 0, 'updated': 0}
    connection = bit_mysql.pymysql.connect(**bit_mysql.config)
    try:
        with connection.cursor() as cursor:
            bit_mysql._ensure_mercado_synced_orders_table(cursor)
            ensure_tables(cursor)
            cursor.execute('INSERT INTO unified_yandex_stores (id, name) VALUES (%s,%s) ON DUPLICATE KEY UPDATE name=VALUES(name), active=1',
                           (-int(store['id']), rows[0]['shop_name']))
            ids = [row['order_id'] for row in rows]
            cursor.execute('SELECT order_id FROM mercado_synced_orders WHERE order_id IN (' + ','.join(['%s'] * len(ids)) + ')', ids)
            existing = {row['order_id'] for row in cursor.fetchall()}
            columns = list(rows[0])
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.executemany('INSERT INTO mercado_synced_orders (' + ','.join('`'+key+'`' for key in columns) + ',first_synced_at,synced_at) VALUES (' + ','.join(['%s'] * (len(columns)+2)) + ') ON DUPLICATE KEY UPDATE ' + ','.join('`'+key+'`=VALUES(`'+key+'`)' for key in columns if key != 'order_id') + ',synced_at=VALUES(synced_at)',
                               [tuple(row[key] for key in columns) + (now, now) for row in rows])
        connection.commit()
        return {'inserted': len(set(ids)-existing), 'updated': len(set(ids)&existing)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


async def sync_all(*, full=True, start_date=None, end_date=None, progress=None):
    from yandex.app.central_authorization import CentralAuthorizationStore
    from yandex.app.yandex_api import YandexSellerClient, YandexApiError
    auth = CentralAuthorizationStore()
    stores = await asyncio.to_thread(auth.list_stores)
    result = {'running': True, 'status': 'running', 'mode': 'full' if full else 'recent', 'fetched': 0, 'inserted': 0, 'updated': 0, 'errors': [], 'stores': len(stores)}
    connection = bit_mysql.pymysql.connect(**bit_mysql.config)
    try:
        with connection.cursor() as cursor:
            ensure_tables(cursor)
            bit_mysql._ensure_mercado_synced_orders_table(cursor)
            cursor.execute('UPDATE unified_yandex_stores SET active=0')
            for store in stores:
                cursor.execute('INSERT INTO unified_yandex_stores (id,name,active) VALUES (%s,%s,1) ON DUPLICATE KEY UPDATE name=VALUES(name),active=1', (-int(store['id']), store.get('alias') or store.get('store_name') or str(store['id'])))
            if not full:
                cursor.execute("SELECT token_id, MIN(date_created) AS oldest FROM mercado_synced_orders WHERE site_id='YANDEX' AND status NOT IN ('delivered','cancelled','refunded') GROUP BY token_id")
                pending = {int(row['token_id']): row['oldest'] for row in cursor.fetchall() if row.get('oldest')}
                for store in stores:
                    store['_oldest_pending'] = pending.get(-int(store['id']))
        connection.commit()
    finally:
        connection.close()
    today = datetime.now(timezone(timedelta(hours=3))).date()
    semaphore = asyncio.Semaphore(3)
    async def sync_store(public):
        async with semaphore:
            await sync_one(public)

    async def sync_one(public):
        try:
            store = await asyncio.to_thread(auth.get_store, public['id'], include_secret=True)
            client = YandexSellerClient(store['access_token'])
            first = date.fromisoformat(start_date or (os.getenv('YANDEX_ORDER_HISTORY_START', '2000-01-01') if full else (today-timedelta(days=29)).isoformat()))
            if not full and public.get('_oldest_pending') and not start_date:
                first = min(first, public['_oldest_pending'].date())
            last = date.fromisoformat(end_date) if end_date else today
            if first > last:
                raise ValueError('起始日期不能晚于截止日期')
            # Walk newest first. Empty windows do not imply no earlier orders.
            while last >= first:
                begin = max(first, last-timedelta(days=29))
                token, seen = '', set()
                while True:
                    page = await client.get_orders(store['business_id'], campaign_id=store['campaign_id'], date_from=begin.isoformat(), date_to=last.isoformat(), page_token=token, limit=50)
                    orders = page.get('orders') or []
                    counts = await asyncio.to_thread(upsert_orders, store, orders)
                    result['fetched'] += len(orders)
                    for key in ('inserted', 'updated'):
                        result[key] += counts[key]
                    result['current_store'] = store.get('alias') or str(store['id'])
                    result['window'] = f'{begin} ~ {last}'
                    if progress:
                        progress(dict(result))
                    token = str((page.get('paging') or {}).get('nextPageToken') or '')
                    if not token:
                        break
                    if token in seen:
                        raise YandexApiError('订单接口返回重复分页标记')
                    seen.add(token)
                last = begin-timedelta(days=1)
        except Exception as exc:
            result['errors'].append({'store_id': public['id'], 'error': str(exc)[:500]})
    await asyncio.gather(*(sync_store(public) for public in stores))
    result.update(running=False, status='partial' if result['errors'] else 'completed')
    if progress:
        progress(dict(result))
    return result


STATE_PATH = Path(__file__).resolve().parents[1] / '.data' / 'yandex-unified-order-sync.json'


def status():
    with _guard:
        try:
            value = json.loads(STATE_PATH.read_text())
        except (OSError, ValueError):
            value = dict(_state)
        if value.get('running') and not get_lock_owner('unified_yandex_order_sync'):
            value.update(running=False, status='interrupted', errors=[{'error': '同步进程已退出，请重新拉取；已保存订单不会丢失'}])
        return value


def start(**kwargs):
    lock = InterProcessLock('unified_yandex_order_sync', owner='yandex_order_sync')
    if not lock.acquire(timeout=0):
        return False
    def update(value):
        with _guard:
            _state.update(value)
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = STATE_PATH.with_suffix('.tmp')
            temporary.write_text(json.dumps(_state, ensure_ascii=False))
            temporary.replace(STATE_PATH)
    update({'running': True, 'status': 'running', 'mode': 'full' if kwargs.get('full', True) else 'recent', 'fetched': 0, 'inserted': 0, 'updated': 0, 'errors': []})
    def run():
        try:
            lock.transfer_to_current_thread()
            asyncio.run(sync_all(progress=update, **kwargs))
        except Exception as exc:
            update({'running': False, 'status': 'error', 'errors': [{'error': str(exc)[:500]}]})
        finally:
            lock.release()
    threading.Thread(target=run, name='yandex-unified-orders', daemon=True).start()
    return True


_scheduler_started = False
_scheduler_guard = threading.Lock()
_scheduler_stop = threading.Event()


def ensure_scheduler():
    global _scheduler_started
    if os.getenv('YANDEX_ORDER_SYNC_DISABLED', '').lower() in {'1', 'true', 'yes', 'on'}:
        return
    with _scheduler_guard:
        if _scheduler_started:
            return
        def run():
            import logging
            while not _scheduler_stop.wait(15 * 60):
                try:
                    start(full=False)
                except Exception:
                    logging.exception('Yandex 统一订单自动同步失败')
        threading.Thread(target=run, name='yandex-unified-order-scheduler', daemon=True).start()
        _scheduler_started = True


if __name__ == '__main__':
    import time
    if not start(full=True):
        raise SystemExit('Yandex 订单同步任务已运行')
    while status().get('running'):
        time.sleep(1)
    print(json.dumps(status(), ensure_ascii=False))
