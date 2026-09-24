from erp.ai_weight_price.models import Models
from erp.ai_weight_price.service import Service

from bit.ai_weight_price_client import detail, search, start


def _service(tmp_path):
    service = Service(tmp_path)
    service.bind_actor({"id": 7, "username": "tester", "display_name": "测试员"})
    service.store.set_state("login", {"confirmed": True})
    service.exchange_rate = lambda _config: {
        "cny_per_usd": "7", "date": "2026-09-23", "source": "test"
    }
    return service


def test_extension_client_start_returns_browser_search_action(tmp_path):
    service = _service(tmp_path)
    result = start(service, {
        "selection": {"category": "", "start_page": 1, "end_page": 1, "start_item": 1},
        "max_items": 1,
        "products": [{
            "erp_goods_id": "1001", "title": "测试商品",
            "main_image_url": "https://img.example.test/product.jpg",
        }],
    })

    assert result["action"] == "search"
    assert result["task"]["erp_goods_id"] == "1001"
    assert result["data"]["execution_target"] == "extension"
    assert result["data"]["execution_terminal"] == "浏览器插件"


def test_extension_client_search_and_detail_keep_model_and_browser_boundaries(tmp_path, monkeypatch):
    service = _service(tmp_path)
    start(service, {
        "selection": {"category": "", "start_page": 1, "end_page": 1, "start_item": 1},
        "max_items": 1,
        "products": [{
            "erp_goods_id": "1001", "title": "测试商品",
            "main_image_url": "https://img.example.test/product.jpg",
        }],
    })
    candidate = {
        "url": "https://detail.1688.com/offer/2002.html",
        "title": "同款货源", "main_image_url": "https://img.example.test/source.jpg",
        "image_confidence": 0.98,
    }
    monkeypatch.setattr(
        Models, "match_images",
        lambda _models, _task, _candidates: (
            [candidate], [{"candidate": candidate, "review": {"confidence": 0.98, "same_product": True}}]
        ),
    )

    matched = search(service, {
        "task_id": "1001", "candidates": [candidate], "runtime_api_key": "test-key"
    })
    assert matched["action"] == "detail"
    assert matched["candidate"]["url"].endswith("2002.html")

    result = detail(service, {
        "task_id": "1001",
        "detail": {
            "url": candidate["url"], "merchant_id": "merchant-1",
            "weight_g": 540,
            "skus": [{"id": "sku-1", "label": "标准", "price": 22, "raw_weight": "540g"}],
        },
        "runtime_api_key": "test-key",
    })
    assert result["action"] == "done"
    task = service.store.get("1001")
    assert task["supplier_url"] == candidate["url"]
    assert str(task["cost_price"]) == "22"
    assert task["status"] == "risk"
