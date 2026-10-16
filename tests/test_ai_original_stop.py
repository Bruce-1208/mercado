"""Cooperative stop tests; no database, model calls or billing."""
import threading

import pytest
from flask import Flask
from test_ai_original_budget_routes import isolated_function, process_route  # noqa: F401
from test_ai_original_batch_isolation import worker, row
from erp import ai_original_products as products
from erp import ai_original_cost_control as scheduling


@pytest.mark.parametrize('running', [True, False])
def test_stop_route_is_idempotent_and_preserves_results(running):
    state = {'running': running, 'status': 'running' if running else 'completed',
             'completed_count': 1, 'results': [{'product_id': 1, 'status': 'completed'}]}
    stop = isolated_function('api_stop_ai_original_products',
        _ai_original_task_lock=threading.Lock(), _ai_original_task_state=state,
        _ai_original_task_snapshot=lambda: dict(state))
    with Flask(__name__).test_request_context(method='POST'):
        for _ in range(2):
            data = stop().get_json()['data']
            assert data['status'] == ('stopping' if running else 'completed')
            assert data['running'] == running
            assert data['completed_count'] == 1
            assert data['results'] == [{'product_id': 1, 'status': 'completed'}]


def test_stop_retains_inflight_success_and_does_not_start_queued_products(monkeypatch):
    items = [row(1), row(2), row(3)]
    fixture = worker(monkeypatch, items)
    def prepare(item, **kwargs):
        fixture.state['stop_requested'] = True
        return fixture.prepare(item, **kwargs)
    monkeypatch.setattr(products, 'prepare_ai_original_product', prepare)
    fixture.run(items, workers=1)
    assert fixture.attempted == [1]
    assert fixture.locks == [1]
    assert fixture.state['status'] == 'stopped'
    assert not fixture.state['running']
    assert fixture.state['completed_count'] == 1
    assert fixture.state['failed_count'] == 0
    assert fixture.state['skipped_count'] == 2


def test_stop_before_paid_request_preserves_checkpoint_for_retry(monkeypatch):
    fixture = worker(monkeypatch, [row(1)])
    def prepare(item, **kwargs):
        kwargs['on_preparation_checkpoint']('copy', {'title_es': 'Saved copy'})
        fixture.state['stop_requested'] = True
        kwargs['before_model_request']()
        pytest.fail('must stop before dispatching paid request')
    monkeypatch.setattr(products, 'prepare_ai_original_product', prepare)
    fixture.run([row(1)], workers=1, model='qwen3.7-flash', cost_control=True)
    snapshot = fixture.saved[-1][1]['source_snapshot_json']['ai_original']
    assert snapshot['status'] == 'pending'
    assert snapshot['title_es'] == 'Saved copy'
    assert fixture.state['status'] == 'stopped'
    assert fixture.state['failed_count'] == 0


def test_stop_interrupts_offpeak_wait_without_taking_product_lock(monkeypatch):
    fixture = worker(monkeypatch, [row(1)])
    def waiting(on_wait, *, sleep):
        fixture.state['stop_requested'] = True
        sleep(30)
        pytest.fail('must stop the offpeak wait')
    monkeypatch.setattr(scheduling, 'wait_for_low_cost_window', waiting)
    fixture.run([row(1)], workers=1, model='offpeak-model', cost_control=True)
    assert fixture.locks == fixture.attempted == fixture.saved == []
    assert fixture.state['status'] == 'stopped'
    assert fixture.state['skipped_count'] == 1


def test_new_task_clears_previous_stop_request(process_route):
    process_route.state.update(running=False, status='stopped', stop_requested=True)
    payload, status = process_route.call({})
    assert status == 202
    assert payload['data']['running'] is True
    assert payload['data']['stop_requested'] is False
