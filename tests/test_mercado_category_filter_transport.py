from unittest.mock import Mock
import pytest
from bit import bit_db_api as api
from erp import mercadolibre_store_link_store as links
from erp import mercadolibre_collection_store as products
from erp import mercadolibre_category_tree as tree


@pytest.mark.parametrize('tokens,expected', [(None, {}), ([], {'token_ids': ['']}), ([9, 7, 9], {'token_ids': [7, 9]})])
def test_category_directory_transport_preserves_scope(monkeypatch, tokens, expected):
    monkeypatch.setattr(api, 'DB_MODE', 'api')
    request = Mock(return_value={'rows': []})
    monkeypatch.setattr(api, '_request', request)
    assert api.list_mercado_product_mercado_categories(token_ids=tokens) == {'rows': []}
    request.assert_called_once_with('GET', '/api/db/mercado-products/mercado-categories',
                                   params=expected, timeout=(3, 25), max_attempts=1)


def test_store_link_parent_filter_does_not_truncate_over_100_descendants(monkeypatch):
    ids = [f'CBT{i}' for i in range(1, 160)]
    monkeypatch.setattr(tree, 'category_filter_ids', lambda value: ids)
    monkeypatch.setattr(links, 'ensure_store_link_table', lambda cursor: None)
    monkeypatch.setattr(products, 'ensure_collection_tables', lambda cursor: None)
    monkeypatch.setattr(links, '_store_link_metadata', lambda *args, **kwargs: {
        'group_map': {}, 'groups': [], 'stores': [], 'store_groups': {}, 'sites': [],
        'mercado_categories': [], 'summary': {},
    })
    queries = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=None): queries.append((sql, params))
        def fetchone(self): return {'total': 0}
        def fetchall(self): return []
    class Connection:
        def cursor(self): return Cursor()
        def close(self): pass
        def commit(self): pass
    result = links.list_store_links(mercado_category='tree:CBT1', connection_factory=Connection)
    assert result['total'] == 0
    sql, params = next((sql, params) for sql, params in queries if 'COUNT(*) AS `total`' in sql)
    assert 'links.`category_id` IN (' in sql
    assert list(params) == ids
