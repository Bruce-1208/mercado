"""Verification failures retain the serial item until a human continues it."""

import copy

import pytest

from bit.ai_weight_price_client import dispatch
from erp.ai_weight_price.service import Service


def _service(path):
    service = Service(path)
    service.bind_actor({"id": 7, "username": "verification-tester"})
    service.store.set_state("login", {"confirmed": True})
    return service


def _start(service, *, lazy=False):
    return dispatch(service, "start", {
        "selection": {"category": "", "start_page": 2, "end_page": 3, "start_item": 1},
        "max_items": 3,
        "lazy_collection": lazy,
        "collection_cursor": {"page": 2, "index": 1, "filters_applied": True},
        "products": [{
            "erp_goods_id": str(key), "title": f"商品 {key}",
            "main_image_url": f"https://img.example.test/{key}.jpg",
        } for key in (1001, 1002, 1003)],
    })


@pytest.mark.parametrize("action,reason", [
    ("search", "1688滑块验证未确认通过：拖动后仍有验证提示或页面未恢复；已暂停，请手动完成验证后继续核重核价"),
    ("detail", "1688滑块验证未确认通过：验证提示已消失，但未读到目标商品或搜索结果；已暂停，请手动完成验证后继续核重核价"),
    ("search", "1688滑块验证未确认通过：无法读取验证状态；已暂停，请手动完成验证后继续核重核价"),
])
def test_verification_failure_preserves_current_item_and_completed_progress(tmp_path, action, reason):
    service = _service(tmp_path)
    first = _start(service)
    current = dispatch(service, "fail", {
        "run_id": first["data"]["run"]["run_id"],
        "step_token": first["step_token"], "task_id": "1001",
        "action": "search", "error": "普通商品定位异常",
    })
    assert current["task"]["erp_goods_id"] == "1002"
    before = copy.deepcopy(service.store.state("run"))
    next_item = copy.deepcopy(service.store.get("1003"))
    current_item = copy.deepcopy(service.store.get("1002"))

    paused = dispatch(service, "fail", {
        "run_id": before["run_id"], "step_token": current["step_token"],
        "task_id": "1002", "action": action, "error": reason,
    })
    run = service.store.state("run")
    assert paused["action"] == "done"
    assert paused["data"]["running"] is False
    assert run["outcome"] == "blocked"
    for field in ("run_id", "current_task_id", "current_item_index", "cursor",
                  "processed_items", "success_items", "skipped_items", "blocked_items",
                  "risk_items", "selection", "collection_cursor", "task_ids", "max_items"):
        assert run[field] == before[field]
    assert run["processed_items"] == 1
    assert service.store.state("pipeline_current")["task_id"] == "1002"
    assert service.store.state("circuit")["reason"] == reason
    task = service.store.get("1002")
    assert task["status"] == "exception"
    assert not task.get("skip_reason")
    for field in ("erp_before", "erp_after", "write_intent", "write_history", "write_verified"):
        assert task.get(field) == current_item.get(field)
    assert service.store.get("1003") == next_item

    with pytest.raises(ValueError, match="请先完成登录或人机验证"):
        dispatch(service, "continue", {"acknowledged": False})
    assert service.store.state("circuit")["reason"] == reason
    with pytest.raises(ValueError, match="当前任务已停止"):
        dispatch(service, "search", {
            "run_id": before["run_id"], "task_id": "1002", "candidates": [],
        })
    assert service.store.state("run")["cursor"] == before["cursor"]

    # Recreate the server so a stored verification pause also survives restart.
    restarted = _service(tmp_path)
    resumed = dispatch(restarted, "continue", {"acknowledged": True})
    assert resumed["action"] == "search"
    assert resumed["task"]["erp_goods_id"] == "1002"
    assert resumed["step_token"] != current["step_token"]
    assert resumed["data"]["run"]["run_id"] == before["run_id"]
    assert restarted.store.state("run")["processed_items"] == 1
    assert restarted.store.state("run")["cursor"] == before["cursor"]
    assert restarted.store.state("circuit") is None
    assert restarted.store.get("1002")["status"] == "pending"
    assert restarted.store.get("1001")["status"] == "skipped"
    assert restarted.store.get("1003") == next_item


def test_verification_pause_does_not_collect_another_lazy_product(tmp_path):
    service = _service(tmp_path)
    current = _start(service, lazy=True)
    before = copy.deepcopy(service.store.state("run"))
    paused = dispatch(service, "fail", {
        "run_id": before["run_id"], "step_token": current["step_token"],
        "task_id": "1001", "action": "search",
        "error": "1688滑块验证未确认通过：本商品已尝试一次，验证仍未解除",
    })
    assert paused["action"] == "done"
    assert service.store.state("run")["task_ids"] == ["1001"]
    assert service.store.state("run")["collection_cursor"] == before["collection_cursor"]
    assert service.store.state("run")["processed_items"] == 0
    resumed = dispatch(service, "continue", {"acknowledged": True})
    assert resumed["action"] == "search"
    assert resumed["task"]["erp_goods_id"] == "1001"
    assert service.store.state("run")["task_ids"] == ["1001"]
    assert service.store.state("run")["collection_cursor"] == before["collection_cursor"]
