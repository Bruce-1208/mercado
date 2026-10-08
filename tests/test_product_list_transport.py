import inspect
from unittest.mock import Mock

import pytest
import requests

from bit import bit_db_api as api
from erp import mercadolibre_collection_store as store


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


def test_internal_product_route_passes_only_supported_store_parameters(monkeypatch):
    import bit.bit_interface as workbench
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


def test_internal_single_page_flag_is_parsed(monkeypatch):
    import bit.bit_interface as workbench
    monkeypatch.setattr(workbench, 'reject_db_api_client_mode', lambda: None)
    listing = Mock(return_value={'rows': [], 'total': None, 'has_more': False, 'total_exact': False})
    monkeypatch.setattr(workbench, 'db_list_mercado_product_items', listing)
    with workbench.app.test_request_context('/api/db/mercado-products?include_total=False&token_ids=7'):
        response = inspect.unwrap(workbench.api_db_mercado_products)()
    assert response.status_code == 200
    assert listing.call_args.kwargs['include_total'] is False
    assert listing.call_args.kwargs['token_ids'] == [7]
