from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest

from bit import weight_dimensions_records as records
from bit import bit_interface as web


@pytest.fixture
def task(monkeypatch):
    task = {'owner': 'user', 'status': 'ready', 'execute_status': 'queued',
            'execute_action': 'zying', 'execute_order_numbers': ['1', '2'],
            'records': [dict(order_number=str(n), product_id=str(n), actual_weight_g='400',
                             actual_dimensions_cm='30x20x10', query_status='已读取',
                             _dimension_compensation_confirmed=True) for n in (1, 2)]}
    monkeypatch.setattr(records, '_tasks', {'task': task})
    monkeypatch.setattr(records, '_order_history_context', lambda *a: {})
    monkeypatch.setattr(records.bit_db_api, 'append_weight_dimensions_record_log', Mock())
    return task


def test_server_stop_preserves_finished_product_and_skips_next(task):
    browser = Mock()
    browser.update_package_by_product_id.side_effect = lambda *a: records.stop_execute('task', 'user')
    @contextmanager
    def factory(*a):
        yield browser
    records._run_execute_guarded('task', 'user', 'zying', ['1', '2'], factory)
    browser.update_package_by_product_id.assert_called_once_with('1', '400', '30x20x10')
    assert task['execute_status'] == 'stopped'
    assert task['records'][0]['_zying_status'] == '成功'
    assert task['records'][1]['_zying_status'] == '已终结'
    assert records._public_record(task['records'][1])['can_execute_zying']


def test_stop_before_server_worker_starts_does_not_open_browser(task):
    records.stop_execute('task', 'user')
    factory = Mock()
    records._run_execute_guarded('task', 'user', 'zying', ['1', '2'], factory)
    factory.assert_not_called()
    assert task['execute_status'] == 'stopped'


def test_agent_stop_sends_cancel_and_preserves_partial_result(task):
    task['agent_job_id'] = 'job'
    cancel = Mock()
    records.stop_execute('task', 'user', agent_cancel=cancel)
    cancel.assert_called_once_with('job')
    records._apply_agent_job(task, {'status': 'stopping'})
    assert task['execute_status'] == 'stopping'
    records._apply_agent_job(task, {'status': 'stopped', 'result': {'rows': [
        {'order_number': '1', 'status': 'success', 'message': '已保存'}]}})
    assert task['execute_status'] == 'stopped'
    assert task['records'][0]['_zying_status'] == '成功'
    assert task['records'][1]['_zying_status'] == '已终结'
    assert task['execute_processed'] == 1


def test_stop_requires_task_owner_and_blocks_restart_while_stopping(task):
    with pytest.raises(KeyError):
        records.stop_execute('task', 'other')
    records.stop_execute('task', 'user')
    with pytest.raises(ValueError, match='正在执行'):
        records.start_execute('task', 'user', action='zying', selected_order_numbers=['2'])


def test_stop_route_dispatches_owned_agent_cancellation():
    store = Mock()
    store.request_cancel.return_value = False
    with web.app.test_request_context(method='POST'), \
         patch.object(web, 'get_current_workbench_user', return_value={'id': 1}), \
         patch.object(web, 'get_local_agent_store', return_value=store), \
         patch.object(records, 'stop_execute') as stop:
        stop.side_effect = lambda *a, **kw: (kw['agent_cancel']('job') or {'status': 'stopping'})
        response = web.api_weight_dimensions_records_stop.__wrapped__('task')
    assert response.get_json()['status'] == 'success'
    store.request_cancel.assert_called_once_with('job')


def test_agent_worker_stops_between_products_and_persists_success(tmp_path):
    import json
    import threading
    import local_agent_worker
    event = threading.Event()
    browser = Mock()
    browser.update_package_by_product_id.side_effect = lambda *a: (event.set() or {})
    @contextmanager
    def factory(*a):
        yield browser
    service = Mock()
    service.weight_dimensions_browser = factory
    job_file = tmp_path / 'jobs' / 'job' / 'job.json'
    job_file.parent.mkdir(parents=True)
    job = {'job_id': 'job', 'created_by_id': 1, 'payload': {'action': 'weight-dimensions-update', 'records': [
        {'order_number': str(n), 'product_id': str(n), 'actual_weight_g': '400',
         'actual_dimensions_cm': '30x20x10'} for n in (1, 2)]}}
    with patch('erp.ai_weight_price.service.Service', return_value=service):
        result = local_agent_worker.run_ai_weight_price(job, event, job_file)
    assert result['status'] == 'stopped'
    assert len(result['rows']) == 1
    assert result['rows'][0]['status'] == 'success'
    assert json.loads(job_file.with_name('result.json').read_text())['rows'] == result['rows']
    browser.update_package_by_product_id.assert_called_once_with('1', '400', '30x20x10')


def test_stop_requires_execution_permission():
    assert web._required_workbench_permissions('/api/weight-dimensions-records/task/stop', 'POST') == ('order_analysis.execute',)
