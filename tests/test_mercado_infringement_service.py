"""Offline end-to-end service, durable queue and publication-policy contracts."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from bit import bit_check_risk
from bit.infringement_tasks import InfringementTaskStore
from bit.infringement_publish import publication_issues, validate_publication
from bit.infringement_state import canonical_record, content_hash, knowledge_version
from bit.mercado_infringement import MercadoInfringementService, public_result


class DeferredThread:
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args
    def start(self):
        pass


def row(row_id, title='Generic box', token_id=1, **values):
    return {'id': row_id, 'source_type': 'collected', 'source_item_id': f'MLM{row_id}',
            'title': title, 'category_name': 'Boxes', 'category_id': 'MLMBOX',
            'token_id': token_id, 'infringement_risk_level': None,
            'infringement_check_json': None, **values}


def screened(product, level=0, scope='title', **values):
    record = canonical_record(product, 'product_list')
    result = {'risk_level': level, 'check_status': 'completed', 'content_hash': content_hash(record, scope),
              'detection_scope': scope, 'rule_version': knowledge_version([]),
              'checked_at': datetime.now(timezone.utc).isoformat(), **values}
    product['infringement_check_json'] = json.dumps(result)
    product['infringement_risk_level'] = level
    return product


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr('bit.mercado_infringement.threading.Thread', DeferredThread)
    state = {'rows': {}, 'credential': 'sk-private-account-key', 'calls': [], 'failed': set(), 'knowledge': []}
    store = InfringementTaskStore(tmp_path / 'tasks.sqlite3')

    def get(ids):
        return [dict(state['rows'][item_id]) for item_id in ids if item_id in state['rows']]

    def listing(**params):
        state['calls'].append(('list', params))
        products = list(state['rows'].values())
        if params.get('exclude_added'):
            products = [item for item in products if not item.get('added_to_products')]
        if 'token_ids' in params:
            products = [item for item in products if item['token_id'] in params['token_ids']]
        if params.get('search'):
            products = [item for item in products if params['search'] in item['title']]
        start = params.get('offset', 0)
        # Deliberately return pages smaller than the requested limit.
        page = products[start:start + min(params.get('limit', 1000), 2)]
        return {'rows': [dict(item) for item in page], 'total': len(products)}

    def candidates(**params):
        state['calls'].append(('candidates', params))
        return [canonical_record(state['rows'][item['row_id']], item['source_type']) for item in params['items']]

    def writer(results):
        for result in results:
            raw = state['rows'][result['row_id']]
            metadata = json.loads(raw.get('infringement_check_json') or '{}')
            metadata.update({name: value for name, value in result.items() if not name.startswith('review') and name != 'manual_decision'})
            raw['infringement_check_json'] = json.dumps(metadata, default=str)
            raw['infringement_risk_level'] = result.get('risk_level')
        return len(results)

    def classify(records, **params):
        if any(record['row_id'] in state['failed'] for record in records):
            raise RuntimeError('model temporarily unavailable')
        return [{'row_id': record['row_id'], 'record_key': record['record_key'], 'risk_level': 0,
                 'keywords': [], 'reason': record['title']} for record in records]

    monkeypatch.setattr(bit_check_risk, 'classify_risk_records', classify)
    deps = {'knowledge': lambda **params: {'rows': state['knowledge']},
            'list_products': listing, 'list_collection': listing,
            'get_products': get, 'get_collection': get,
            'credential': lambda user_id: state['credential'],
            'scan': lambda **params: bit_check_risk.scan_products(batch_size=1, **params),
            'candidates': candidates, 'writer': writer,
            'review': lambda *args, **params: {'updated': 1}}
    service = MercadoInfringementService(store, deps)
    return service, store, state


def start(service, ids, user_id=7, **values):
    return service.start({'list_mode': 'products', 'selection_mode': 'selected', 'item_ids': ids, **values}, user_id)


def test_service_detects_only_selected_current_ids_and_titles(harness):
    service, store, state = harness
    state['rows'] = {i: row(i) for i in range(1, 5)}
    task = start(service, [2, 4])
    state['rows'][2]['title'] = 'Current edited title'
    service.run(task['task_id'], 7)
    result = store.get(task['task_id'], 7)
    assert result['status'] == 'completed'
    assert result['completed'] == 2
    requested = next(params['items'] for name, params in state['calls'] if name == 'candidates')
    assert requested == [{'source_type': 'product_list', 'row_id': 2}, {'source_type': 'product_list', 'row_id': 4}]
    assert state['rows'][1]['infringement_check_json'] is None
    assert state['rows'][3]['infringement_check_json'] is None
    metadata = json.loads(state['rows'][2]['infringement_check_json'])
    assert metadata['reason'] == 'Current edited title'
    assert metadata['content_hash'] == content_hash(state['rows'][2])


def test_cached_valid_screening_reuses_result_without_model_credential(harness):
    service, store, state = harness
    state['rows'] = {1: screened(row(1))}
    state['credential'] = None
    task = start(service, [1])
    service.run(task['task_id'], 7)
    result = store.get(task['task_id'], 7)
    assert result['status'] == 'completed' and result['completed'] == 1
    assert result['results'][0]['reused'] is True
    assert not any(name == 'candidates' for name, _ in state['calls'])


def test_cancel_while_waiting_for_execution_capacity(harness):
    service, store, state = harness
    state['rows'] = {1: row(1)}
    service.queue_slots.acquire()
    service.queue_slots.acquire()
    try:
        task = start(service, [1])
        store.cancel(task['task_id'], 7)
        service.run(task['task_id'], 7)
        assert store.get(task['task_id'], 7)['status'] == 'cancelled'
        assert not any(name == 'candidates' for name, _ in state['calls'])
    finally:
        service.queue_slots.release()
        service.queue_slots.release()


def test_filtered_selection_reads_all_pages_and_risk_filter_then_paginates(harness):
    service, store, state = harness
    state['rows'] = {i: screened(row(i), level=i % 2) for i in range(1, 8)}
    result = service.filtered('products', {'risk_status': 'low'}, 7, limit=1, offset=1)
    assert result['total'] == 3
    assert [item['id'] for item in result['rows']] == [4]
    assert [params['offset'] for name, params in state['calls'] if name == 'list'] == [0, 2, 4, 6]
    task = service.start({'list_mode': 'products', 'selection_mode': 'filtered',
                          'filters': {'risk_status': 'low'}, 'recheck': True}, 7)
    assert [item['row_id'] for item in store.items(task['task_id'])] == [2, 4, 6]


def test_existing_valid_result_reuses_cache_but_content_changes_queue_scan(harness):
    service, store, state = harness
    state['rows'] = {1: screened(row(1)), 2: screened(row(2))}
    state['rows'][2]['title'] = 'changed'
    task = start(service, [1, 2])
    service.run(task['task_id'], 7)
    assert store.get(task['task_id'], 7)['completed'] == 2
    requested = next(params['items'] for name, params in state['calls'] if name == 'candidates')
    assert requested == [{'source_type': 'product_list', 'row_id': 2}]


def test_task_and_selected_product_permissions_are_account_scoped(harness):
    service, store, state = harness
    state['rows'] = {1: row(1, token_id=1), 2: row(2, token_id=2)}
    with pytest.raises(PermissionError):
        service.start({'list_mode': 'products', 'item_ids': [2]}, 7, allowed_tokens={1})
    task = service.start({'list_mode': 'products', 'item_ids': [1]}, 7, allowed_tokens={1})
    with pytest.raises(KeyError):
        store.get(task['task_id'], 8)
    with pytest.raises(KeyError):
        store.cancel(task['task_id'], 8)
    assert store.list(8) == []
    assert store.item_statuses([{'source_type': 'product_list', 'row_id': 1}], 8) == {}
    assert store.item_history('product_list', 1, 8) == []


def test_failed_retry_selects_only_unfinished_products(harness):
    service, store, state = harness
    state['rows'] = {1: row(1), 2: row(2)}
    state['failed'] = {2}
    task = start(service, [1, 2])
    service.run(task['task_id'], 7)
    old = store.get(task['task_id'], 7)
    assert (old['status'], old['completed'], old['failed']) == ('partial', 1, 1)
    retry_ids = [item['row_id'] for item in store.items(task['task_id'], unfinished_only=True)]
    assert retry_ids == [2]
    state['failed'] = set()
    retry = start(service, retry_ids, recheck=True)
    service.run(retry['task_id'], 7)
    assert store.get(retry['task_id'], 7)['status'] == 'completed'
    requested = [params['items'] for name, params in state['calls'] if name == 'candidates']
    assert requested[-1] == [{'source_type': 'product_list', 'row_id': 2}]


def test_cancelled_queue_keeps_completed_snapshots_and_releases_active_items(harness):
    service, store, state = harness
    state['rows'] = {1: row(1), 2: row(2)}
    task = start(service, [1, 2])
    store.update_item(task['task_id'], {'source_type': 'product_list', 'row_id': 1, 'status': 'completed', 'risk_level': 0})
    store.cancel(task['task_id'], 7)
    service.run(task['task_id'], 7)
    saved = store.get(task['task_id'], 7)
    assert saved['status'] == 'cancelled'
    assert saved['completed'] == 1
    assert store.items(task['task_id'], unfinished_only=True) == [{'source_type': 'product_list', 'row_id': 2}]
    retry = start(service, [2])
    assert retry['status'] == 'queued'


def test_normal_execution_never_persists_credential(harness):
    service, store, state = harness
    state['rows'] = {1: row(1)}
    task = start(service, [1])
    service.run(task['task_id'], 7)
    with store.connect() as db:
        saved = '\n'.join(str(tuple(record)) for table in ('tasks', 'task_items', 'task_logs')
                          for record in db.execute(f'SELECT * FROM {table}'))
    assert state['credential'] not in saved
    assert 'api_key' not in store.get(task['task_id'], 7)['params']
    with pytest.raises(ValueError, match='凭证'):
        store.create(7, {'api_key': 'forbidden'}, [{'source_type': 'product_list', 'row_id': 3}])


def test_service_rejects_missing_credential_before_creating_task(harness):
    service, store, state = harness
    state['rows'] = {1: row(1)}
    state['credential'] = ''
    with pytest.raises(ValueError, match='Token'):
        start(service, [1])
    assert store.list(7) == []


def test_restart_marks_only_unfinished_items_interrupted(harness):
    service, store, state = harness
    state['rows'] = {1: row(1), 2: row(2)}
    task = start(service, [1, 2])
    store.update_item(task['task_id'], {'source_type': 'product_list', 'row_id': 1, 'status': 'completed'})
    reopened = InfringementTaskStore(store.path)
    reopened.recover()
    saved = reopened.get(task['task_id'], 7)
    assert saved['status'] == 'interrupted'
    assert saved['completed'] == 1
    assert {result['status'] for result in saved['results']} == {'completed', 'interrupted'}
    assert reopened.create(7, {}, [{'source_type': 'product_list', 'row_id': 2}])['status'] == 'queued'


def test_same_item_cannot_have_two_active_tasks(harness):
    service, store, state = harness
    state['rows'] = {1: row(1)}
    task = start(service, [1])
    with pytest.raises(ValueError, match='正在检测'):
        start(service, [1])
    store.set_status(task['task_id'], 'failed', 'failed')
    assert start(service, [1])['status'] == 'queued'


@pytest.mark.parametrize('level, expected', [(0, None), (1, '疑似'), (2, '高风险')])
def test_publication_requires_risk_disposition_independent_of_ordinary_review(level, expected):
    product = screened(row(1, review_status='approved'), level=level)
    issues = publication_issues(product, 'MLM', knowledge_version([]))
    if expected:
        assert any(expected in issue for issue in issues)
        with pytest.raises(ValueError):
            validate_publication(product, 'MLM', knowledge_version([]))
    else:
        assert issues == []


def test_publication_rejects_unchecked_failed_and_changed_content():
    product = row(1, review_status='approved')
    assert '尚未检测' in publication_issues(product)[0]
    screened(product)
    product['title'] = 'new title'
    assert '失效' in publication_issues(product)[0]
    product['infringement_check_json'] = json.dumps({'check_status': 'failed', 'risk_level': None})
    product['infringement_risk_level'] = None
    assert '失败' in publication_issues(product)[0]


def test_scoped_authorization_requires_current_content_site_evidence_and_expiry():
    product = screened(row(1), level=2)
    metadata = json.loads(product['infringement_check_json'])
    metadata.update(manual_decision='allow', review_content_hash=content_hash(product, 'title_image'),
                    review_site_ids=['MLM'], review_evidence_ref='authorization-proof',
                    review_expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    product['infringement_check_json'] = json.dumps(metadata)
    assert publication_issues(product, 'MLM') == []
    assert '目标站点' in publication_issues(product, 'MLB')[0]
    product['title'] = 'different product content'
    assert publication_issues(product, 'MLM')
    product['title'] = 'Generic box'
    metadata['review_expires_at'] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    product['infringement_check_json'] = json.dumps(metadata)
    assert publication_issues(product, 'MLM')


def test_manual_block_is_enforced_even_when_ai_low():
    product = screened(row(1))
    metadata = json.loads(product['infringement_check_json'])
    metadata.update(manual_decision='block', review_content_hash=content_hash(product, 'title_image'))
    product['infringement_check_json'] = json.dumps(metadata)
    assert '已拦截' in publication_issues(product, 'MLM')[0]


def test_collection_tasks_reject_already_moved_rows(harness):
    service, store, state = harness
    state['rows'] = {1: row(1, added_to_products=True)}
    with pytest.raises(ValueError, match='已转入'):
        service.start({'list_mode': 'collection', 'item_ids': [1]}, 7)
    assert store.list(7) == []


def test_credential_revoked_after_queue_does_not_fall_back_to_environment_key(harness):
    service, store, state = harness
    state['rows'] = {1: row(1)}
    task = start(service, [1])
    state['credential'] = None
    service.run(task['task_id'], 7)
    saved = store.get(task['task_id'], 7)
    assert saved['status'] == 'failed'
    assert not any(name == 'candidates' for name, _ in state['calls'])
    assert state['rows'][1]['infringement_check_json'] is None


def test_error_logs_and_callback_results_cannot_persist_credentials(harness):
    service, store, state = harness
    state['rows'] = {1: row(1)}
    secret = state['credential']
    def broken_scan(**params):
        params['item_callback']({'source_type': 'product_list', 'row_id': 1, 'status': 'failed',
                                 'error': 'provider error ' + secret, 'nested': {'api_key': secret}})
        params['log_callback']('provider debug ' + secret)
        raise RuntimeError('provider refused ' + secret)
    service.deps['scan'] = broken_scan
    task = start(service, [1])
    service.run(task['task_id'], 7)
    with store.connect() as db:
        saved = '\n'.join(str(tuple(record)) for table in ('tasks', 'task_items', 'task_logs')
                          for record in db.execute(f'SELECT * FROM {table}'))
    assert secret not in saved
    assert 'api_key' not in saved


def test_new_retry_activity_wins_when_task_timestamps_are_identical(harness, monkeypatch):
    service, store, state = harness
    monkeypatch.setattr('bit.infringement_tasks._now', lambda: '2026-10-06T01:00:00+00:00')
    key = {'source_type': 'product_list', 'row_id': 1}
    old = store.create(7, {}, [key])
    store.update_item(old['task_id'], {**key, 'status': 'failed', 'error': 'old failure'})
    store.set_status(old['task_id'], 'failed')
    new = store.create(7, {}, [key])
    store.update_item(new['task_id'], {**key, 'status': 'completed', 'risk_level': 0})
    store.set_status(new['task_id'], 'completed')
    activity = store.item_statuses([key], 7)[('product_list', 1)]
    assert activity['task_id'] == new['task_id']
    assert activity['status'] == 'completed'


def test_historical_failed_task_cannot_override_new_database_result():
    product = screened(row(1))
    result = public_result(product, knowledge_version([]), {'status': 'failed', 'error': 'old failure'})
    assert result['status'] == 'low'


def test_filtered_selection_respects_current_product_visibility_on_every_page(harness):
    service, store, state = harness
    state['rows'] = {i: row(i, token_id=1 if i % 2 else 2) for i in range(1, 8)}
    task = service.start({'list_mode': 'products', 'selection_mode': 'filtered', 'filters': {}},
                         7, allowed_tokens={1})
    assert [item['row_id'] for item in store.items(task['task_id'])] == [1, 3, 5, 7]
    assert all(params['token_ids'] == [1] for name, params in state['calls'] if name == 'list')


def test_detail_links_precise_matched_rule_ids_including_alias_hits(harness):
    service, store, state = harness
    state['knowledge'] = [
        {'id': 1, 'brand_name': 'Nike', 'aliases': ['N1ke'], 'list_type': 'blacklist', 'source_type': 'manual'},
        {'id': 2, 'brand_name': 'N1ke', 'list_type': 'pending', 'source_type': 'wipo'},
    ]
    state['rows'] = {1: screened(row(1, title='N1ke box'), 2,
                                keywords=['N1ke'], matched_rule_ids=[1])}
    detail = service.detail('products', 1, 7)
    assert [rule['id'] for rule in detail['rules']] == [1]


def test_manual_block_applies_to_its_declared_site():
    product = screened(row(1))
    metadata = json.loads(product['infringement_check_json'])
    metadata.update(manual_decision='block', review_site_ids=['MLM'],
                    review_content_hash=content_hash(product, 'title_image'))
    product['infringement_check_json'] = json.dumps(metadata)
    assert publication_issues(product, 'MLM')
    assert publication_issues(product, 'MLB') == []


def interface_function(name, namespace):
    """Extract a pure helper without importing the server or starting its stores."""
    import ast
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / 'bit' / 'bit_interface.py'
    parsed = ast.parse(source.read_text())
    function = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(source), 'exec'), namespace)
    return namespace[name]


def publish_namespace(harness):
    import threading
    from types import SimpleNamespace
    service, store, state = harness
    return {
        'threading': threading,
        'bit_check_risk': bit_check_risk,
        '_risk_check_lock': threading.Lock(),
        'get_mercado_infringement_service': lambda: service,
        'db_get_mercado_product_items_by_ids': lambda ids: [dict(state['rows'][value]) for value in ids if value in state['rows']],
        '_mercado_publish_state_update': lambda **values: state['calls'].append(('publish_state', values)),
        'browser_extension_models': SimpleNamespace(get_api_key=lambda *args: state['credential']),
        'app': SimpleNamespace(secret_key='offline-app-secret'),
    }


def test_publication_auto_check_completes_before_returning_rows(harness):
    service, store, state = harness
    state['rows'] = {1: row(1, review_status='approved')}
    helper = interface_function('_screen_mercado_publish_rows', publish_namespace(harness))
    rows = helper([dict(state['rows'][1])], 7)
    assert public_result(rows[0], knowledge_version([]))['status'] == 'low'
    assert store.list(7)[0]['status'] == 'completed'
    assert any(name == 'candidates' for name, params in state['calls'])


def test_publication_auto_check_rejects_existing_active_task(harness):
    service, store, state = harness
    state['rows'] = {1: row(1, review_status='approved')}
    start(service, [1])
    helper = interface_function('_screen_mercado_publish_rows', publish_namespace(harness))
    with pytest.raises(ValueError, match='正在'):
        helper([dict(state['rows'][1])], 7)
    assert not any(name == 'candidates' for name, params in state['calls'])


def test_publication_auto_check_failure_never_returns_publishable_rows(harness):
    service, store, state = harness
    state['rows'] = {1: row(1, review_status='approved')}
    state['failed'] = {1}
    helper = interface_function('_screen_mercado_publish_rows', publish_namespace(harness))
    with pytest.raises(ValueError, match='补检未完成'):
        helper([dict(state['rows'][1])], 7)
    assert store.list(7)[0]['status'] == 'failed'
    assert public_result(state['rows'][1])['status'] == 'failed'


def test_publication_existing_high_risk_reaches_individual_review_gate(harness):
    service, store, state = harness
    state['rows'] = {1: screened(row(1, review_status='approved'), 2)}
    helper = interface_function('_screen_mercado_publish_rows', publish_namespace(harness))
    from bit.infringement_publish import validate_publication, InfringementReviewRequired
    rows = helper([dict(state['rows'][1])], 7)
    with pytest.raises(InfringementReviewRequired, match='高风险'):
        validate_publication(rows[0])
    assert store.list(7) == []


def test_final_draft_preflight_screens_actual_outgoing_title_and_target(harness, monkeypatch):
    service, store, state = harness
    state['rows'] = {1: screened(row(1, review_status='approved'))}
    before = dict(state['rows'][1])
    captured = []
    def classify(records, **params):
        captured.extend(records)
        return [{'row_id': record['row_id'], 'record_key': record['record_key'], 'risk_level': 0,
                 'keywords': [], 'reason': 'generic'} for record in records]
    monkeypatch.setattr(bit_check_risk, 'classify_risk_records', classify)
    builder = interface_function('_build_mercado_infringement_preflight', publish_namespace(harness))
    preflight = builder(7, 42)
    preflight(before, {'payload': {'title': 'Actual Portuguese final title'}}, 'MLB')
    assert captured[0]['title'] == 'Actual Portuguese final title'
    assert captured[0]['site_id'] == 'MLB'
    assert captured[0]['token_id'] == 42
    assert state['rows'][1]['infringement_check_json'] == before['infringement_check_json']
    preflight(before, {'payload': {'title': 'Actual Portuguese final title'}}, 'MLB')
    assert len(captured) == 1


def test_final_draft_preflight_blocks_changed_content_and_final_title_risk(harness, monkeypatch):
    service, store, state = harness
    state['rows'] = {1: screened(row(1, review_status='approved'))}
    before = dict(state['rows'][1])
    builder = interface_function('_build_mercado_infringement_preflight', publish_namespace(harness))
    preflight = builder(7, 42)
    state['rows'][1]['title'] = 'changed after draft preparation'
    with pytest.raises(ValueError, match='内容已变化'):
        preflight(before, {'payload': {'title': 'Title'}}, 'MLM')
    state['rows'][1]['title'] = before['title']
    monkeypatch.setattr(bit_check_risk, 'classify_risk_records', lambda records, **params: [
        {'row_id': record['row_id'], 'record_key': record['record_key'], 'risk_level': 2,
         'keywords': ['Nike'], 'reason': 'final title contains Nike'} for record in records
    ])
    with pytest.raises(ValueError, match='最终上架标题存在侵权风险'):
        preflight(before, {'payload': {'title': 'Nike outgoing product'}}, 'MLM')


def test_collection_risk_filters_keep_salesperson_scope(harness):
    service, _store, state = harness
    service.filtered('collection', {'risk_status': 'unchecked', 'salesperson': '业务乙'}, 7)
    reads = [params for name, params in state['calls'] if name == 'list']
    assert reads and all(params['salesperson'] == '业务乙' for params in reads)
    state['calls'].clear()
    service.read('products', {'salesperson': '业务乙'})
    reads = [params for name, params in state['calls'] if name == 'list']
    assert reads and all(params['salesperson'] == '业务乙' for params in reads)


@pytest.mark.parametrize('selection', ['selected', 'filtered', 'unchecked'])
def test_detection_cannot_modify_coworker_products(harness, selection):
    from bit import product_access
    service, store, state = harness
    state['rows'][1] = row(1, organization_key='acme', owner_user_id=8, visibility='enterprise')
    with product_access.scope({'id': 7, 'role_key': 'member', 'organization_key': 'acme'}):
        with pytest.raises(PermissionError, match='只能操作自己的产品'):
            service.start({'list_mode': 'products', 'selection_mode': selection, 'item_ids': [1]}, 7)
    assert not store.list(7)
