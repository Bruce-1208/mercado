import pytest
from erp import mercadolibre_category_tree as tree


def test_children_localized_and_site_validated(monkeypatch):
    calls = []
    monkeypatch.setattr(tree, '_category_children_cached', lambda site, parent, hour: calls.append((site, parent)) or [{'id': 'MLM2', 'name': 'Tools'}])
    monkeypatch.setattr(tree, '_translate_names', lambda language, names: {'Tools': '工具'})
    assert tree.category_children('mlm', 'mlm1') == [{'id': 'MLM2', 'name': 'Tools', 'name_zh': '工具'}]
    assert calls == [('MLM', 'MLM1')]
    with pytest.raises(ValueError):
        tree.category_children('MLM', 'MLB1')
    with pytest.raises(ValueError):
        tree.category_children('../invalid')


def test_leaf_and_root_transport(monkeypatch):
    tree._category_children_cached.cache_clear()
    urls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'children_categories': []} if '/categories/MLM' in urls[-1] else [{'id': 'MLM1', 'name': 'Tools'}]
    monkeypatch.setattr(tree.requests, 'get', lambda url, **kwargs: urls.append(url) or Response())
    assert tree._category_children_cached('MLM', '', 1) == [{'id': 'MLM1', 'name': 'Tools'}]
    assert tree._category_children_cached('MLM', 'MLM1', 1) == []
    tree._category_children_cached.cache_clear()
