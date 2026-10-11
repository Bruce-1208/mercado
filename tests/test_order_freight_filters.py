from decimal import Decimal
import sqlite3
from unittest.mock import patch

import pytest
from werkzeug.datastructures import MultiDict
from bit.order_freight_filters import difference_range, difference_predicate


@pytest.mark.parametrize('low,high,expected', [
    (None,None,['negative','zero','positive','rounded','no-actual','no-quote']),
    ('-1','1',['negative','zero','positive','rounded']),
    ('0',None,['zero','positive','rounded']),
    (None,'-0.01',['negative']),
    ('1','1',['positive','rounded']),
    ('0','0',['zero']),
])
def test_sql_range_uses_signed_difference_includes_bounds_and_excludes_missing_freight(low,high,expected):
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE orders (id TEXT, actual REAL, quoted REAL)')
    db.executemany('INSERT INTO orders VALUES (?,?,?)', [
        ('negative',4,5),('zero',5,5),('positive',6,5),('rounded',6.004,5),
        ('no-actual',None,5),('no-quote',6,None)])
    clause, params=difference_predicate('actual','quoted',low,high)
    rows=db.execute('SELECT id FROM orders'+(' WHERE '+clause.replace('%s','?') if clause else ''),
                    [float(value) for value in params]).fetchall()
    assert [row[0] for row in rows]==expected
    db.close()


@pytest.mark.parametrize('low,high', [('NaN',''),('Infinity',''),('abc',''),('2','1')])
def test_invalid_ranges_are_rejected(low,high):
    with pytest.raises(ValueError): difference_range(low,high)


def test_zero_and_whitespace_are_not_confused_with_missing_bounds():
    assert difference_range(' 0 ',' ') == (Decimal('0'),None)


def test_routes_and_api_clients_preserve_zero_and_negative_bounds():
    from bit import bit_interface,bit_db_api
    params=bit_interface._order_list_query_params(MultiDict({'freight_difference_min':'-2.50','freight_difference_max':'0'}))
    assert params['freight_difference_min']=='-2.50'
    assert params['freight_difference_max']=='0'
    with patch.object(bit_db_api,'DB_MODE','api'),patch.object(bit_db_api,'_request',return_value={}) as request:
        bit_db_api.list_orders(freight_difference_min=0,freight_difference_max='5')
    assert request.call_args.kwargs['params']['freight_difference_min']=='0'
    assert request.call_args.kwargs['params']['freight_difference_max']=='5'
