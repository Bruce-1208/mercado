"""Regressions for real account failures and the publish retry SQL."""
from contextlib import nullcontext
import ast
import concurrent.futures
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from erp import ai_original_products as original


@pytest.mark.parametrize('status,available,match', [
    (200, True, None), (200, False, '余额不足'),
    (401, None, '失效'), (402, None, '余额不足'), (403, None, '权限'),
    (200, None, '无法核验'),
])
def test_account_preflight(status, available, match):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=status, raise_for_status=lambda: None,
                               json=lambda: {'is_available': available})
    if match:
        with pytest.raises(original.OriginalModelAccountError, match=match):
            original.check_original_model_account(api_key='test', base_url='https://api.deepseek.com', http_get=get)
    else:
        original.check_original_model_account(api_key='test', base_url='https://api.deepseek.com', http_get=get)
    assert calls == ['https://api.deepseek.com/user/balance']


@pytest.mark.parametrize('url', ['https://other.example/v1', 'https://api.deepseek.com.evil.test', 'http://api.deepseek.com'])
def test_preflight_does_not_send_other_provider_credentials(url):
    original.check_original_model_account(api_key='test', base_url=url,
        http_get=lambda *a, **k: pytest.fail('must not send credentials'))


def test_preflight_network_failure_is_not_reported_as_insufficient_balance():
    def get(*a, **k):
        raise requests.Timeout('timeout')
    with pytest.raises(original.OriginalModelAccountError, match='无法核验'):
        original.check_original_model_account(api_key='test', base_url='https://api.deepseek.com', http_get=get)


def test_account_failure_is_recorded_independently_for_remaining_products(monkeypatch):
    node = next(n for n in ast.parse(Path('bit/bit_interface.py').read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == '_run_ai_original_task')
    saved = []
    attempted = []
    calls = []
    state = {}
    class PaymentError(RuntimeError):
        status_code = 402
    def model(*a, **k):
        calls.append(1)
        raise PaymentError('Insufficient Balance')
    def prepare(row, **kwargs):
        attempted.append(row['id'])
        kwargs['chat']([])
    from erp import ai_original_cost_control as scheduling
    monkeypatch.setattr(scheduling, 'product_generation_lock', lambda pid: nullcontext(lambda: None))
    monkeypatch.setattr(original, '_listing_json_chat', lambda **kw: model)
    monkeypatch.setattr(original, 'prepare_ai_original_product', prepare)
    namespace = dict(json=json, logging=logging,
        ThreadPoolExecutor=concurrent.futures.ThreadPoolExecutor,
        as_completed=concurrent.futures.as_completed,
        _ai_original_task_lock=threading.Lock(), _ai_original_task_state=state,
        db_get_mercado_product_items_by_ids=lambda ids: [{'id': ids[0], 'source_type': 'ai_original'}],
        db_update_ai_original_product=lambda pid, changes: saved.append(pid))
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<worker>', 'exec'), namespace)
    namespace['_run_ai_original_task']([{'id': i} for i in range(1, 5)], workers=1)
    assert attempted == [1, 2, 3, 4]
    assert saved == [1, 1, 2, 2, 3, 3, 4, 4]
    assert calls == [1, 1, 1, 1]
    assert state['running'] is False
    assert state['processed_count'] == 4
    assert all('余额不足' in result['message'] for result in state['results'])
    assert sorted(r['status'] for r in state['results']) == ['failed', 'failed', 'failed', 'failed']


def test_retry_query_interpolates_with_real_pymysql(monkeypatch):
    from pymysql.connections import Connection
    from erp import mercadolibre_collection_store as store
    # Use the actual driver's interpolation, with no network/database writes.
    db = Connection(defer_connect=True)
    db.server_status = 0
    driver_cursor = db.cursor()
    queries = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params):
            queries.append(driver_cursor.mogrify(query, params))
        def fetchall(self): return []
    connection = SimpleNamespace(cursor=Cursor, commit=lambda: None,
                                 rollback=lambda: None, close=lambda: None)
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda c: None)
    assert store.claim_due_rate_limited_product_publish_records(connection_factory=lambda: connection) == []
    assert len(queries) == 1
    assert "LIKE '%get /%'" in queries[0]
    assert 'LIMIT 50 FOR UPDATE' in queries[0]
