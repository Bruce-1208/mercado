"""Review retries persist every outcome and expose a user-visible task trace."""
from unittest.mock import patch
import pytest
from test_bit_interface_mercado_collection import (
    workbench, offline_console, _client, _reset_publish_state, _actual_shipping,
)


def record(identifier, product, status='pending_review'):
    return {'id':identifier,'product_item_id':product,'token_id':5,'site_id':'MLB',
            'quantity':3,'status':status}


def product(identifier, approved=True):
    return {'id':identifier,'title':'模拟商品','source_item_id':f'MLM{identifier}',
            'review_status':'approved' if approved else 'pending',
            'weight_g':350, **_actual_shipping(350), 'net_proceeds_usd':8}


def test_review_retries_valid_record_and_reports_unapproved_product():
    _reset_publish_state()
    with patch.object(workbench, 'db_get_mercado_product_publish_records_by_ids', return_value=[record(82,10),record(81,9)]), \
         patch.object(workbench, 'db_get_mercado_product_items_by_ids', return_value=[product(10,False),product(9)]), \
         patch.object(workbench.bit_db_api, 'list_mercado_store_tokens', return_value={'rows':[{'id':5,'site_id':'CBT'}]}), \
         patch.object(workbench.threading, 'Thread') as thread:
        response = _client().post('/api/mercado-publish-records/retry', json={'record_ids':[81,82],'review_before_publish':True})
    assert response.status_code == 200
    data = response.get_json()['data']
    assert data['requested_count'] == 2
    assert data['review_issues'][0]['record_ids'] == [82]
    assert '审核状态未通过' in data['review_issues'][0]['issues']
    target = thread.call_args.kwargs['args'][1][0]
    assert [row['id'] for row in target['product_rows']] == [10, 9]
    assert target['source_record_ids'] == {'10': 82, '9': 81}
    assert thread.call_args.kwargs['kwargs']['review_before_publish'] is True
    thread.return_value.start.assert_called_once()
    _reset_publish_state()


def test_review_all_invalid_still_starts_a_recorded_attempt():
    _reset_publish_state()
    with patch.object(workbench, 'db_get_mercado_product_publish_records_by_ids', return_value=[record(81,9)]), \
         patch.object(workbench, 'db_get_mercado_product_items_by_ids', return_value=[product(9,False)]), \
         patch.object(workbench.bit_db_api, 'list_mercado_store_tokens', return_value={'rows':[{'id':5,'site_id':'CBT'}]}), \
         patch.object(workbench.threading, 'Thread') as thread:
        response = _client().post('/api/mercado-publish-records/retry', json={'record_ids':[81],'review_before_publish':True})
    assert response.status_code == 200
    data = response.get_json()['data']
    assert data['requested_count'] == 1
    assert data['running'] is True
    assert data['review_issues'][0]['product_item_id'] == 9
    assert [row['id'] for row in thread.call_args.kwargs['args'][1][0]['product_rows']] == [9]
    assert thread.call_args.kwargs['kwargs']['review_before_publish'] is True
    thread.return_value.start.assert_called_once()
    _reset_publish_state()


def test_review_thread_start_failure_returns_json_and_finishes_state():
    _reset_publish_state()
    with patch.object(
        workbench,
        'db_get_mercado_product_publish_records_by_ids',
        return_value=[record(81, 9)],
    ), patch.object(
        workbench,
        'db_get_mercado_product_items_by_ids',
        return_value=[product(9)],
    ), patch.object(
        workbench.bit_db_api,
        'list_mercado_store_tokens',
        return_value={'rows': [{'id': 5, 'site_id': 'CBT'}]},
    ), patch.object(workbench.threading, 'Thread') as thread:
        thread.return_value.start.side_effect = RuntimeError('线程资源耗尽')
        response = _client().post(
            '/api/mercado-publish-records/retry',
            json={'record_ids': [81], 'review_before_publish': True},
        )

    assert response.status_code == 500
    assert response.is_json
    body = response.get_json()
    assert body['status'] == 'error'
    assert '线程启动失败' in body['message']
    assert body['data']['running'] is False
    assert body['data']['status'] == 'error'
    assert body['data']['finished_at']
    assert '线程启动失败' in body['data']['logs'][-1]
    assert workbench._mercado_publish_state['running'] is False
    assert workbench._mercado_publish_state['status'] == 'error'
    assert workbench._mercado_publish_state['finished_at']
    assert '线程启动失败' in workbench._mercado_publish_state['logs'][-1]
    _reset_publish_state()


def test_review_successful_record_still_cannot_republish():
    _reset_publish_state()
    with patch.object(workbench, 'db_get_mercado_product_publish_records_by_ids', return_value=[record(81,9,'published')]), \
         patch.object(workbench.threading, 'Thread') as thread:
        response = _client().post('/api/mercado-publish-records/retry', json={'record_ids':[81],'review_before_publish':True})
    assert response.status_code == 400
    thread.assert_not_called()


def test_review_requires_product_operation_permission():
    _reset_publish_state()
    with patch.object(workbench, 'db_get_mercado_product_publish_records_by_ids', return_value=[record(81,9)]), \
         patch.object(workbench.bit_db_api, 'check_catalog_access', side_effect=PermissionError('read-only')) as access, \
         patch.object(workbench.threading, 'Thread') as thread:
        response = _client().post('/api/mercado-publish-records/retry', json={'record_ids':[81],'review_before_publish':True})
    assert response.status_code == 403
    access.assert_called_once_with('products',[9],operate=True)
    thread.assert_not_called()


def test_review_worker_records_invalid_and_successful_outcomes_with_logs():
    _reset_publish_state()
    rows = [product(9), product(10, False)]
    created = []
    updates = []

    def create_attempts(selected_rows, **metadata):
        created.append(([row['id'] for row in selected_rows], metadata))
        return {9: 109, 10: 110}

    def publish(selected_rows, **kwargs):
        assert [row['id'] for row in selected_rows] == [9]
        assert kwargs['create_records'](selected_rows) == {9: 109, 10: 110}
        kwargs['update_record'](
            109,
            status='published',
            published_item_id='MLB900',
            failure_reason='',
            result={'id': 'MLB900'},
            finished=True,
        )
        kwargs['on_progress']({
            'current': 1,
            'published_count': 1,
            'failed_count': 0,
            'pending_review_count': 0,
            'source_item_id': 'MLM9',
            'worker_count': 1,
            'result': {
                'record_id': 109, 'product_id': 9, 'source_item_id': 'MLM9',
                'status': 'published', 'published_item_id': 'MLB900', 'message': '上架成功',
            },
        })
        # Visible before the publisher returns the whole target's final result.
        state = workbench._mercado_publish_state_snapshot()
        assert {item['product_id']: item['status'] for item in state['results']} == {
            9: 'published', 10: 'failed',
        }
        assert any('成功 · MLM9' in line and 'MLB900' in line for line in state['logs'])
        assert any('失败 · MLM10' in line for line in state['logs'])
        return {
            'requested_count': 1,
            'published_count': 1,
            'failed_count': 0,
            'pending_review_count': 0,
            'results': [{
                'record_id': 109,
                'product_id': 9,
                'source_item_id': 'MLM9',
                'status': 'published',
                'published_item_id': 'MLB900',
                'message': '上架成功',
            }],
        }

    target = {
        'token_id': 5,
        'store_name': '测试店铺',
        'site_id': 'MLB',
        'site_name': '巴西',
        'product_rows': rows,
        'source_record_ids': {'9': 81, '10': 82},
    }
    with patch.object(
        workbench, 'db_get_published_mercado_product_item_ids', return_value=[]
    ), patch.object(
        workbench, 'db_get_existing_mercado_user_product_ids', return_value={}
    ), patch.object(
        workbench, 'db_create_mercado_product_publish_records', side_effect=create_attempts
    ), patch.object(
        workbench, 'db_update_mercado_product_publish_record',
        side_effect=lambda record_id, **changes: updates.append((record_id, changes)),
    ), patch.object(
        workbench, '_screen_mercado_publish_rows', side_effect=lambda selected, *_: selected
    ), patch.object(
        workbench, '_build_mercado_infringement_preflight', return_value=None
    ), patch(
        'erp.mercadolibre_batch_publish.publish_product_batch', side_effect=publish
    ):
        workbench._run_mercado_product_publish_targets(
            [], [target], quantity=1, worker_count=1,
            batch_id='retry-worker', created_by='测试用户',
            review_before_publish=True,
        )

    assert created[0][0] == [9, 10]
    assert created[0][1]['batch_id'] == 'retry-worker-1'
    invalid = next(changes for record_id, changes in updates if record_id == 110 and changes.get('status') == 'failed')
    assert invalid['status'] == 'failed'
    assert invalid['started'] is True and invalid['finished'] is True
    assert '审核状态未通过' in invalid['failure_reason']
    assert invalid['result'] == {
        'stage': 'local_review',
        'review_before_publish': True,
        'issues': ['审核状态未通过'],
        'platform_submitted': False,
        'source_record_id': 82,
    }
    successful = next(changes for record_id, changes in updates if record_id == 109 and changes.get('status') != 'publishing')
    assert successful['status'] == 'published'
    assert successful['published_item_id'] == 'MLB900'

    state = dict(workbench._mercado_publish_state)
    assert state['running'] is False
    assert state['status'] == 'partial'
    assert state['requested_count'] == state['processed_count'] == 2
    assert state['published_count'] == 1
    assert state['failed_count'] == 1
    assert state['pending_review_count'] == 0
    assert {item['product_id']: item['status'] for item in state['results']} == {
        9: 'published', 10: 'failed',
    }
    assert any('已创建 2 条上架尝试记录' in line for line in state['logs'])
    assert any('失败 · MLM10' in line for line in state['logs'])
    assert any('成功 · MLM9' in line for line in state['logs'])
    assert sum('成功 · MLM9' in line for line in state['logs']) == 1
    assert '成功 1 件；失败 1 件；待审核 0 件' in state['logs'][-1]
    _reset_publish_state()


def test_review_worker_counts_pending_review_in_terminal_state():
    _reset_publish_state()
    updates = []
    row = product(9)

    def publish(selected_rows, **kwargs):
        assert kwargs['create_records'](selected_rows) == {9: 109}
        kwargs['update_record'](
            109,
            status='pending_review',
            failure_reason='最终上架标题存在侵权风险',
            result={'stage': 'publication_preflight'},
            finished=True,
        )
        kwargs['on_progress']({
            'current': 1,
            'published_count': 0,
            'failed_count': 0,
            'pending_review_count': 1,
            'source_item_id': 'MLM9',
            'worker_count': 1,
        })
        return {
            'requested_count': 1,
            'published_count': 0,
            'failed_count': 0,
            'pending_review_count': 1,
            'results': [{
                'record_id': 109,
                'product_id': 9,
                'source_item_id': 'MLM9',
                'status': 'pending_review',
                'message': '最终上架标题存在侵权风险',
            }],
        }

    with patch.object(
        workbench, 'db_get_published_mercado_product_item_ids', return_value=[]
    ), patch.object(
        workbench, 'db_get_existing_mercado_user_product_ids', return_value={}
    ), patch.object(
        workbench, 'db_create_mercado_product_publish_records', return_value={9: 109}
    ), patch.object(
        workbench, 'db_update_mercado_product_publish_record',
        side_effect=lambda record_id, **changes: updates.append((record_id, changes)),
    ), patch.object(
        workbench, '_screen_mercado_publish_rows', side_effect=lambda selected, *_: selected
    ), patch.object(
        workbench, '_build_mercado_infringement_preflight', return_value=None
    ), patch(
        'erp.mercadolibre_batch_publish.publish_product_batch', side_effect=publish
    ):
        workbench._run_mercado_product_publish_targets(
            [], [{
                'token_id': 5,
                'store_name': '测试店铺',
                'site_id': 'MLB',
                'site_name': '巴西',
                'product_rows': [row],
                'source_record_ids': {'9': 81},
            }],
            quantity=1, worker_count=1, batch_id='retry-pending',
            created_by='测试用户', review_before_publish=True,
        )

    pending = next(changes for record_id, changes in updates if record_id == 109 and changes.get('status') != 'publishing')
    assert pending['status'] == 'pending_review'
    state = dict(workbench._mercado_publish_state)
    assert state['running'] is False
    assert state['status'] == 'error'
    assert state['processed_count'] == 1
    assert state['published_count'] == state['failed_count'] == 0
    assert state['pending_review_count'] == 1
    assert state['results'][0]['status'] == 'pending_review'
    assert '成功 0 件；失败 0 件；待审核 1 件' in state['message']
    assert any('待审核 · MLM9' in line for line in state['logs'])
    _reset_publish_state()


@pytest.mark.parametrize('failing_query', ['published_items', 'user_products'])
def test_review_worker_fails_closed_when_publish_history_query_fails(failing_query):
    _reset_publish_state()
    updates = []

    def published_items(*_args, **_kwargs):
        if failing_query == 'published_items':
            raise RuntimeError('published history unavailable')
        return []

    def user_products(*_args, **_kwargs):
        if failing_query == 'user_products':
            raise RuntimeError('user product history unavailable')
        return {}

    with patch.object(
        workbench,
        'db_get_published_mercado_product_item_ids',
        side_effect=published_items,
    ), patch.object(
        workbench,
        'db_get_existing_mercado_user_product_ids',
        side_effect=user_products,
    ), patch.object(
        workbench,
        'db_create_mercado_product_publish_records',
        return_value={9: 109},
    ), patch.object(
        workbench,
        'db_update_mercado_product_publish_record',
        side_effect=lambda record_id, **changes: updates.append((record_id, changes)),
    ), patch.object(
        workbench,
        '_screen_mercado_publish_rows',
        side_effect=lambda selected, *_args: selected,
    ), patch(
        'erp.mercadolibre_batch_publish.publish_product_batch'
    ) as publisher:
        workbench._run_mercado_product_publish_targets(
            [], [{
                'token_id': 5,
                'store_name': '测试店铺',
                'site_id': 'MLB',
                'site_name': '巴西',
                'product_rows': [product(9)],
                'source_record_ids': {'9': 81},
            }],
            quantity=1,
            worker_count=1,
            batch_id=f'retry-history-{failing_query}',
            created_by='测试用户',
            review_before_publish=True,
        )

    publisher.assert_not_called()
    failed = next(
        changes for record_id, changes in updates
        if record_id == 109 and changes.get('status') == 'failed'
    )
    assert failed['finished'] is True
    assert failed['result']['stage'] == 'preparation'
    assert failed['result']['platform_submitted'] is False
    assert '读取历史' in failed['failure_reason']
    state = dict(workbench._mercado_publish_state)
    assert state['running'] is False
    assert state['status'] == 'error'
    assert state['failed_count'] == 1
    assert state['pending_review_count'] == 0
    assert state['results'][0]['status'] == 'failed'
    assert state['results'][0]['record_persisted'] is True
    _reset_publish_state()


def test_publish_record_list_returns_current_publish_state_and_logs():
    _reset_publish_state()
    with workbench._mercado_publish_lock:
        workbench._mercado_publish_state.update({
            'running': False,
            'batch_id': 'retry-visible',
            'status': 'partial',
            'requested_count': 2,
            'processed_count': 2,
            'published_count': 1,
            'failed_count': 1,
            'pending_review_count': 0,
            'results': [
                {'product_id': 9, 'status': 'published'},
                {'product_id': 10, 'status': 'failed'},
            ],
            'logs': ['[12:00:00] 成功 · MLM9', '[12:00:01] 失败 · MLM10'],
        })
    listed = {
        'rows': [],
        'total': 0,
        'counts': {'all': 0, 'published': 0, 'failed': 0},
    }
    with patch.object(
        workbench, 'db_list_mercado_product_publish_records', return_value=listed
    ):
        response = _client().get('/api/mercado-publish-records')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    data = response.get_json()['data']
    assert data['publish_running'] is False
    assert data['publish_state']['batch_id'] == 'retry-visible'
    assert data['publish_state']['status'] == 'partial'
    assert data['publish_state']['published_count'] == 1
    assert data['publish_state']['failed_count'] == 1
    assert data['publish_state']['pending_review_count'] == 0
    assert data['publish_state']['logs'] == [
        '[12:00:00] 成功 · MLM9', '[12:00:01] 失败 · MLM10',
    ]
    assert [item['status'] for item in data['publish_state']['results']] == [
        'published', 'failed',
    ]
    _reset_publish_state()


def test_publish_record_list_hides_state_for_unauthorized_store():
    _reset_publish_state()
    with workbench._mercado_publish_lock:
        workbench._mercado_publish_state.update({
            'running': True,
            'batch_id': 'private-batch',
            'status': 'running',
            'message': '正在上架私有店铺商品',
            'token_id': 5,
            'token_ids': [5],
            'results': [{'product_id': 9, 'token_id': 5, 'status': 'failed'}],
            'logs': ['[12:00:00] 私有店铺日志'],
        })
    listed = {
        'rows': [],
        'total': 0,
        'counts': {'all': 0, 'published': 0, 'failed': 0},
    }
    with patch.object(
        workbench,
        '_authorized_token_ids_for_user',
        return_value={99},
    ), patch.object(
        workbench,
        'db_list_mercado_product_publish_records',
        return_value=listed,
    ) as list_records:
        response = _client().get('/api/mercado-publish-records')

    assert response.status_code == 200
    data = response.get_json()['data']
    assert data['publish_running'] is True
    assert data['publish_state']['running'] is True
    assert data['publish_state']['batch_id'] == ''
    assert data['publish_state']['logs'] == []
    assert data['publish_state']['results'] == []
    list_records.assert_called_once()
    assert list_records.call_args.kwargs['token_ids'] == [99]
    _reset_publish_state()


@pytest.mark.parametrize('all_invalid', [False, True])
def test_product_list_review_retains_invalid_products_and_reports_reasons(all_invalid):
    _reset_publish_state()
    rows = [product(9, not all_invalid), product(10, False)]
    with patch.object(workbench, 'db_get_mercado_product_items_by_ids', return_value=rows), \
         patch.object(workbench, 'db_move_mercado_product_items_to_collection') as move, \
         patch.object(workbench.bit_db_api, 'list_mercado_store_tokens', return_value={'rows':[{'id':5,'site_id':'CBT'}]}), \
         patch.object(workbench.threading, 'Thread') as thread:
        response = _client().post('/api/mercado-products/publish', json={
            'product_item_ids':[9,10], 'token_ids':[5], 'site_ids':['MLB'],
            'quantity':3, 'review_before_publish':True})
    assert response.status_code == 200, response.get_json()
    data = response.get_json()['data']
    assert len(data['review_issues']) == (2 if all_invalid else 1)
    assert '审核状态未通过' in data['review_issues'][-1]['issues']
    move.assert_not_called()
    assert data['requested_count'] == 2
    assert [row['id'] for row in thread.call_args.kwargs['args'][0]] == [9, 10]
    target = thread.call_args.kwargs['args'][1][0]
    assert set(target['attempt_record_ids']) == {9, 10}
    thread.return_value.start.assert_called_once()
    _reset_publish_state()


def test_product_list_review_checks_original_content_before_starting():
    _reset_publish_state()
    row = {**product(9), 'source_type':'ai_original', 'source_snapshot_json':{}}
    with patch.object(workbench, 'db_get_mercado_product_items_by_ids', return_value=[row]), \
         patch.object(workbench, 'db_move_mercado_product_items_to_collection') as move, \
         patch.object(workbench.bit_db_api, 'list_mercado_store_tokens', return_value={'rows':[{'id':5,'site_id':'CBT'}]}), \
         patch.object(workbench.threading, 'Thread') as thread:
        response = _client().post('/api/mercado-products/publish', json={
            'product_item_ids':[9], 'token_ids':[5], 'site_ids':['MLB'], 'review_before_publish':True})
    assert response.status_code == 200
    assert 'AI 原创任务尚未完成' in response.get_json()['data']['review_issues'][0]['issues']
    move.assert_not_called()
    assert thread.call_args.kwargs["args"][1][0]["attempt_record_ids"]
    thread.return_value.start.assert_called_once()


@pytest.mark.parametrize('mode', ['immediate', 'retry', 'scheduled'])
def test_submission_commits_all_attempts_before_any_worker_can_run(mode):
    from datetime import datetime, timedelta, timezone
    _reset_publish_state()
    saved = {}
    def create(rows, **metadata):
        result = {}
        for row in rows:
            identifier = 100 + row['id']
            saved[identifier] = {'status': 'pending', 'batch_id': metadata['batch_id']}
            result[row['id']] = identifier
        return result
    def update(identifier, **changes):
        saved[identifier].update(changes)
    def assert_queued():
        assert set(saved) == {109, 110}
        assert all(row['status'] == 'pending' and row['result']['stage'] == 'queued' for row in saved.values())
    with (
        patch.object(workbench, 'db_get_mercado_product_items_by_ids', return_value=[product(9), product(10, False)]),
        patch.object(workbench, 'db_get_mercado_product_publish_records_by_ids', return_value=[record(81, 9), record(82, 10)]),
        patch.object(workbench.bit_db_api, 'list_mercado_store_tokens', return_value={'rows': [{'id': 5, 'site_id': 'CBT'}]}),
        patch.object(workbench, 'db_create_mercado_product_publish_records', side_effect=create),
        patch.object(workbench, 'db_update_mercado_product_publish_record', side_effect=update),
        patch.object(workbench.bit_db_api, 'create_mercado_product_publish_schedule', return_value=7) as schedule,
        patch.object(workbench.threading, 'Thread') as thread,
    ):
        thread.return_value.start.side_effect = assert_queued
        payload = {'product_item_ids': [9, 10], 'token_ids': [5], 'site_ids': ['MLB'], 'review_before_publish': True}
        path = '/api/mercado-products/publish'
        if mode == 'retry':
            path = '/api/mercado-publish-records/retry'
            payload = {'record_ids': [81, 82], 'review_before_publish': True}
        if mode == 'scheduled':
            payload['scheduled_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        response = _client().post(path, json=payload)
    assert response.status_code == 200, response.get_json()
    assert_queued()
    if mode == 'scheduled':
        thread.assert_not_called()
        assert schedule.call_args.args[1]['targets'][0]['attempt_record_ids'] == {9: 109, 10: 110}
        assert all(row['result']['scheduled_at'] for row in saved.values())
    else:
        thread.return_value.start.assert_called_once()
    _reset_publish_state()


def test_retry_start_failure_finishes_durable_record_as_not_executed():
    _reset_publish_state()
    updates = []
    with (
        patch.object(workbench, 'db_get_mercado_product_publish_records_by_ids', return_value=[record(81, 9)]),
        patch.object(workbench, 'db_get_mercado_product_items_by_ids', return_value=[product(9)]),
        patch.object(workbench.bit_db_api, 'list_mercado_store_tokens', return_value={'rows': [{'id': 5, 'site_id': 'CBT'}]}),
        patch.object(workbench, 'db_update_mercado_product_publish_record', side_effect=lambda identifier, **changes: updates.append(changes)),
        patch.object(workbench.threading, 'Thread') as thread,
    ):
        thread.return_value.start.side_effect = RuntimeError('线程资源不足')
        response = _client().post('/api/mercado-publish-records/retry', json={'record_ids': [81], 'review_before_publish': True})
    assert response.status_code == 500
    assert updates[0]['status'] == 'pending'
    assert updates[-1]['status'] == 'failed'
    assert updates[-1]['result'] == {'stage': 'not_executed', 'platform_submitted': False}
    assert updates[-1]['finished'] is True
    assert '线程资源不足' in updates[-1]['failure_reason']
    _reset_publish_state()


def test_worker_reuses_submitted_attempt_instead_of_creating_another():
    _reset_publish_state()
    with (
        patch.object(workbench, 'db_create_mercado_product_publish_records') as create,
        patch.object(workbench, 'db_get_published_mercado_product_item_ids', return_value=[]),
        patch.object(workbench, 'db_get_existing_mercado_user_product_ids', return_value={}),
        patch.object(workbench, 'db_update_mercado_product_publish_record') as update,
    ):
        workbench._run_mercado_product_publish_targets([], [{
            'token_id': 5, 'site_id': 'MLB', 'product_rows': [product(9, False)],
            'attempt_record_ids': {'9': 109}, 'batch_id': 'queued-1',
        }], quantity=1, worker_count=1, batch_id='queued', created_by='测试', review_before_publish=True)
    create.assert_not_called()
    assert update.call_args_list[0].args == (109,)
    assert update.call_args_list[0].kwargs['result']['stage'] == 'preparing'
    assert update.call_args_list[-1].kwargs['result']['platform_submitted'] is False
    assert update.call_args_list[-1].kwargs['status'] == 'failed'
    _reset_publish_state()
