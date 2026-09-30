"""Order workflow shared by listing, procurement and platform synchronization."""
GROUPS = {
    '找货': ['找货-没汇总', '找货-已采购', '找货-待确定'],
    '审核': ['审核-待审核', '审核-已审核'],
    '转运': ['转运-待收货', '转运-待出库', '转运-已出库', '转运-待退回'],
    '待发': ['待发'],
    '已发': ['已发-在途中', '已发-异常'],
    '问题': ['问题-验货异常', '问题-物流退件', '问题-亏损超重'],
    '交付': ['交付'],
    '取消': ['取消-发货前', '取消-发货后'],
}
STATUSES = frozenset(status for values in GROUPS.values() for status in values)
LEGACY = {'找货': '', '待付款': '', '缺货': '找货-待确定',
          '审核': '审核-待审核', '配货': '转运-待收货', '转运': '转运-待收货',
          '待打印': '转运-已出库', '已发': '已发-在途中', '问题': '问题-验货异常',
          '退货': '问题-物流退件', '取消': '取消-发货前', '备查': '找货-待确定',
          '补发': '', 'FBA': ''}
EARLY = {'找货-没汇总', '找货-已采购', '找货-待确定'}


def effective_status(row):
    workflow = LEGACY.get(row.get('workflow_status'), row.get('workflow_status')) or ''
    platform = str(row.get('status') or '').lower()
    if platform == 'delivered':
        return '交付'
    if platform in {'cancelled', 'canceled'}:
        return '取消-发货后' if workflow in {'已发-在途中', '已发-异常', '交付', '取消-发货后'} else '取消-发货前'
    if workflow == '交付':
        return '交付'
    if platform == 'shipped':
        return workflow if workflow in {'已发-异常', '问题-物流退件'} else '已发-在途中'
    if platform == 'not_delivered':
        return '已发-异常'
    if platform in {'refunded', 'partially_refunded'}:
        return '问题-物流退件'
    if workflow in STATUSES:
        return workflow
    if row.get('purchase_tracking'):
        return '转运-待收货'
    if row.get('purchase_order'):
        return '找货-已采购'
    return '找货-没汇总'


def procurement_status(before, changes):
    current = effective_status(before)
    if current not in EARLY | {'审核-待审核', '审核-已审核'}:
        return current
    updated = {**before, **changes}
    if updated.get('purchase_tracking'):
        return '转运-待收货'
    if updated.get('purchase_order') and current in EARLY:
        return '找货-已采购'
    return current


def display_status_sql(alias='synced'):
    prefix = f'{alias}.' if alias else ''
    wf = f"{prefix}`workflow_status`"
    status = f"LOWER(COALESCE({prefix}`status`, ''))"
    legacy = ' '.join(f"WHEN '{old}' THEN '{new}'" for old, new in LEGACY.items())
    normalized = f"(CASE {wf} {legacy} ELSE {wf} END)"
    values = ','.join(f"'{value}'" for value in sorted(STATUSES))
    return f"""CASE
        WHEN {status} = 'delivered' THEN '交付'
        WHEN {status} IN ('cancelled', 'canceled') THEN
            CASE WHEN {normalized} IN ('已发-在途中','已发-异常','交付','取消-发货后')
                 THEN '取消-发货后' ELSE '取消-发货前' END
        WHEN {normalized} = '交付' THEN '交付'
        WHEN {status} = 'shipped' THEN
            CASE WHEN {normalized} IN ('已发-异常','问题-物流退件') THEN {normalized} ELSE '已发-在途中' END
        WHEN {status} = 'not_delivered' THEN '已发-异常'
        WHEN {status} IN ('refunded','partially_refunded') THEN '问题-物流退件'
        WHEN {normalized} IN ({values}) THEN {normalized}
        WHEN COALESCE({prefix}`purchase_tracking`, '') <> '' THEN '转运-待收货'
        WHEN COALESCE({prefix}`purchase_order`, '') <> '' THEN '找货-已采购'
        ELSE '找货-没汇总' END"""
