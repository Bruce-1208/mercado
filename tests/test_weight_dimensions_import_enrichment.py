import json
from unittest.mock import patch

from bit import weight_dimensions_records as records


def test_enrichment_preserves_measurements_identifiers_and_execution():
    original = {"time": "2026-01-01", "product_id": "", "title": "official",
                "actual_weight_g": "123", "_token_id": 7, "_global_item_id": "CBT1",
                "execution_status": "成功", "query_error": "缺少产品id，请先通过订单导入补全"}
    with patch.object(records.bit_mysql.pymysql, "connect") as connect:
        cursor = connect.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"record_json": json.dumps(original)}
        result = records.bit_mysql.save_weight_dimensions_records([{
            "order_number": "1", "product_id": "P123", "title": "imported", "time": "",
            "actual_weight_g": "999", "_token_id": 8, "_global_item_id": "CBT2",
            "execution_status": "", "execution_logs": [], "salesperson": "Alice",
        }], enrich_only=True)
        sql, params = cursor.execute.call_args.args
        assert sql.startswith("UPDATE")
        assert "execution_logs_json" not in sql
        saved = json.loads(params[1])
        assert saved == {**original, "product_id": "P123", "salesperson": "Alice", "query_error": ""}
        assert result["enriched"] == 1
        assert result["inserted"] == 0


def test_enrichment_does_not_insert_missing_record_or_clear_existing_fields():
    for original in (None, {"record_json": '{"product_id":"P123"}'}):
        with patch.object(records.bit_mysql.pymysql, "connect") as connect:
            cursor = connect.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = original
            result = records.bit_mysql.save_weight_dimensions_records([
                {"order_number": "1", "product_id": ""}], enrich_only=True)
            assert result["enriched"] == 0
            assert not any(call.args[0].startswith(("INSERT", "UPDATE")) for call in cursor.execute.call_args_list)


def test_upload_enriches_by_order_number_without_store_or_marketplace_lookup():
    task = {"status": "preparing"}
    with patch.object(records, "_tasks", {"test": task}), \
         patch.object(records, "read_order_file", return_value=[{"order_number": "1", "product_id": "P1"}, {"order_number": "2", "product_id": "P2"}]), \
         patch.object(records.bit_db_api, "get_weight_dimensions_record_order_numbers", return_value=["1", "2"]), \
         patch.object(records.bit_db_api, "list_weight_dimensions_records", return_value=[{"order_number": "1"}]) as fetch, \
         patch.object(records.bit_db_api, "save_weight_dimensions_records", return_value={"enriched": 2}) as save, \
         patch.object(records, "_client_and_token") as client:
        records._run_read("test", b"file", "orders.xlsx", [], set())
        assert set(fetch.call_args.args[0]) == {"1", "2"}
        save.assert_called_once_with([{"order_number": "1", "product_id": "P1"}, {"order_number": "2", "product_id": "P2"}], enrich_only=True)
        client.assert_not_called()
        assert task["status"] == "ready"
        assert "补充 2 条" in task["message"]


def test_unmatched_upload_never_queries_store_or_marketplace():
    task = {"status": "preparing"}
    uploaded = [{"order_number": str(i), "company_store": "unknown"} for i in range(63)]
    with patch.object(records, "_tasks", {"test": task}), \
         patch.object(records, "read_order_file", return_value=uploaded), \
         patch.object(records.bit_db_api, "get_weight_dimensions_record_order_numbers", return_value=[]), \
         patch.object(records.bit_db_api, "save_weight_dimensions_records") as save, \
         patch.object(records, "_resolve_store") as resolve, \
         patch.object(records, "_read_one_record") as read, \
         patch.object(records.bit_mysql, "get_mercado_store_token") as token:
        records._run_read("test", b"file", "orders.xlsx", [], set())
        save.assert_not_called()
        resolve.assert_not_called()
        read.assert_not_called()
        token.assert_not_called()
        assert task["status"] == "ready"
        assert task["upload_unmatched"] == 63
        assert "未匹配单号 63 条" in task["message"]
        assert "查询失败" not in task["message"]
        assert all(row["query_status"] == "未匹配" for row in task["records"])


def test_duplicate_lines_merge_metadata_and_count_unique_orders():
    task = {"status": "preparing"}
    with patch.object(records, "_tasks", {"test": task}), \
         patch.object(records, "read_order_file", return_value=[
             {"order_number": "1", "product_id": "P1", "salesperson": ""},
             {"order_number": "1", "product_id": "P2", "salesperson": "Alice"}]), \
         patch.object(records.bit_db_api, "get_weight_dimensions_record_order_numbers", return_value=["1"]), \
         patch.object(records.bit_db_api, "list_weight_dimensions_records", return_value=[{"order_number": "1", "product_id": "P1"}]), \
         patch.object(records.bit_db_api, "save_weight_dimensions_records", return_value={"enriched": 1}) as save:
        records._run_read("test", b"file", "orders.xlsx", [], set())
        save.assert_called_once_with([{"order_number": "1", "product_id": "P1", "salesperson": "Alice"}], enrich_only=True)
        assert task["upload_skipped"] == 1
        assert task["total"] == 1
        assert task["records"][0]["product_id"] == "P1"


def test_refresh_preserves_imported_metadata_even_with_stale_snapshot():
    original = {"product_id": "P123", "salesperson": "Alice", "actual_weight_g": "123",
                "execution_status": "成功"}
    with patch.object(records.bit_mysql.pymysql, "connect") as connect:
        cursor = connect.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"record_json": json.dumps(original)}
        records.bit_mysql.save_weight_dimensions_records([{
            "order_number": "1", "product_id": "", "salesperson": "  ",
            "actual_weight_g": "456", "execution_status": "",
            "query_error": "缺少产品id，请先通过订单导入补全",
        }], refresh=True)
        sql, params = cursor.execute.call_args.args
        saved = json.loads(params[1])
        assert saved["product_id"] == "P123"
        assert saved["salesperson"] == "Alice"
        assert saved["actual_weight_g"] == "456"
        assert saved["execution_status"] == "成功"
        assert saved["query_error"] == ""
        assert "execution_logs_json" not in sql


def test_changed_refresh_restores_excel_metadata_before_official_lookup():
    task = {"status": "preparing"}
    def read(row, *args):
        assert row["product_id"] == "P123"
        assert row["salesperson"] == "Alice"
        row["query_status"] = "已读取"
    with patch.object(records, "_tasks", {"test": task}), \
         patch.object(records.bit_db_api, "list_weight_dimensions_changed_orders", return_value=[{
             "order_number": "1", "_token_id": 1, "product_id": "", "salesperson": ""}]), \
         patch.object(records.bit_db_api, "list_weight_dimensions_records", return_value=[{
             "order_number": "1", "product_id": "P123", "salesperson": "Alice"}]), \
         patch.object(records.bit_mysql, "get_mercado_store_token", return_value={"id": 1}), \
         patch.object(records, "_client_and_token", return_value=(object(), {})), \
         patch.object(records, "_read_one_record", side_effect=read), \
         patch.object(records.bit_db_api, "save_weight_dimensions_records") as save:
        records._run_changed_read("test", "user", {}, [], {1})
    assert task["status"] == "ready"
    assert task["records"][0]["product_id"] == "P123"
    assert save.call_args.args[0][0]["product_id"] == "P123"
