"""Fault injection for MySQL transactions; no live database or browser writes."""
import pytest
from pymysql.err import InterfaceError, OperationalError, ProgrammingError

from erp.ai_weight_price import store as module


class Connection:
    def __init__(self, committed, *, fail_query=0, error=None, rollback_error=False,
                 commit_error=False, close_error=False, cursor_close_error=False):
        self.committed = committed
        self.pending = []
        self.queries = []
        self.begins = self.commits = 0
        self.closed = False
        self.fail_query = fail_query
        self.error = error or OperationalError(2013, 'Lost connection during query')
        self.rollback_error = rollback_error
        self.commit_error = commit_error
        self.close_error = close_error
        self.cursor_close_error = cursor_close_error

    def begin(self):
        self.begins += 1

    def cursor(self):
        connection = self
        class Cursor:
            description = None
            rowcount = 1

            def execute(self, sql, args):
                assert connection.begins == 1
                connection.queries.append((sql, args))
                if len(connection.queries) == connection.fail_query:
                    raise connection.error
                connection.pending.append((sql, args))

            def close(self):
                if connection.cursor_close_error:
                    raise InterfaceError(0, '')
        return Cursor()

    def commit(self):
        self.commits += 1
        self.committed.extend(self.pending)
        self.pending.clear()
        if self.commit_error:
            raise OperationalError(2013, 'Commit response lost')

    def rollback(self):
        if self.rollback_error:
            raise InterfaceError(0, '')
        self.pending.clear()

    def close(self):
        self.closed = True
        self.pending.clear()
        if self.close_error:
            raise InterfaceError(0, '')


@pytest.fixture
def make_store(monkeypatch):
    monkeypatch.setattr(module.time, 'sleep', lambda _seconds: None)
    def make(connections):
        store = module.Store.__new__(module.Store)
        store.backend = 'mysql'
        store.dirty = False
        remaining = iter(connections)
        calls = []
        def connect():
            item = next(remaining)
            calls.append(item)
            if isinstance(item, Exception):
                raise item
            return item
        store.connection_factory = connect
        return store, calls
    return make


def test_log_reconnects_and_keeps_original_error_when_rollback_also_fails(make_store, caplog):
    committed = []
    first = Connection(committed, fail_query=1, rollback_error=True, cursor_close_error=True)
    second = Connection(committed)
    store, calls = make_store([first, second])

    store.log('progress')

    assert len(calls) == 2
    assert first.closed and second.closed
    assert len(committed) == 1
    assert committed[0][1][-1] == 'progress'
    assert '重新连接重试 1/2' in caplog.text
    assert 'Lost connection during query' in caplog.text


def test_retries_whole_transaction_instead_of_only_last_statement(make_store):
    committed = []
    first = Connection(committed, fail_query=2, rollback_error=True)
    second = Connection(committed)
    store, _ = make_store([first, second])

    assert store.clear_run('run-1') == {'run_items': 1}

    assert len(first.queries) == len(second.queries) == len(committed) == 2
    assert first.queries == second.queries == committed
    assert first.commits == 0
    assert second.commits == 1


def test_connection_acquisition_can_retry(make_store):
    committed = []
    store, calls = make_store([OperationalError(2003, 'Cannot connect'), Connection(committed)])
    store.set_state('checkpoint', {'page': 4})
    assert len(calls) == 2
    assert len(committed) == 1


def test_commit_response_loss_is_never_replayed(make_store):
    committed = []
    connection = Connection(committed, commit_error=True, rollback_error=True)
    store, calls = make_store([connection])

    with pytest.raises(module.MySQLCommitUncertain, match='提交结果待核对') as error:
        store.log('already committed')

    assert error.value.__cause__.args[0] == 2013
    assert len(calls) == len(committed) == 1
    assert connection.closed


def test_close_failure_after_successful_commit_does_not_repeat_write(make_store):
    committed = []
    store, calls = make_store([Connection(committed, close_error=True)])
    store.log('committed')
    assert len(calls) == len(committed) == 1


@pytest.mark.parametrize('error', [ProgrammingError(1064, 'SQL error'), OperationalError(1045, 'Access denied')])
def test_permanent_error_is_preserved_without_retry(make_store, error):
    connection = Connection([], fail_query=1, error=error, rollback_error=True)
    store, calls = make_store([connection])
    with pytest.raises(type(error)) as caught:
        store.log('test')
    assert caught.value is error
    assert len(calls) == 1
    assert connection.closed


def test_persistent_disconnect_is_bounded_and_explained(make_store):
    connections = [Connection([], fail_query=1, error=InterfaceError(0, ''), rollback_error=True)
                   for _ in range(3)]
    store, calls = make_store(connections)
    with pytest.raises(RuntimeError, match='MySQL连接中断，重试2次后仍失败') as caught:
        store.log('test')
    assert isinstance(caught.value.__cause__, InterfaceError)
    assert len(calls) == 3
    assert all(connection.closed for connection in connections)


def test_sqlite_begin_alias_does_not_restart_mysql_transaction(make_store):
    committed = []
    connection = Connection(committed)
    store, _ = make_store([connection])
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE state SET value=? WHERE key=?', ('1', 'test'))
    assert connection.begins == 1
    assert len(committed) == 1

