from contextlib import contextmanager
from unittest.mock import Mock
import threading

import pytest
from bit import bit_zying_caiji as collector


def setup_frontend(monkeypatch, responses=None):
    frontend = Mock()
    frontend.call_api_many.side_effect = responses or (
        lambda token, command, rows: [{'ok': True, 'response': {'code': 200, 'data': row}} for row in rows])
    @contextmanager
    def session():
        yield frontend
    monkeypatch.setattr(collector, '_zying_frontend_signer_session', session)
    return frontend


def test_reads_use_bounded_batches_and_preserve_order(monkeypatch):
    frontend = setup_frontend(monkeypatch)
    rows = [{'id': i} for i in range(17)]
    assert collector._zying_api_read_many('token', 'sale.detail', rows) == rows
    assert [len(c.args[2]) for c in frontend.call_api_many.call_args_list] == [6, 6, 5]


def test_only_failed_read_is_retried(monkeypatch):
    frontend = setup_frontend(monkeypatch, [[
        {'ok': True, 'response': {'code': 200, 'data': {'id': 1}}},
        {'ok': False, 'status': 503, 'error': 'temporary'},
    ]])
    frontend.call_api.return_value = {'code': 200, 'data': {'id': 2}}
    assert collector._zying_api_read_many('token', 'sale.detail', [{'id': 1}, {'id': 2}]) == [{'id': 1}, {'id': 2}]
    frontend.call_api.assert_called_once_with('token', 'sale.detail', {'id': 2})


@pytest.mark.parametrize('code,error', [(401, collector.ZyingAuthenticationError), (429, collector.ZyingRateLimitError)])
def test_login_or_rate_limit_stops_before_retry_or_next_batch(monkeypatch, code, error):
    frontend = setup_frontend(monkeypatch, [[
        {'ok': False, 'status': 503, 'error': 'temporary'},
        {'ok': False, 'status': code, 'error': 'stop'},
    ]])
    with pytest.raises(error):
        collector._zying_api_read_many('token', 'sale.detail', [{'id': i} for i in range(9)])
    assert frontend.call_api_many.call_count == 1
    frontend.call_api.assert_not_called()


def test_stop_between_batches_does_not_issue_more_reads(monkeypatch):
    event = threading.Event()
    def responses(token, command, rows):
        event.set()
        return [{'ok': True, 'response': {'code': 200, 'data': row}} for row in rows]
    frontend = setup_frontend(monkeypatch, responses)
    with collector._zying_collection_stop_scope(event), pytest.raises(collector.ZyingCollectionStopped):
        collector._zying_api_read_many('token', 'sale.detail', [{'id': i} for i in range(12)])
    assert frontend.call_api_many.call_count == 1


def test_batch_never_accepts_write_commands(monkeypatch):
    frontend = setup_frontend(monkeypatch)
    with pytest.raises(ValueError):
        collector._zying_api_read_many('token', 'sale.update', [{'id': 1}])
    frontend.call_api_many.assert_not_called()


def test_category_cache_reuses_across_pages_but_not_across_runs(monkeypatch):
    read = Mock(return_value=[{'root': [{'cate_cateid': 'CBT1', 'cate_name': 'Example'}]}])
    monkeypatch.setattr(collector, '_zying_api_read_many', read)
    def record():
        return {'product_id': '1', '_category_site': 'CBT', '_category_id': '1'}
    @collector._reuse_zying_frontend_signer
    def run():
        for _ in range(2):
            rows = [record(), record()]
            collector._enrich_product_categories('token', rows)
            assert [r['product_category_id'] for r in rows] == ['CBT1', 'CBT1']
    run()
    assert read.call_count == 1
    run()
    assert read.call_count == 2
    assert collector._ZYING_RUN_CATEGORY_CACHE.get() is None


def test_missing_detail_fails_before_category_or_save(monkeypatch):
    monkeypatch.setattr(collector, '_zying_api_read_many', lambda *a: [{'root': []}])
    category = Mock()
    monkeypatch.setattr(collector, '_enrich_product_categories', category)
    with pytest.raises(RuntimeError, match='本页未入库'):
        collector._enrich_product_records(None, [{'product_id': '1'}], token='token')
    category.assert_not_called()
