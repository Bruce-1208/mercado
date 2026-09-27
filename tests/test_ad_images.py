from bit.ad_images import enrich
from bit.bit_ad_analysis import _product_image
from erp import mercadolibre_store_link_store as store


def test_invalid_thumbnail_falls_back_to_picture():
    assert _product_image({'thumbnail_url': 'invalid', 'pictures': [None, {'url': 'http://example.com/image.jpg'}]}) == 'https://example.com/image.jpg'


def test_local_images_are_scoped_and_preserve_existing(monkeypatch):
    class Connection:
        closed = False
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params):
            assert params == (1, 'MLM1', 2, 'MLM1')
        def fetchall(self):
            return [{'token_id': 1, 'item_id': 'MLM1', 'thumbnail_url': 'http://example.com/a.jpg'}]
        def close(self): self.closed = True
    connection = Connection()
    monkeypatch.setattr(store, '_connect', lambda: connection)
    rows = [{'token_id': 1, 'item_id': 'MLM1'}, {'token_id': 2, 'item_id': 'MLM1'},
            {'token_id': 3, 'item_id': 'MLM2', 'thumbnail_url': 'https://example.com/b.jpg'}]
    enrich(rows)
    assert rows[0]['thumbnail_url'] == 'https://example.com/a.jpg'
    assert rows[1]['thumbnail_url'] == ''
    assert rows[2]['thumbnail_url'] == 'https://example.com/b.jpg'
    assert connection.closed
