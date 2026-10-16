"""Bounded, read-only platform refresh for the visible listing page."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import math
import threading
import time

from bit import bit_mysql
from bit.mercado_promotions import _adapter
from bit.product_diagnosis import _read
from bit.store_link_item_lock import store_link_item_locks
from bit.store_link_visits import fetch_total_visits
from erp.mercadolibre_promotion_store import PromotionStore, APPLIED_STATUSES
from erp.mercadolibre_store_link_store import get_store_links_by_ids

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='listing-metrics')
_guard = threading.Lock()
_cache = {}
_pending = {}


def quality(client, item):
    up = str(item.get('user_product_id') or '')
    path = f'/user-product/{up}/performance' if up else f"/item/{item['id']}/performance"
    result = _read(client, path)
    data = result.get('data') or {}
    value = data.get('score') if isinstance(data, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100:
        return {'quality_score': value, 'quality_error': ''}
    return {'quality_score': None, 'quality_error': (result.get('message') or '平台尚未提供质量分数').replace('重新诊断', '刷新')}


def activities(adapter, token, row, item):
    seller = str(item.get('seller_id') or '')
    if not seller:
        raise ValueError('缺少站点卖家编号')
    snapshot_started_at = datetime.now(timezone.utc).isoformat()
    live = adapter.get_item_promotions(seller, row['item_id'])
    store = PromotionStore()
    available, enrolled = [], []
    for activity in live:
        status = activity.get('status') or ''
        status = str(status.get('id') if isinstance(status, dict) else status).lower()
        if status != 'candidate' and status not in APPLIED_STATUSES:
            if status not in {'finished', 'ended', 'cancelled', 'canceled', 'deleted', 'withdrawn', 'rejected'}:
                raise ValueError('平台活动状态未知')
            continue
        kind = str(activity.get('type') or '').upper()
        campaign = dict(activity)
        # An unnamed PRICE_DISCOUNT candidate is not an existing campaign.
        pk = None
        if activity.get('id'):
            pk = store.ensure_listing_promotion(token_id=row['token_id'], site_id=row['site_id'],
                seller_id=seller, application_id=str(token.get('client_id') or ''),
                store_name=row.get('store_name') or '', row=campaign)
            store.refresh_item_activity(
                pk, row['item_id'], dict(activity, status=status),
                snapshot_started_at=snapshot_started_at,
            )
        detail = {'promotion_fk': pk, 'promotion_id': activity.get('id') or '',
                  'promotion_type': kind, 'name': activity.get('name') or ('自定义价格折扣' if kind == 'PRICE_DISCOUNT' else kind),
                  'status_raw': status, 'price': activity.get('price'),
                  'currency_id': activity.get('currency_id') or '', 'last_synced_at': time.strftime('%Y-%m-%d %H:%M:%S')}
        (available if status == 'candidate' else enrolled).append(detail)
    # Only this item is reconciled; no full-store completeness is inferred.
    key = (int(row['token_id']), row['site_id'], row['item_id'])
    current = {(x['promotion_id'], x['promotion_type']) for x in enrolled}
    for old in store.applied_item_details([key]).get(key, []):
        if (old['promotion_id'], old['promotion_type']) not in current:
            store.set_item_status(
                old['promotion_fk'], row['item_id'], 'withdrawn',
                snapshot_started_at=snapshot_started_at,
            )
    return {'available_promotions': available, 'enrolled_promotions': enrolled,
            'promotion_applied': bool(enrolled), 'activity_applied': bool(enrolled), 'promotions_error': ''}


def collect(row):
    result = {'id': row['id'], 'metrics_error': ''}
    with store_link_item_locks(row['token_id'], [row['item_id']], owner='listing-metrics'):
        try:
            adapter, token = _adapter(dict(bit_mysql.get_mercado_store_token(row['token_id']) or {}))
            item = adapter.client.get_marketplace_item(row['item_id'])
            if not isinstance(item, dict) or item.get('id') != row['item_id']:
                raise ValueError('商品详情不匹配')
        except Exception:
            return dict(result, metrics_error='商品数据读取失败，请检查店铺授权后刷新')
        sold = item.get('sold_quantity')
        if type(sold) is int and sold >= 0:
            result.update(sold_quantity=sold, sales_error='')
        else:
            result['sales_error'] = '平台未返回总销量'
        result.update(fetch_total_visits(adapter.client, row['item_id']))
        result.update(quality(adapter.client, item))
        try:
            result.update(activities(adapter, token, row, item))
        except Exception:
            result['promotions_error'] = '活动读取失败，请检查授权后刷新'
    return result


def _collect_cached(row):
    key = (int(row['token_id']), row['item_id'])
    try:
        result = collect(row)
        with _guard:
            if len(_cache) >= 2000:
                _cache.pop(next(iter(_cache)))
            _cache[key] = (time.monotonic() + 60, result)
        return result
    finally:
        with _guard:
            _pending.pop(key, None)


def refresh(link_ids, allowed_token_ids=None):
    ids = list(dict.fromkeys(int(value) for value in link_ids))
    if not ids or len(ids) > 8 or any(value <= 0 for value in ids):
        raise ValueError('每次读取 1 至 8 个商品')
    rows = get_store_links_by_ids(ids)
    if allowed_token_ids is not None and any(int(row['token_id']) not in allowed_token_ids for row in rows):
        raise PermissionError('无权读取这些店铺的商品')
    results, futures = [], []
    with _guard:
        for row in rows:
            key = (int(row['token_id']), row['item_id'])
            cached = _cache.get(key)
            if cached and cached[0] > time.monotonic():
                results.append(deepcopy(cached[1]))
                continue
            if key not in _pending:
                if len(_pending) >= 64:
                    raise ValueError('当前读取任务较多，请稍后刷新')
                _pending[key] = _pool.submit(_collect_cached, row)
            futures.append(_pending[key])
    results.extend(deepcopy(future.result()) for future in futures)
    return results


def invalidate(token_id, item_id):
    with _guard:
        _cache.pop((int(token_id), str(item_id)), None)
