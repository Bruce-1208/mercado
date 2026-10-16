import inspect
from unittest.mock import Mock

import pytest
import requests

from bit import bit_db_api as api
from erp import mercadolibre_collection_store as store


@pytest.fixture
def offline_workbench():
    import ast
    import logging
    from pathlib import Path
    from types import ModuleType
    from flask import Flask, jsonify, request
    path = Path(__file__).parents[1] / 'bit/bit_interface.py'
    names = {'api_mercado_products', 'api_db_mercado_products', '_parse_int_param',
             'api_db_mercado_product_publish_records', 'api_mercado_product_publish_records', 'api_mercado_product_salespeople',
             'api_db_mercado_product_salespeople'}
    functions = [node for node in ast.parse(path.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in functions:
        node.decorator_list = []
    module = ModuleType('offline_product_routes')
    module.__dict__.update(app=Flask(__name__), jsonify=jsonify, request=request, logging=logging,
                           reject_db_api_client_mode=lambda: None,
                           _mercado_infringement_filters=lambda args: {},
                           _decorate_mercado_infringement=lambda data, *args: data,
                           db_list_mercado_product_items=lambda **kwargs: {},
                           db_list_mercado_product_salespeople=lambda **kwargs: {"salespeople": []})
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), module.__dict__)
    return module


@pytest.mark.parametrize('mode', ['mysql', 'api'])
def test_publish_record_transport_preserves_salesperson_and_store_filters(monkeypatch, mode):
    monkeypatch.setattr(api, 'DB_MODE', mode)
    listing = Mock(return_value={'rows': [], 'total': 0})
    monkeypatch.setattr(api, '_request', listing)
    monkeypatch.setattr(api, '_collection_store_call', listing)
    api.list_mercado_product_publish_records(
        salesperson=' 张三 ', token_ids=[7, 9], filter_token_ids=[9], product_item_id=41,
    )
    params = listing.call_args.kwargs['params'] if mode == 'api' else listing.call_args.kwargs
    assert params['salesperson'] == '张三'
    assert params['token_ids'] == [7, 9]
    assert params['filter_token_ids'] == [9]
    assert params['product_item_id'] == 41
    inspect.signature(store.list_product_publish_records).bind(**params)


def test_internal_publish_record_route_preserves_salesperson_and_store_scope(monkeypatch, offline_workbench):
    listing = Mock(return_value={'rows': [], 'total': 0})
    monkeypatch.setattr(offline_workbench, 'db_list_mercado_product_publish_records', listing, raising=False)
    with offline_workbench.app.test_request_context(
        '/api/db/mercado-publish-records?salesperson=张三&token_ids=7&token_ids=9&filter_token_ids=9&product_item_id=41'
    ):
        response = offline_workbench.api_db_mercado_product_publish_records()
    assert response.status_code == 200
    params = listing.call_args.kwargs
    assert params['salesperson'] == '张三'
    assert params['token_ids'] == [7, 9]
    assert params['filter_token_ids'] == [9]
    assert params['product_item_id'] == 41
    inspect.signature(store.list_product_publish_records).bind(**params)


@pytest.mark.parametrize('name', ['list_mercado_product_items', 'list_mercado_collection_items'])
def test_list_transport_preserves_weight_filter_and_has_single_bounded_attempt(monkeypatch, name):
    monkeypatch.setattr(api, 'DB_MODE', 'api')
    request = Mock(return_value={'rows': [], 'total': 0})
    monkeypatch.setattr(api, '_request', request)
    getattr(api, name)(weight_status='missing')
    kwargs = request.call_args.kwargs
    assert kwargs['timeout'] == (3, 25)
    assert kwargs['max_attempts'] == 1
    assert kwargs['params']['weight_status'] == 'missing'
    inspect.signature(store._list_rows).bind(store.PRODUCT_TABLE, **kwargs['params'])


def test_list_transport_does_not_multiply_browser_retry_budget(monkeypatch):
    monkeypatch.setattr(api, 'DB_MODE', 'api')
    request = Mock(side_effect=requests.Timeout('timeout'))
    monkeypatch.setattr(api.DB_API_SESSION, 'request', request)
    with pytest.raises(RuntimeError):
        api.list_mercado_product_items()
    assert request.call_count == 1
    assert 'max_attempts' not in request.call_args.kwargs


def test_internal_product_route_passes_only_supported_store_parameters(monkeypatch, offline_workbench):
    workbench = offline_workbench
    monkeypatch.setattr(workbench, 'reject_db_api_client_mode', lambda: None)
    def validate(**kwargs):
        inspect.signature(store._list_rows).bind(store.PRODUCT_TABLE, **kwargs)
        assert kwargs['weight_status'] == 'missing'
        assert kwargs['token_ids'] == [7]
        return {'rows': [], 'total': 0}
    monkeypatch.setattr(workbench, 'db_list_mercado_product_items', validate)
    with workbench.app.test_request_context('/api/db/mercado-products?weight_status=missing&token_ids=7'):
        response = inspect.unwrap(workbench.api_db_mercado_products)()
    assert response.status_code == 200


def test_single_page_transport_keeps_flag_and_scope(monkeypatch):
    monkeypatch.setattr(api, 'DB_MODE', 'api')
    request = Mock(return_value={'rows': [], 'total': None, 'total_exact': False, 'has_more': False})
    monkeypatch.setattr(api, '_request', request)
    api.list_mercado_product_items(include_total=False, token_ids=[7], limit=100)
    assert request.call_args.kwargs['params']['include_total'] is False
    assert request.call_args.kwargs['params']['token_ids'] == [7]


def test_internal_single_page_flag_is_parsed(monkeypatch, offline_workbench):
    workbench = offline_workbench
    monkeypatch.setattr(workbench, 'reject_db_api_client_mode', lambda: None)
    listing = Mock(return_value={'rows': [], 'total': None, 'has_more': False, 'total_exact': False})
    monkeypatch.setattr(workbench, 'db_list_mercado_product_items', listing)
    with workbench.app.test_request_context('/api/db/mercado-products?include_total=False&token_ids=7'):
        response = inspect.unwrap(workbench.api_db_mercado_products)()
    assert response.status_code == 200
    assert listing.call_args.kwargs['include_total'] is False
    assert listing.call_args.kwargs['token_ids'] == [7]


def test_product_salesperson_directory_and_filter_survive_api_transport(monkeypatch):
    monkeypatch.setattr(api, 'DB_MODE', 'api')
    request = Mock(return_value={'rows': [], 'salespeople': ['业务甲']})
    monkeypatch.setattr(api, '_request', request)
    api.list_mercado_product_items(salesperson=' 业务甲 ', include_salesperson_options=True)
    params = request.call_args.kwargs['params']
    assert params['salesperson'] == '业务甲'
    assert params['include_salesperson_options'] is True
    inspect.signature(store._list_rows).bind(store.PRODUCT_TABLE, **params)


@pytest.mark.parametrize('function,path', [('api_mercado_products', '/api/mercado-products'),
                                         ('api_db_mercado_products', '/api/db/mercado-products')])
def test_product_routes_forward_salesperson_options(monkeypatch, function, path, offline_workbench):
    workbench = offline_workbench
    monkeypatch.setattr(workbench, 'reject_db_api_client_mode', lambda: None)
    monkeypatch.setattr(workbench, '_mercado_infringement_filters', lambda args: {})
    monkeypatch.setattr(workbench, '_decorate_mercado_infringement', lambda data, *args: data)
    listing = Mock(return_value={'rows': [], 'salespeople': ['业务甲']})
    monkeypatch.setattr(workbench, 'db_list_mercado_product_items', listing)
    with workbench.app.test_request_context(path + '?salesperson=业务甲&include_salesperson_options=1'):
        response = inspect.unwrap(getattr(workbench, function))()
    assert response.status_code == 200
    assert listing.call_args.kwargs['salesperson'] == '业务甲'
    assert listing.call_args.kwargs['include_salesperson_options'] is True


@pytest.mark.parametrize('tokens,expected', [(None, {}), ([], {'token_ids': ['']}), ([7], {'token_ids': [7]})])
def test_salesperson_directory_transport_preserves_visibility(monkeypatch, tokens, expected):
    monkeypatch.setattr(api, 'DB_MODE', 'api')
    request = Mock(return_value={'salespeople': []})
    monkeypatch.setattr(api, '_request', request)
    api.list_mercado_product_salespeople(token_ids=tokens)
    request.assert_called_once_with('GET', '/api/db/mercado-products/salespeople',
                                    params=expected, timeout=(3, 5), max_attempts=1)


def test_salesperson_directory_failure_is_independent_of_products(offline_workbench, monkeypatch):
    listing = Mock(side_effect=TimeoutError('deadline'))
    monkeypatch.setattr(offline_workbench, 'db_list_mercado_product_salespeople', listing)
    with offline_workbench.app.test_request_context('/api/mercado-products/salespeople'):
        response, code = offline_workbench.api_mercado_product_salespeople()
    assert code == 503 and response.get_json()['status'] == 'error'


def test_salesperson_directory_uses_web_service():
    from bit.service_split import is_worker_path
    assert not is_worker_path('/api/mercado-products/salespeople', 'GET')
    assert not is_worker_path('/api/db/mercado-products/salespeople', 'GET')


@pytest.mark.parametrize('mode', ['mysql', 'api'])
def test_grouped_skus_flag_reaches_product_store(monkeypatch, mode):
    monkeypatch.setattr(api, 'DB_MODE', mode)
    listing = Mock(return_value={'rows': [], 'total': 0, 'group_count': 0})
    monkeypatch.setattr(api, '_request', listing)
    monkeypatch.setattr(api, '_collection_store_call', listing)
    api.list_mercado_product_items(group_skus=True, token_ids=[7])
    params = listing.call_args.kwargs['params'] if mode == 'api' else listing.call_args.kwargs
    assert params['group_skus'] is True
    assert params['token_ids'] == [7]
    inspect.signature(store._list_rows).bind(store.PRODUCT_TABLE, **params)


@pytest.mark.parametrize('handler,path', [('api_mercado_products','/api/mercado-products'),
                                         ('api_db_mercado_products','/api/db/mercado-products')])
def test_grouping_route_forwards_request_flag(monkeypatch, offline_workbench, handler, path):
    module = offline_workbench
    listing = Mock(return_value={'rows': [], 'total': 0, 'group_count': 0})
    monkeypatch.setattr(module, 'db_list_mercado_product_items', listing, raising=False)
    monkeypatch.setattr(module, 'reject_db_api_client_mode', lambda: None, raising=False)
    with module.app.test_request_context(path + '?group_skus=1'):
        response = getattr(module, handler)()
    assert response.status_code == 200
    assert listing.call_args.kwargs['group_skus'] is True


@pytest.mark.parametrize('scope', [None, set(), {7, 9}])
def test_product_history_public_route_preserves_access_scope(monkeypatch, offline_workbench, scope):
    listing = Mock(return_value={'rows': [], 'total': 0})
    monkeypatch.setattr(offline_workbench, 'db_list_mercado_product_publish_records', listing, raising=False)
    monkeypatch.setattr(offline_workbench, '_authorized_token_ids_for_user', lambda: scope, raising=False)
    monkeypatch.setattr(offline_workbench, '_mercado_publish_state_snapshot', lambda **kwargs: {}, raising=False)
    with offline_workbench.app.test_request_context('/api/mercado-publish-records?product_item_id=41&limit=100&offset=100'):
        response = offline_workbench.api_mercado_product_publish_records()
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    params = listing.call_args.kwargs
    assert params['product_item_id'] == 41
    assert params['offset'] == 100
    assert params.get('token_ids') == (sorted(scope) if scope is not None else None)


@pytest.mark.parametrize('mode', ['mysql', 'api'])
@pytest.mark.parametrize('product_id', [0, -1, 'bad'])
def test_product_history_transport_rejects_invalid_product(monkeypatch, mode, product_id):
    monkeypatch.setattr(api, 'DB_MODE', mode)
    request = Mock()
    monkeypatch.setattr(api, '_request', request)
    monkeypatch.setattr(api, '_collection_store_call', request)
    with pytest.raises(ValueError):
        api.list_mercado_product_publish_records(product_item_id=product_id)
    request.assert_not_called()
