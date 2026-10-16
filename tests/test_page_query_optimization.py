from unittest.mock import MagicMock

import pytest

from bit.order_lookup_sql import context_lookup
from bit import service_split, service_topology


@pytest.mark.parametrize('isolation', ['0', '1'])
@pytest.mark.parametrize('path', [
    '/api/db/official-infractions/live',
    '/api/db/official-infractions/live/jobs',
    '/api/db/official-infractions/live/jobs/test',
])
def test_live_scan_and_status_use_same_background_owner(monkeypatch, isolation, path):
    monkeypatch.setenv('BIT_WORKER_ISOLATION', isolation)
    assert service_split.is_worker_path(path, 'GET')
    assert service_topology.request_group(path) == 'stores'


def test_lookup_separates_index_paths_and_keeps_exact_pack_match():
    import pymysql
    identifiers = ['123', '9' * 80]
    sql, params = context_lookup(identifiers, 7)
    connection = pymysql.connections.Connection(defer_connect=True)
    connection.server_status = 0
    rendered = connection.cursor().mogrify(sql, params)
    assert sql.count(' UNION ') == 2  # DISTINCT avoids duplicate orders across keys.
    assert sql.count('`token_id`=%s') == 3
    assert ' OR ' not in sql
    assert "'" + '9' * 64 + "'" in rendered
    assert "'" + '9' * 80 + "'" in rendered
    assert 'STRAIGHT_JOIN' in sql


def test_inventory_options_group_before_fetching_names(monkeypatch):
    from bit import bit_inventory
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{'token_id': 7, 'site_id': 'MLM', 'store_name': 'store'}]
    monkeypatch.setattr(bit_inventory, '_connect', lambda: connection)
    monkeypatch.setattr(bit_inventory, 'ensure_inventory_tables', lambda c: None)
    monkeypatch.setattr(bit_inventory, '_table_exists', lambda *a: True)
    result = bit_inventory.list_inventory_sku_store_sites(token_ids=[7])
    sql, params = cursor.execute.call_args.args
    assert 'GROUP BY `token_id`, `site_id`' in sql
    assert 'MIN(`id`)' in sql
    assert params == (7,)
    assert result['rows'][0]['token_id'] == 7
    cursor.execute.reset_mock()
    assert bit_inventory.list_inventory_sku_store_sites(token_ids=[]) == {'rows': []}
    cursor.execute.assert_not_called()


def test_index_inspection_reuses_equivalent_and_rejects_collision():
    from scripts.optimize_order_lookup_indexes import inspect
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        {'Key_name': 'existing_shipping', 'Seq_in_index': i + 1, 'Column_name': col,
         'Index_type': 'BTREE', 'Visible': 'YES', 'Sub_part': None}
        for i, col in enumerate(['token_id', 'shipping_id'])
    ]
    assert inspect(cursor)['idx_orders_token_shipping'] == 'existing_shipping'
    cursor.fetchall.return_value[0]['Key_name'] = 'idx_orders_token_created'
    with pytest.raises(ValueError, match='mismatch'):
        inspect(cursor)


def test_live_scans_obey_configured_budget(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from bit import mercado_infraction_sync as sync, bit_db_api
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'mysql')
    monkeypatch.setattr(sync, 'INFRACTION_STORE_WORKERS', 1)
    monkeypatch.setattr(sync, '_token_records', lambda ids: [{'id': i} for i in ids])
    pools, admissions = [], []
    def executor(**kwargs):
        pools.append(kwargs['max_workers'])
        return ThreadPoolExecutor(**kwargs)
    def budget(kind, token, fn, *args, **kwargs):
        admissions.append((kind, token, kwargs.pop("capacity_foreground", False)))
        kwargs.pop("capacity_timeout", None)
        kwargs.pop("capacity_stop_event", None)
        return fn(*args, **kwargs)
    monkeypatch.setattr(sync, 'ThreadPoolExecutor', executor)
    monkeypatch.setattr(sync, 'run_capacity_store', budget)
    monkeypatch.setattr(sync, '_collect_live_detection_target', lambda record, target, **k: {
        'store': str(record['id']), 'status': 'success', 'rows': [], 'scanned': 0,
    })
    result = sync.collect_live_detection_infractions([{'token_id': 1}, {'token_id': 2}], max_workers=8)
    assert pools == [1]
    assert admissions == [('infractions', 1, True), ('infractions', 2, True)]
    assert len(result['results']) == 2
