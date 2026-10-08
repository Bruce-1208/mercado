from unittest.mock import AsyncMock, MagicMock, patch
from bit import yandex_order_sync as sync
import asyncio
import json

STORE = {'id': 2, 'campaign_id': 33, 'business_id': 44, 'alias': 'Yandex test', 'access_token': 'test'}
ORDER = {'orderId': 123, 'status': 'DELIVERED', 'creationDate': '2026-09-01T13:00:00+03:00',
         'items': [{'offerId': 'SKU', 'offerName': '商品', 'count': 2, 'prices': {'payment': {'value': 20, 'currencyId': 'CNY'}}}],
         'prices': {'payment': {'value': 20, 'currencyId': 'CNY'}}}


def test_scheduler_syncs_recent_orders_every_15_minutes_and_survives_failure(monkeypatch):
    monkeypatch.delenv('YANDEX_ORDER_SYNC_DISABLED', raising=False)
    monkeypatch.setattr(sync, '_scheduler_started', False)
    stop = MagicMock()
    stop.wait.side_effect = [False, False, True]
    monkeypatch.setattr(sync, '_scheduler_stop', stop)
    with patch.object(sync.threading, 'Thread') as thread, patch.object(sync, 'start', side_effect=[RuntimeError('temporary failure'), True]) as start:
        sync.ensure_scheduler()
        sync.ensure_scheduler()
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        thread.call_args.kwargs['target']()
    assert [call.args for call in stop.wait.call_args_list] == [(900,), (900,), (900,)]
    assert [call.kwargs for call in start.call_args_list] == [{'full': False}, {'full': False}]


def test_identity_currency_quantity_and_timezone():
    row = sync.normalize(STORE, ORDER)
    assert row['order_id'] == 'yandex:33:123'
    assert row['token_id'] == -2
    assert row['date_created'] == '2026-09-01 10:00:00'
    assert row['total_amount'] == 20  # API amount is already a line total.
    assert row['currency_id'] == 'CNY'
    assert row['quantity'] == 2
    assert row['status'] == 'delivered'
    assert json.loads(row['raw_json'])['pack_id'] == '123'


def test_unknown_money_not_invented():
    row = sync.normalize(STORE, {'orderId': 1, 'items': []})
    assert row['total_amount'] is None
    assert 'sale_fee' not in row and 'purchase_cost' not in row


def test_upsert_preserves_manual_fields_and_namespaces_ids():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{'order_id': 'yandex:33:123'}]
    with patch.object(sync.bit_mysql.pymysql, 'connect', return_value=conn), patch.object(sync.bit_mysql, '_ensure_mercado_synced_orders_table'):
        result = sync.upsert_orders(STORE, [ORDER])
    sql, rows = cursor.executemany.call_args.args
    update = sql.split('ON DUPLICATE KEY UPDATE')[1]
    for field in ('purchase_order', 'purchase_cost', 'workflow_status', 'order_remark', 'first_synced_at'):
        assert field not in update
    assert result == {'inserted': 0, 'updated': 1}
    assert rows[0][0] == 'yandex:33:123'


def test_history_walks_empty_windows_and_every_page():
    client = MagicMock()
    client.get_orders = AsyncMock(side_effect=[
        {'orders': [], 'paging': {}},
        {'orders': [ORDER], 'paging': {'nextPageToken': 'page2'}},
        {'orders': [dict(ORDER, orderId=124)], 'paging': {}},
    ])
    auth = MagicMock()
    auth.list_stores.return_value = [STORE]
    auth.get_store.return_value = STORE
    with patch('yandex.app.central_authorization.CentralAuthorizationStore', return_value=auth), patch('yandex.app.yandex_api.YandexSellerClient', return_value=client), patch.object(sync.bit_mysql.pymysql, 'connect'), patch.object(sync, 'upsert_orders', side_effect=lambda s, rows: {'inserted': len(rows), 'updated': 0}):
        result = asyncio.run(sync.sync_all(start_date='2026-08-01', end_date='2026-09-29'))
    assert result['status'] == 'completed' and result['fetched'] == 2
    calls = client.get_orders.call_args_list
    assert calls[0].kwargs['date_from'] == '2026-08-31'
    assert calls[1].kwargs['date_to'] == '2026-08-30'
    assert calls[2].kwargs['page_token'] == 'page2'


def test_repeated_page_token_reports_partial_not_success():
    client = MagicMock()
    client.get_orders = AsyncMock(return_value={'orders': [], 'paging': {'nextPageToken': 'repeat'}})
    auth = MagicMock()
    auth.list_stores.return_value = [STORE]
    auth.get_store.return_value = STORE
    with patch('yandex.app.central_authorization.CentralAuthorizationStore', return_value=auth), patch('yandex.app.yandex_api.YandexSellerClient', return_value=client), patch.object(sync.bit_mysql.pymysql, 'connect'), patch.object(sync, 'upsert_orders', return_value={'inserted': 0, 'updated': 0}):
        result = asyncio.run(sync.sync_all(start_date='2026-09-01', end_date='2026-09-02'))
    assert result['status'] == 'partial'
    assert len(result['errors']) == 1
    assert client.get_orders.call_count == 2


def test_negative_store_filter_survives_api_adapter():
    from bit import bit_db_api
    with patch.object(bit_db_api, 'DB_MODE', 'mysql'), patch.object(bit_db_api, '_local_call', return_value={}) as call:
        bit_db_api.list_orders(store_ids=[-2, 3], origin='yandex')
    assert call.call_args.kwargs['store_ids'] == [-2, 3]


def test_unified_list_keeps_filter_and_page_on_same_authorized_sources():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [{'total': 0}, {}]
    cursor.fetchall.return_value = []
    with patch.object(sync.bit_mysql.pymysql, 'connect', return_value=conn), patch.object(sync.bit_mysql, '_ensure_mercado_synced_orders_table'), patch.object(sync.bit_mysql, '_ensure_mercado_store_tokens_table'), patch.object(sync.bit_mysql, '_ensure_mercado_store_site_settings_table'), patch('erp.mercadolibre_profitability_cache.ensure_profitability_cache_tables'), patch('erp.mercadolibre_store_link_store.ensure_store_link_table'):
        sync.bit_mysql.list_orders(store_ids=[-2], origin='yandex')
    queries = [entry.args for entry in cursor.execute.call_args_list]
    filter_sql = next(args for args in queries if args[0].startswith('CREATE TEMPORARY TABLE `tmp_mercado_order_filter_source`'))
    detail_sql = next(args for args in queries if args[0].startswith('SELECT order_source.*'))
    assert 'unified_yandex_stores WHERE active = 1' in filter_sql[0]
    assert 'unified_yandex_stores WHERE active = 1' in detail_sql[0]
    page = next(args for args in queries if args[0].startswith('CREATE TEMPORARY TABLE `tmp_mercado_order_page`'))
    assert 'yandex' in page[1] and -2 in page[1]


def test_authorization_scope_applies_before_all_facets():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [{'total': 0}, {}]
    cursor.fetchall.return_value = []
    with patch.object(sync.bit_mysql.pymysql, 'connect', return_value=conn), patch.object(sync.bit_mysql, '_ensure_mercado_synced_orders_table'), patch.object(sync.bit_mysql, '_ensure_mercado_store_tokens_table'), patch.object(sync.bit_mysql, '_ensure_mercado_store_site_settings_table'), patch('erp.mercadolibre_profitability_cache.ensure_profitability_cache_tables'), patch('erp.mercadolibre_store_link_store.ensure_store_link_table'):
        sync.bit_mysql.list_orders(allowed_store_ids=[-2], origin='yandex')
    query = next(call.args for call in cursor.execute.call_args_list if call.args[0].startswith('CREATE TEMPORARY TABLE `tmp_mercado_order_filter_source`'))
    assert 'synced.`token_id` IN (%s)' in query[0]
    assert -2 in query[1]


def test_incremental_sync_refreshes_pending_orders_older_than_30_days():
    from datetime import datetime, timezone
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 5, 12, tzinfo=timezone.utc).astimezone(tz)
    client = MagicMock()
    client.get_orders = AsyncMock(return_value={'orders': [], 'paging': {}})
    auth = MagicMock()
    auth.list_stores.return_value = [dict(STORE)]
    auth.get_store.return_value = STORE
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [{'token_id': -2, 'oldest': datetime(2026, 7, 1)}]
    with patch('yandex.app.central_authorization.CentralAuthorizationStore', return_value=auth), patch('yandex.app.yandex_api.YandexSellerClient', return_value=client), patch.object(sync.bit_mysql.pymysql, 'connect', return_value=connection), patch.object(sync.bit_mysql, '_ensure_mercado_synced_orders_table'), patch.object(sync, 'datetime', FixedDatetime):
        result = asyncio.run(sync.sync_all(full=False))
    assert result['status'] == 'completed'
    assert client.get_orders.call_args_list[0].kwargs['date_to'] == '2026-10-05'
    assert client.get_orders.call_args_list[-1].kwargs['date_from'] == '2026-07-01'
    assert client.get_orders.call_count == 4


def test_native_prices_and_enriched_main_image():
    order = dict(ORDER, items=[dict(ORDER['items'][0], image_url='https://example.com/main.jpg')],
                 prices={'payment': {'value': 20, 'currencyId': 'CNY'},
                         'cashback': {'value': 5, 'currencyId': 'CNY'}})
    row = sync.normalize(STORE, order)
    assert row['image_url'] == 'https://example.com/main.jpg'
    assert json.loads(row['raw_json'])['order_items'][0]['item']['thumbnail'] == row['image_url']
    prices = sync.display_prices(order)
    assert prices['original']['value'] == 20
    assert prices['points']['value'] == 5
    assert prices['total'] == {'value': 25, 'currency': 'CNY'}
    assert sync.display_prices(ORDER)['total'] is None
    order['prices']['cashback']['currencyId'] = 'RUR'
    assert sync.display_prices(order)['total'] is None
