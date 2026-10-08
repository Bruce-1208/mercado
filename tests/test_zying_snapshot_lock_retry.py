"""A partial snapshot batch must roll back before retry, never duplicate rows."""
from unittest.mock import Mock

import pytest

from bit import bit_mysql


@pytest.mark.parametrize('code', [1205, 1213])
def test_lock_conflict_rolls_back_and_releases_before_retry(monkeypatch, code):
    committed, events, connections = [], [], []

    class Connection:
        def __init__(self):
            self.pending = []
            connections.append(self)

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def executemany(self, sql, rows):
            self.pending.extend(rows[:1])
            if len(connections) == 1:
                raise bit_mysql.pymysql.OperationalError(code, 'lock conflict')
            self.pending.extend(rows[1:])

        def rollback(self):
            self.pending.clear()
            events.append('rollback')

        def close(self):
            events.append('close')

        def commit(self):
            committed.extend(self.pending)
            events.append('commit')

    monkeypatch.setattr(bit_mysql.pymysql, 'connect', lambda **kw: Connection())
    monkeypatch.setattr(bit_mysql, '_ensure_zying_product_table', lambda cursor: None)
    monkeypatch.setattr(bit_mysql.time, 'sleep', lambda seconds: events.append('sleep'))
    records = ({'product_id': str(i), 'title': '产品'} for i in range(2))
    assert bit_mysql.insert_zying_product_info(records) == 2
    assert [row[0] for row in committed] == ['0', '1']
    assert events == ['rollback', 'close', 'sleep', 'commit', 'close']


@pytest.mark.parametrize('code,attempts', [(1205, 3), (1213, 3), (2013, 1), (2006, 1), (1062, 1)])
def test_retry_is_bounded_and_does_not_replay_uncertain_writes(monkeypatch, code, attempts):
    error = bit_mysql.pymysql.OperationalError(code, 'failure')
    writer = Mock(side_effect=error)
    monkeypatch.setattr(bit_mysql, '_insert_zying_product_info_once', writer)
    monkeypatch.setattr(bit_mysql.time, 'sleep', lambda seconds: None)
    with pytest.raises(bit_mysql.pymysql.OperationalError) as caught:
        bit_mysql.insert_zying_product_info([{'product_id': '1'}])
    assert caught.value is error
    assert writer.call_count == attempts
