"""USD publication floors use the same formula across tasks and retries."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from erp import mercadolibre_batch_publish as pricing
from test_erp_mercadolibre_batch_publish import _rows
from test_bit_interface_mercado_collection import workbench, offline_console, _client  # noqa: F401


@pytest.mark.parametrize('floor,expected', [(None, 28.8), ('', 28.8), (0, 28.8), (20, 28.8), (28.8, 28.8), (35, 35)])
def test_floor_is_applied_after_all_ratios(floor, expected):
    row = {'net_proceeds_usd': 12, 'management_category_listing_ratio_percent': 150}
    assert pricing.discounted_net_proceeds_usd(row, 80, 200, floor) == expected
    assert row['net_proceeds_usd'] == 12


@pytest.mark.parametrize('invalid', [-1, 'bad', 'NaN', 'Infinity', 1e13, '0.001', True])
def test_reject_invalid_floor(invalid):
    with pytest.raises(ValueError, match='最低净收益'):
        pricing.discounted_net_proceeds_usd({'net_proceeds_usd': 10}, 100, 100, invalid)


def test_floor_rescues_positive_formula_that_rounds_to_zero():
    assert pricing.discounted_net_proceeds_usd({'net_proceeds_usd': '0.0001'}, 1, 1, 5) == 5
    with pytest.raises(ValueError, match='必须大于 0'):
        pricing.discounted_net_proceeds_usd({'net_proceeds_usd': 0}, 100, 100, 5)


@pytest.mark.parametrize('failure', [False, True])
def test_real_publisher_submits_floor_and_saves_calculation(monkeypatch, failure):
    monkeypatch.setattr(pricing, '_token_record', lambda _: {'access_token': 'test'})
    publish = Mock(side_effect=RuntimeError('rate limit') if failure else None,
                   return_value={'result': {'id': 'CBT123'}})
    monkeypatch.setattr(pricing, 'follow_sell', publish)
    saved = []
    result = pricing.publish_product_batch(
        [_rows()[0]], token_id=1, discount_rate=80, net_proceeds_ratio=200,
        minimum_net_proceeds_usd=25, client=object(),
        update_state=lambda *a, **kw: None,
        create_records=lambda *a, **kw: {11: 101},
        update_record=lambda *a, **kw: saved.append(kw),
    )
    assert publish.call_args.kwargs['net_proceeds'] == 25
    calculation = saved[-1]['result']['net_proceeds_calculation']
    assert calculation['calculated_net_proceeds_usd'] == 16
    assert calculation['minimum_net_proceeds_usd'] == 25
    assert calculation['minimum_applied'] is True
    assert result['failed_count'] == int(failure)


@pytest.fixture
def publish_context(monkeypatch):
    monkeypatch.setattr(workbench, '_mercado_publish_state', {'running': False})
    rows = _rows()
    monkeypatch.setattr(workbench, 'db_get_mercado_product_items_by_ids', lambda ids: [r for r in rows if r['id'] in ids])
    monkeypatch.setattr(workbench.bit_db_api, 'list_mercado_store_tokens', lambda: {'rows': [{'id': 7, 'site_id': 'CBT'}]})
    monkeypatch.setattr(workbench, '_filter_mercado_tokens_for_user', lambda data, *a: data)
    worker = Mock()
    monkeypatch.setattr(workbench.threading, 'Thread', worker)
    return _client(), worker, rows


@pytest.mark.parametrize('source', ['collected', 'ai_original'])
@pytest.mark.parametrize('scheduled', [False, True])
def test_entry_and_schedule_preserve_minimum(publish_context, monkeypatch, source, scheduled):
    client, worker, rows = publish_context
    rows[0]['source_type'] = source
    saved = Mock(return_value=55)
    monkeypatch.setattr(workbench.bit_db_api, 'create_mercado_product_publish_schedule', saved)
    body = {'product_item_ids': [11], 'token_id': 7, 'site_id': 'MLM', 'minimum_net_proceeds_usd': 25.5}
    if scheduled:
        body['scheduled_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    response = client.post('/api/mercado-products/publish', json=body)
    assert response.status_code == 200, response.json
    assert response.json['data']['minimum_net_proceeds_usd'] == 25.5
    if scheduled:
        payload = saved.call_args.args[1]
        assert payload['minimum_net_proceeds_usd'] == 25.5
        assert payload['targets'][0]['minimum_net_proceeds_usd'] == 25.5
        worker.assert_not_called()
    else:
        assert worker.call_args.kwargs['args'][1][0]['minimum_net_proceeds_usd'] == 25.5


@pytest.mark.parametrize('invalid', [-1, 'NaN', 1.001])
def test_route_rejects_floor_before_starting_task(publish_context, invalid):
    client, worker, _ = publish_context
    response = client.post('/api/mercado-products/publish', json={
        'product_item_ids': [11], 'token_id': 7, 'minimum_net_proceeds_usd': invalid})
    assert response.status_code == 400
    worker.assert_not_called()


def test_retry_keeps_distinct_floors_in_separate_targets(publish_context, monkeypatch):
    client, worker, _ = publish_context
    records = [{'id': i, 'product_item_id': pid, 'token_id': 7, 'site_id': 'MLM',
                'quantity': 3, 'status': 'failed', 'result_json': {'net_proceeds_calculation': {
                    'minimum_net_proceeds_usd': floor, 'listing_ratio_percent': 200}}}
               for i, pid, floor in [(101, 11, 25), (102, 12, 35)]]
    monkeypatch.setattr(workbench, 'db_get_mercado_product_publish_records_by_ids', lambda ids: records)
    response = client.post('/api/mercado-publish-records/retry', json={'record_ids': [101, 102]})
    assert response.status_code == 200, response.json
    targets = worker.call_args.kwargs['args'][1]
    assert {t['minimum_net_proceeds_usd'] for t in targets} == {25, 35}


def test_auto_rate_limit_retry_keeps_floor(publish_context, monkeypatch):
    from erp import mercadolibre_collection_store as store
    _, worker, _ = publish_context
    monkeypatch.setattr(workbench, '_mercado_rate_limit_retry_last_scan', 0)
    monkeypatch.setattr(store, 'claim_due_rate_limited_product_publish_records', lambda **kw: [
        {'id': 101, 'product_item_id': 11, 'token_id': 7, 'site_id': 'MLM', 'quantity': 3,
         'result_json': {'net_proceeds_calculation': {'minimum_net_proceeds_usd': 25.5}}}])
    assert workbench._claim_and_start_due_mercado_rate_limit_retries()
    assert worker.call_args.kwargs['args'][1][0]['minimum_net_proceeds_usd'] == 25.5


def test_worker_passes_target_floor_to_publisher(publish_context, monkeypatch):
    _, _, rows = publish_context
    monkeypatch.setattr(workbench, 'db_get_published_mercado_product_item_ids', lambda *a, **kw: [])
    monkeypatch.setattr(workbench, 'db_get_existing_mercado_user_product_ids', lambda *a, **kw: {})
    publish = Mock(return_value={'published_count': 1, 'results': []})
    monkeypatch.setattr(pricing, 'publish_product_batch', publish)
    workbench._run_mercado_product_publish_targets(
        rows[:1], [{'token_id': 7, 'site_id': 'MLM', 'discount_rate': 80,
                    'minimum_net_proceeds_usd': 25.5}], 3, 1, 'test', 'tester')
    assert publish.call_args.kwargs['minimum_net_proceeds_usd'] == 25.5


def test_preflight_uses_the_same_floor(publish_context, monkeypatch):
    from erp import mercadolibre_follow_sell as follow
    from erp import mercadolibre_publication_drafts as drafts
    client, _, _ = publish_context
    monkeypatch.setattr(workbench, 'db_get_existing_mercado_user_product_ids', lambda *a, **kw: {})
    monkeypatch.setattr(pricing, 'DatabaseMercadoLibreClient', lambda _: object())
    monkeypatch.setattr(pricing, '_prepared_listing_from_product_row', lambda *a: ({}, ''))
    publish = Mock(return_value={'draft': {'revision': 'r1'}, 'preflight': {'status': 'ready'}})
    monkeypatch.setattr(follow, 'follow_sell', publish)
    monkeypatch.setattr(drafts, 'shared_draft_storage', lambda *a, **kw: None)
    response = client.post('/api/mercado-products/attribute-preflight', json={
        'product_item_ids': [11], 'token_id': 7, 'minimum_net_proceeds_usd': 25.5})
    assert response.status_code == 200, response.json
    assert publish.call_args.kwargs['net_proceeds'] == 25.5


def test_scheduled_execution_passes_saved_floor_to_worker(publish_context, monkeypatch):
    _, _, rows = publish_context
    run = Mock()
    monkeypatch.setattr(workbench, '_run_mercado_product_publish_targets', run)
    monkeypatch.setattr(workbench.bit_db_api, 'update_mercado_product_publish_schedule', Mock())
    workbench._run_mercado_product_publish_schedule({'id': 55, 'payload': {
        'product_item_ids': [11], 'quantity': 3, 'worker_count': 1,
        'targets': [{'token_id': 7, 'site_id': 'MLM', 'minimum_net_proceeds_usd': 25.5}]}})
    assert run.call_args.args[1][0]['minimum_net_proceeds_usd'] == 25.5
