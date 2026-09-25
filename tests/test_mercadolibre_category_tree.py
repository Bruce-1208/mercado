from erp import mercadolibre_category_tree as tree


def test_category_paths_use_official_hierarchy_and_cache(monkeypatch):
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
