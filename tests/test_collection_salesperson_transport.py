"""Salesperson selection survives the console-to-database API hop."""
from bit import bit_db_api


def test_collection_salesperson_and_options_forward_to_database_api(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    monkeypatch.setattr(bit_db_api, '_request', lambda *args, **kwargs: calls.append((args, kwargs)) or {'rows': []})
    bit_db_api.list_mercado_collection_items(salesperson='  业务乙  ', include_salesperson_options=True, exclude_added=True)
    args, kwargs = calls[0]
    assert args == ('GET', '/api/db/mercado-collection/items')
    assert kwargs['params']['salesperson'] == '业务乙'
    assert kwargs['params']['include_salesperson_options'] is True
    assert kwargs['params']['exclude_added'] is True
