import pytest
from bit.order_purchase_platform import infer_platforms, platform_filter_sql
from bit.order_workflow import selected_statuses


@pytest.mark.parametrize('value,expected', [
    ('261010-428364347761287', ['pdd']),
    ('5127514670081027336', ['1688']),
    ('3317112372030010072', ['taobao_xianyu']),
    ('261010-428364347761287,3317112372030010072；5127514670081027336', ['pdd', '1688', 'taobao_xianyu']),
    ('采购单：待补充', []), ('', []),
    ('0261010-428364347761287', []),
])
def test_identifies_whole_procurement_ids_and_mixed_orders(value, expected):
    assert infer_platforms(value) == expected


def test_platform_filter_is_bound_and_rejects_injection():
    sql, params = platform_filter_sql('`purchase_order`', 'pdd')
    assert '%s' in sql and len(params) == 1
    with pytest.raises(ValueError):
        platform_filter_sql('`purchase_order`', "pdd' OR 1=1")


def test_workflow_group_expansion_and_invalid_status():
    assert selected_statuses('@转运') == ['转运-待收货', '转运-待出库', '转运-已出库', '转运-待退回']
    assert selected_statuses('已发-异常') == ['已发-异常']
    with pytest.raises(ValueError):
        selected_statuses('随便一个状态')
