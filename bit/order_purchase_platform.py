"""Infer procurement sources from individual IDs, including mixed batches.

The observed 19-digit procurement IDs use 5... for 1688 and 3... for
Taobao/Xianyu; other numeric shapes remain unidentified. These are heuristics.
Taobao and Xianyu numeric IDs are deliberately kept together: an order number
alone cannot establish which of the two sites issued it.
"""
import re

PATTERNS = {
    'pdd': r'[0-9]{6}-[0-9]{15}',
    '1688': r'5[0-9]{18}',
    'taobao_xianyu': r'3[0-9]{18}',
}
LABELS = {'pdd': '拼多多', '1688': '1688', 'taobao_xianyu': '淘宝 / 闲鱼'}


def validate_platform(value):
    value = str(value or '').strip().lower()
    if value and value not in PATTERNS:
        raise ValueError('采购平台筛选参数无效')
    return value


def infer_platforms(value):
    ids = re.split(r'[\s,，;；]+', str(value or '').strip())
    return [key for key, pattern in PATTERNS.items()
            if any(re.fullmatch(pattern, order_id) for order_id in ids)]


def tracking_orders_for_platform(orders, platform):
    """Select individual procurement IDs; never send another platform's IDs.

    Numeric Taobao and Xianyu IDs cannot be distinguished without source metadata.
    Mixed-platform rows retain their other IDs so the runner can refuse a partial
    overwrite of the row's shared tracking field.
    """
    key = 'taobao_xianyu' if platform in ('taobao', 'xianyu') else platform
    pattern = PATTERNS[key]
    selected = []
    for row in orders:
        ids = list(dict.fromkeys(filter(None, re.split(
            r'[\s,，;；]+', str(row.get('purchase_order') or '').strip()))))
        matching = [oid for oid in ids if re.fullmatch(pattern, oid)]
        if matching:
            selected.append({
                **row, 'purchase_order': ','.join(matching),
                'purchase_orders': matching,
                'other_purchase_orders': [oid for oid in ids if oid not in matching],
            })
    return selected


def platform_filter_sql(column, value):
    value = validate_platform(value)
    return f"COALESCE({column}, '') REGEXP %s", [
        r'(^|[[:space:],，;；])' + PATTERNS[value] + r'($|[[:space:],，;；])']
