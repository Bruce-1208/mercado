import pytest

from erp.ai_weight_price.models import Models
from erp.ai_weight_price.service import Service

from bit.ai_weight_price_client import categories, collect, detail, fail, search, start, writeback


def _service(tmp_path):
    service = Service(tmp_path)
    service.bind_actor({"id": 7, "username": "tester", "display_name": "测试员"})
    service.store.set_state("login", {"confirmed": True})
    service.exchange_rate = lambda _config: {
        "cny_per_usd": "7", "date": "2026-09-23", "source": "test"
    }
    config = service.config.load()
    config["writeback_enabled"] = False
    service.config.save(config)
    return service


def test_extension_client_start_returns_browser_search_action(tmp_path):
    service = _service(tmp_path)
    result = start(service, {
        "selection": {
            "category": "", "start_page": 1, "end_page": 1, "start_item": 1,
            "product_developer_id": "17", "product_developer_name": "产品开发甲",
        },
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
    assert result["task"]["product_developer_id"] == "17"
    assert result["task"]["product_developer_name"] == "产品开发甲"


def test_extension_client_category_refresh_keeps_last_known_developers(tmp_path):
    service = _service(tmp_path)
    service.store.set_state("developers", [{"id": "17", "name": "产品开发甲"}])

    result = categories(service, {
        "credential": "page-token",
        "categories": [{"category_id": "8", "category_name": "家居"}],
        "developers": [],
    })

    assert result["developers"] == [{"id": "17", "name": "产品开发甲"}]
    assert result["data"]["developers"] == [{"id": "17", "name": "产品开发甲"}]


def test_extension_client_lazy_run_collects_only_after_previous_item_finishes(tmp_path, monkeypatch):
    service = _service(tmp_path)
    first = {
        "erp_goods_id": "1001", "title": "第一件",
        "main_image_url": "https://img.example.test/1.jpg",
    }
    second = {
        "erp_goods_id": "1002", "title": "第二件",
        "main_image_url": "https://img.example.test/2.jpg",
    }
    result = start(service, {
        "selection": {"category": "", "start_product_id": ""}, "max_items": 2,
        "products": [first, second], "lazy_collection": True,
        "collection_cursor": {"page": 1, "index": 1},
    })

    assert result["action"] == "search"
    assert result["task"]["erp_goods_id"] == "1001"
    assert service.store.state("run")["task_ids"] == ["1001"]
    monkeypatch.setattr(Models, "match_images", lambda *_args: ([], []))
    next_step = search(service, {
        "task_id": "1001",
        "candidates": [{"url": "https://detail.1688.com/offer/1.html"}],
        "runtime_api_key": "test-key",
    })

    assert next_step["action"] == "collect"
    assert next_step["collection"]["cursor"]["seen_ids"] == ["1001"]
    assert service.store.state("run")["processed_items"] == 1
    second_step = collect(service, {
        "product": second, "cursor": {"page": 1, "index": 2}
    })
    assert second_step["action"] == "search"
    assert second_step["task"]["erp_goods_id"] == "1002"
    assert service.store.state("run")["task_ids"] == ["1001", "1002"]

    finished = search(service, {
        "task_id": "1002",
        "candidates": [{"url": "https://detail.1688.com/offer/2.html"}],
        "runtime_api_key": "test-key",
    })
    assert finished["action"] == "done"
    assert service.store.state("run")["outcome"] == "completed"


def test_extension_client_completes_twenty_items_strictly_in_order(tmp_path, monkeypatch):
    service = _service(tmp_path)
    config = service.config.load()
    config["writeback_enabled"] = True
    service.config.save(config)
    products = [{
        "erp_goods_id": str(2000 + index),
        "title": f"连续测试商品{index}",
        "main_image_url": f"https://img.example.test/{index}.jpg",
        "source_index": index,
    } for index in range(1, 21)]
    monkeypatch.setattr(Models, "match_images", lambda _self, _task, candidates: (
        [candidates[0]], [{"candidate": candidates[0], "review": {
            "confidence": 0.99, "same_product": True,
        }}],
    ))

    step = start(service, {
        "selection": {"category": "", "start_product_id": ""},
        "max_items": 20, "products": [products[0]], "lazy_collection": True,
        "collection_cursor": {"page": 1, "index": 1},
    })
    completed = []
    for index, product in enumerate(products, 1):
        assert step["action"] == "search"
        assert step["task"]["erp_goods_id"] == product["erp_goods_id"]
        candidate = {
            "url": f"https://detail.1688.com/offer/{9000 + index}.html",
            "title": f"同款货源{index}",
            "main_image_url": f"https://img.example.test/source-{index}.jpg",
            "image_confidence": 0.99,
        }
        step = search(service, {
            "task_id": product["erp_goods_id"], "candidates": [candidate],
            "runtime_api_key": "test-key",
        })
        assert step["action"] == "detail"
        step = detail(service, {
            "task_id": product["erp_goods_id"], "runtime_api_key": "test-key",
            "detail": {
                "url": candidate["url"], "weight_g": 500 + index,
                "skus": [{"id": f"sku-{index}", "label": "标准",
                          "price": 10 + index, "raw_weight": f"{500 + index}g"}],
            },
        })
        assert step["action"] == "writeback"
        before = {"erp_goods_id": product["erp_goods_id"], "review_status": "待审核"}
        after = {"erp_goods_id": product["erp_goods_id"], **step["changes"]}
        step = writeback(service, {
            "task_id": product["erp_goods_id"], "before": before, "actual": after,
            "changes": step["changes"], "submitted": True, "persisted": True,
        })
        completed.append(product["erp_goods_id"])
        if index < 20:
            assert step["action"] == "collect"
            step = collect(service, {
                "product": products[index], "cursor": {"page": 1, "index": index + 1},
            })

    assert step["action"] == "done"
    run = service.store.state("run")
    assert run["outcome"] == "completed"
    assert run["processed_items"] == 20
    assert run["success_items"] == 20
    assert run["task_ids"] == completed


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


def test_extension_client_only_marks_writeback_success_after_persisted_reload_readback(tmp_path):
    service = _service(tmp_path)
    config = service.config.load()
    config["writeback_enabled"] = True
    service.config.save(config)
    start(service, {
        "selection": {"category": "", "start_page": 1, "end_page": 1, "start_item": 1},
        "max_items": 1,
        "products": [{
            "erp_goods_id": "1001", "title": "测试商品",
            "main_image_url": "https://img.example.test/product.jpg",
            "source_page": 2, "source_index": 7,
        }],
    })
    service.store.update("1001", best_match_confidence=0.99)
    pending = detail(service, {
        "task_id": "1001",
        "detail": {
            "url": "https://detail.1688.com/offer/2002.html",
            "weight_g": 540,
            "skus": [{"id": "sku-1", "label": "标准", "price": 22, "raw_weight": "540g"}],
        },
        "runtime_api_key": "test-key",
    })
    assert pending["action"] == "writeback"
    assert pending["task"]["source_page"] == 2
    assert pending["task"]["source_index"] == 7
    changes = pending["changes"]
    before = {
        "erp_goods_id": "1001", "weight_g": 500,
        "net_income_usd": 9, "review_status": "待审核",
    }
    after = {"erp_goods_id": "1001", **changes}

    with pytest.raises(ValueError, match="持久化回读"):
        writeback(service, {
            "task_id": "1001", "before": before, "actual": after,
            "changes": changes, "submitted": True,
        })
    assert service.store.get("1001").get("write_verified") is not True

    result = writeback(service, {
        "task_id": "1001", "before": before, "actual": after,
        "changes": changes, "submitted": True, "persisted": True,
    })
    task = service.store.get("1001")
    assert result["action"] == "done"
    assert task["status"] == "success"
    assert task["erp_before"] == before
    assert task["erp_after"] == after
    assert task["write_verified"] is True
    assert task["write_history"][-1]["verified"] is True


def test_extension_client_rejects_same_dom_false_positive_and_records_failure(tmp_path):
    service = _service(tmp_path)
    start(service, {
        "selection": {"category": "", "start_page": 1, "end_page": 1, "start_item": 1},
        "max_items": 1,
        "products": [{
            "erp_goods_id": "1001", "title": "测试商品",
            "main_image_url": "https://img.example.test/product.jpg",
        }],
    })
    with pytest.raises(ValueError, match="审核状态保存回读不一致"):
        writeback(service, {
            "task_id": "1001",
            "before": {"erp_goods_id": "1001", "review_status": "待审核"},
            "actual": {"erp_goods_id": "1001", "weight_g": 540,
                       "net_income_usd": 8, "review_status": "待审核"},
            "changes": {"weight_g": 540, "net_income_usd": 8, "review_status": "通过"},
            "submitted": True, "persisted": True,
        })
    task = service.store.get("1001")
    assert task["write_verified"] is False
    assert task["write_history"][-1]["verified"] is False

    result = fail(service, {"task_id": "1001", "action": "writeback", "error": "刷新后状态未保存"})
    assert result["action"] == "done"
    assert service.store.state("run")["outcome"] == "failed"
    assert service.store.state("run_error") == "刷新后状态未保存"
