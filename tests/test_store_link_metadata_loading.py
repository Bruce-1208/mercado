"""Permission-scoped cache concurrency and bounded connection ownership."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import threading

import pytest

from erp import mercadolibre_store_link_store as store


@pytest.fixture(autouse=True)
def fresh_cache():
    store.invalidate_store_link_metadata_cache()
    store._metadata_index_cache.clear()
    yield
    store.invalidate_store_link_metadata_cache()
    store._metadata_index_cache.clear()


def metadata(value=1):
    return {'group_map': {}, 'groups': [], 'stores': [], 'store_groups': {},
            'sites': [], 'mercado_categories': [],
            'summary': {'all_count': value, 'current_count': value, 'store_count': 1}}


class Cursor:
    def __enter__(self): return self
    def __exit__(self, *_args): return False


class Connection:
    def __init__(self): self.closed = False
    def cursor(self): return Cursor()
    def close(self): self.closed = True


def test_slow_scope_does_not_block_cached_or_other_scope_and_waiters_do_not_borrow(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    connections, calls = [], Counter()
    def connect():
        connection = Connection()
        connections.append(connection)
        return connection
    def query(cursor, *, token_ids=None, **_kwargs):
        scope = tuple(token_ids)
        calls[scope] += 1
        if scope == (1,):
            entered.set()
            assert release.wait(3)
        return metadata(scope[0])
    monkeypatch.setattr(store, '_connect', connect)
    monkeypatch.setattr(store, 'ensure_store_link_table', lambda cursor: None)
    monkeypatch.setattr(store, '_query_store_link_metadata', query)
    def load(scope):
        return store._store_link_metadata(None, use_cache=True, token_ids=scope,
                                          include_categories=False)
    load([2])
    with ThreadPoolExecutor(max_workers=4) as executor:
        slow = executor.submit(load, [1])
        try:
            assert entered.wait(1)
            same_scope = executor.submit(load, [1])
            cached = executor.submit(load, [2])
            other = executor.submit(load, [3])
            assert cached.result(timeout=1)['summary']['current_count'] == 2
            assert other.result(timeout=1)['summary']['current_count'] == 3
            assert len(connections) == 3  # cached caller and waiter borrow none
            assert sum(not c.closed for c in connections) == 1
        finally:
            release.set()
        assert slow.result(timeout=1) == same_scope.result(timeout=1)
    assert calls == Counter({(1,): 1, (2,): 1, (3,): 1})
    assert all(c.closed for c in connections)


def test_invalidation_during_refresh_cannot_restore_old_scoped_metadata(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def query(cursor, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            return metadata(1)
        return metadata(2)
    monkeypatch.setattr(store, '_query_store_link_metadata', query)
    def load(): return store._store_link_metadata(object(), use_cache=True, token_ids=[7])
    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(load)
        try:
            assert entered.wait(1)
            store.invalidate_store_link_metadata_cache()
            assert executor.submit(load).result(timeout=1)['summary']['current_count'] == 2
        finally:
            release.set()
        assert old.result(timeout=1)['summary']['current_count'] == 1
    assert load()['summary']['current_count'] == 2
    assert len(calls) == 2


def test_category_picker_reuses_base_metadata_and_keeps_scopes_separate(monkeypatch):
    base_calls, category_calls, connections = [], [], []
    def connect():
        c = Connection(); connections.append(c); return c
    def base(cursor, *, token_ids, include_categories):
        assert include_categories is False
        base_calls.append(tuple(token_ids))
        return metadata(token_ids[0])
    def categories(cursor, *, token_ids):
        category_calls.append(tuple(token_ids))
        return [{'category_id': str(token_ids[0])}]
    monkeypatch.setattr(store, '_connect', connect)
    monkeypatch.setattr(store, 'ensure_store_link_table', lambda cursor: None)
    monkeypatch.setattr(store, '_query_store_link_metadata', base)
    monkeypatch.setattr(store, '_query_store_link_categories', categories)
    first = store._store_link_metadata(None, use_cache=True, token_ids=[1], include_categories=False)
    full = store._store_link_metadata(None, use_cache=True, token_ids=[1])
    assert base_calls == [(1,)] and category_calls == [(1,)]
    assert full['mercado_categories'] == [{'category_id': '1'}]
    full['summary']['current_count'] = 99
    assert first['summary']['current_count'] == 1
    assert store._store_link_metadata(None, use_cache=True, token_ids=[1])['summary']['current_count'] == 1
    assert store._store_link_metadata(None, use_cache=True, token_ids=[2])['summary']['current_count'] == 2
    assert base_calls == [(1,), (2,)] and category_calls == [(1,), (2,)]
    assert all(c.closed for c in connections)


def test_refresh_failure_releases_waiting_callers(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def query(cursor, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            raise RuntimeError('query failed')
        return metadata(2)
    monkeypatch.setattr(store, '_query_store_link_metadata', query)
    def load(): return store._store_link_metadata(object(), use_cache=True, token_ids=[1])
    with ThreadPoolExecutor(max_workers=2) as executor:
        failed = executor.submit(load)
        try:
            assert entered.wait(1)
            waiter = executor.submit(load)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match='query failed'):
            failed.result(timeout=1)
        assert waiter.result(timeout=1)['summary']['current_count'] == 2
    assert not store._metadata_refreshes


@pytest.mark.parametrize('index_exists', [True, False])
def test_scoped_statistics_remain_authorized_and_use_covering_query(monkeypatch, index_exists):
    queries = []
    class SQLCursor:
        def execute(self, sql, values=()):
            self.sql = sql
            queries.append((sql, values))
        def fetchall(self): return []
        def fetchone(self):
            if 'AS `all_count`' in self.sql:
                return {'all_count': 4, 'current_count': 3, 'store_count': 2,
                        **({'last_synced_at': '2026-10-06'} if index_exists else {})}
            return {'last_synced_at': '2026-10-06'}
    monkeypatch.setattr(store, '_metadata_index_exists', lambda cursor: index_exists)
    result = store._query_store_link_metadata(SQLCursor(), token_ids=[7, 2, 7], include_categories=True)
    summary_queries = [(sql, values) for sql, values in queries if 'AS `all_count`' in sql]
    assert len(summary_queries) == 1
    assert summary_queries[0][1] == (2, 7)
    assert 'SUM(`is_current` = 1)' in summary_queries[0][0]
    assert 'COUNT(DISTINCT CASE WHEN `is_current` = 1 THEN `token_id` END)' in summary_queries[0][0]
    category_sql, values = next((sql, values) for sql, values in queries if 'AS category_counts' in sql)
    assert values == (2, 7) and 'links.`token_id` IN (%s, %s)' in category_sql
    expected = store.STORE_LINK_METADATA_INDEX if index_exists else 'idx_erp_meli_store_link_store'
    assert f'FORCE INDEX (`{expected}`)' in category_sql
    assert store.STORE_LINK_CATEGORY_PAGE_INDEX not in category_sql
    assert result['summary']['all_count'] == 4 and result['summary']['current_count'] == 3
    assert result['summary']['last_synced_at'] == '2026-10-06'
    if not index_exists:
        last_sql, values = queries[-1]
        assert 'ORDER BY `last_synced_at` DESC LIMIT 1' in last_sql and values == (2, 7)


def test_empty_permission_scope_does_not_query_global_last_sync(monkeypatch):
    queries = []
    class SQLCursor:
        def execute(self, sql, values=()): queries.append(sql)
        def fetchall(self): return []
        def fetchone(self): return {'all_count': 0, 'current_count': 0, 'store_count': 0}
    monkeypatch.setattr(store, '_metadata_index_exists', lambda cursor: False)
    result = store._query_store_link_metadata(SQLCursor(), token_ids=[], include_categories=True)
    assert result['summary']['last_synced_at'] is None
    assert not any('ORDER BY `last_synced_at`' in sql for sql in queries)
    assert result['mercado_categories'] == []


@pytest.mark.parametrize('scope', [None, [1], [1, 2], [], [3, 3]])
def test_metadata_aggregates_real_rows_without_cross_scope_data(monkeypatch, scope):
    import sqlite3
    import re
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE mercado_store_site_settings(token_id INT, site_id TEXT, group_name TEXT, salesperson TEXT);
        CREATE TABLE mercado_store_tokens(id INT, display_name TEXT, organization_key TEXT DEFAULT 'wuhan-zeshun');
        CREATE TABLE erp_mercadolibre_store_links(id INT, token_id INT, is_current INT,
            site_id TEXT, last_synced_at TEXT, category_id TEXT, item_id TEXT);
        CREATE TABLE erp_mercadolibre_products(source_item_id TEXT, category_name TEXT, organization_key TEXT DEFAULT 'wuhan-zeshun');
        INSERT INTO mercado_store_tokens(id,display_name) VALUES(1,'A'),(2,'B'),(3,'C');
        INSERT INTO mercado_store_site_settings VALUES(1,'MLM','A','张三'),(2,'MLB','B','李四'),(3,'MLC','C','张三');
    ''')
    rows = [
        (1, 1, 1, 'MLM', '2026-10-01', 'C1', 'MLM1'),
        (2, 1, 1, 'MLM', None, 'C1', 'MLM2'),
        (3, 1, 0, 'MLM', '2026-10-06', 'C2', 'MLM3'),
        (4, 2, 1, 'MLB', '2026-10-03', 'C2', 'MLB4'),
        (5, 3, 1, 'MLC', '2026-10-05', 'C3', 'MLC5'),
        (6, 3, 1, '', None, '', 'MLC6'),
    ]
    db.executemany('INSERT INTO erp_mercadolibre_store_links VALUES(?,?,?,?,?,?,?)', rows)
    class SQLCursor:
        def execute(self, sql, values=()):
            # SQLite executes the aggregate/permission semantics; MySQL index
            # selection is verified independently through EXPLAIN diagnostics.
            sql = re.sub(r'FORCE INDEX \(`[^`]+`\)', '', sql).replace('%s', '?')
            self.cursor = db.execute(sql, values)
        def fetchall(self): return [dict(row) for row in self.cursor.fetchall()]
        def fetchone(self):
            row = self.cursor.fetchone()
            return None if row is None else dict(row)
    monkeypatch.setattr(store, '_metadata_index_exists', lambda cursor: True)
    try:
        actual = store._query_store_link_metadata(SQLCursor(), token_ids=scope)
    finally:
        db.close()
    expected_salespeople = {name for token, name in [(1, '张三'), (2, '李四'), (3, '张三')] if scope is None or token in scope}
    assert set(actual['salespeople']) == expected_salespeople
    permitted = [row for row in rows if scope is None or row[1] in scope]
    current = [row for row in permitted if row[2]]
    assert actual['summary']['all_count'] == len(permitted)
    assert actual['summary']['current_count'] == len(current)
    assert actual['summary']['store_count'] == len({row[1] for row in current})
    assert actual['summary']['last_synced_at'] == max((row[4] for row in current if row[4]), default=None)
    assert {s['site_id']: s['link_count'] for s in actual['sites']} == dict(Counter(row[3] for row in current if row[3]))
    assert {c['category_id']: c['link_count'] for c in actual['mercado_categories']} == dict(Counter(row[5] for row in current if row[5]))
    assert {s['token_id'] for s in actual['stores']} == ({1, 2, 3} if scope is None else set(scope))


def test_workflow_decoration_happens_after_mysql_connection_is_closed(monkeypatch):
    class PageCursor(Cursor):
        def execute(self, sql, values=()): self.sql = sql
        def fetchone(self): return {'table_exists': 0}
        def fetchall(self): return [{'id': 1, 'token_id': 1, 'item_id': 'MLM1', 'site_id': 'MLM'}]
    class PageConnection(Connection):
        def cursor(self): return PageCursor()
    connection = PageConnection()
    decorated = []
    monkeypatch.setattr(store, 'ensure_store_link_table', lambda cursor: None)
    monkeypatch.setattr(store, '_store_link_metadata', lambda *args, **kwargs: metadata())
    def decorate(rows):
        assert connection.closed
        decorated.extend(rows)
    monkeypatch.setattr(store, '_decorate_store_link_markers', decorate)
    result = store.list_store_links(connection_factory=lambda: connection, include_categories=False)
    assert result['rows'] == decorated and len(decorated) == 1
