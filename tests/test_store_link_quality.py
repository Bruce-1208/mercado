from types import SimpleNamespace

import pytest

from bit.bit_store_link_sync import _marketplace_item_with_quality
from erp.mercadolibre_store_link_store import _json_safe_row, listing_record


@pytest.mark.parametrize('value,expected', [(0, 0), ('69.5', 69.5), (100, 100), (None, None), ('null', None), ('NaN', None), (-1, None), (101, None), (True, None)])
def test_quality_score_validation(value, expected):
    assert _json_safe_row({'quality_score': value})['quality_score'] == expected


def test_quality_sync_persists_official_score_in_snapshot():
    calls = []
    def request(method, path):
        calls.append((method, path))
        return {'score': 0, 'level': 'Basic', 'calculated_at': '2026-10-07T00:00:00Z'}
    client = SimpleNamespace(get_marketplace_item=lambda *a, **k: {'id': 'MLM1'}, request=request)
    detail = _marketplace_item_with_quality(client, 'MLM1', attributes=('id',))
    assert calls == [('GET', '/item/MLM1/performance')]
    assert detail['listing_performance']['score'] == 0
    assert '"score":0' in listing_record({'id': 1}, detail, '2026-10-07')['remote_json']


def test_unavailable_quality_keeps_listing_details():
    def request(*args):
        raise RuntimeError('not generated')
    client = SimpleNamespace(get_marketplace_item=lambda *a, **k: {'id': 'MLM1', 'title': 'Product'}, request=request)
    detail = _marketplace_item_with_quality(client, 'MLM1', attributes=('id',))
    assert detail['title'] == 'Product'
    assert detail['listing_performance'] is None
