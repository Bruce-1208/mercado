from unittest.mock import patch

from bit import bit_interface as web
from bit import weight_dimensions_records as records
from bit.local_agent_hub import LocalAgentStore


def test_queue_reports_other_job_types_and_excludes_later_jobs(tmp_path):
    store = LocalAgentStore(tmp_path / 'queue.sqlite3')
    store.enqueue_job('first', 'agent-test', 'daily_task', {}, now=1)
    store.claim_job('agent-test', now=2)
    store.enqueue_job('second', 'agent-test', 'zying_order_sync', {}, now=3)
    store.enqueue_job('target', 'agent-test', 'ai_weight_price', {}, now=4)
    store.enqueue_job('later', 'agent-test', 'appeal', {}, now=5)
    with patch.object(web, 'get_local_agent_store', return_value=store):
        job = web._weight_dimensions_agent_job('target')
    assert '前方 2 个任务' in job['message']
    assert '每日任务（first）' in job['message']
    task = {"records": []}
    records._apply_agent_job(task, job)
    assert task['execute_status'] == 'queued'
    assert task['execute_message'] == job['message']
    assert store.get_job('target')['message'] == '等待本机 Agent 接收'


def test_dispatch_rejection_preserves_error_instead_of_http_500():
    def execute(*args, **kwargs):
        kwargs['agent_dispatch']([{'order_number': 'one'}])
    with web.app.test_request_context(json={'action': 'zying', 'agent_id': 'agent-test', 'order_numbers': ['one']}), \
         patch.object(web, 'get_current_workbench_user', return_value={'id': 1}), \
         patch.object(records, 'start_execute', side_effect=execute), \
         patch.object(web, 'enqueue_local_agent_ai_weight_price', side_effect=lambda *a: (web.jsonify(status='error', message='所选 Agent 不在线'), 409)):
        response, code = web.api_weight_dimensions_records_execute.__wrapped__('test')
    assert code == 400
    assert response.get_json()['message'] == '所选 Agent 不在线'


def test_server_route_uses_browser_without_agent_dispatch():
    with web.app.test_request_context(json={'action': 'zying', 'execution_target': 'server', 'order_numbers': ['one']}), \
         patch.object(web, 'get_current_workbench_user', return_value={'id': 1}), \
         patch.object(records, 'start_execute', return_value={'status': 'queued'}) as start:
        _, code = web.api_weight_dimensions_records_execute.__wrapped__('test')
    assert code == 202
    assert start.call_args.kwargs['agent_dispatch'] is None
    assert callable(start.call_args.kwargs['erp_browser_factory'])
