"""Offline regression coverage for display ordering, pagination and worker order."""
import pytest
from flask import Flask

from erp.ai_weight_price.service import Service
from erp.ai_weight_price.web import create_blueprint


def populate(service):
    store = service.store
    store.save_run({'run_id': 'sorting'})
    for index in range(53):
        key = f'product-{index:02}'
        store.add({'erp_goods_id': key, 'title': '排序测试商品',
                   'source_page': 1, 'source_index': index + 1})
        store.update(key, execution_finished_at=1700000000 + index)
        store.record_run_item('sorting', key)
    # A recent unrelated update must not move an old execution to the top.
    store.update('product-00', decision_reason='补充说明')
    for key, stamps in (
        ('started', {'execution_finished_at': None, 'execution_started_at': 1700000100}),
        ('zero-finish', {'execution_finished_at': 0, 'execution_started_at': 1700000099}),
        ('unprocessed', {}),
    ):
        store.add({'erp_goods_id': key, 'title': '排序测试商品'})
        store.update(key, **stamps)
        store.record_run_item('sorting', key)
    return ['started', 'zero-finish'] + [f'product-{i:02}' for i in reversed(range(53))] + ['unprocessed']


@pytest.mark.parametrize('endpoint', ['tasks', 'run-items'])
def test_display_order_precedes_pagination_and_keeps_worker_order(tmp_path, endpoint):
    service = Service(tmp_path)
    expected = populate(service)
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(service))
    client = app.test_client()
    pages = [client.get(f'/api/ai-weight-price/{endpoint}?page={page}&search=排序测试').get_json()
             for page in (1, 2)]
    assert [len(result['rows']) for result in pages] == [50, 6]
    assert all(result['total'] == 56 for result in pages)
    assert [row['erp_goods_id'] for result in pages for row in result['rows']] == expected
    assert [row['erp_goods_id'] for row in service.store.list(page_size=53)['rows']] == [
        f'product-{i:02}' for i in range(53)]
    assert service.store.run_items('sorting')['rows'][0]['erp_goods_id'] == 'product-00'
