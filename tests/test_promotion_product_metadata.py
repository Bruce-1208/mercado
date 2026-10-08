from bit.mercado_promotions import enrich_item_products
from erp import mercadolibre_store_link_store as links


def test_metadata_lookup_is_scoped_and_preserves_promotion_fields(monkeypatch):
    class Connection:
        closed = False
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params):
            assert 'token_id=%s AND site_id=%s' in sql
            assert params == (7, 'MLM', 'MLM1')
        def fetchall(self):
            return [{'item_id': 'MLM1', 'title': 'Product', 'thumbnail_url': 'https://example.test/main.jpg'}]
        def close(self): self.closed = True
    connection = Connection()
    monkeypatch.setattr(links, '_connect', lambda: connection)
    rows = [{'item_id': 'MLM1', 'title': 'Existing title', 'thumbnail': '', 'original_price': 100}]
    result = enrich_item_products({'token_id': 7, 'site_id': 'MLM'}, rows)
    assert result[0]['title'] == 'Existing title'
    assert result[0]['thumbnail'] == 'https://example.test/main.jpg'
    assert result[0]['original_price'] == 100
    assert connection.closed
