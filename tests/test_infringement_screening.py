import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from bit import bit_check_risk, bit_db_api, bit_mysql
from bit.infringement_state import (
    canonical_record, content_hash, knowledge_version, normalize_items,
    result_state, review_is_current,
)


def product(row_id=1, title='Generic storage box', **fields):
    return {
        'id': row_id, 'row_id': row_id, 'source_row_id': row_id,
        'source_type': 'product_list', 'product_id': f'MLM{row_id}',
        'record_key': f'product_list:{row_id}', 'title': title,
        'category_id': 'MLM1', 'category_name': 'Boxes', **fields,
    }


def test_current_row_hash_is_identical_to_candidate_and_changes_with_final_titles():
    row = product(source_snapshot_json=json.dumps({'ai_original': {'title_es': 'Caja', 'title_pt': 'Caixa'}}))
    candidate = canonical_record(row)
    assert content_hash(row) == content_hash(candidate)
    row['infringement_check_json'] = json.dumps({'title': 'old saved title', 'risk_level': 0})
    assert content_hash(row) == content_hash(candidate)
    row['source_snapshot_json'] = json.dumps({'ai_original': {'title_es': 'Nike caja', 'title_pt': 'Caixa'}})
    assert content_hash(row) != content_hash(candidate)


def test_result_state_reads_snapshot_and_never_reuses_changed_content_or_rules():
    row = product()
    row['infringement_check_json'] = json.dumps({
        'risk_level': 0, 'content_hash': content_hash(row), 'rule_version': 'v1',
        'detection_scope': 'title', 'check_status': 'completed',
    })
    assert result_state(row, 'v1') == 'completed'
    assert result_state(row, 'v2') == 'stale'
    assert result_state(row, 'v1', 'title_image') == 'stale'
    row['title'] = 'Nike box'
    assert result_state(row, 'v1') == 'stale'
    row['infringement_check_json'] = json.dumps({'check_status': 'failed', 'risk_level': None})
    assert result_state(row) == 'failed'
    assert result_state(product()) == 'unchecked'


def test_review_is_bound_to_content_expiry_and_site():
    now = datetime.now(timezone.utc)
    row = product(manual_decision='allow', review_site_ids=['MLM'], review_expires_at=(now + timedelta(hours=1)).isoformat())
    row['review_content_hash'] = content_hash(row, 'title_image')
    assert review_is_current(row, now=now, site_id='MLM')
    assert not review_is_current(row, now=now, site_id='MLB')
    assert not review_is_current(row, now=now + timedelta(hours=2))
    row['title'] = 'changed title'
    assert not review_is_current(row, now=now)


def test_item_identity_deduplicates_per_source_not_numeric_id():
    items = [{'source_type': 'product_list', 'row_id': 1}, {'source_type': 'collection_list', 'row_id': 1}]
    assert normalize_items(items + items) == items
    with pytest.raises(ValueError):
        normalize_items([{'source_type': 'product_list', 'row_id': 0}])


def test_moved_list_json_cannot_replace_current_item_identity():
    moved = product(27, infringement_check_json=json.dumps({
        'source_type': 'collection_list', 'source_row_id': 12,
        'row_id': 12, 'record_key': 'collection_list:12', 'risk_level': 1,
    }))
    current = canonical_record(moved, 'product_list')
    assert current['record_key'] == 'product_list:27'
    assert current['row_id'] == 27


def test_private_image_urls_are_rejected_before_network(monkeypatch):
    monkeypatch.setattr(bit_check_risk.socket, 'getaddrinfo', lambda *args: [
        (2, 1, 6, '', ('127.0.0.1', 80)),
    ])
    monkeypatch.setattr(bit_check_risk, '_get_http_session', lambda: pytest.fail('Private image must not connect'))
    with pytest.raises(ValueError, match='公开地址'):
        bit_check_risk._read_screening_image('http://127.0.0.1/secret')


def test_knowledge_version_ignores_pending_and_changes_with_scope_aliases():
    rule = {'id': 1, 'brand_name': 'Nike', 'list_type': 'blacklist', 'source_type': 'manual'}
    first = knowledge_version([rule])
    assert first == knowledge_version([rule, {**rule, 'id': 2, 'source_type': 'analysis'}])
    assert first != knowledge_version([rule, {**rule, 'id': 2, 'source_type': 'wipo'}])
    assert first != knowledge_version([{**rule, 'aliases': ['N1ke']}])
    assert first != knowledge_version([{**rule, 'scope': {'mode': 'scoped', 'sites': ['MLM']}}])


def test_title_image_sends_actual_bytes_and_uses_explicit_vision_model(monkeypatch):
    picture = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(picture, format='PNG')
    captured = {}
    def chat(messages, **kwargs):
        captured.update(messages=messages, **kwargs)
        return json.dumps([{'row_id': 'product_list:1', 'risk_level': 2, 'keywords': ['Nike'], 'reason': '图形 Logo'}])
    monkeypatch.setattr(bit_check_risk, 'chat_deepseek', chat)
    results = bit_check_risk.classify_risk_records(
        [product(main_image_url='https://image.example.test/product.png')],
        detection_scope='title_image', vision_model='configured-vision-model',
        image_reader=lambda url: picture.getvalue(), retries=0,
    )
    assert results[0]['risk_level'] == 2
    assert captured['model'] == 'configured-vision-model'
    image_message = next(item for item in captured['messages'][1]['content'] if item['type'] == 'image_url')
    assert image_message['image_url']['url'].startswith('data:image/png;base64,')
    assert '图形 Logo' in captured['messages'][0]['content']


def test_image_failure_never_writes_safe_result(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_check_risk, 'chat_deepseek', lambda *a, **kw: pytest.fail('Invalid image must not reach model'))
    summary = bit_check_risk.scan_products(
        items=[{'source_type': 'product_list', 'row_id': 1}],
        candidate_reader=lambda **kwargs: [product(main_image_url='https://example.test/bad')],
        risk_writer=lambda rows: calls.extend(rows) or len(rows),
        detection_scope='title_image', image_reader=lambda url: b'<html>not an image</html>', retries=0,
    )
    assert summary['checked'] == summary['risk_0'] == 0
    assert summary['failed'] == 1
    assert calls[0]['risk_level'] is None
    assert calls[0]['check_status'] == 'failed'
    assert calls[0]['image_checked'] is False


def test_empty_titles_and_missing_ai_rows_are_failed_not_zero(monkeypatch):
    events, written = [], []
    monkeypatch.setattr(bit_check_risk, 'classify_risk_records', lambda *a, **kw: [])
    summary = bit_check_risk.scan_products(
        sources=['product_list'], batch_size=1,
        candidate_reader=lambda **kwargs: [product(title=''), product(2)],
        risk_writer=lambda rows: written.extend(rows) or len(rows), item_callback=events.append,
    )
    assert summary['failed'] == 2
    assert summary['checked'] == 0
    assert all(row['risk_level'] is None for row in written)
    assert [event['status'] for event in events] == ['processing', 'failed', 'processing', 'failed']


def test_stop_is_checked_between_batches_and_retains_completed_results(monkeypatch):
    monkeypatch.setattr(bit_check_risk, 'classify_risk_records', lambda records, **kwargs: [
        {'row_id': row['row_id'], 'record_key': row['record_key'], 'risk_level': 0, 'keywords': []} for row in records
    ])
    stops = iter([False, True])
    summary = bit_check_risk.scan_products(
        sources=['product_list'], batch_size=1, dry_run=True,
        candidate_reader=lambda **kwargs: [product(1), product(2), product(3)],
        stop_callback=lambda: next(stops),
    )
    assert summary['checked'] == 1
    assert summary['cancelled'] == 2


def test_scoped_alias_matches_only_current_site(monkeypatch):
    rule = {'id': 10, 'brand_name': 'Nike', 'aliases': ['N1ke'], 'source_type': 'manual',
            'list_type': 'blacklist', 'scope': {'mode': 'scoped', 'sites': ['MLM'], 'categories': [], 'shops': []}}
    calls = []
    monkeypatch.setattr(bit_check_risk, 'classify_risk_records', lambda records, **kwargs: calls.extend(records) or [
        {'row_id': row['row_id'], 'record_key': row['record_key'], 'risk_level': 1, 'keywords': ['N1ke']} for row in records
    ])
    summary = bit_check_risk.scan_products(
        sources=['product_list'], dry_run=True, knowledge_records=[rule],
        candidate_reader=lambda **kwargs: [product(1, 'N1ke box', site_id='MLM'), product(2, 'N1ke box', site_id='MLB')],
    )
    assert summary['results'][0]['risk_level'] == 2
    assert summary['results'][0]['matched_rule_ids'] == [10]
    assert summary['results'][1]['risk_level'] == 1
    assert [row['row_id'] for row in calls] == [2]


class Cursor:
    def __init__(self, calls, rows=None):
        self.calls, self.rows = calls, rows or []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def execute(self, sql, params=None):
        self.calls.append(('execute', ' '.join(sql.split()), params))
    def executemany(self, sql, params):
        self.calls.append(('executemany', ' '.join(sql.split()), list(params)))
    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, calls, rows=None):
        self.calls, self.rows = calls, rows
    def cursor(self):
        return Cursor(self.calls, self.rows)
    def commit(self):
        self.calls.append(('commit',))
    def rollback(self):
        self.calls.append(('rollback',))
    def close(self):
        pass


def fake_database(monkeypatch, calls, rows=None):
    monkeypatch.setattr(bit_mysql.pymysql, 'connect', lambda **kwargs: Connection(calls, rows))
    monkeypatch.setattr(bit_mysql, '_ensure_infringement_risk_checks_table', lambda cursor: None)
    monkeypatch.setattr('erp.mercadolibre_collection_store.ensure_collection_tables', lambda cursor: None)


def test_precise_candidates_use_current_product_rows_for_every_source(monkeypatch):
    calls = []
    fake_database(monkeypatch, calls, [{**product(), 'source_row_id': 1, 'source_type': 'ai_original'}])
    rows = bit_mysql.get_infringement_risk_candidates(items=[{'source_type': 'product_list', 'row_id': 1}], include_checked=True)
    sql = next(call[1] for call in calls if call[0] == 'execute')
    params = next(call[2] for call in calls if call[0] == 'execute')
    assert "products.`source_type` = 'collected'" not in sql
    assert 'products.`id` IN (%s)' in sql
    assert 'products.`source_snapshot_json`' in sql
    assert params == ('product_list', 1, 0, 200)
    assert 'products.`id` > %s' in sql
    assert sql.endswith('LIMIT %s')
    assert rows[0]['source_type'] == 'product_list'


def test_ai_writer_preserves_manual_fields_and_stores_failure_metadata(monkeypatch):
    calls = []
    fake_database(monkeypatch, calls)
    result = product(risk_level=None, check_status='failed', error='image unavailable',
                     manual_decision='allow', review_reason='do not replace')
    assert bit_mysql.update_infringement_product_risks([result]) == 1
    insert = next(call for call in calls if call[0] == 'executemany' and call[1].startswith('INSERT INTO'))
    assert insert[2][0][12] is None
    assert '`manual_decision` = VALUES' not in insert[1]
    assert 'review_reason' not in insert[2][0][-1]
    snapshot = next(call for call in calls if call[0] == 'executemany' and 'JSON_MERGE_PATCH' in call[1])
    assert json.loads(snapshot[2][0][0])['check_status'] == 'failed'


def test_manual_review_updates_independent_history_and_snapshot(monkeypatch):
    calls = []
    fake_database(monkeypatch, calls)
    row = product(risk_level=2, manual_decision='block', review_history=[{'manual_decision': 'block'}])
    monkeypatch.setattr(bit_mysql, 'get_infringement_risk_candidates', lambda **kwargs: [row])
    result = bit_mysql.update_infringement_risk_review(
        [{'source_type': 'product_list', 'row_id': 1}], 'allow', '已验证授权', 'reviewer',
        site_ids=['MLM'], evidence_ref='authorization-id',
    )
    assert result['rows'][0]['risk_level'] == 2
    assert result['rows'][0]['manual_decision'] == 'allow'
    assert result['rows'][0]['review_history'][-1]['previous_decision'] == 'block'
    insert = next(call for call in calls if call[0] == 'executemany' and call[1].startswith('INSERT INTO'))
    update_sql = insert[1].split('ON DUPLICATE KEY UPDATE')[1]
    assert '`risk_level`' not in update_sql
    assert '`review_history_json` = VALUES' in update_sql
    assert insert[1].count('%s') == len(insert[2][0])


def test_results_include_unchecked_stale_and_failed(monkeypatch):
    good = product(1, risk_level=0, detection_scope='title', rule_version='v1')
    good['content_hash'] = content_hash(good)
    stale = product(2, risk_level=2, content_hash='old', rule_version='v1')
    failure = product(3, risk_level=None, check_status='failed')
    monkeypatch.setattr(bit_mysql, 'get_infringement_risk_candidates', lambda **kwargs: [good, stale, failure, product(4)])
    result = bit_mysql.get_infringement_risk_results(sources=['product_list'], rule_version='v1')
    assert (result['risk_0'], result['stale'], result['failed'], result['unchecked']) == (1, 1, 1, 1)
    assert len(bit_mysql.get_infringement_risk_results(risk_level='stale', rule_version='v1')['rows']) == 1


def test_move_helper_copies_human_review_metadata_with_bound_ids():
    calls = []
    bit_mysql.copy_infringement_check_metadata(Cursor(calls), 'collection_list', 'product_list', [(12, 27)])
    assert calls[0][2] == [(12, 27)]
    assert 'target.`review_history_json` = source.`review_history_json`' in calls[0][1]


def test_db_api_forwards_precise_items_and_review(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    monkeypatch.setattr(bit_db_api, '_request', lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {})
    items = [{'source_type': 'product_list', 'row_id': 1}]
    bit_db_api.get_infringement_risk_candidates(items=items, detection_scope='title_image', rule_version='v1')
    assert calls[-1][2]['json']['items'] == items
    bit_db_api.update_infringement_risk_review(items, 'allow', 'reason', 'reviewer', site_ids=['MLM'])
    assert calls[-1][1] == '/api/db/infringement-risk/review'
    assert calls[-1][2]['json']['site_ids'] == ['MLM']


def test_image_revision_changes_image_hash_even_when_url_is_reused():
    item = product(main_image_url='https://example.test/reused.jpg',
                   source_snapshot_json={'ai_original': {'image_content_revision': 'bytes-a',
                                                        'image_generation_method': 'ai_image_edit'}})
    title_hash = content_hash(item)
    visual_hash = content_hash(item, 'title_image')
    item['source_snapshot_json']['ai_original']['image_content_revision'] = 'bytes-b'
    assert content_hash(item) == title_hash
    assert content_hash(item, 'title_image') != visual_hash


def test_title_only_manual_review_still_invalidates_when_picture_changes():
    item = product(detection_scope='title', manual_decision='allow',
                   main_image_url='https://example.test/original.jpg')
    item['review_content_hash'] = content_hash(item, 'title_image')
    assert review_is_current(item)
    item['main_image_url'] = 'https://example.test/changed.jpg'
    assert not review_is_current(item)


def test_saved_result_translations_cannot_override_current_snapshot(monkeypatch):
    calls = []
    previous = product(source_snapshot_json={'ai_original': {'title_es': 'old title', 'title_pt': 'old pt'}})
    previous_hash = content_hash(previous)
    current = product(source_snapshot_json={'ai_original': {'title_es': 'changed title', 'title_pt': 'changed pt'}},
                      details_json=json.dumps({'title_es': 'old title', 'title_pt': 'old pt'}),
                      risk_level=0, check_status='completed', content_hash=previous_hash, detection_scope='title')
    fake_database(monkeypatch, calls, [current])
    records = bit_mysql.get_infringement_risk_candidates(items=[{'source_type': 'product_list', 'row_id': 1}])
    assert records[0]['title_es'] == 'changed title'
    assert records[0]['title_pt'] == 'changed pt'
    assert records[0]['risk_status'] == 'stale'


def test_precise_items_disappearing_before_candidate_read_are_task_failures_only(monkeypatch):
    events, written = [], []
    monkeypatch.setattr(bit_check_risk, 'classify_risk_records', lambda records, **kwargs: [
        {'row_id': item['row_id'], 'record_key': item['record_key'], 'risk_level': 0, 'keywords': []}
        for item in records
    ])
    summary = bit_check_risk.scan_products(
        items=[{'source_type': 'product_list', 'row_id': 1}, {'source_type': 'product_list', 'row_id': 2}],
        recheck=True, candidate_reader=lambda **kwargs: [product(1)],
        risk_writer=lambda results: written.extend(results) or len(results), item_callback=events.append,
    )
    assert (summary['total'], summary['checked'], summary['failed']) == (2, 1, 1)
    assert any(event['row_id'] == 2 and event['status'] == 'failed' for event in events)
    assert [item['row_id'] for item in written] == [1]


def test_local_generated_image_route_reads_only_known_bounded_asset(tmp_path, monkeypatch):
    from erp import ai_original_products
    asset = tmp_path / '1688-123-ai-white.jpg'
    asset.write_bytes(b'generated image bytes')
    monkeypatch.setattr(ai_original_products, 'IMAGE_DIR', tmp_path)
    monkeypatch.setattr(ai_original_products, 'IMAGE_BASE_URL', '')
    monkeypatch.setattr(bit_check_risk, '_get_http_session', lambda: pytest.fail('Local assets must not connect'))
    assert bit_check_risk._read_screening_image('/api/ai-original-products/images/1688-123-ai-white.jpg') == asset.read_bytes()
    assert bit_check_risk._read_screening_image('http://127.0.0.1:5000/api/ai-original-products/images/1688-123-ai-white.jpg') == asset.read_bytes()
    with pytest.raises(ValueError, match='不存在'):
        bit_check_risk._read_screening_image('/api/ai-original-products/images/1688-missing-ai-white.jpg')


def test_collection_and_transferred_product_generated_copy_share_current_hash():
    copy = {'title_es': 'Caja real', 'title_pt': 'Caixa atual'}
    collection = product(ai_copy_json=json.dumps(copy))
    transferred = product(source_snapshot_json={'ai_copy': copy})
    assert canonical_record(collection)['title_es'] == 'Caja real'
    assert canonical_record(transferred)['title_pt'] == 'Caixa atual'
    assert content_hash(collection) == content_hash(transferred)
    transferred['source_snapshot_json']['ai_copy']['title_pt'] = 'changed translation'
    assert content_hash(collection) != content_hash(transferred)


def test_collection_candidate_selects_current_generated_copy(monkeypatch):
    calls = []
    current = product(ai_copy_json=json.dumps({'title_es': 'actual es', 'title_pt': 'actual pt'}))
    fake_database(monkeypatch, calls, [current])
    records = bit_mysql.get_infringement_risk_candidates(items=[{'source_type': 'collection_list', 'row_id': 1}], include_checked=True)
    assert 'collection_items.`ai_copy_json`' in calls[0][1]
    assert records[0]['title_es'] == 'actual es'
    assert records[0]['title_pt'] == 'actual pt'


def test_product_and_collection_scope_context_comes_from_current_plugin_snapshot():
    plugin = {'token_id': 12, 'site_id': 'MLM', 'store_name': 'Current shop'}
    collection = product(plugin_snapshot_json=json.dumps(plugin),
                         infringement_check_json=json.dumps({'token_id': 99, 'account_name': 'Old shop'}))
    transferred = product(source_snapshot_json={'plugin_snapshot': plugin})
    current = canonical_record(collection)
    assert current['token_id'] == 12
    assert current['account_name'] == 'Current shop'
    assert content_hash(collection) == content_hash(transferred)
    transferred['source_snapshot_json']['plugin_snapshot']['token_id'] = 13
    assert content_hash(collection) != content_hash(transferred)
