from unittest.mock import patch

import pytest

from bit import weight_dimensions_records as records


def test_refresh_without_upload_reuses_running_task():
    with patch.object(records, "_tasks", {}), patch.object(records, "_latest_task_by_owner", {}), patch.object(records.threading, "Thread") as thread:
        first = records.start_changed_refresh("user", {"store_ids": [1]}, [], {1})
        second = records.start_changed_refresh("user", {"store_ids": [1]}, [], {1})
        assert first["task_id"] == second["task_id"]
        assert thread.call_count == 1
        records._tasks[first["task_id"]]["status"] = "ready"
        third = records.start_changed_refresh("user", {"store_ids": [1]}, [], {1})
        assert third["task_id"] != first["task_id"]


def test_refresh_does_not_reuse_other_filters_owner_or_permissions():
    with patch.object(records, "_tasks", {}), patch.object(records, "_latest_task_by_owner", {}), patch.object(records.threading, "Thread"):
        calls = [
            ("user", {"store_ids": [1]}, {1}),
            ("other", {"store_ids": [1]}, {1}),
            ("user", {"store_ids": [1], "region": "MX"}, {1}),
            ("user", {"store_ids": [1]}, {1, 2}),
        ]
        ids = {records.start_changed_refresh(owner, filters, [], allowed)["task_id"]
               for owner, filters, allowed in calls}
        assert len(ids) == len(calls)


@pytest.mark.parametrize("save_error", [None, RuntimeError("write failed")])
def test_changed_refresh_saves_successful_records_in_batches(save_error):
    rows = [{"order_number": str(i), "_token_id": 1} for i in range(101)]
    task = {"status": "preparing"}
    def read(row, *args):
        row["query_status"] = "已读取"
    with patch.object(records, "_tasks", {"test": task}), \
         patch.object(records.bit_db_api, "list_weight_dimensions_changed_orders", return_value=rows), \
         patch.object(records.bit_db_api, "list_weight_dimensions_records", return_value=[]) as saved, \
         patch.object(records.bit_mysql, "get_mercado_store_token", return_value={"id": 1}), \
         patch.object(records, "_client_and_token", return_value=(object(), {})), \
         patch.object(records, "_read_one_record", side_effect=read), \
         patch.object(records.bit_db_api, "save_weight_dimensions_records", side_effect=save_error) as save:
        records._run_changed_read("test", "user", {}, [], {1})
    assert saved.call_args.args[0] == [str(i) for i in range(101)]
    if save_error:
        assert task["status"] == "failed"
        assert "write failed" in task["message"]
        return
    assert task["status"] == "ready"
    assert task["processed"] == 101
    assert [len(call.args[0]) for call in save.call_args_list] == [100, 1]


def test_changed_refresh_reports_background_failure():
    task = {"status": "querying"}
    with patch.object(records, "_tasks", {"test": task}), \
         patch.object(records, "_read_changed_records", side_effect=RuntimeError("database unavailable")):
        records._run_changed_read("test", "user", {}, [], {1})
    assert task["status"] == "failed"
    assert "database unavailable" in task["message"]
    assert task["finished_at"]


@pytest.mark.parametrize("filters, expected", [({}, ("", "")), ({"date_from": "2026-01-01", "date_to": "2026-02-01"}, ("2026-01-01", "2026-02-01"))])
def test_changed_orders_dates_are_unrestricted_unless_filtered(filters, expected):
    with patch.object(records.bit_mysql, "list_orders", return_value={"rows": []}) as query:
        assert records.bit_mysql.list_weight_dimensions_changed_orders(filters) == []
    assert query.call_args.kwargs["freight_checked_from"] == expected[0]
    assert query.call_args.kwargs["freight_checked_to"] == expected[1]


def test_task_status_paginates_before_serializing_and_progress_skips_records():
    task = {"task_id": "large", "owner": "user", "status": "ready", "records": [
        {"order_number": str(i)} for i in range(10000)
    ]}
    with patch.object(records, "_tasks", {"large": task}), \
         patch.object(records, "_public_record", side_effect=lambda row: dict(row)) as public:
        data = records.task_status("large", "user", page=2, page_size=50)
        assert data["record_total"] == 10000
        assert data["page"] == 2
        assert len(data["records"]) == 50
        assert data["records"][0]["order_number"] == "50"
        assert public.call_count == 50
        public.reset_mock()
        data = records.task_status("large", "user", include_records=False)
        assert data["records"] == []
        public.assert_not_called()
        data = records.task_status("large", "user", page=9999, page_size=1000)
        assert data["page_size"] == 100
        assert data["page"] == 100
        assert len(data["records"]) == 100
        with pytest.raises(KeyError):
            records.task_status("large", "other", page=1)


def test_changed_refresh_defaults_to_recent_week_and_preserves_custom_dates():
    with patch.object(records, "_tasks", {}), patch.object(records, "_latest_task_by_owner", {}), patch.object(records.threading, "Thread"):
        result = records.start_changed_refresh("user", {}, [], {1})
        expected = (records.datetime.now() - records.timedelta(days=6)).strftime("%Y-%m-%d 00:00")
        assert records._tasks[result["task_id"]]["filters"]["date_from"] == expected
        result = records.start_changed_refresh("user", {"date_from": "2020-01-01"}, [], {1})
        assert records._tasks[result["task_id"]]["filters"]["date_from"] == "2020-01-01"


def test_empty_saved_record_lookup_does_not_open_database():
    with patch.object(records.bit_mysql.pymysql, "connect") as connect:
        assert records.bit_mysql.list_weight_dimensions_records([]) == []
        connect.assert_not_called()


def test_saved_record_lookup_filters_in_sql():
    with patch.object(records.bit_mysql.pymysql, "connect") as connect:
        cursor = connect.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{"order_number": "123", "record_json": "{}", "execution_logs_json": "[]"}]
        result = records.bit_mysql.list_weight_dimensions_records(["123", "123", "456"])
        sql, values = cursor.execute.call_args.args
        assert "WHERE `order_number` IN (%s,%s)" in sql
        assert values == ["123", "456"]
        assert result[0]["order_number"] == "123"


def test_saved_view_only_reads_database_and_scopes_permissions():
    row = {"order_number": "1", "_token_id": 1, "_global_item_id": "CBT1"}
    with patch.object(records, "_tasks", {}), patch.object(records, "_latest_task_by_owner", {}), \
         patch.object(records.bit_db_api, "list_weight_dimensions_records", return_value=[row]) as saved, \
         patch.object(records, "_read_one_record") as marketplace, \
         patch.object(records.threading, "Thread") as thread:
        result = records.load_saved_changes("user", {"store_ids": [1, 2]}, {1})
        assert result["status"] == "ready"
        assert saved.call_args.kwargs["filters"]["store_ids"] == [1]
        assert records._tasks[result["task_id"]]["records"] == [row]
        marketplace.assert_not_called()
        thread.assert_not_called()


def test_daily_save_updates_snapshot_and_preserves_execution_state():
    import json
    with patch.object(records.bit_mysql.pymysql, "connect") as connect:
        cursor = connect.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"record_json": json.dumps({"actual_weight_g": "1", "_zeshun_status": "成功"})}
        records.bit_mysql.save_weight_dimensions_records([
            {"order_number": "1", "actual_weight_g": "2", "_token_id": 1,
             "_global_item_id": "CBT1", "_zeshun_status": "旧状态", "execution_logs": []}
        ], refresh=True)
        sql, params = cursor.execute.call_args.args
        assert sql.startswith("UPDATE")
        assert "execution_logs_json" not in sql
        data = json.loads(params[1])
        assert data["actual_weight_g"] == "2"
        assert data["_zeshun_status"] == "成功"
        assert data["_global_item_id"] == "CBT1"
        assert data["_token_id"] == 1
        connect.return_value.commit.assert_called_once()


def test_saved_filters_are_parameterized_and_reject_invalid_ranges():
    sql, params = records.bit_mysql._weight_dimensions_saved_filters({
        "store_ids": [], "source": "test'", "date_to": "2026-09-27T12:34",
        "salespeople": ["__unassigned__"], "freight_min": "-10", "freight_max": "20",
    })
    assert "1 = 0" in sql
    assert "test'" not in " ".join(sql)
    assert "test'" in params
    assert "2026-09-27 12:34:59" in params
    for filters in ({"freight_min": "nan"}, {"freight_min": "2", "freight_max": "1"}):
        with pytest.raises(ValueError):
            records.bit_mysql._weight_dimensions_saved_filters(filters)


def test_db_api_forwards_saved_filters_and_refresh_mode():
    import json
    api = records.bit_db_api
    with patch.object(api, "DB_MODE", "api"), patch.object(api, "_request", return_value=[]) as request:
        api.list_weight_dimensions_records(filters={"store_ids": [1]})
        assert json.loads(request.call_args.kwargs["params"]["filters"]) == {"store_ids": [1]}
        api.save_weight_dimensions_records([{"order_number": "1"}], refresh=True)
        assert request.call_args.kwargs["json"]["refresh"] is True


def test_failed_enrichment_is_persisted_for_database_readers():
    task = {"status": "preparing"}
    with patch.object(records, "_tasks", {"test": task}), \
         patch.object(records.bit_db_api, "list_weight_dimensions_changed_orders", return_value=[{"order_number": "1"}]), \
         patch.object(records.bit_db_api, "list_weight_dimensions_records", return_value=[]), \
         patch.object(records.bit_db_api, "save_weight_dimensions_records") as save:
        records._run_changed_read("test", "user", {}, [], set())
        assert task["status"] == "ready"
        assert save.call_args.args[0][0]["query_status"] == "查询失败"
        assert save.call_args.kwargs["refresh"] is True
