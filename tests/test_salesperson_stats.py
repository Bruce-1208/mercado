import sqlite3
from functools import wraps

import pytest
from flask import Flask, request
from bit.salesperson_stats import build_query
from bit import bit_db_api, salesperson_stats_routes


def query_rows(start='', end='', scope=None):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE mercado_order_operation_logs (operator_id INTEGER, operator_name TEXT,
            order_id TEXT, action_type TEXT, created_at TEXT);
        CREATE TABLE mercado_synced_orders (order_id TEXT, token_id INTEGER);
        CREATE TABLE mercado_store_tokens (id INTEGER);
        INSERT INTO mercado_store_tokens VALUES (11), (22);
        INSERT INTO mercado_synced_orders VALUES ('A',11), ('B',11), ('C',22);
    ''')
    logs = [
        (1, '张三', 'A', 'label_printed', '2026-10-01 10:00:00'),
        (1, '张三', 'A', 'label_printed', '2026-10-02 10:00:00'),
        (1, '张三', 'A', 'purchase_created', '2026-10-01 11:00:00'),
        (1, '张三', 'A', 'purchase_created', '2026-10-02 11:00:00'),
        (1, '张三', 'B', 'purchase_updated', '2026-10-02 11:00:00'),
        (1, '张三', 'B', 'label_printed', '2026-10-02 23:59:59'),
        (2, '张三', 'A', 'label_printed', '2026-10-02 11:00:00'),
        (1, '张三', 'C', 'purchase_created', '2026-10-02 11:00:00'),
        (None, '系统', 'B', 'label_printed', '2026-10-02 11:00:00'),
    ]
    db.executemany('INSERT INTO mercado_order_operation_logs VALUES (?,?,?,?,?)', logs)
    sql, params = build_query(start, end, scope)
    try:
        return [dict(row) for row in db.execute(sql.replace('%s', '?'), params)]
    finally:
        db.close()


def test_lifetime_dedup_and_actor_identity():
    rows = query_rows(scope={11})
    assert [(r['operator_id'], r['print_count'], r['purchase_count']) for r in rows] == [(1, 2, 1), (2, 1, 0)]


def test_dates_apply_after_dedup_and_include_full_end_day():
    rows = query_rows('2026-10-02', '2026-10-02', {11})
    assert [(r['print_count'], r['purchase_count']) for r in rows] == [(1, 0), (1, 0)]


def test_scope_empty_and_other_company():
    assert query_rows(scope=[]) == []
    assert query_rows(scope={22})[0]['purchase_count'] == 1
    assert query_rows(scope={22})[0]['print_count'] == 0


@pytest.mark.parametrize('start,end', [('bad', ''), ('2026-02-30', ''), ('2026-10-03', '2026-10-01')])
def test_invalid_dates(start, end):
    with pytest.raises(ValueError):
        build_query(start, end)


def test_routes_enforce_login_permission_and_server_scope(monkeypatch):
    app = Flask(__name__)
    calls = []
    def auth(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if request.headers.get('X-Auth') != 'yes':
                return {}, 401
            return fn(*args, **kwargs)
        return wrapped
    monkeypatch.setattr(bit_db_api, 'list_salesperson_stats', lambda *args, **kwargs: calls.append((args, kwargs)) or {'rows': []})
    salesperson_stats_routes.register_routes(app, login_required=auth, internal_api_required=auth,
        authorized_token_ids=lambda: {11}, current_user=lambda: {},
        has_permission=lambda *_: request.headers.get('X-Permission') == 'yes',
        reject_client_mode=lambda: None)
    client = app.test_client()
    assert client.get('/api/salesperson-stats').status_code == 401
    assert client.get('/api/salesperson-stats', headers={'X-Auth': 'yes'}).status_code == 403
    response = client.get('/api/salesperson-stats?allowed_token_id=22', headers={'X-Auth': 'yes', 'X-Permission': 'yes'})
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert calls[-1][1]['allowed_token_ids'] == {11}
    response = client.get('/api/db/salesperson-stats?scope_limited=1', headers={'X-Auth': 'yes'})
    assert response.status_code == 200
    assert calls[-1][1]['allowed_token_ids'] == set()


def test_client_preserves_empty_scope(monkeypatch):
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    calls = []
    monkeypatch.setattr(bit_db_api, '_request', lambda *a, **k: calls.append((a, k)))
    bit_db_api.list_salesperson_stats(allowed_token_ids=set())
    assert calls[0][1]['params']['scope_limited'] == '1'
    assert calls[0][1]['params']['allowed_token_id'] == []
