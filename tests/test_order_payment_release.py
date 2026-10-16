from unittest.mock import MagicMock, Mock, patch

import pytest
from werkzeug.datastructures import MultiDict

from bit import order_payment_release as release
from mercado_api.client import MercadoLibreClient


def payment(state='released', date='2026-10-12T23:00:00-06:00', id='1'):
    return {'payment_id': id, 'money_release_status': state, 'money_release_date': date}


@pytest.mark.parametrize('payments,state,day', [
    ([payment()], 'released', '2026-10-12'),
    ([payment('pending', '2030-01-01')], 'unreleased', None),
    ([payment('blocked')], 'unreleased', None),
    ([payment(), payment('pending', id='2')], 'partial', '2026-10-12'),
    ([payment(), payment(date='2026-10-13', id='2')], 'released', '2026-10-13'),
    ([payment(date='')], 'released', None),
    ([payment(date='not a date')], 'released', None),
    ([payment(), payment(date='', id='2')], 'released', None),
    ([payment(), payment('new-platform-state', id='2')], 'unknown', None),
    ([payment('')], 'unknown', None),
    ([payment(id='')], 'unknown', None),
    ([], 'unknown', None),
])
def test_release_state_and_actual_platform_date(payments, state, day):
    assert release.release_snapshot([{'payment_info': payments}]) == {'state': state, 'release_date': day}


def test_duplicate_payments_do_not_hide_conflicting_reports():
    entries = [{'payment_info': [payment()]}, {'payment_info': [payment()]}]
    assert release.release_snapshot(entries)['state'] == 'released'
    entries[1]['payment_info'][0]['money_release_status'] = 'pending'
    assert release.release_snapshot(entries)['state'] == 'unknown'


def test_billing_client_uses_read_only_batch_and_local_seller():
    client = MercadoLibreClient('offline', session=Mock())
    client.request = Mock(return_value={'results': []})
    client.get_order_billing(['1', '2', '1'], seller_id='400')
    client.request.assert_called_once_with('GET', '/billing/integration/group/ML/order/details',
                                           params={'order_ids': '1,2', 'seller_id': '400'})
    for ids in ([], ['bad'], list(range(61))):
        with pytest.raises(ValueError):
            client.get_order_billing(ids, seller_id='400')


def test_sync_batches_and_matches_exact_order_and_seller(monkeypatch):
    from bit import bit_mysql, bit_order_sync
    rows = [{'token_id': 7, 'order_id': str(i), 'seller_id': '400'} for i in range(1, 63)]
    rows += [{'token_id': 8, 'order_id': '90', 'seller_id': '500'}]
    monkeypatch.setattr(release, 'pending_orders', lambda limit: rows)
    monkeypatch.setattr(bit_mysql, 'get_mercado_store_token', lambda id: {'id': id, 'enabled': 1})
    client = Mock()
    client.get_order_billing.side_effect = lambda ids, seller_id: {'results': [
        {'order_id': id, 'payment_info': [payment()]} for id in [*ids[:-1], '999999']
    ]}
    monkeypatch.setattr(bit_order_sync, '_client_and_token', lambda token: (client, token))
    save = Mock()
    monkeypatch.setattr(release, 'save_results', save)
    assert release.sync_pending()['requested'] == 63
    assert [len(c.args[0]) for c in client.get_order_billing.call_args_list] == [60, 2, 1]
    assert [c.kwargs['seller_id'] for c in client.get_order_billing.call_args_list] == ['400', '400', '500']
    assert save.call_args_list[0].args[1]['1']['state'] == 'released'
    assert save.call_args_list[0].args[1]['60']['state'] == 'unknown'
    assert all('999999' not in c.args[1] for c in save.call_args_list)


@pytest.mark.parametrize('seller,response', [
    ('400', RuntimeError('403')),
    ('400', {'results': [], 'total': 1}),
    ('', {'results': []}),
])
def test_failed_or_incomplete_reads_do_not_become_unreleased(monkeypatch, seller, response):
    from bit import bit_mysql, bit_order_sync
    monkeypatch.setattr(release, 'pending_orders', lambda limit: [{'token_id': 1, 'order_id': '22', 'seller_id': seller}])
    monkeypatch.setattr(bit_mysql, 'get_mercado_store_token', lambda id: {'id': id, 'enabled': 1})
    client = Mock()
    client.get_order_billing.side_effect = [response]
    monkeypatch.setattr(bit_order_sync, '_client_and_token', lambda token: (client, token))
    save = Mock()
    monkeypatch.setattr(release, 'save_results', save)
    assert release.sync_pending()['failed'] == 1
    assert save.call_args.kwargs['error']
    assert save.call_args.args[1] == {'22': None}
    if not seller:
        client.get_order_billing.assert_not_called()


def test_error_save_preserves_verified_state_and_returns_connection(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    monkeypatch.setattr(release, '_connect', lambda: connection)
    release.save_results(7, {'22': None}, error='offline')
    query, params = cursor.execute.call_args.args
    updates = query.split('ON DUPLICATE KEY UPDATE')[1]
    assert 'release_state' not in updates and 'release_date' not in updates and 'checked_at' not in updates
    assert params == (7, '22', 'offline')
    connection.commit.assert_called_once()
    connection.close.assert_called_once()


def test_scheduler_candidates_are_bounded_due_and_never_reread_released(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    monkeypatch.setattr(release, '_connect', lambda: connection)
    assert release.pending_orders(120) == []
    sql, params = cursor.execute.call_args.args
    assert "r.release_state <> 'released'" in sql
    assert 'INTERVAL 6 HOUR' in sql and 't.enabled=1' in sql
    assert "'$.seller.id'" in sql and params == (120,)
    connection.close.assert_called_once()


def test_shutdown_does_not_save_spurious_errors(monkeypatch):
    from bit import bit_order_sync
    monkeypatch.setattr(release, 'pending_orders', lambda limit: [{'token_id': 1, 'order_id': '22', 'seller_id': '400'}])
    monkeypatch.setattr(bit_order_sync, '_raise_if_interpreter_shutting_down', Mock(side_effect=bit_order_sync._InterpreterShutdownRequested()))
    save = Mock()
    monkeypatch.setattr(release, 'save_results', save)
    with pytest.raises(bit_order_sync._InterpreterShutdownRequested):
        release.sync_pending()
    save.assert_not_called()


@pytest.mark.parametrize('outcome,delay', [({'requested': 120}, 2), (RuntimeError('offline'), 21600)])
def test_maintenance_worker_runs_release_sync_under_existing_lock(monkeypatch, outcome, delay):
    from bit import bit_order_sync
    lock = Mock()
    lock.acquire.return_value = True
    stop = Mock()
    stop.is_set.side_effect = [False, True]
    monkeypatch.setattr(bit_order_sync, 'InterProcessLock', lambda *a, **k: lock)
    monkeypatch.setattr(bit_order_sync, '_financial_backfill_stop_event', stop)
    monkeypatch.setattr(bit_order_sync.bit_mysql, 'backfill_mercado_order_sale_fees', lambda: {})
    monkeypatch.setattr(bit_order_sync, 'backfill_order_financials', lambda **kw: {'requested': 0})
    monkeypatch.setattr(bit_order_sync.bit_mysql, 'refresh_mercado_order_quoted_freight', lambda **kw: {'requested': 0})
    def sync(**kwargs):
        assert lock.acquire.called and not lock.release.called
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(release, 'sync_pending', sync)
    bit_order_sync._financial_backfill_loop()
    lock.release.assert_called_once()
    stop.wait.assert_called_once_with(delay)


def test_release_filter_is_forwarded_and_validated():
    from bit import bit_interface, bit_db_api
    assert bit_interface._order_list_query_params(MultiDict({'payment_release': 'released'}))['payment_release'] == 'released'
    with pytest.raises(ValueError):
        bit_interface._order_list_query_params(MultiDict({'payment_release': "released' OR 1=1"}))
    with patch.object(bit_db_api, 'DB_MODE', 'api'), patch.object(bit_db_api, '_request', return_value={}) as request:
        bit_db_api.list_orders(payment_release='unreleased', allowed_store_ids=[7])
    assert request.call_args.kwargs['params']['payment_release'] == 'unreleased'
    assert request.call_args.kwargs['params']['allowed_store_id'] == [7]


@pytest.mark.parametrize('state', ['released', 'unreleased', 'partial', 'unknown'])
@pytest.mark.parametrize('summary_only', [False, True])
def test_release_filter_is_applied_before_paging_and_summary(state, summary_only):
    from bit import bit_mysql
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {'total': 5}
    cursor.fetchall.return_value = []
    with patch.object(bit_mysql.pymysql, 'connect', return_value=connection), \
         patch.object(bit_mysql, '_prepare_order_list_schema', return_value='mercado_store_tokens'):
        bit_mysql.list_orders(payment_release=state, allowed_store_ids=[7], page=2, page_size=10,
                              include_summary=summary_only, summary_only=summary_only)
    query, params = next(c.args for c in cursor.execute.call_args_list
                         if c.args[0].startswith('CREATE TEMPORARY TABLE `tmp_mercado_order_filter_source`'))
    assert "COALESCE(payment_release.release_state, 'unknown')" in query
    assert '`payment_release_state` = %s' in query and state in params
    assert "`data_origin` <> 'yandex'" in query
    assert 'synced.`token_id` IN (%s)' in query and 7 in params
    assert 'payment_release.token_id=synced.token_id' in query
    if not summary_only:
        detail = next(c.args[0] for c in cursor.execute.call_args_list if c.args[0].startswith('SELECT order_source.*'))
        assert 'payment_release_date' in detail and 'payment_release_checked_at' in detail
