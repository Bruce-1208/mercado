import pytest

from erp import mercadolibre_category_tree as tree


@pytest.fixture(autouse=True)
def cached_translations(monkeypatch):
    monkeypatch.setattr(tree, "_translated_names", {})
    monkeypatch.setattr(tree, "_translate_names", lambda language, names: {name: name for name in names})


def test_category_paths_use_official_hierarchy_and_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(tree, "_CACHE_DIR", tmp_path)
    tree._site_cache.clear()
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "CBT1": {"path_from_root": [{"id": "CBT1", "name": "Appliances"}]},
                "CBT2": {"path_from_root": [
                    {"id": "CBT1", "name": "Appliances"},
                    {"id": "CBT2", "name": "Refrigerators"},
                ]},
            }

    def get(url, **kwargs):
        calls.append(url)
        return Response()

    monkeypatch.setattr(tree.requests, "get", get)
    assert tree.category_paths_for_ids(["CBT2", "invalid"]) == {
        "CBT2": [
            {"id": "CBT1", "name": "Appliances", "name_source": "Appliances"},
            {"id": "CBT2", "name": "Refrigerators", "name_source": "Refrigerators"},
        ]
    }
    assert tree.category_paths_for_ids(["CBT1"])["CBT1"][0]["name"] == "Appliances"
    assert calls == ["https://api.mercadolibre.com/sites/CBT/categories/all"]


def test_saved_paths_survive_process_cache_reset(monkeypatch, tmp_path):
    monkeypatch.setattr(tree, "_CACHE_DIR", tmp_path)
    tree._site_cache.clear()
    paths = {"CBT2": [{"id": "CBT2", "name": "Refrigerators"}]}
    tree._save_paths("CBT", paths)
    def unexpected_request(*args, **kwargs):
        raise AssertionError("saved paths should avoid downloading the full catalog")
    monkeypatch.setattr(tree.requests, "get", unexpected_request)
    assert tree.category_paths_for_ids(["CBT2"])["CBT2"][0]["name"] == "Refrigerators"


def test_expired_and_corrupt_saved_paths_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(tree, "_CACHE_DIR", tmp_path)
    (tmp_path / "CBT.json").write_text('{"expires_at": 0, "paths": {}}')
    assert tree._read_saved_paths("CBT") is None
    (tmp_path / "CBT.json").write_text('incomplete')
    assert tree._read_saved_paths("CBT") is None


def test_fast_paths_use_expired_catalog_and_cached_translation_without_network(monkeypatch, tmp_path):
    monkeypatch.setattr(tree, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(tree, "_site_cache", {})
    (tmp_path / "CBT.json").write_text('{"expires_at": 0, "paths": {"CBT2": [{"id": "CBT2", "name": "Refrigerators"}]}}')
    tree._translated_names["en"] = {"Refrigerators": "冰箱"}
    jobs = []
    monkeypatch.setattr(tree, "_warm_category_paths", lambda site, ids: jobs.append((site, ids)))
    monkeypatch.setattr(tree.requests, "get", lambda *a, **k: pytest.fail("fast cached read must not use network"))
    assert tree.category_paths_for_ids(["CBT2"], fast=True)["CBT2"][0]["name"] == "冰箱"
    assert jobs == [("CBT", ["CBT2"])]


def test_translation_failure_preserves_official_hierarchy(monkeypatch, tmp_path):
    monkeypatch.setattr(tree, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(tree, "_site_cache", {})
    tree._save_paths("CBT", {"CBT2": [{"id": "CBT2", "name": "Refrigerators"}]})
    def fail(*args):
        raise TimeoutError("translation unavailable")
    monkeypatch.setattr(tree, "_translate_names", fail)
    assert tree.category_paths_for_ids(["CBT2"])["CBT2"][0]["name"] == "Refrigerators"


def test_fast_read_does_not_wait_for_translation_lock(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(tree, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(tree, "_site_cache", {})
    tree._save_paths("CBT", {"CBT2": [{"id": "CBT2", "name": "Refrigerators"}]})
    monkeypatch.setattr(tree, "_warm_category_paths", lambda *args: None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with tree._translation_lock:
            result = pool.submit(tree.category_paths_for_ids, ["CBT2"], fast=True).result(timeout=1)
    assert result["CBT2"][0]["name"] == "Refrigerators"


def test_background_refresh_is_deduplicated_and_failure_releases_site(monkeypatch):
    jobs = []
    class Thread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            jobs.append(self.target)
    monkeypatch.setattr(tree, "_background_jobs", set())
    monkeypatch.setattr(tree.threading, "Thread", Thread)
    def fail(*args):
        raise TimeoutError("catalog unavailable")
    monkeypatch.setattr(tree, "category_paths_for_ids", fail)
    tree._warm_category_paths("CBT", ["CBT2"])
    tree._warm_category_paths("CBT", ["CBT2"])
    assert len(jobs) == 1
    jobs.pop()()
    assert not tree._background_jobs
    tree._warm_category_paths("CBT", ["CBT2"])
    assert len(jobs) == 1


def test_cold_fast_read_schedules_catalog_without_blocking(monkeypatch, tmp_path):
    monkeypatch.setattr(tree, '_CACHE_DIR', tmp_path)
    monkeypatch.setattr(tree, '_site_cache', {})
    jobs = []
    monkeypatch.setattr(tree, '_warm_category_paths', lambda site, ids: jobs.append((site, ids)))
    monkeypatch.setattr(tree.requests, 'get', lambda *a, **k: pytest.fail('fast read must not download catalog'))
    assert tree.category_paths_for_ids(['CBT2'], fast=True) == {}
    assert jobs == [('CBT', ['CBT2'])]


def test_parent_filter_expands_all_descendants_and_keeps_sites_separate(monkeypatch):
    monkeypatch.setattr(tree, '_site_cache', {'CBT': (0, {
        'CBT1': [{'id': 'CBT1'}],
        **{f'CBT{i}': [{'id': 'CBT1'}, {'id': f'CBT{i}'}] for i in range(2, 152)},
        'CBT999': [{'id': 'CBT999'}],
    }), 'MLB': (0, {'MLB2': [{'id': 'MLB1'}, {'id': 'MLB2'}]})})
    result = tree.category_filter_ids('tree:CBT1,MLB1')
    assert len(result) == 153
    assert 'CBT151' in result and 'MLB2' in result and 'CBT999' not in result


@pytest.mark.parametrize('value', ['tree:', 'tree:CBT1,', 'tree:CBT1 OR 1=1', 'tree:家具'])
def test_invalid_tree_filters_rejected(value):
    with pytest.raises(ValueError):
        tree.category_filter_ids(value)


def test_fast_read_warms_missing_category_in_existing_catalog(monkeypatch, tmp_path):
    monkeypatch.setattr(tree, '_CACHE_DIR', tmp_path)
    monkeypatch.setattr(tree, '_site_cache', {})
    tree._save_paths('CBT', {'CBT1': [{'id': 'CBT1', 'name': 'Tools'}]})
    tree._translated_names['en'] = {'Tools': '工具'}
    jobs = []
    monkeypatch.setattr(tree, '_warm_category_paths', lambda site, ids: jobs.append((site, ids)))
    assert tree.category_paths_for_ids(['CBT2'], fast=True) == {}
    assert jobs == [('CBT', ['CBT2'])]


def test_missing_cached_category_is_filled_from_official_detail(monkeypatch, tmp_path):
    monkeypatch.setattr(tree, '_CACHE_DIR', tmp_path)
    monkeypatch.setattr(tree, '_site_cache', {})
    tree._save_paths('CBT', {'CBT1': [{'id': 'CBT1', 'name': 'Tools'}]})
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'path_from_root': [{'id': 'CBT1', 'name': 'Tools'}, {'id': 'CBT2', 'name': 'Hammers'}]}
    def get(url, **kwargs):
        calls.append(url)
        return Response()
    monkeypatch.setattr(tree.requests, 'get', get)
    assert tree.category_paths_for_ids(['CBT2'])['CBT2'][-1]['id'] == 'CBT2'
    tree._site_cache.clear()
    assert tree.category_paths_for_ids(['CBT2'])['CBT2'][-1]['id'] == 'CBT2'
    assert calls == ['https://api.mercadolibre.com/categories/CBT2']


def test_bulk_catalog_failure_falls_back_to_details_and_chinese(monkeypatch, tmp_path):
    monkeypatch.setattr(tree, '_CACHE_DIR', tmp_path)
    monkeypatch.setattr(tree, '_site_cache', {})
    monkeypatch.setattr(tree, '_translate_names', lambda lang, names: {'Tools': '工具', 'Hammers': '锤子'})
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'path_from_root': [{'id': 'CBT1', 'name': 'Tools'}, {'id': 'CBT2', 'name': 'Hammers'}]}
    def get(url, **kwargs):
        if '/sites/' in url:
            raise tree.requests.HTTPError('catalog unavailable')
        return Response()
    monkeypatch.setattr(tree.requests, 'get', get)
    result = tree.category_paths_for_ids(['CBT2'])
    assert [node['name'] for node in result['CBT2']] == ['工具', '锤子']
    assert tree.category_filter_ids('tree:CBT1') == ['CBT1', 'CBT2']


def test_translation_merged_lines_retry_individually_and_save(monkeypatch, tmp_path):
    # Use the actual implementation rather than this file's translation fixture.
    import importlib.util
    spec = importlib.util.spec_from_file_location('isolated_category_translation', tree.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._CACHE_DIR = tmp_path
    calls = []
    class Response:
        def __init__(self, value): self.value = value
        def raise_for_status(self): pass
        def json(self): return [[[self.value]]]
    def get(url, **kwargs):
        value = kwargs['params']['q']
        calls.append(value)
        return Response({'Hammers': '锤子', 'Tools': '工具'}.get(value, '工具和锤子'))
    monkeypatch.setattr(module.requests, 'get', get)
    assert module._translate_names('en', {'Tools', 'Hammers'}) == {'Tools': '工具', 'Hammers': '锤子'}
    assert len(calls) == 3
    assert (tmp_path / 'names_en_zh.json').exists()
