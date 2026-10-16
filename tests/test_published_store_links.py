from unittest.mock import Mock
import pytest
from erp import mercadolibre_published_links as links
from erp import mercadolibre_store_link_store as store


@pytest.mark.parametrize('result,expected', [
    ({'id': 'MLM123', 'status': 'under_review'}, ['MLM123']),
    ({'id': 'CBTU1', 'site_items': [{'item_id': 'MLM123'}, {'item_id': 'MLB123'}]}, ['MLM123']),
    ({'user_products': [{'site_items': [{'item_id': 'MLM123'}]}, {'site_items': [{'item_id': 'MLM456'}]}]}, ['MLM123', 'MLM456']),
])
def test_confirmed_links_are_written_without_scan(monkeypatch, result, expected):
    saved = []
    monkeypatch.setattr(links, 'replace_store_snapshot', lambda token, items, **kw: saved.append((token, list(items), kw)))
    invalidate = Mock()
    monkeypatch.setattr(links, 'invalidate_store_link_metadata_cache', invalidate)
    client = Mock()
    links.save_published_store_links({'id': 7}, {'result': result, 'payload': {'title': '新商品'}},
                                     site_id='MLM', product={}, quantity=3, client=client)
    token, items, options = saved[0]
    assert token['id'] == 7
    assert [row['id'] for row in items] == expected
    assert all(row['site_id'] == 'MLM' and row['permalink'] and row['available_quantity'] == 3 for row in items)
    assert options == {'finalize': False}
    assert items[0]['status'] == result.get('status', 'active')
    client.request.assert_not_called()
    invalidate.assert_called_once()


def test_parent_only_response_resolves_target_without_full_scan(monkeypatch):
    write = Mock()
    monkeypatch.setattr(links, 'replace_store_snapshot', write)
    monkeypatch.setattr(links, 'invalidate_store_link_metadata_cache', Mock())
    client = Mock()
    client.request.return_value = {'marketplace_items': [{'item_id': 'MLM123'}]}
    links.save_published_store_links({'id': 7}, {'result': {'id': 'CBT123'}},
                                     site_id='MLM', product={'title': '标题'}, quantity=1, client=client)
    client.request.assert_called_once_with('GET', '/items/CBT123/marketplace_items')
    assert list(write.call_args.args[1])[0]['id'] == 'MLM123'


def test_missing_target_does_not_insert_parent_or_other_site(monkeypatch):
    write = Mock()
    monkeypatch.setattr(links, 'replace_store_snapshot', write)
    with pytest.raises(ValueError, match='目标站点商品编号'):
        links.save_published_store_links({'id': 7}, {'result': {'site_items': [{'item_id': 'MLB123'}]}},
                                         site_id='MLM', product={}, quantity=1, client=Mock())
    write.assert_not_called()


def test_scan_finalization_keeps_concurrent_publications(monkeypatch):
    monkeypatch.setattr(store, 'ensure_store_link_table', lambda c: None)
    connection = Mock()
    cursor = Mock(rowcount=0)
    connection.cursor.return_value.__enter__ = Mock(return_value=cursor)
    connection.cursor.return_value.__exit__ = Mock(return_value=False)
    store.finalize_store_snapshot(7, 'scan', scan_started_at='2026-10-15 10:00:00', connection_factory=lambda: connection)
    sql, params = cursor.execute.call_args.args
    assert '`last_synced_at` < %s' in sql
    assert params == (7, 'scan', '2026-10-15 10:00:00')
    connection.commit.assert_called_once()
