import json
import tracemalloc

import pytest

from bit import bit_mysql, bounded_records
from bit.infringement_state import canonical_record, content_hash, result_state


def record(row_id, **fields):
    return dict(source_row_id=row_id, row_id=row_id, source_type='product_list',
                title=f'Product {row_id}', product_id=str(row_id), **fields)


class Database:
    def __init__(self, rows, fail=False):
        self.rows, self.calls, self.closed, self.fail = rows, [], False, fail
        self.page = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params):
        assert ' > %s' in sql and sql.endswith(' LIMIT %s')
        assert 'OFFSET' not in sql
        assert 0 < params[-1] <= 200
        self.calls.append(params)
        if self.fail:
            raise RuntimeError('database unavailable')
        self.page = [row for row in self.rows if row['source_row_id'] > params[-2]][:params[-1]]

    def fetchall(self):
        return self.page

    def commit(self):
        pass

    def close(self):
        self.closed = True


def database(monkeypatch, rows, **kwargs):
    db = Database(rows, **kwargs)
    monkeypatch.setattr(bit_mysql.pymysql, 'connect', lambda **kwargs: db)
    monkeypatch.setattr(bit_mysql, '_ensure_infringement_risk_checks_table', lambda c: None)
    monkeypatch.setattr('erp.mercadolibre_collection_store.ensure_collection_tables', lambda c: None)
    return db


def test_candidates_page_past_completed_rows_and_apply_global_limit(monkeypatch):
    rows = []
    for i in range(1, 8):
        row = record(i, risk_level=0, rule_version='v1', detection_scope='title')
        if i < 5:
            row['content_hash'] = content_hash(row)
        rows.append(row)
    db = database(monkeypatch, rows)
    actual = bit_mysql.get_infringement_risk_candidates(
        sources=['product_list', 'collection_list'], limit=2, rule_version='v1')
    assert [row['row_id'] for row in actual] == [5, 6]
    assert [call[-2] for call in db.calls] == [0, 2, 4]
    assert all(call[0] == 'product_list' for call in db.calls)
    assert db.closed


def test_candidate_pages_have_no_duplicates_or_gaps_and_close_early(monkeypatch):
    db = database(monkeypatch, [record(i) for i in range(1, 452)])
    rows = bit_mysql.get_infringement_risk_candidates(sources=['product_list'], include_checked=True)
    assert [r['row_id'] for r in rows] == list(range(1, 452))
    assert [call[-2] for call in db.calls] == [0, 200, 400]
    assert db.closed
    db.closed = False
    stream = bit_mysql.get_infringement_risk_candidates(sources=['product_list'], _stream=True)
    next(stream)
    stream.close()
    assert db.closed


def test_database_error_closes_connection(monkeypatch):
    db = database(monkeypatch, [], fail=True)
    with pytest.raises(RuntimeError, match='unavailable'):
        bit_mysql.get_infringement_risk_candidates(sources=['product_list'])
    assert db.closed


def test_compact_candidates_preserve_current_content_hash_and_reviews(monkeypatch):
    row = record(1, source_snapshot_json=json.dumps({
        'ai_original': {'title_es': 'Caja', 'title_pt': 'Caixa',
                        'image_content_revision': 'r2', 'preparation': {'image_fingerprint': 'fp'}},
        'plugin_snapshot': {'token_id': 7, 'store_name': 'Store', 'site_id': 'MLM'},
        'unused_payload': 'x' * 100000,
    }), risk_level=0, rule_version='v1', detection_scope='title_image',
        review_history_json='[{"decision":"allow"}]')
    row['content_hash'] = content_hash(row, 'title_image')
    database(monkeypatch, [row])
    actual = bit_mysql.get_infringement_risk_candidates(
        sources=['product_list'], include_checked=True, detection_scope='title_image', rule_version='v1')[0]
    assert 'source_snapshot_json' not in actual
    assert actual['title_es'] == 'Caja' and actual['token_id'] == 7
    assert actual['review_history'] == [{'decision': 'allow'}]
    assert content_hash(actual, 'title_image') == row['content_hash']
    assert result_state(actual, 'v1', 'title_image') == 'completed'


@pytest.mark.parametrize('reverse', [True, False])
def test_top_results_equal_stable_full_sort_with_exact_summary(monkeypatch, reverse):
    rows = [record(i, risk_level=i % 3, content_hash='outdated') for i in range(1, 501)]
    seen = []
    def candidates(**kwargs):
        assert kwargs['_stream'] is True
        for row in rows:
            seen.append(row['row_id'])
            yield dict(row)
    monkeypatch.setattr(bit_mysql, 'get_infringement_risk_candidates', candidates)
    result = bit_mysql.get_infringement_risk_results(
        rule_version='v1', limit=17, sort_dir='desc' if reverse else 'asc')
    expected = sorted(rows, key=lambda r: r['risk_level'], reverse=reverse)[:17]
    assert [r['row_id'] for r in result['rows']] == [r['row_id'] for r in expected]
    assert result['total'] == result['stale'] == len(rows) == len(seen)


def test_materialization_guard_raises_without_silent_truncation_and_closes(monkeypatch):
    monkeypatch.setattr(bounded_records, 'MAX_MATERIALIZED_RECORDS', 3)
    db = database(monkeypatch, [record(i) for i in range(1, 6)])
    with pytest.raises(ValueError, match='分批'):
        bit_mysql.get_infringement_risk_candidates(sources=['product_list'])
    assert db.closed
    with pytest.raises(ValueError, match='分批'):
        bit_mysql.get_infringement_risk_results(rule_version='v1', sources=['product_list'], limit=0)
    assert db.closed


def test_result_selection_memory_does_not_scale_with_input_count():
    def rows():
        for i in range(100000):
            yield {'id': i, 'payload': str(i) + 'x' * 4096}
    tracemalloc.start()
    try:
        result = bounded_records.select_bounded(rows(), limit=30, key=lambda row: row['id'], reverse=True)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert [r['id'] for r in result] == list(range(99999, 99969, -1))
    # Materializing the source would need over 400 MB; retain only 30 rows.
    assert peak < 2 * 1024 * 1024
