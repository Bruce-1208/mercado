from erp import mercadolibre_category_tree as tree


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
            {"id": "CBT1", "name": "Appliances"},
            {"id": "CBT2", "name": "Refrigerators"},
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
    assert tree.category_paths_for_ids(["CBT2"]) == paths


def test_expired_and_corrupt_saved_paths_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(tree, "_CACHE_DIR", tmp_path)
    (tmp_path / "CBT.json").write_text('{"expires_at": 0, "paths": {}}')
    assert tree._read_saved_paths("CBT") is None
    (tmp_path / "CBT.json").write_text('incomplete')
    assert tree._read_saved_paths("CBT") is None
