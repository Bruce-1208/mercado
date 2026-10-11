import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest
import pymysql
from flask import Flask, g, jsonify, session

from bit import order_read_cache as cache
from bit.database_availability import install_database_error_handlers, unavailable_response
from bit.db_pool import PoolAcquireTimeout


def test_unrelated_order_queries_do_not_wait_on_hash_collisions(monkeypatch):
    cache.invalidate()
    monkeypatch.setattr(cache, 'hash', lambda _: 0, raising=False)
    entered, release = threading.Event(), threading.Event()
    def slow(**params):
        entered.set()
        assert release.wait(3)
        return {'rows': [params]}
    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(cache.read, slow, {'page': 1}, scope={'id': 1})
        assert entered.wait(1)
        try:
            second = executor.submit(cache.read, lambda **p: p, {'page': 2}, scope={'id': 2})
            assert second.result(timeout=1) == {'page': 2}
        finally:
            release.set()
        first.result(timeout=2)
    assert not cache._FLIGHTS


def test_identical_order_reads_coalesce_and_return_independent_copies():
    cache.invalidate()
    start = threading.Barrier(12)
    loader = Mock(return_value={'rows': [{'id': 1}]})
    def read():
        start.wait(timeout=3)
        return cache.read(loader, {'page': 1}, scope={'id': 1})
    with ThreadPoolExecutor(12) as executor:
        results = list(executor.map(lambda _: read(), range(12)))
    assert loader.call_count == 1
    results[0]['rows'].clear()
    assert all(value['rows'] for value in results[1:])
    assert not cache._FLIGHTS


def test_failed_and_invalidated_flights_never_poison_cache():
    cache.invalidate()
    def invalidating(**params):
        cache.invalidate()
        return {'old': True}
    cache.read(invalidating, {})
    loader = Mock(return_value={'new': True})
    assert cache.read(loader, {}) == {'new': True}
    assert loader.call_count == 1
    cache.invalidate()
    with pytest.raises(RuntimeError):
        cache.read(Mock(side_effect=RuntimeError('failed')), {})
    assert not cache._FLIGHTS
    assert cache.read(loader, {}) == {'new': True}
    assert loader.call_count == 2


@pytest.mark.parametrize('error', [PoolAcquireTimeout('busy'), pymysql.InterfaceError(0, ''),
    pymysql.OperationalError(2003, 'connection refused'), pymysql.OperationalError(2013, 'lost')])
def test_auth_outage_returns_503_without_session_loss_or_recursive_db_read(error):
    app = Flask(__name__)
    app.secret_key = 'test'
    install_database_error_handlers(app)
    source = Path('bit/bit_interface.py').read_text()
    fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'get_current_workbench_user')
    lookup = Mock(side_effect=error)
    namespace = dict(g=g, session=session, USE_DB_API=False, get_workbench_user=lookup,
                     build_workbench_session_user=lambda row: row)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<auth>', 'exec'), namespace)
    get_user = namespace['get_current_workbench_user']
    @app.before_request
    def auth():
        get_user()
    @app.after_request
    def response_hook(response):
        get_user()
        return response
    @app.get('/orders')
    def orders():
        return jsonify(status='success')
    client = app.test_client()
    with client.session_transaction() as state:
        state['workbench_user'] = {'id': 1, 'access_version': 1}
    response = client.get('/orders')
    assert response.status_code == 503
    assert response.json['code'] == 'database_unavailable'
    assert response.headers['Retry-After'] == '2'
    assert response.headers['Cache-Control'] == 'no-store'
    assert lookup.call_count == 1
    with client.session_transaction() as state:
        assert state['workbench_user']['id'] == 1
    lookup.side_effect = None
    lookup.return_value = {'id': 1, 'is_active': True, 'access_version': 1}
    assert client.get('/orders').status_code == 200
    assert lookup.call_count == 2


def test_programming_error_not_disguised_as_outage_and_writes_not_retried():
    app = Flask(__name__)
    with app.test_request_context('/write', method='POST'):
        assert unavailable_response(pymysql.OperationalError(1054, 'unknown column')) is None
        response = unavailable_response(pymysql.OperationalError(2013, 'lost'))
        assert response.status_code == 503
        assert 'Retry-After' not in response.headers


def test_order_schema_initialization_only_cached_after_commit(monkeypatch):
    from bit import bit_mysql as db
    from bit import yandex_order_sync
    from erp import mercadolibre_profitability_cache as rates, mercadolibre_store_link_store as links
    db._order_list_schema_sources.clear()
    target = ['database-a']
    monkeypatch.setattr(db, '_column_check_key', lambda *a: target[0])
    ensure = Mock()
    for name in ('_ensure_mercado_synced_orders_table', '_ensure_mercado_store_tokens_table', '_ensure_mercado_store_site_settings_table'):
        monkeypatch.setattr(db, name, ensure)
    monkeypatch.setattr(rates, 'ensure_profitability_cache_tables', ensure)
    monkeypatch.setattr(links, 'ensure_store_link_table', ensure)
    monkeypatch.setattr(yandex_order_sync, 'store_source_sql', lambda q: 'stores')
    c = Mock()
    c.commit.side_effect = RuntimeError('commit failed')
    with pytest.raises(RuntimeError):
        db._prepare_order_list_schema(c, object())
    assert not db._order_list_schema_sources
    c.commit.side_effect = None
    ensure.reset_mock()
    assert db._prepare_order_list_schema(c, object()) == 'stores'
    assert ensure.call_count == 5
    assert db._prepare_order_list_schema(c, object()) == 'stores'
    assert ensure.call_count == 5
    target[0] = 'database-b'
    db._prepare_order_list_schema(c, object())
    assert ensure.call_count == 10
    db._order_list_schema_sources.clear()
