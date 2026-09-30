"""Exercise the actual route bodies without starting workbench background services."""
import ast
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, request, jsonify


def load_routes(monkeypatch):
    from bit import bit_ai_appeal_copy
    source = Path('bit/bit_interface.py').read_text()
    tree = ast.parse(source)
    names = {'api_db_ai_appeal_product_contexts', 'api_db_get_ai_appeal_records', 'api_ai_appeal_records'}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    app = Flask(__name__)
    calls = []
    env = dict(request=request, jsonify=jsonify, reject_db_api_client_mode=lambda: None,
               db_get_ai_appeal_records=lambda limit, **kw: calls.append((limit, kw)) or {'rows': []},
               enrich_ai_appeal_records=lambda x: x, _filter_store_rows_for_user=lambda x, **kw: x,
               logging=SimpleNamespace(error=lambda *args: None))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<appeal-routes>', 'exec'), env)
    return app, env, calls


def test_both_record_routes_forward_mode(monkeypatch):
    app, env, calls = load_routes(monkeypatch)
    with app.test_request_context('/?limit=500&appeal_copy_mode=AI话术模式'):
        env['api_db_get_ai_appeal_records']()
        env['api_ai_appeal_records']()
    assert calls == [('500', {'appeal_copy_mode': 'AI话术模式'})] * 2


def test_product_route_returns_only_facts(monkeypatch):
    from bit import bit_ai_appeal_copy
    app, env, calls = load_routes(monkeypatch)
    monkeypatch.setattr(bit_ai_appeal_copy, 'fetch_store_product_contexts',
                        lambda name, ids: [{'product_id': ids[0], 'title': name}])
    with app.test_request_context('/', method='POST', json={'shop_name': 'shop', 'product_ids': ['MLM123']}):
        assert env['api_db_ai_appeal_product_contexts']().json['data'] == [{'product_id': 'MLM123', 'title': 'shop'}]
    with app.test_request_context('/', method='POST', json={'product_ids': 'MLM123'}):
        assert env['api_db_ai_appeal_product_contexts']()[1] == 400


def test_agent_allowlist_includes_fact_route():
    tree = ast.parse(Path('bit/bit_interface.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'DAILY_AGENT_DB_POST_PATHS' for t in n.targets))
    assert '/api/db/ai-appeal-product-contexts' in ast.literal_eval(node.value.args[0])
