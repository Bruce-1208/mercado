from unittest.mock import MagicMock
import pytest
from bit import product_access
from erp import mercadolibre_collection_store as store


def test_collected_query_is_bounded_and_respects_visibility(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{'source_item_id': '1688123456'}]
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    user = {'id': 19, 'username': 'collector', 'organization_key': 'org-test', 'role': 'member'}
    with product_access.scope(user):
        assert store.get_collected_1688_ids(['123456', '234567', '123456'], connection_factory=lambda: connection) == ['123456']
        access, params = product_access.predicate(store.PRODUCT_TABLE)
    sql, values = cursor.execute.call_args.args
    assert access in sql
    assert "`source_type` = 'ai_original'" in sql
    assert values == tuple(['1688123456', '1688234567'] + params)
    assert 'visibility' in sql
    connection.close.assert_called_once()


@pytest.mark.parametrize('ids', [['bad'], ['12345'] * 201, ['12345 OR 1=1']])
def test_invalid_ids_do_not_query(ids):
    # Enforce the request limit before deduplicating, too.
    with pytest.raises(ValueError):
        store.get_collected_1688_ids(ids, connection_factory=lambda: pytest.fail('must not connect'))


def test_status_endpoint_uses_authenticated_user_scope(monkeypatch):
    import ast
    from pathlib import Path
    from flask import Flask, g, jsonify, request
    source = ast.parse(Path('bit/bit_interface.py').read_text())
    endpoint = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'api_browser_extension_1688_collected')
    app = Flask(__name__)
    user = {'id': 19, 'username': 'collector', 'organization_key': 'org-test'}
    def authenticated(function):
        def wrapped():
            g.browser_extension_user = user
            return function()
        return wrapped
    def lookup(ids):
        assert product_access.actor() == user
        assert ids == ['123456']
        return ['123456']
    monkeypatch.setattr(store, 'get_collected_1688_ids', lookup)
    scope = dict(app=app, browser_extension_login_required=authenticated, g=g, jsonify=jsonify, request=request)
    exec(compile(ast.Module(body=[endpoint], type_ignores=[]), '<endpoint>', 'exec'), scope)
    with app.test_client() as client:
        response = client.post('/api/browser-extension/1688/collected', json={'offer_ids': ['123456']})
        assert response.get_json()['data']['collected_ids'] == ['123456']
        assert response.headers['Cache-Control'] == 'no-store'
        assert client.post('/api/browser-extension/1688/collected', json={}).status_code == 400
