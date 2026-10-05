"""Exercise list AI routes without booting database-backed console services."""
import ast
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import threading

from flask import Flask, jsonify, request

from bit import collection_ai_workflow
from erp import collection_ai_check
from erp.ai_weight_price.service import Service


def route(name, **dependencies):
    tree = ast.parse(Path('bit/bit_interface.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    app = Flask(__name__)
    namespace = dict(app=app, jsonify=jsonify, request=request, login_required=lambda fn: fn, **dependencies)
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<list-ai-route>', 'exec'), namespace)
    return app.test_client()


def test_product_check_route_creates_shared_ai_run(tmp_path, monkeypatch):
    service = Service(tmp_path)
    user = {'id': 7, 'username': 'tester'}
    monkeypatch.setattr(collection_ai_workflow, 'queue_lock', lambda _: nullcontext())
    requested = []
    def products(ids, *, list_mode):
        requested.append((ids, list_mode))
        return [{'product_item_id': 12, 'collection_item_id': None, 'erp_goods_id': 'products:12',
                 'title': 'Product', 'main_image_url': 'https://example.com/a.jpg'}]
    monkeypatch.setattr(collection_ai_check, 'products', products)
    monkeypatch.setattr(collection_ai_check, 'save_result', lambda *args: {})
    client = route('api_mercado_collection_ai_weight_price',
        _authorize_ai_weight_price=lambda _: None,
        get_current_workbench_user=lambda: user, ai_weight_price_service=service,
        browser_extension_models=SimpleNamespace(get_api_key=lambda *args: 'test-key'),
        enqueue_local_agent_ai_weight_price=lambda *args: jsonify(status='success', message='Queued',
            data={'task_id': 'job-1', 'agent_id': 'office', 'execution_terminal': 'Office'}))
    response = client.post('/api/mercado-collection/ai-weight-price', json={
        'product_item_ids': [12], 'agent_id': 'office'})
    assert response.status_code == 200, response.json
    assert requested == [([12], 'products')]
    with service.store.actor_scope(user):
        run = service.store.state('run')
        assert run['agent_job_id'] == 'job-1'
        assert run['task_ids'][0].startswith('product:12:')
        assert service.store.run_items(run['run_id'])['total'] == 1


def test_product_copy_route_adapts_snapshot_and_starts_worker(monkeypatch):
    import os
    thread = Mock()
    rows = [{'id': 12, 'title': 'Original', 'source_snapshot_json': '{"source":{"attributes":[{"id":"A"}]}}'}]
    load_products = Mock(return_value=rows)
    client = route('api_start_mercado_collection_ai_copy',
        _mercado_publish_lock=threading.Lock(), _mercado_publish_state={},
        _mercado_collection_copy_lock=threading.Lock(), _mercado_collection_copy_state={},
        db_get_mercado_product_items_by_ids=load_products,
        _resolve_ai_original_text_api_key=lambda _: ('deepseek', 'test-key'),
        threading=SimpleNamespace(Thread=thread), os=os,
        _run_mercado_collection_copy_task=lambda *args: None,
        _mercado_collection_copy_snapshot=lambda: {'running': True})
    response = client.post('/api/mercado-collection/ai-copy', json={'product_item_ids': [12]})
    assert response.status_code == 202, response.json
    load_products.assert_called_once_with([12])
    submitted = thread.call_args.kwargs['args'][0][0]
    assert submitted['_ai_copy_product']
    assert 'attributes' in submitted['source_json']
    thread.return_value.start.assert_called_once()
