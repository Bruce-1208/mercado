"""Execution shutdown regressions use mocks and never update live products."""
from contextlib import contextmanager
from copy import deepcopy
import threading
from unittest.mock import Mock

import pytest

from bit import weight_dimensions_records as records


def order(number):
    return {
        "order_number": str(number), "product_id": str(number),
        "actual_weight_g": "400", "actual_dimensions_cm": "30x20x10",
        "query_status": "已读取", "_dimension_compensation_confirmed": True,
        "_token_id": 1, "_global_item_id": "CBT1", "marketplace_item_id": "MLM1",
    }


@pytest.fixture
def lifecycle_task(monkeypatch):
    task = {"owner": "user", "status": "ready", "execute_status": "idle",
            "records": [order(1), order(2)], "authorized_ids": [1]}
    monkeypatch.setattr(records, "_tasks", {"task": task})
    monkeypatch.setattr(records, "_execution_threads", {})
    monkeypatch.setattr(records, "_executions_shutting_down", False)
    monkeypatch.setattr(records, "_order_history_context", lambda *args: {})
    monkeypatch.setattr(records, "_history_site_settings", lambda *args: [])
    monkeypatch.setattr(records.bit_db_api, "append_weight_dimensions_record_log", Mock())
    monkeypatch.setattr(records.bit_mysql, "get_mercado_store_token", Mock(return_value={}))
    return task


def marketplace(monkeypatch):
    client = Mock(access_token="test", timeout=10)
    client.get_marketplace_item.return_value = {"id": "MLM1", "title": "Same", "cbt_item_id": "CBT1"}
    monkeypatch.setattr(records, "_client_and_token", lambda *args: (client, {}))
    monkeypatch.setattr(records, "MercadoLibreClient", Mock(return_value=client))
    return client


@pytest.mark.parametrize("stop_at", ["first_item", "first_page"])
def test_stop_during_discovery_prevents_next_remote_request(lifecycle_task, monkeypatch, stop_at):
    task = lifecycle_task
    task.update(execute_status="queued", execute_action="zeshun", execute_order_numbers=["1", "2"])
    client = marketplace(monkeypatch)

    def read(*args, **kwargs):
        if stop_at == "first_item":
            records.stop_execute("task", "user")
        return {"id": "MLM1", "title": "Same", "cbt_item_id": "CBT1"}

    def page(*args, **kwargs):
        records.stop_execute("task", "user")
        return {"pages": 2, "rows": []}

    client.get_marketplace_item.side_effect = read
    listing = Mock(side_effect=page)
    monkeypatch.setattr(records.bit_db_api, "list_mercado_store_links", listing)
    package = Mock()
    monkeypatch.setattr(records, "_package_attributes", package)
    records._run_execute_guarded("task", "user", "zeshun", ["1", "2"], None)
    assert client.get_marketplace_item.call_count == 1
    assert listing.call_count == (0 if stop_at == "first_item" else 1)
    package.assert_not_called()
    assert task["execute_status"] == "stopped"
    assert all(row["_zeshun_status"] == "已终结" for row in task["records"])
    assert not any(log["status"] == "失败" for row in task["records"] for log in row["execution_logs"])


def test_stop_after_package_write_preserves_success_and_does_not_fail_unsubmitted_net(lifecycle_task, monkeypatch):
    task = lifecycle_task
    task.update(execute_status="queued", execute_action="zeshun", execute_order_numbers=["1"])
    client = marketplace(monkeypatch)
    monkeypatch.setattr(records.bit_db_api, "list_mercado_store_links", Mock(return_value={"pages": 1, "rows": []}))
    package = Mock(side_effect=lambda *args: records.stop_execute("task", "user"))
    monkeypatch.setattr(records, "_package_attributes", package)
    executor = Mock(wraps=records.ThreadPoolExecutor)
    monkeypatch.setattr(records, "ThreadPoolExecutor", executor)
    records._run_execute_guarded("task", "user", "zeshun", ["1"], None)
    package.assert_called_once()
    client.update_global_item.assert_not_called()
    assert executor.call_count == 1, "stopping must not create a new executor for net proceeds"
    assert task["execute_status"] == "stopped"
    row = task["records"][0]
    assert row["_zeshun_status"] == "已终结"
    assert any(log["stage"] == "美客多链接" and log["status"] == "成功" for log in row["execution_logs"])
    assert not any(log["status"] == "失败" for log in row["execution_logs"])


def test_stopped_group_preserves_real_failure_success_and_identifies_pending_child(lifecycle_task):
    parent, completed_parent = lifecycle_task["records"]
    children = [
        {**parent, "_execution_parent": parent, "marketplace_item_id": item,
         "_zeshun_status": status, "execution_error": error}
        for item, status, error in (("MLM1", "成功", ""), ("MLM2", "失败", "remote rejected"),
                                    ("MLM3", "执行中", ""))
    ]
    children.append({**completed_parent, "_execution_parent": completed_parent, "_zeshun_status": "成功"})
    records._finish_same_title_records([parent, completed_parent], children, stopped=True)
    assert children[0]["_zeshun_status"] == "成功"
    assert children[1]["_zeshun_status"] == "失败"
    assert children[2]["_zeshun_status"] == "已终结"
    assert parent["_zeshun_status"] == "失败"
    assert parent["execution_error"] == "MLM2：remote rejected"
    assert "未完成 1 条" in parent["execution_logs"][-1]["message"]
    assert completed_parent["_zeshun_status"] == "成功"


def test_shutdown_wait_is_bounded_and_stops_after_current_server_product(lifecycle_task):
    task = lifecycle_task
    agent_task = {"execute_status": "queued", "agent_job_id": "agent-job"}
    records._tasks["agent"] = agent_task
    agent_before = deepcopy(agent_task)
    entered, release = threading.Event(), threading.Event()
    browser = Mock()

    def write(*args):
        entered.set()
        assert release.wait(2), "test must release the in-flight product write"

    browser.update_package_by_product_id.side_effect = write

    @contextmanager
    def factory(*args):
        yield browser

    records.start_execute("task", "user", action="zying", select_all_matching=True, erp_browser_factory=factory)
    worker = records._execution_threads["task"]
    try:
        assert entered.wait(2)
        assert records.shutdown_executions(timeout=0.01) == {"requested": 1, "pending": 1}
        assert task["execute_stop_requested"]
        assert task["execute_status"] == "stopping"
        assert agent_task == agent_before
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert records._execution_threads == {}
    browser.update_package_by_product_id.assert_called_once_with("1", "400", "30x20x10")
    assert task["records"][0]["_zying_status"] == "成功"
    assert task["records"][1]["_zying_status"] == "已终结"
    assert task["execute_status"] == "stopped"
    with pytest.raises(ValueError, match="服务正在关闭"):
        records.start_execute("task", "user", action="zying", selected_order_numbers=["2"])


def test_shutdown_does_not_cancel_or_prevent_agent_dispatch(lifecycle_task):
    assert records.shutdown_executions(timeout=0) == {"requested": 0, "pending": 0}
    dispatch = Mock(return_value="agent-job")
    result = records.start_execute("task", "user", action="zying", select_all_matching=True, agent_dispatch=dispatch)
    assert result["agent_job_id"] == "agent-job"
    dispatch.assert_called_once()
    assert records._execution_threads == {}
    assert not lifecycle_task["execute_stop_requested"]


def test_worker_start_failure_rolls_back_tracking_and_queued_state(lifecycle_task, monkeypatch):
    thread = Mock()
    thread.start.side_effect = RuntimeError("thread unavailable")
    monkeypatch.setattr(records.threading, "Thread", Mock(return_value=thread))
    with pytest.raises(RuntimeError, match="thread unavailable"):
        records.start_execute("task", "user", action="zying", select_all_matching=True)
    assert records._execution_threads == {}
    assert lifecycle_task["execute_status"] == "idle"


def test_guarded_failure_logs_traceback_and_finalizes_selected_rows(lifecycle_task, monkeypatch, caplog):
    monkeypatch.setattr(records, "_run_execute", Mock(side_effect=AttributeError("malformed response")))
    records._run_execute_guarded("task", "user", "zying", ["1"], None)
    assert lifecycle_task["records"][0]["_zying_status"] == "失败"
    assert "_zying_status" not in lifecycle_task["records"][1]
    assert lifecycle_task["execute_status"] == "completed"
    logged = next(event for event in caplog.records if "重量尺寸执行任务" in event.message)
    assert isinstance(logged.exc_info[1], AttributeError)
    assert "Traceback" in caplog.text
