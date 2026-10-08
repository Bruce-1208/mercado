from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, MagicMock

from bit import weight_dimensions_audit as mirror
from erp import store_link_audit as audit


def source_record():
    return {"order_number": "1001", "time": "2026-10-07 09:00:00", "salesperson": "Alice",
            "company_store": "Shop", "declared_weight_g": "100", "actual_weight_g": "200",
            "execution_logs": [{"time": "2026-10-07T10:00:00", "stage": "美客多链接", "status": "成功",
                                "operator": "Bob", "salesperson": "Alice", "marketplace_item_id": "MLM123",
                                "submitted_weight_g": "200", "submitted_dimensions_cm": "2x3x4"}]}


def test_existing_records_and_logs_mirror_once_with_original_times_and_identity():
    source = source_record()
    original = deepcopy(source)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(mirror.mirror_record, [source] * 6))
    rows = audit.history(operation_type="weight_dimensions")
    assert len(rows) == 2
    assert source == original
    assert rows[0]["action"] == "weight_dimensions_execution"
    assert rows[0]["created_at"] == "2026-10-07T02:00:00+00:00"
    assert rows[0]["details"]["context"]["actor"]["display_name"] == "Bob"
    assert rows[0]["details"]["entry"]["submitted_dimensions_cm"] == "2x3x4"
    assert len(audit.history(operation_type="weight_dimensions", salesperson="Alice")) == 2
    assert len(audit.history(operation_type="weight_dimensions", salesperson="Bob")) == 1
    assert audit.history(operation_type="rights_holder") == []


def test_later_execution_appends_without_repeating_previous_history():
    source = source_record()
    mirror.mirror_record(source)
    source["execution_logs"].append({"time": "2026-10-08T10:00:00", "stage": "净收益", "status": "失败", "message": "remote error"})
    mirror.mirror_record(source)
    mirror.mirror_record(source)
    rows = audit.history(operation_type="weight_dimensions")
    assert len(rows) == 3 and rows[0]["phase"] == "failed"
    source["actual_weight_g"] = "300"
    mirror.mirror_record(source)
    assert len(audit.history(operation_type="weight_dimensions")) == 4


def test_backfilled_old_events_sort_and_page_by_original_time():
    audit.record_once("new", "delete_store_links", "completed", {}, created_at="2026-10-08T10:00:00Z")
    audit.record_once("old", "weight_dimensions_record", "completed", {}, created_at="2026-10-01T10:00:00Z")
    audit.record_once("middle", "process_rights_holder_product", "completed", {}, created_at="2026-10-04T10:00:00Z")
    rows = audit.history()
    assert [row["action"] for row in rows] == ["delete_store_links", "process_rights_holder_product", "weight_dimensions_record"]
    assert [row["id"] for row in audit.history(before_id=rows[0]["id"])] == [row["id"] for row in rows[1:]]
    assert audit.history(before_id=rows[1]["id"])[0]["id"] == rows[2]["id"]
    assert len(audit.history(created_after="2026-10-03T00:00:00Z", created_before="2026-10-05T00:00:00Z")) == 1


def test_backfill_retries_failure_and_survives_process_restart(monkeypatch):
    from bit import bit_db_api
    fetch = Mock(side_effect=[RuntimeError("db down"), [source_record()], [source_record()]])
    monkeypatch.setattr(bit_db_api, "list_weight_dimensions_records", fetch)
    monkeypatch.setattr(mirror, "_backfilled", False)
    import pytest
    with pytest.raises(RuntimeError):
        mirror.ensure_history_backfilled()
    mirror.ensure_history_backfilled()
    mirror.ensure_history_backfilled()
    assert fetch.call_count == 2
    monkeypatch.setattr(mirror, "_backfilled", False)
    mirror.ensure_history_backfilled()
    assert len(audit.history(operation_type="weight_dimensions")) == 2


def test_db_log_append_mirrors_the_committed_source(monkeypatch):
    import json
    from bit import bit_mysql
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    source = source_record()
    cursor.fetchone.return_value = {"record_json": json.dumps(source), "execution_logs_json": "[]"}
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_: connection)
    monkeypatch.setattr(bit_mysql, "_ensure_weight_dimensions_record_table", lambda _: None)
    bit_mysql.append_weight_dimensions_record_log("1001", source["execution_logs"][0], {"execution_status": "成功"})
    connection.commit.assert_called_once()
    rows = audit.history(operation_type="weight_dimensions")
    assert len(rows) == 2
    assert rows[0]["details"]["order_number"] == "1001"
