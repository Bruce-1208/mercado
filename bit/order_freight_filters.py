"""Validate and query the signed USD difference shown on order rows."""
from decimal import Decimal, InvalidOperation


def difference_range(minimum=None, maximum=None):
    def parse(value):
        if value is None or not str(value).strip():
            return None
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation:
            raise ValueError('运费差额区间请输入有效数字') from None
        if not number.is_finite():
            raise ValueError('运费差额区间请输入有效数字')
        return number

    low, high = parse(minimum), parse(maximum)
    if low is not None and high is not None and low > high:
        raise ValueError('运费差额最小值不能大于最大值')
    return low, high


def difference_predicate(actual, quoted, minimum=None, maximum=None):
    low, high = difference_range(minimum, maximum)
    if low is None and high is None:
        return '', []
    clauses = [f'{actual} IS NOT NULL', f'{quoted} IS NOT NULL']
    params = []
    for bound, operator in ((low, '>='), (high, '<=')):
        if bound is not None:
            clauses.append(f'ROUND({actual} - {quoted}, 2) {operator} %s')
            params.append(bound)
    return ' AND '.join(clauses), params
