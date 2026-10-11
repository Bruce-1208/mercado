import ast
import logging
from pathlib import Path

from flask import Flask, jsonify, request
import pytest
from bit.ai_billing import can_view_ai_costs


@pytest.mark.parametrize('role', ['super_admin', 'enterprise_admin', 'member'])
@pytest.mark.parametrize('query,expected_calls', [('', 1), ('?include_cost_summary=0', 0)])
def test_list_can_skip_all_product_cost_scan(monkeypatch, query, expected_calls, role):
    calls = []
    monkeypatch.setattr('erp.ai_original_cost_control.generation_cost_summary',
                        lambda: calls.append(True) or {'total_tokens': 123})
    node = next(node for node in ast.parse(Path('bit/bit_interface.py').read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == 'api_ai_original_products')
    node.decorator_list = []
    namespace = dict(request=request, jsonify=jsonify, logging=logging,
                     can_view_ai_costs=can_view_ai_costs,
                     get_current_workbench_user=lambda: {"role_key": role},
                     db_list_mercado_product_items=lambda **kwargs: {'rows': [{'id': 1}], 'total': 1},
                     _parse_int_param=lambda args, key, default, low, high: int(args.get(key, default)))
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<route>', 'exec'), namespace)
    with Flask(__name__).test_request_context('/' + query):
        response = namespace['api_ai_original_products']()
        assert response.status_code == 200
        assert response.get_json()['data']['rows'] == [{'id': 1}]
        assert len(calls) == (expected_calls if role == "super_admin" else 0)


def test_fast_list_skips_count_and_removes_only_duplicate_snapshot():
    calls = []
    def listing(**kwargs):
        calls.append(kwargs)
        return {"rows": [{"id": 1, "source_snapshot_json": "large supplier payload",
                          "original_1688": {"title": "供应商商品"},
                          "ai_original": {"status": "completed"}}]}
    node = next(node for node in ast.parse(Path('bit/bit_interface.py').read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == 'api_ai_original_products')
    node.decorator_list = []
    namespace = dict(request=request, jsonify=jsonify, logging=logging,
                     can_view_ai_costs=can_view_ai_costs,
                     get_current_workbench_user=lambda: {"role_key": "member"},
                     db_list_mercado_product_items=listing,
                     _parse_int_param=lambda args, key, default, low, high: int(args.get(key, default)))
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<route>', 'exec'), namespace)
    with Flask(__name__).test_request_context('/?include_total=0&include_cost_summary=0&include_generator_options=0'):
        response = namespace['api_ai_original_products']()
    assert calls[0]['include_total'] is False
    assert calls[0]['include_generator_options'] is False
    row = response.get_json()['data']['rows'][0]
    assert 'source_snapshot_json' not in row
    assert row['original_1688']['title'] == '供应商商品'
    assert row['ai_original']['status'] == 'completed'
