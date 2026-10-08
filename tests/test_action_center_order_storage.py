from datetime import datetime, timedelta
import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from bit import bit_db_api, bit_mysql


NOW = datetime(2026, 10, 7, 12)
NOW_UTC = NOW - timedelta(hours=8)
WATCH_TOPICS = (
    'purchase_order_missing_24h', 'purchase_tracking_missing_48h',
    'order_not_in_transit_7d', 'procurement_tracking_stalled',
    'shipment_deadline_check', 'order_not_delivered_30d',
)


class QueryConnection:
    """Execute the production selection SQL with only MySQL date syntax adapted."""

    def __init__(self, database):
        self.database = database
        self.executed = []
        self.closed = 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))
        for amount, unit in ((30, 'DAY'), (7, 'DAY'), (12, 'HOUR')):
            delta = timedelta(days=amount) if unit == 'DAY' else timedelta(hours=amount)
            sql = sql.replace(
                f'DATE_SUB(UTC_TIMESTAMP(), INTERVAL {amount} {unit})',
                f"'{NOW_UTC - delta:%Y-%m-%d %H:%M:%S}'",
            )
        self.result = self.database.execute(sql.replace('%s', '?'), [
            value.isoformat(' ') if isinstance(value, datetime) else value for value in params
        ])

    def fetchall(self):
        return [dict(row) for row in self.result.fetchall()]

    def close(self):
        self.closed += 1


@pytest.fixture
def order_query(monkeypatch):
    database = sqlite3.connect(':memory:')
    database.row_factory = sqlite3.Row
    database.execute('''CREATE TABLE mercado_synced_orders (
        order_id TEXT, token_id INTEGER, shop_name TEXT, site_id TEXT,
        status TEXT, status_label TEXT, date_created TEXT, last_updated TEXT,
        shipping_id TEXT, workflow_status TEXT, purchase_order TEXT,
        purchase_tracking TEXT, logistics_company TEXT, tracking_cache_json TEXT,
        tracking_checked_at TEXT, raw_json TEXT
    )''')
    database.execute('''CREATE TABLE mercado_operator_tasks (
        token_id INTEGER, resource TEXT, topic TEXT, status TEXT
    )''')
    connection = QueryConnection(database)
    ensure_orders = MagicMock()
    ensure_tasks = MagicMock()
    monkeypatch.setattr(bit_mysql.pymysql, 'connect', lambda **kwargs: connection)
    monkeypatch.setattr(bit_mysql, '_ensure_mercado_synced_orders_table', ensure_orders)
    monkeypatch.setattr(bit_mysql, '_ensure_mercado_action_center_tables', ensure_tasks)
    yield database, connection, ensure_orders, ensure_tasks
    database.close()


def seed_order(database, order_id, *, token_id=7, age=timedelta(days=45),
               status='paid', workflow_status='', raw=None, tracking=None):
    database.execute('''INSERT INTO mercado_synced_orders
        (order_id, token_id, status, workflow_status, date_created, last_updated,
         raw_json, tracking_cache_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', (
        order_id, token_id, status, workflow_status,
        f'{NOW_UTC - age:%Y-%m-%d %H:%M:%S}', f'{NOW_UTC:%Y-%m-%d %H:%M:%S}',
        json.dumps(raw or {}), json.dumps(tracking or {}),
    ))


def seed_watch(database, order_id, *, token_id=7, topic='order_not_delivered_30d', status='open'):
    database.execute('INSERT INTO mercado_operator_tasks VALUES (?, ?, ?, ?)',
                     (token_id, order_id, topic, status))


def test_order_query_includes_old_unfulfilled_orders_and_recent_terminal_orders(order_query):
    database, connection, ensure_orders, ensure_tasks = order_query
    expected = {'old-paid', 'old-shipped', 'old-not-delivered', 'old-partial-refund',
                'old-warehouse-receipt', 'old-unknown', 'recent-delivered', 'recent-invalid'}
    for order_id, changes in (
        ('old-paid', {}), ('old-shipped', {'status': 'shipped'}),
        ('old-not-delivered', {'status': 'not_delivered'}),
        ('old-partial-refund', {'status': 'partially_refunded', 'workflow_status': '退货'}),
        ('old-warehouse-receipt', {'workflow_status': '已入库'}),
        ('old-unknown', {'status': None, 'workflow_status': None}),
        ('recent-delivered', {'status': 'delivered', 'age': timedelta(days=30)}),
        ('recent-invalid', {'status': 'invalid', 'age': timedelta(days=1)}),
        ('foreign-old-paid', {'token_id': 8}),
        ('foreign-recent-paid', {'token_id': 8, 'age': timedelta(days=1)}),
    ):
        seed_order(database, order_id, **changes)
    for status in ('delivered', 'cancelled', 'canceled', 'invalid', 'expired', 'refunded', ' DELIVERED '):
        seed_order(database, f'old-{status}', status=status)
    for workflow in ('交付', '已签收', '完成', '已取消', '取消', '取消-发货前', '取消-发货后'):
        seed_order(database, f'old-workflow-{workflow}', workflow_status=workflow)

    rows = bit_mysql.list_mercado_action_center_orders(token_ids=[7])

    assert {row['order_id'] for row in rows} == expected
    ensure_orders.assert_called_once_with(connection)
    ensure_tasks.assert_called_once_with(connection)
    assert connection.closed == 1
    assert 'NOW()' not in connection.executed[0][0]
    assert connection.executed[0][0].count('UTC_TIMESTAMP()') == 3


@pytest.mark.parametrize('topic', WATCH_TOPICS)
@pytest.mark.parametrize('task_status', ['open', 'in_progress'])
def test_old_terminal_order_remains_available_until_its_watch_is_resolved(order_query, topic, task_status):
    database, *_ = order_query
    seed_order(database, '123', status='delivered')
    seed_watch(database, '123', topic=topic, status=task_status)

    assert [row['order_id'] for row in bit_mysql.list_mercado_action_center_orders(token_ids=[7])] == ['123']

    database.execute("UPDATE mercado_operator_tasks SET status = 'resolved'")
    assert bit_mysql.list_mercado_action_center_orders(token_ids=[7]) == []


def test_active_watch_cannot_expand_authorized_scope_or_match_another_order(order_query):
    database, *_ = order_query
    for order_id in ('cancelled-watch', 'unrelated-topic', 'another-store', 'another-order'):
        seed_order(database, order_id, status='cancelled')
    seed_order(database, 'foreign', token_id=8, status='delivered')
    seed_watch(database, 'cancelled-watch', topic='order_not_delivered_30d')
    seed_watch(database, 'unrelated-topic', topic='marketplace_orders')
    seed_watch(database, 'another-store', token_id=8)
    seed_watch(database, 'missing-order')
    seed_watch(database, 'foreign', token_id=8)

    rows = bit_mysql.list_mercado_action_center_orders(token_ids=[7])

    assert [row['order_id'] for row in rows] == ['cancelled-watch']


def test_order_query_has_stable_pages_and_retains_json_payloads(order_query):
    database, connection, *_ = order_query
    for token_id, order_id in ((8, 'c'), (7, 'b'), (8, 'a'), (7, 'a'), (7, 'c')):
        seed_order(database, order_id, token_id=token_id,
                   raw={'shipping': {'status': 'shipped'}}, tracking={'status': 'in_transit'})
    rows = []
    for offset in range(0, 6, 2):
        rows.extend(bit_mysql.list_mercado_action_center_orders(token_ids=[8, 7, 7], limit=2, offset=offset))

    assert [(row['token_id'], row['order_id']) for row in rows] == [
        (7, 'a'), (7, 'b'), (7, 'c'), (8, 'a'), (8, 'c'),
    ]
    assert [params for _, params in connection.executed] == [(7, 8, 2, 0), (7, 8, 2, 2), (7, 8, 2, 4)]
    assert rows[0]['raw'] == {'shipping': {'status': 'shipped'}}
    assert rows[0]['tracking'] == {'status': 'in_transit'}
    assert rows[0]['date_created'] == '2026-08-23 04:00:00'
    assert 'raw_json' not in rows[0] and 'tracking_cache_json' not in rows[0]


def test_order_query_bounds_page_size_and_offset_and_requires_store_scope(order_query):
    _, connection, *_ = order_query
    assert bit_mysql.list_mercado_action_center_orders(token_ids=[], offset=1000) == []
    assert not connection.executed
    bit_mysql.list_mercado_action_center_orders(token_ids=[7], limit=5000, offset=-1)
    assert connection.executed[0][1] == (7, 1000, 0)


@pytest.mark.parametrize('offset', [0, 1000])
def test_api_order_query_forwards_page_offset(monkeypatch, offset):
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    request = MagicMock(return_value=[])
    monkeypatch.setattr(bit_db_api, '_request', request)

    bit_db_api.list_mercado_action_center_orders(token_ids=[7], limit=1000, offset=offset)

    request.assert_called_once_with('POST', '/api/db/mercado-action-center', timeout=20, json={
        'operation': 'list_orders', 'payload': {'token_ids': [7], 'limit': 1000, 'offset': offset},
    })


def test_mysql_api_wrapper_and_local_handler_forward_offset(monkeypatch):
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'mysql')
    query = MagicMock(return_value=[{'order_id': '123'}])
    monkeypatch.setattr(bit_mysql, 'list_mercado_action_center_orders', query)

    assert bit_db_api.list_mercado_action_center_orders(token_ids=[7], limit=1000, offset=2000) == [{'order_id': '123'}]
    query.assert_called_once_with(token_ids=[7], limit=1000, offset=2000)
    query.reset_mock()
    bit_db_api._mercado_action_center_local('list_orders', {'token_ids': [7]})
    query.assert_called_once_with(token_ids=[7], limit=500, offset=0)


def test_watch_deadline_is_json_serializable_over_the_database_api(monkeypatch):
    import requests

    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    bodies = []

    def request(method, url, **kwargs):
        prepared = requests.Request(method, url, json=kwargs['json']).prepare()
        bodies.append(json.loads(prepared.body))
        response = MagicMock(status_code=200, ok=True)
        response.json.return_value = {'status': 'success', 'data': 123}
        return response

    monkeypatch.setattr(bit_db_api.DB_API_SESSION, 'request', request)
    task = {'task_key': 'a' * 64, 'topic': 'order_not_delivered_30d', 'due_at': NOW}
    assert bit_db_api.upsert_mercado_operator_task(task) == 123
    assert bodies == [{'operation': 'upsert_task', 'payload': {'task': {
        **task, 'due_at': '2026-10-07T12:00:00',
    }}}]
    assert task['due_at'] == NOW


def task_connection(monkeypatch, previous_remark):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [
        {'id': 1, 'token_id': 7, 'resource': '123', 'topic': 'order_not_delivered_30d'},
        None if previous_remark is None else {'order_remark': previous_remark},
    ]
    monkeypatch.setattr(bit_mysql.pymysql, 'connect', lambda **kwargs: connection)
    monkeypatch.setattr(bit_mysql, '_ensure_mercado_action_center_tables', lambda cursor: None)
    monkeypatch.setattr(bit_mysql, 'get_mercado_operator_task', lambda task_id: {'id': task_id})
    return connection, cursor


def test_30_day_alert_handling_appends_order_remark_in_the_task_transaction(monkeypatch):
    connection, cursor = task_connection(monkeypatch, '原订单备注')

    assert bit_mysql.update_mercado_operator_task(1, actor='甲', status='resolved', note='已核实交付') == {'id': 1}

    writes = [(call.args[0], call.args[1]) for call in cursor.execute.call_args_list]
    remarks = [params for sql, params in writes if 'UPDATE `mercado_synced_orders`' in sql]
    assert len(remarks) == 1
    assert remarks[0][0].startswith('原订单备注\n[采购必办 ')
    assert remarks[0][0].endswith(' 甲] 已核实交付')
    assert remarks[0][1:] == (7, '123')
    assert any('UPDATE `mercado_operator_tasks`' in sql and params[0] == 'resolved' for sql, params in writes)
    assert any('INSERT INTO `mercado_operator_task_logs`' in sql and params[2:4] == ('note', '已核实交付') for sql, params in writes)
    connection.commit.assert_called_once()
    connection.rollback.assert_not_called()
    connection.close.assert_called_once()


@pytest.mark.parametrize('previous_remark,error', [(None, '关联订单不存在'), ('字' * 5000, '5000')])
def test_30_day_alert_remark_failure_rolls_back_task_status_and_logs(monkeypatch, previous_remark, error):
    connection, cursor = task_connection(monkeypatch, previous_remark)

    with pytest.raises(ValueError, match=error):
        bit_mysql.update_mercado_operator_task(1, actor='甲', status='resolved', note='已核实交付')

    assert any('UPDATE `mercado_operator_tasks`' in call.args[0] for call in cursor.execute.call_args_list)
    assert not any('INSERT INTO `mercado_operator_task_logs`' in call.args[0] for call in cursor.execute.call_args_list)
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
    connection.close.assert_called_once()


class TaskTransactionConnection(QueryConnection):
    def __init__(self, database):
        super().__init__(database)
        self.commits = 0
        self.rollbacks = 0
        self.fail_reopen_log = False

    def execute(self, sql, params):
        if self.fail_reopen_log and "'auto_reopen'" in sql:
            raise RuntimeError('log write failed')
        duplicate_insert = 'ON DUPLICATE KEY UPDATE' in sql
        sql = sql.replace('FOR UPDATE', '')
        sql = sql.replace('ON DUPLICATE KEY UPDATE `id` = LAST_INSERT_ID(`id`)',
                          'ON CONFLICT (`task_key`) DO UPDATE SET `id` = `id` RETURNING `id`')
        super().execute(sql, params)
        self.lastrowid = self.result.fetchone()['id'] if duplicate_insert else self.result.lastrowid

    def fetchone(self):
        row = self.result.fetchone()
        return dict(row) if row is not None else None

    def executemany(self, sql, params):
        self.result = self.database.executemany(sql.replace('%s', '?'), [
            [value.isoformat(' ') if isinstance(value, datetime) else value for value in values]
            for values in params
        ])

    def commit(self):
        self.database.commit()
        self.commits += 1

    def rollback(self):
        self.database.rollback()
        self.rollbacks += 1


@pytest.fixture
def task_storage(monkeypatch):
    database = sqlite3.connect(':memory:')
    database.row_factory = sqlite3.Row
    database.execute('''CREATE TABLE mercado_operator_tasks (
        id INTEGER PRIMARY KEY, task_key TEXT UNIQUE, organization_key TEXT,
        token_id INTEGER, event_id INTEGER, source TEXT, topic TEXT, resource TEXT, title TEXT,
        details TEXT, entry_url TEXT, priority TEXT, due_at TEXT, owner TEXT,
        status TEXT, resolution TEXT, created_at TEXT, updated_at TEXT
    )''')
    database.execute('''CREATE TABLE mercado_operator_task_logs (
        id INTEGER PRIMARY KEY, task_id INTEGER, actor TEXT, action TEXT,
        note TEXT, created_at TEXT
    )''')
    database.execute('''CREATE TABLE mercado_store_tokens (
        id INTEGER PRIMARY KEY, display_name TEXT, nickname TEXT
    )''')
    database.create_function('CONCAT', -1, lambda *values: ''.join(str(value) for value in values))
    connection = TaskTransactionConnection(database)
    monkeypatch.setattr(bit_mysql.pymysql, 'connect', lambda **kwargs: connection)
    monkeypatch.setattr(bit_mysql, '_ensure_mercado_action_center_tables', lambda cursor: None)
    yield database, connection
    database.close()


def watch_task(*, topic='order_not_delivered_30d', source='order_age_watch'):
    return {
        'task_key': 'a' * 64, 'organization_key': 'org', 'token_id': 7,
        'source': source, 'topic': topic, 'resource': '123', 'title': '订单告警',
        'due_at': NOW,
    }


@pytest.mark.parametrize('topic', WATCH_TOPICS)
@pytest.mark.parametrize('source', ['order_watch', 'procurement_watch', 'order_age_watch'])
def test_auto_resolved_order_watch_reopens_once_and_keeps_manual_assignment(task_storage, topic, source):
    database, connection = task_storage
    task = watch_task(topic=topic, source=source)
    task_id = bit_mysql.upsert_mercado_operator_task(task)
    database.execute("UPDATE mercado_operator_tasks SET owner = '甲', due_at = '2026-11-01 12:00:00'")
    database.execute('''INSERT INTO mercado_operator_task_logs
        (task_id, actor, action, note, created_at)
        VALUES (?, '甲', 'deadline', '人工改期', '2026-10-07 12:00:00')''', (task_id,))
    database.commit()
    assert bit_mysql.resolve_mercado_operator_tasks_by_keys([task['task_key']], '系统已确认完成') == 1

    assert bit_mysql.upsert_mercado_operator_task(task) == task_id
    assert bit_mysql.upsert_mercado_operator_task(task) == task_id

    saved = dict(database.execute('SELECT * FROM mercado_operator_tasks').fetchone())
    assert saved['status'] == 'open' and saved['resolution'] is None
    assert saved['owner'] == '甲' and saved['due_at'] == '2026-11-01 12:00:00'
    assert [row['action'] for row in database.execute('SELECT * FROM mercado_operator_task_logs ORDER BY id')] == [
        'deadline', 'auto_resolve', 'auto_reopen',
    ]
    assert connection.rollbacks == 0


def test_manual_completion_after_auto_resolution_prevents_reopening_even_with_later_notes(task_storage):
    database, _ = task_storage
    task = watch_task()
    task_id = bit_mysql.upsert_mercado_operator_task(task)
    bit_mysql.resolve_mercado_operator_tasks_by_keys([task['task_key']], '系统已确认完成')
    latest_time = database.execute('SELECT MAX(created_at) FROM mercado_operator_task_logs').fetchone()[0]
    database.executemany('''INSERT INTO mercado_operator_task_logs
        (task_id, actor, action, note, created_at) VALUES (?, '甲', ?, '人工处理', ?)''',
                         [(task_id, action, latest_time) for action in ('status', 'note')])
    database.commit()

    bit_mysql.upsert_mercado_operator_task(task)

    assert database.execute('SELECT status FROM mercado_operator_tasks').fetchone()[0] == 'resolved'
    assert database.execute("SELECT COUNT(*) FROM mercado_operator_task_logs WHERE action = 'auto_reopen'").fetchone()[0] == 0


@pytest.mark.parametrize('latest_action', ['note', 'assign', 'deadline'])
def test_non_status_handling_after_auto_resolution_keeps_automatic_reopening(task_storage, latest_action):
    database, _ = task_storage
    task = watch_task()
    task_id = bit_mysql.upsert_mercado_operator_task(task)
    bit_mysql.resolve_mercado_operator_tasks_by_keys([task['task_key']], '系统已确认完成')
    latest_time = database.execute('SELECT MAX(created_at) FROM mercado_operator_task_logs').fetchone()[0]
    database.execute('''INSERT INTO mercado_operator_task_logs
        (task_id, actor, action, note, created_at) VALUES (?, '甲', ?, '人工处理', ?)''',
                     (task_id, latest_action, latest_time))
    database.commit()

    bit_mysql.upsert_mercado_operator_task(task)

    assert database.execute('SELECT status FROM mercado_operator_tasks').fetchone()[0] == 'open'
    assert database.execute("SELECT COUNT(*) FROM mercado_operator_task_logs WHERE action = 'auto_reopen'").fetchone()[0] == 1


@pytest.mark.parametrize('changes', [
    {'topic': 'marketplace_orders'}, {'source': 'mercado_notification'},
])
def test_automatic_reopening_is_limited_to_order_watch_tasks(task_storage, changes):
    database, _ = task_storage
    task = watch_task(**changes)
    bit_mysql.upsert_mercado_operator_task(task)
    bit_mysql.resolve_mercado_operator_tasks_by_keys([task['task_key']], '自动结束')

    bit_mysql.upsert_mercado_operator_task(task)

    assert database.execute('SELECT status FROM mercado_operator_tasks').fetchone()[0] == 'resolved'
    assert database.execute('SELECT COUNT(*) FROM mercado_operator_task_logs').fetchone()[0] == 1


def test_reopening_log_failure_rolls_back_status_change(task_storage):
    database, connection = task_storage
    task = watch_task()
    bit_mysql.upsert_mercado_operator_task(task)
    database.execute("UPDATE mercado_operator_tasks SET due_at = '2026-10-07 04:00:00'")
    database.commit()
    bit_mysql.resolve_mercado_operator_tasks_by_keys([task['task_key']], '自动结束')
    connection.fail_reopen_log = True
    commits_before = connection.commits

    with pytest.raises(RuntimeError, match='log write failed'):
        bit_mysql.upsert_mercado_operator_task(task)

    saved = dict(database.execute('SELECT * FROM mercado_operator_tasks').fetchone())
    assert saved['status'] == 'resolved' and saved['resolution'] == '自动结束'
    assert saved['due_at'] == '2026-10-07 04:00:00'
    assert database.execute('SELECT COUNT(*) FROM mercado_operator_task_logs').fetchone()[0] == 1
    assert connection.commits == commits_before
    assert connection.rollbacks == 1


@pytest.mark.parametrize('previous_state', ['open', 'auto_resolved', 'manually_resolved'])
def test_default_deadline_corrects_legacy_eight_hour_error_and_keeps_owner(task_storage, previous_state):
    database, _ = task_storage
    task = watch_task()
    task_id = bit_mysql.upsert_mercado_operator_task({**task, 'due_at': NOW - timedelta(hours=8)})
    database.execute("UPDATE mercado_operator_tasks SET owner = '甲'")
    if previous_state == 'manually_resolved':
        database.execute("UPDATE mercado_operator_tasks SET status = 'resolved'")
        database.execute('''INSERT INTO mercado_operator_task_logs
            (task_id, actor, action, note, created_at)
            VALUES (?, '甲', 'status', '人工完成', '2026-10-07 12:00:00')''', (task_id,))
    database.commit()
    if previous_state == 'auto_resolved':
        bit_mysql.resolve_mercado_operator_tasks_by_keys([task['task_key']], '自动结束')

    assert bit_mysql.upsert_mercado_operator_task(task) == task_id

    saved = dict(database.execute('SELECT * FROM mercado_operator_tasks').fetchone())
    assert saved['due_at'] == '2026-10-07 12:00:00'
    assert saved['owner'] == '甲'
    assert saved['status'] == ('resolved' if previous_state == 'manually_resolved' else 'open')


def test_any_previous_manual_deadline_record_preserves_deadline_after_later_logs(task_storage):
    database, _ = task_storage
    task = watch_task()
    task_id = bit_mysql.upsert_mercado_operator_task(task)
    database.execute("UPDATE mercado_operator_tasks SET due_at = '2026-11-01 12:00:00'")
    database.executemany('''INSERT INTO mercado_operator_task_logs
        (task_id, actor, action, note, created_at)
        VALUES (?, '甲', ?, '人工处理', '2026-10-07 12:00:00')''',
                         [(task_id, action) for action in ('deadline', 'note', 'assign')])
    database.commit()

    bit_mysql.upsert_mercado_operator_task(task)

    assert database.execute('SELECT due_at FROM mercado_operator_tasks').fetchone()[0] == '2026-11-01 12:00:00'


def test_operator_task_query_paginates_all_authorized_tasks_with_logs(task_storage):
    database, connection = task_storage
    database.executemany('''INSERT INTO mercado_operator_tasks
        (id, organization_key, token_id, topic, status, due_at)
        VALUES (?, 'org', 7, 'marketplace_orders', 'open', '2026-10-01 12:00:00')''',
                         [(task_id,) for task_id in range(1, 1002)])
    for task_id, organization, token_id, topic, status in (
        (2001, 'other', 7, 'order_not_delivered_30d', 'open'),
        (2002, 'org', 8, 'order_not_delivered_30d', 'open'),
        (2003, 'org', 7, 'order_not_delivered_30d', 'resolved'),
        (2004, 'org', 7, 'promotion_deadline', 'open'),
    ):
        database.execute('''INSERT INTO mercado_operator_tasks
            (id, organization_key, token_id, topic, status, due_at)
            VALUES (?, ?, ?, ?, ?, '2026-10-01 12:00:00')''',
                         (task_id, organization, token_id, topic, status))
    database.executemany('''INSERT INTO mercado_operator_task_logs
        (task_id, actor, action, note, created_at)
        VALUES (?, '甲', 'note', '已联系供应商', '2026-10-07 12:00:00')''', [(1,), (1001,)])
    database.commit()

    first = bit_mysql.list_mercado_operator_tasks(token_ids=[7], organization_key='org', limit=1000)
    second = bit_mysql.list_mercado_operator_tasks(token_ids=[7], organization_key='org', limit=1000, offset=1000)

    assert [row['id'] for row in first['rows'] + second['rows']] == list(range(1001, 0, -1))
    assert first['total'] == first['open'] == 1000
    assert second['total'] == second['open'] == 1
    assert first['rows'][0]['logs'][0]['note'] == second['rows'][0]['logs'][0]['note'] == '已联系供应商'
    assert first['rows'][0]['store_name'] == '店铺 #7'
    task_queries = [(sql, params) for sql, params in connection.executed if 'LEFT JOIN `mercado_store_tokens`' in sql]
    assert [params for _, params in task_queries] == [('org', 7, 1000, 0), ('org', 7, 1000, 1000)]
    assert all('t.`id` DESC LIMIT %s OFFSET %s' in sql for sql, _ in task_queries)
    closed = bit_mysql.list_mercado_operator_tasks(
        token_ids=[7], organization_key='org', include_closed=True, limit=1000, offset=1001,
    )
    assert [row['id'] for row in closed['rows']] == [2003]


def test_operator_task_query_preserves_empty_scope_and_bounds_pages(task_storage):
    _, connection = task_storage
    assert bit_mysql.list_mercado_operator_tasks(token_ids=[], offset=1000) == {
        'rows': [], 'total': 0, 'open': 0, 'overdue': 0,
    }
    assert not connection.executed
    bit_mysql.list_mercado_operator_tasks(token_ids=[7], limit=5000, offset=-1)
    assert connection.executed[0][1] == (7, 1000, 0)


@pytest.mark.parametrize('offset', [0, 1000])
def test_api_operator_task_query_forwards_page_offset(monkeypatch, offset):
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    request = MagicMock(return_value={'rows': []})
    monkeypatch.setattr(bit_db_api, '_request', request)

    bit_db_api.list_mercado_operator_tasks(
        token_ids=[7], organization_key='org', include_closed=True, limit=1000, offset=offset,
    )

    request.assert_called_once_with('POST', '/api/db/mercado-action-center', timeout=20, json={
        'operation': 'list_tasks', 'payload': {
            'token_ids': [7], 'organization_key': 'org', 'include_closed': True,
            'limit': 1000, 'offset': offset,
        },
    })


def test_mysql_operator_task_wrapper_and_handler_forward_offset(monkeypatch):
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'mysql')
    query = MagicMock(return_value={'rows': []})
    monkeypatch.setattr(bit_mysql, 'list_mercado_operator_tasks', query)

    bit_db_api.list_mercado_operator_tasks(token_ids=[7], organization_key='org', limit=1000, offset=2000)

    query.assert_called_once_with(token_ids=[7], organization_key='org', include_closed=False, limit=1000, offset=2000)
    query.reset_mock()
    bit_db_api._mercado_action_center_local('list_tasks', {'token_ids': [7]})
    query.assert_called_once_with(token_ids=[7], organization_key='', include_closed=False, limit=300, offset=0)
