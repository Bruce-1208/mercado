from flask import Flask, jsonify, g
from bit import product_access
import ast
import re
from pathlib import Path


def invoke(monkeypatch, row, user):
    node = next(n for n in ast.parse(Path('bit/bit_interface.py').read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'api_browser_extension_recollect_source')
    node.decorator_list = []
    namespace = dict(jsonify=jsonify, g=g, re=re, db_get_mercado_product_items_by_ids=lambda ids: [row] if row else [])
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<recollect-route>', 'exec'), namespace)
    with Flask(__name__).test_request_context('/api/browser-extension/ai-original-products/42/recollect-source'):
        g.browser_extension_user=user
        with product_access.scope(user):
            return namespace['api_browser_extension_recollect_source'](42)


def test_recollect_source_rejects_other_owners(monkeypatch):
    row={'source_type':'ai_original','owner_user_id':2,'organization_key':'wuhan-zeshun'}
    result=invoke(monkeypatch,row,{'id':1,'role_key':'member','organization_key':'wuhan-zeshun'})
    assert result[1]==403


def test_recollect_source_uses_canonical_supplier_id(monkeypatch):
    row={'source_type':'ai_original','owner_user_id':1,'organization_key':'wuhan-zeshun',
         'original_1688':{'source_1688_item_id':'123456'},'source_url':'https://unrelated.test/', 'management_category_id':7}
    response=invoke(monkeypatch,row,{'id':1,'role_key':'member','organization_key':'wuhan-zeshun'})
    assert response.json['data']=={'source_item_id':'123456','source_url':'https://detail.1688.com/offer/123456.html','management_category_id':7}
    assert response.headers['Cache-Control']=='no-store'
