"""Persist per-unit contribution and explain conservative advertising decisions."""
import math
import os
import sqlite3
from pathlib import Path

from bit.bit_runtime_lock import RUNTIME_LOCK_DIR


def _connect():
    path = Path(os.environ.get('BIT_AD_PROFIT_PATH') or RUNTIME_LOCK_DIR / 'ad_profit.sqlite3')
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute('CREATE TABLE IF NOT EXISTS profits (token_id INTEGER, site_id TEXT, item_id TEXT, currency_id TEXT, profit REAL, PRIMARY KEY (token_id, site_id, item_id, currency_id))')
    return conn


def key(row):
    return (int(row.get('token_id') or 0), str(row.get('site_id') or '').upper(),
            str(row.get('item_id') or '').upper(), str(row.get('profit_currency_id') or 'CNY').strip().upper())


def save_profit(row, value):
    identity = key(row)
    if identity[0] <= 0 or not all(identity[1:]):
        raise ValueError('商品、店铺和站点不能为空')
    if value is not None:
        if isinstance(value, bool):
            raise ValueError('单件利润必须是有效数字')
        try:
            value = float(value)
        except (ValueError, TypeError):
            raise ValueError('单件利润必须是有效数字') from None
        if not math.isfinite(value) or abs(value) > 1e12:
            raise ValueError('单件利润必须是有限数字且绝对值不超过一万亿')
    conn = _connect()
    try:
        with conn:
            if value is None:
                conn.execute('DELETE FROM profits WHERE token_id=? AND site_id=? AND item_id=? AND currency_id=?', identity)
            else:
                conn.execute('INSERT OR REPLACE INTO profits VALUES (?,?,?,?,?)', (*identity, value))
    finally:
        conn.close()
    return {'unit_profit': value, 'profit_currency_id': identity[3], 'recommendation': recommendation(row, value)}


def recommendation(row, profit):
    result = {'action': 'observe', 'label': '继续观察', 'reason': '', 'net_profit': None, 'break_even_cpc': None}
    if profit is None:
        return {**result, 'action': 'missing', 'label': '待填写利润', 'reason': '填写单件广告前利润后计算'}
    profit_currency = key(row)[3]
    ad_currency = str(row.get('currency_id') or '').strip().upper()
    if ad_currency != profit_currency:
        return {**result, 'reason': '利润已按人民币保存；广告币种缺失或与利润币种不同，暂无法计算利润建议' if profit_currency == 'CNY' else '广告币种与利润币种不同，暂无法计算利润建议'}
    m = row.get('metrics') or {}
    cost = float(m.get('cost') or 0)
    clicks = int(m.get('clicks') or 0)
    # Indirect sales may belong to other products with different margins.
    if not row.get('direct_units_available', False):
        return {**result, 'reason': '缺少直接成交件数，暂无法计算利润建议'}
    units = int(m.get('direct_units_quantity') or 0)
    net = units * profit - cost
    result.update(net_profit=round(net, 2), break_even_cpc=round(units * profit / clicks, 4) if clicks else None)
    if profit <= 0:
        return {**result, 'action': 'pause', 'label': '建议暂停广告', 'reason': '单件广告前利润不为正，先改善商品利润'}
    if cost <= 0 or (clicks < 30 and units < 5):
        return {**result, 'reason': '样本不足（至少30次点击或5件直接成交），暂不加预算'}
    if units == 0 or net <= -cost * .3:
        return {**result, 'action': 'pause', 'label': '建议暂停广告', 'reason': '无直接成交或广告亏损达到开销的30%，建议检查商品与投放'}
    if net < 0:
        return {**result, 'action': 'decrease', 'label': '建议减少预算', 'reason': '直接成交利润不足以覆盖广告开销，可先减少10%–20%'}
    if units >= 5 and net >= cost * .3:
        return {**result, 'action': 'increase', 'label': '建议增加预算', 'reason': '至少5件直接成交且广告后利润达到开销的30%，可小幅增加10%–20%'}
    return {**result, 'action': 'continue', 'label': '建议继续广告', 'reason': '已覆盖广告开销，维持预算并继续观察'}


def enrich(snapshot):
    conn = _connect()
    try:
        profits = {tuple(row[:4]): row[4] for row in conn.execute('SELECT * FROM profits')}
    finally:
        conn.close()
    for row in snapshot.get('links') or []:
        row['profit_currency_id'] = key(row)[3]
        row['unit_profit'] = profits.get(key(row))
        row['recommendation'] = recommendation(row, row['unit_profit'])
    return snapshot
