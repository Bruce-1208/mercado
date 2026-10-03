"""A failed browser check must never enqueue product writes or erase history."""
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from bit import bit_interface as web
from bit import weight_dimensions_records as records


def order(number, **changes):
    return {
        "order_number": str(number), "product_id": str(1000 + number % 33),
        "actual_weight_g": "354", "actual_dimensions_cm": "21x12x11",
        "_dimension_compensation_confirmed": True, "query_status": "已读取",
        "_token_id": 1, "_global_item_id": "CBT1", "marketplace_item_id": "MLB1",
        **changes,
    }


@pytest.fixture
def task(monkeypatch):
    task = {"owner": "1", "status": "ready", "execute_status": "idle",
            "records": [order(number) for number in range(38)]}
    monkeypatch.setattr(records, "_tasks", {"test": task})
    # No production DB reads/writes are permitted in this suite.
    monkeypatch.setattr(records, "_order_history_context", lambda *args: {})
    monkeypatch.setattr(records.bit_db_api, "append_weight_dimensions_record_log", Mock())
    return task


@pytest.mark.parametrize("action", ["zying", "combined"])
@pytest.mark.parametrize("database_view", [False, True])
def test_browser_check_failure_preserves_all_38_orders_and_does_not_start_worker(task, action, database_view):
    for row in task["records"]:
        row.update(_zying_status="失败", execution_error="old error",
                   execution_logs=[{"stage": "智赢插件", "status": "失败", "message": "old error"}])
    saved_rows = task["records"]
    saved_before = deepcopy(saved_rows)
    if database_view:
        task.update(database_view=True, filters={"store_ids": [1]}, records=[])
    before = deepcopy(task)
    check = Mock(side_effect=ValueError("请在执行电脑的 Edge 窗口登录智赢后重新执行"))
    with patch.object(records.bit_db_api, "list_weight_dimensions_records", return_value=saved_rows), \
         patch.object(records.threading, "Thread") as worker, pytest.raises(ValueError, match="登录智赢"):
        records.start_execute("test", "1", action=action, select_all_matching=True,
                              erp_browser_preflight=check)
    check.assert_called_once_with()
    worker.assert_not_called()
    records.bit_db_api.append_weight_dimensions_record_log.assert_not_called()
    assert task == before
    assert saved_rows == saved_before


def test_browser_check_runs_once_before_38_orders_are_queued(task):
    with patch.object(records.threading, "Thread") as worker:
        def check():
            assert task["execute_status"] == "idle"
            worker.assert_not_called()
        checker = Mock(side_effect=check)
        result = records.start_execute("test", "1", action="zying", select_all_matching=True,
                                       erp_browser_preflight=checker)
    checker.assert_called_once_with()
    worker.return_value.start.assert_called_once_with()
    assert result == {"status": "queued", "ready_count": 38}
    assert len(task["execute_order_numbers"]) == 38


def test_agent_checks_its_own_browser_and_skips_server_preflight(task):
    check = Mock(side_effect=AssertionError("must not connect to server Edge"))
    dispatch = Mock(return_value="agent-job")
    with patch.object(records.threading, "Thread") as worker:
        result = records.start_execute("test", "1", action="zying", select_all_matching=True,
                                       agent_dispatch=dispatch, erp_browser_preflight=check)
    check.assert_not_called()
    worker.assert_not_called()
    assert len(dispatch.call_args.args[0]) == 38
    assert result["agent_job_id"] == "agent-job"


def test_zeshun_does_not_require_an_edge_browser(task):
    check = Mock(side_effect=AssertionError("Zeshun uses marketplace APIs"))
    with patch.object(records.threading, "Thread") as worker:
        result = records.start_execute("test", "1", action="zeshun", select_all_matching=True,
                                       erp_browser_preflight=check)
    check.assert_not_called()
    worker.return_value.start.assert_called_once_with()
    assert result["ready_count"] == 38


def test_already_succeeded_orders_skip_browser_check(task):
    for row in task["records"]:
        row["_zying_status"] = "成功"
    check = Mock()
    with patch.object(records.threading, "Thread") as worker:
        result = records.start_execute("test", "1", action="zying", select_all_matching=True,
                                       erp_browser_preflight=check)
    check.assert_not_called()
    worker.assert_not_called()
    assert result["skipped_success_count"] == 38


def test_missing_measurements_fail_before_opening_edge(task):
    task["records"] = [order(1, actual_weight_g="")]
    check = Mock()
    with patch.object(records.threading, "Thread") as worker, pytest.raises(ValueError, match="没有具备可执行数据"):
        records.start_execute("test", "1", action="zying", select_all_matching=True,
                              erp_browser_preflight=check)
    check.assert_not_called()
    worker.assert_not_called()


def test_competing_request_cannot_replace_records_or_start_second_worker(task):
    task.update(database_view=True, filters={"store_ids": [1]})
    original_records = task["records"]
    selected_records = [order(100)]
    def competing_request():
        task["execute_status"] = "running"
    with patch.object(records.bit_db_api, "list_weight_dimensions_records", return_value=selected_records), \
         patch.object(records.threading, "Thread") as worker, pytest.raises(ValueError, match="当前任务正在执行"):
        records.start_execute("test", "1", action="zying", select_all_matching=True,
                              erp_browser_preflight=competing_request)
    worker.assert_not_called()
    assert task["records"] is original_records


def test_overlapping_preflight_only_starts_one_worker(task):
    def competing_request():
        records.start_execute("test", "1", action="zying", select_all_matching=True)
    with patch.object(records.threading, "Thread") as worker, pytest.raises(ValueError, match="当前任务正在执行"):
        records.start_execute("test", "1", action="zying", select_all_matching=True,
                              erp_browser_preflight=competing_request)
    worker.assert_called_once()
    worker.return_value.start.assert_called_once_with()


def test_server_api_returns_recovery_error_instead_of_accepting_failed_batch(task):
    before = deepcopy(task)
    with web.app.test_request_context(json={"action": "zying", "execution_target": "server", "select_all_matching": True}), \
         patch.object(web, "get_current_workbench_user", return_value={"id": 1}), \
         patch.object(web.ai_weight_price_service, "check_weight_dimensions_browser", side_effect=ValueError("Edge 连接不可用，请登录智赢后重新执行")) as check, \
         patch.object(records.threading, "Thread") as worker:
        response, code = web.api_weight_dimensions_records_execute.__wrapped__("test")
    assert code == 400
    assert response.get_json() == {"status": "error", "message": "Edge 连接不可用，请登录智赢后重新执行"}
    check.assert_called_once_with()
    worker.assert_not_called()
    assert task == before


def test_runtime_failure_summary_exposes_failed_count_and_reason(task):
    @contextmanager
    def unavailable(log):
        raise ValueError("Edge 连接已中断，请重新打开登录窗口")
        yield

    records._run_execute("test", "1", action="zying", erp_browser_factory=unavailable)
    assert task["execute_status"] == "completed"
    assert "成功 0 条，失败 38 条" in task["execute_message"]
    assert "Edge 连接已中断" in task["execute_message"]
    assert all(row["_zying_status"] == "失败" for row in task["records"])


def test_legacy_combined_entry_forwards_browser_preflight(task):
    check = Mock()
    with patch.object(records, "_latest_task_by_owner", {"1": "test"}), \
         patch.object(records, "start_execute", return_value={"status": "queued"}) as start:
        records.start_latest_execute("1", erp_browser_preflight=check)
    assert start.call_args.kwargs["action"] == "combined"
    assert start.call_args.kwargs["erp_browser_preflight"] is check
