import json
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from bit import bit_mysql


def _saved(order_number, record, logs=None):
    return {
        "order_number": order_number,
        "record_json": json.dumps(record, ensure_ascii=False),
        "execution_logs_json": json.dumps(logs or [], ensure_ascii=False),
    }


def _database(rows):
    """An offline row store; no connection to the configured database."""
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = lambda: deepcopy(rows)

    def execute(sql, params=()):
        if sql.startswith("UPDATE `zying_weight_dimensions_records`"):
            saved = next(row for row in rows if row["order_number"] == params[2])
            saved.update(record_json=params[0], execution_logs_json=params[1])

    cursor.execute.side_effect = execute
    return connection, cursor


@pytest.mark.parametrize("status", ["执行中", "重量尺寸已更新", "重量已更新"])
def test_startup_marks_unconfirmed_server_execution_interrupted_once(status):
    history = [
        {"stage": "美客多链接", "status": "失败", "message": "previous failure",
         "execution_id": "old-batch", "action": "zeshun"},
        {"stage": "美客多链接", "status": "成功", "message": "dimension submitted",
         "execution_id": "current-batch", "action": "zeshun"},
    ]
    rows = [_saved("1", {
        "_zeshun_status": status, "_zying_status": "成功",
        "execution_status": "执行中", "execution_error": "original error",
        "actual_weight_g": "600", "current_net_proceeds_usd": "5.50",
    }, history)]
    connection, cursor = _database(rows)
    with patch.object(bit_mysql.pymysql, "connect", return_value=connection), \
         patch.object(bit_mysql, "_ensure_weight_dimensions_record_table"):
        result = bit_mysql.recover_weight_dimensions_record_executions()
        assert result == {"interrupted": 1, "normalized_failures": 0, "skipped_invalid": 0}
        first = deepcopy(rows)
        assert bit_mysql.recover_weight_dimensions_record_executions() == {
            "interrupted": 0, "normalized_failures": 0, "skipped_invalid": 0,
        }
    assert rows == first, "recovery must not duplicate its log on the next startup"
    record = json.loads(rows[0]["record_json"])
    assert record["_zeshun_status"] == "已中断"
    assert record["execution_status"] == "已中断"
    assert record["_zying_status"] == "成功"
    assert record["actual_weight_g"] == "600"
    assert record["current_net_proceeds_usd"] == "5.50"
    assert record["execution_error"].startswith("original error；服务中断")
    assert "先回读" in record["execution_error"]
    logs = json.loads(rows[0]["execution_logs_json"])
    assert logs[:-1] == history
    assert logs[-1]["stage"] == "任务恢复"
    assert logs[-1]["status"] == "已中断"
    assert logs[-1]["execution_id"] == "current-batch"
    assert logs[-1]["action"] == "zeshun"
    sql, params = cursor.execute.call_args_list[0].args
    assert sql.endswith("FOR UPDATE")
    assert "JSON_VALID" in sql
    assert "COALESCE" in sql
    assert params == ("执行中", "重量尺寸已更新", "重量已更新", "失败", "执行中",
                      "排队中", "执行中", "进行中")
    assert connection.commit.call_count == 2
    connection.rollback.assert_not_called()
    assert connection.close.call_count == 2


def test_startup_normalizes_known_failure_without_overwriting_reason():
    reason = "美客多链接重量尺寸更新失败：item is closed"
    history = [{"stage": "美客多链接", "status": "失败", "message": reason}]
    rows = [_saved("1", {
        "_zeshun_status": "失败", "execution_status": "执行中",
        "execution_error": reason, "_zying_status": "失败",
    }, history)]
    connection, _ = _database(rows)
    with patch.object(bit_mysql.pymysql, "connect", return_value=connection), \
         patch.object(bit_mysql, "_ensure_weight_dimensions_record_table"):
        assert bit_mysql.recover_weight_dimensions_record_executions() == {
            "interrupted": 0, "normalized_failures": 1, "skipped_invalid": 0,
        }
        first = deepcopy(rows)
        bit_mysql.recover_weight_dimensions_record_executions()
    assert rows == first
    record = json.loads(rows[0]["record_json"])
    assert record["execution_status"] == "失败"
    assert record["_zeshun_status"] == "失败"
    assert record["_zying_status"] == "失败"
    assert record["execution_error"] == reason
    logs = json.loads(rows[0]["execution_logs_json"])
    assert logs[:-1] == history
    assert logs[-1]["status"] == "失败"


def test_startup_preserves_terminal_rows_and_active_agent_state():
    rows = [
        _saved("success", {"_zeshun_status": "成功", "execution_status": "完成"}),
        _saved("failure", {"_zeshun_status": "失败", "execution_status": "部分完成"}),
        _saved("unstarted", {"execution_status": "未执行"}),
        _saved("queued-agent", {"_zeshun_status": "执行中", "_zying_status": "排队中",
                                "execution_status": "排队中"}),
        _saved("active-agent", {"_zeshun_status": "执行中", "_zying_status": "执行中",
                                "execution_status": "执行中"}),
        _saved("old-active-agent", {"_zeshun_status": "执行中", "_zying_status": "进行中",
                                    "execution_status": "进行中"}),
    ]
    before = deepcopy(rows)
    connection, cursor = _database(rows)
    with patch.object(bit_mysql.pymysql, "connect", return_value=connection), \
         patch.object(bit_mysql, "_ensure_weight_dimensions_record_table"):
        result = bit_mysql.recover_weight_dimensions_record_executions()
    assert result == {"interrupted": 0, "normalized_failures": 0, "skipped_invalid": 0}
    assert rows == before
    assert cursor.execute.call_count == 1


@pytest.mark.parametrize("raw_logs", ["not JSON", "{}"])
def test_startup_keeps_invalid_audit_data_untouched(raw_logs):
    rows = [_saved("1", {"_zeshun_status": "执行中", "execution_status": "执行中"})]
    rows[0]["execution_logs_json"] = raw_logs
    before = deepcopy(rows)
    connection, cursor = _database(rows)
    with patch.object(bit_mysql.pymysql, "connect", return_value=connection), \
         patch.object(bit_mysql, "_ensure_weight_dimensions_record_table"):
        assert bit_mysql.recover_weight_dimensions_record_executions() == {
            "interrupted": 0, "normalized_failures": 0, "skipped_invalid": 1,
        }
    assert rows == before
    assert cursor.execute.call_count == 1


def test_startup_rolls_back_when_persisting_recovery_fails():
    rows = [_saved("1", {"_zeshun_status": "执行中", "execution_status": "执行中"})]
    connection, cursor = _database(rows)

    def execute(sql, params=()):
        if sql.startswith("UPDATE"):
            raise RuntimeError("database unavailable")

    cursor.execute.side_effect = execute
    with patch.object(bit_mysql.pymysql, "connect", return_value=connection), \
         patch.object(bit_mysql, "_ensure_weight_dimensions_record_table"), \
         pytest.raises(RuntimeError, match="database unavailable"):
        bit_mysql.recover_weight_dimensions_record_executions()
    connection.commit.assert_not_called()
    connection.rollback.assert_called_once()
    connection.close.assert_called_once()
