"""Name-based analysis of authorized Zeshun synchronized orders.

Use the order-management reader so exchange rates, actual/quoted freight and
workflow status have the same meaning in both screens. Never read Excel orders.
"""
from collections import OrderedDict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from threading import Lock
from time import monotonic

from bit import bit_db_api

_CACHE = OrderedDict()
_LOCK = Lock()
_REQUEST_LOCKS = [Lock() for _ in range(16)]


def _number(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _aggregate(rows):
    products, seen = {}, set()
    for order in rows:
        identity = (order.get('store_id'), str(order.get('id') or ''))
        if identity in seen or order.get('data_origin') != 'token':
            continue
        seen.add(identity)
        title = ' '.join(str(order.get('title') or '').split())
        if not title:
            continue
        product = products.setdefault(title, dict(
            title=title, image='', sites=set(), total_quantity=0, order_count=0,
            after_sale_quantity=0, after_sale_orders=0, latest_after_sale_time='',
            total_income=Decimal(0), total_profit=Decimal(0), latest_order_time='',
            finance_orders=0, comparable_orders=0, total_actual_freight=Decimal(0),
            total_quoted_freight=Decimal(0), freight_change=Decimal(0),
            max_freight_change=Decimal(0), latest_freight_time='',
        ))
        quantity = max(int(_number(order.get('quantity')) or 1), 1)
        ordered_at = str(order.get('ordered_at') or '')
        product['order_count'] += 1
        product['total_quantity'] += quantity
        product['sites'].add(str(order.get('country') or order.get('site_id') or ''))
        product['image'] = product['image'] or order.get('image_url') or ''
        product['latest_order_time'] = max(product['latest_order_time'], ordered_at)
        if order.get('status') == '取消-发货后':
            product['after_sale_quantity'] += quantity
            product['after_sale_orders'] += 1
            product['latest_after_sale_time'] = max(product['latest_after_sale_time'], ordered_at)
        income, profit = _number(order.get('income')), _number(order.get('profit'))
        if income is not None and profit is not None:
            product['finance_orders'] += 1
            product['total_income'] += income
            product['total_profit'] += profit
        actual = _number(order.get('actual_freight_usd'))
        quoted = _number(order.get('quoted_freight_usd'))
        if actual is not None and quoted is not None:
            change = actual - quoted
            product['comparable_orders'] += 1
            product['total_actual_freight'] += actual
            product['total_quoted_freight'] += quoted
            product['latest_freight_time'] = max(product['latest_freight_time'], ordered_at)
            if abs(change) > product['max_freight_change']:
                product['max_freight_change'] = abs(change)
                product['freight_change'] = change
    result = []
    for product in products.values():
        product['sites'] = '、'.join(sorted(product['sites'] - {''}))
        product['after_sale_rate'] = round(product['after_sale_quantity'] / product['total_quantity'] * 100, 2)
        product['profit_rate'] = (round(float(product['total_profit'] / product['total_income'] * 100), 2)
                                  if product['total_income'] else 0)
        for key, value in product.items():
            if isinstance(value, Decimal):
                product[key] = round(float(value), 2)
        result.append(product)
    return result


def _products(store_ids, date_from, date_to, salesperson, salespeople):
    key = (tuple(sorted(store_ids)), date_from, date_to, salesperson, tuple(sorted(salespeople or ())))
    with _REQUEST_LOCKS[hash(key) % len(_REQUEST_LOCKS)]:
        with _LOCK:
            cached = _CACHE.get(key)
        if cached and monotonic() - cached[0] < 60:
            return deepcopy(cached[1])
        def orders():
            page = 1
            while store_ids:
                data = bit_db_api.list_orders(
                    origin='token', allowed_store_ids=list(store_ids),
                    start_date=date_from, end_date=date_to, salesperson=salesperson,
                    salespeople=salespeople, page=page, page_size=200,
                    include_summary=False,
                )
                yield from data.get('rows') or []
                if page >= int(data.get('pages') or 1):
                    break
                page += 1
        products = _aggregate(orders())
        with _LOCK:
            _CACHE[key] = (monotonic(), products)
            _CACHE.move_to_end(key)
            while len(_CACHE) > 16:
                _CACHE.popitem(last=False)
        return deepcopy(products)


def analyze(kind, *, allowed_store_ids, sort_by='', sort_dir='desc', search='',
            date_from='', date_to='', limit=100, salesperson='', salespeople=None):
    from bit.bit_mysql import _filter_datetime_bounds
    _filter_datetime_bounds(date_from, date_to)
    sorts = {'after_sales': ('after_sale_quantity', 'after_sale_rate'),
             'profits': ('total_profit', 'profit_rate'), 'freight': ('max_freight_change',)}
    sort_by = sort_by or sorts[kind][0]
    if sort_by not in sorts[kind]:
        raise ValueError('不支持的产品排序字段')
    rows = _products(allowed_store_ids, date_from, date_to, salesperson, salespeople)
    rows = [row for row in rows if search.casefold() in row['title'].casefold()]
    if kind == 'after_sales':
        rows = [row for row in rows if row['after_sale_quantity'] > 0]
        total = sum(row['total_quantity'] for row in rows)
        after_sale = sum(row['after_sale_quantity'] for row in rows)
        summary = dict(alert_products=len(rows), total_quantity=total, after_sale_quantity=after_sale,
                       after_sale_rate=round(after_sale / total * 100, 2) if total else 0)
    elif kind == 'profits':
        rows = [row for row in rows if row['total_profit'] > 0]
        income = sum(row['total_income'] for row in rows)
        profit = sum(row['total_profit'] for row in rows)
        summary = dict(profitable_products=len(rows), total_income=round(income, 2), total_profit=round(profit, 2),
                       profit_rate=round(profit / income * 100, 2) if income else 0)
    else:
        rows = [row for row in rows if row['comparable_orders'] and row['max_freight_change'] > 0]
        summary = dict(products=len(rows), comparable_orders=sum(row['comparable_orders'] for row in rows))
    rows.sort(key=lambda row: row['title'])
    rows.sort(key=lambda row: row[sort_by], reverse=sort_dir != 'asc')
    return dict(rows=rows[:max(1, min(int(limit), 500))], summary=summary,
                sort_by=sort_by, sort_dir=sort_dir, date_from=date_from, date_to=date_to)
