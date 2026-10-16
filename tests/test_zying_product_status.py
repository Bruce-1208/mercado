import json
import pytest
from bit import bit_zying_caiji as collector
from erp import mercadolibre_collection_store as store
from test_mercado_product_developers import Connection, existing_schema
from test_product_list_query_routing import routes, invoke


@pytest.mark.parametrize('field', ['sale_stat', 'stat', 'status'])
def test_list_status_survives_missing_detail_status(field):
    row = {'id': 123, field: 5001}
    record = collector._zying_api_list_record(row, 1, '2026-10-14')
    assert record['zying_status'] == '疑似'
    collector._merge_detail_record(record, row, {'sale_id': 123})
    assert record['zying_status'] == '疑似'
    collector._merge_detail_record(record, row, {'sale_id': 123, 'sale_stat': 1000})
    assert record['zying_status'] == '通过'


def test_status_filters_before_pagination_and_total():
    connection = Connection()
    connection.products([(i, str(i), None, 'zying', '', '') for i in range(1, 5)])
    for i, status in enumerate(['通过', '疑似', '疑似', ''], 1):
        connection.db.execute('UPDATE erp_mercadolibre_products SET source_snapshot_json=? WHERE id=?',
                              (json.dumps({'plugin_snapshot': {'zying_status': status}}), i))
    result = store.list_product_items(zying_status='疑似', limit=1, offset=1,
                                      connection_factory=lambda: connection)
    assert result['total'] == 2
    assert len(result['rows']) == 1
    assert result['rows'][0]['id'] in (2, 3)


def test_route_forwards_zying_status(routes):
    response = invoke(routes, 'products', {'zying_status': '疑似'})
    assert response.status_code == 200
    assert routes.products.call_args.kwargs['zying_status'] == '疑似'
