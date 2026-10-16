"""Offline regression for a DB outage during publication response filtering."""
import ast
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
from flask import Flask, g, request
from bit.db_pool import PoolAcquireTimeout


@pytest.mark.parametrize('status', [200, 503])
@pytest.mark.parametrize('path', ['/api/mercado-products/publish', '/api/mercado-products/attribute-preflight', '/api/mercado-products/publish/status'])
def test_capacity_outage_preserves_json_result_and_hides_capacity(status, path):
    app = Flask(__name__)
    tree = ast.parse(Path('bit/bit_interface.py').read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'hide_task_capacity_from_non_admin')
    fn.decorator_list = []
    lookup = Mock(side_effect=PoolAcquireTimeout('busy'))
    scope = dict(app=app, request=request, logging=logging,
                 workbench_user_can_change_task_workers=lookup,
                 _hide_task_capacity=lambda payload: {k: v for k, v in payload.items() if k != 'worker_count'})
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<response-hook>', 'exec'), scope)
    with app.test_request_context(path):
        response = app.json.response({'status': 'success' if status == 200 else 'error', 'worker_count': 7})
        response.status_code = status
        result = scope['hide_task_capacity_from_non_admin'](response)
        assert result.status_code == status
        assert result.json == {'status': 'success' if status == 200 else 'error'}
        assert g.workbench_database_unavailable
        assert lookup.call_count == 1
