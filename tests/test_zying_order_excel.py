import io
import threading

import pytest
from openpyxl import Workbook

from bit.zying_order_excel import read_excel
from bit.zying_order_sync import AgentSync, SyncManager


def workbook(rows, extra=None):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    if extra:
        sheet = book.create_sheet("其他订单")
        for row in extra:
            sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


HEADERS = ["平台订单号", "采购成本", "找货订单号", "物流号"]


def test_excel_sums_distinct_purchase_costs():
    records = read_excel(workbook([
        ["智赢订单导出"], HEADERS,
        ["20000000000000001", "￥18.505", "00123", "0000456"],
        ["20000000000000001", 18.51, "P2", "0000456"],
    ], [HEADERS, ["20000000000000001", 3, "P3", "T2"]]), "订单.xlsx")
    assert len(records) == 1
    assert records[0]["changes"] == {"purchase_cost": "40.02", "purchase_order": "00123,P2,P3", "purchase_tracking": "0000456,T2"}
    assert records[0]["error"] == ""
    assert records[0]["locations"] == ["Sheet 第 3 行", "Sheet 第 4 行", "其他订单 第 2 行"]


def test_excel_replaces_empty_fields_and_accepts_zero():
    records = read_excel(workbook([HEADERS, [123, 0, None, None], [124, None, None, None]]), "订单.XLSX")
    assert records[0]["order_id"] == "123"
    assert records[0]["changes"] == {"purchase_order": "", "purchase_tracking": "", "purchase_cost": "0.00"}
    assert records[1]["changes"] == records[0]["changes"]


@pytest.mark.parametrize("value", [-1, "NaN", "Infinity", "***", "=SUM(A1:A2)", "12,34"])
def test_invalid_cost_rejects_entire_order(value):
    record = read_excel(workbook([HEADERS, ["123", value, "P1", "T1"]]), "订单.xlsx")[0]
    assert "成本无效" in record["error"]


def test_excel_conflicting_cost_and_long_numeric_identifier_fail():
    records = read_excel(workbook([HEADERS,
        ["123", 5, "P1", "T1"], ["123", 6, "P1", "T2"],
        [20000000000000001, 3, "P3", "T3"],
        ["124", 3, "P4", 20000000000000002],
        [None, 3, "P5", "T5"],
    ]), "订单.xlsx")
    assert "不同成本" in records[0]["error"]
    assert "丢失精度" in records[1]["error"]
    assert "丢失精度" in records[2]["error"]
    assert "缺少平台订单号" in records[3]["error"]


def test_invalid_file_or_missing_headers():
    with pytest.raises(ValueError, match=".xls"):
        read_excel(b"test", "订单.xls")
    with pytest.raises(ValueError, match="无法读取"):
        read_excel(b"test", "订单.xlsx")
    with pytest.raises(ValueError, match="未识别"):
        read_excel(workbook([["打包单号", "成本"], [123, 4]]), "订单.xlsx")


def test_currency_header_suffix_and_thousands_amount():
    record = read_excel(workbook([["平台订单号", "采购成本（元）", "采购单号"], ["123", "CNY 1,234.50", "P1"]]), "订单.xlsx")[0]
    assert record["changes"] == {"purchase_order": "P1", "purchase_tracking": "", "purchase_cost": "1234.50"}


def test_upload_file_and_row_limits(monkeypatch):
    from bit import zying_order_excel
    with pytest.raises(ValueError, match="10 MB"):
        read_excel(b"x" * (zying_order_excel.MAX_FILE_BYTES + 1), "订单.xlsx")
    monkeypatch.setattr(zying_order_excel, "MAX_ROWS", 1)
    with pytest.raises(ValueError, match="拆分文件"):
        read_excel(workbook([HEADERS, ["123", 2, None, None], ["124", 3, None, None]]), "订单.xlsx")


def join_import():
    for thread in threading.enumerate():
        if thread.name == "zying-excel-sync":
            thread.join(3)
            assert not thread.is_alive()


def test_background_sync_only_matches_child_ids_and_keeps_partial_results():
    manager = SyncManager()
    records = read_excel(workbook([HEADERS,
        ["123", 5, "P1", "T1"], ["PACK", 99, "P2", "T2"],
        ["124", None, None, None], ["125", -1, "P3", "T3"],
        ["126", 10, "P4", "T4"],
    ]), "订单.xlsx")
    calls = []
    def writer(order_id, changes):
        calls.append((order_id, changes))
        if order_id == "126":
            raise ValueError("店铺权限已变更")
        return {"matched": 1}
    manager.start_excel(7, {}, records,
        lambda **kwargs: {"rows": [{"id": "123", "order_number": "PACK", "pack_id": "PACK"},
                                  {"id": "124"}, {"id": "125"}, {"id": "126"}], "total": 4}, writer)
    join_import()
    state = manager.status(7)
    assert not state["running"]
    assert (state["total"], state["completed"], state["updated"], state["skipped"], state["failed"]) == (5, 5, 2, 1, 2)
    assert [call[0] for call in calls] == ["123", "124", "126"]
    assert manager.status(8)["phase"] == "idle"


def test_stopping_import_during_snapshot_prevents_writes_and_releases_slot():
    manager = SyncManager()
    entered, release = threading.Event(), threading.Event()
    def reader(**params):
        entered.set()
        assert release.wait(3)
        return {"rows": [{"id": "123"}], "total": 1}
    writes = []
    records = read_excel(workbook([HEADERS, ["123", 2, None, None]]), "订单.xlsx")
    manager.start_excel(7, {}, records, reader, lambda *args: writes.append(args))
    assert entered.wait(1)
    with pytest.raises(ValueError, match="正在运行"):
        manager.start_excel(8, {}, records, reader, None)
    assert manager.stop(7)["running"] is False
    release.set()
    join_import()
    assert writes == []
    assert manager.running is False


@pytest.mark.usefixtures("isolated_legacy_console_user", "subscribed_workbench_account")
def test_upload_route_scopes_read_and_write_and_reuses_operator(monkeypatch):
    from bit import bit_interface as app_module, zying_order_sync
    manager = SyncManager()
    monkeypatch.setattr(zying_order_sync, "manager", manager)
    monkeypatch.setattr(zying_order_sync, "agent_sync", AgentSync())
    class Store:
        def reap_expired_jobs(self):
            pass
        def list_jobs(self, **kwargs):
            return []
    monkeypatch.setattr(app_module, "get_local_agent_store", lambda: Store())
    monkeypatch.setattr(app_module, "_authorized_token_ids_for_user", lambda: {7})
    monkeypatch.setattr(app_module, "workbench_user_own_store_only", lambda *args: True)
    monkeypatch.setattr(app_module, "workbench_user_salesperson", lambda: "本人")
    reads, writes = [], []
    def reader(**params):
        reads.append(params)
        return {"rows": [{"id": "123"}], "total": 1}
    def writer(ids, **params):
        writes.append((ids, params))
        return {"matched": 1}
    monkeypatch.setattr(app_module, "db_list_orders", reader)
    monkeypatch.setattr(app_module.bit_db_api, "bulk_update_orders", writer)
    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = {"id": 711, "username": "测试", "role_key": "employee"}
    result = client.post("/api/orders/zying-sync/upload?store_id=7&store_id=99&salesperson=其他&status=待采", data={
        "file": (io.BytesIO(workbook([HEADERS, ["123", 2, "P1", "T1"], ["999", 4, "P2", "T2"]])), "智赢.xlsx")})
    assert result.status_code == 202
    join_import()
    assert reads[0]["store_ids"] == [7]
    assert reads[0]["allowed_store_ids"] == [7]
    assert reads[0]["salesperson"] == "本人"
    assert reads[0]["status"] == "待采"
    assert writes == [(["123"], {"operator_id": 711, "operator_name": "测试", "allowed_token_ids": [7],
        "purchase_order": "P1", "purchase_tracking": "T1", "purchase_cost": "2.00"})]
    assert manager.status(711)["skipped"] == 1
    assert client.get("/api/orders/zying-sync/status").status_code == 200
    assert client.post("/api/orders/zying-sync/stop").status_code == 200
    result = client.post("/api/orders/zying-sync/upload?store_id=99", data={
        "file": (io.BytesIO(workbook([HEADERS, ["123", 8, "P3", "T3"]])), "智赢.xlsx")})
    assert result.status_code == 400
    assert len(writes) == 1
    assert client.post("/api/orders/zying-sync/upload").status_code == 400


@pytest.mark.usefixtures("isolated_legacy_console_user")
def test_upload_requires_execution_permission(monkeypatch):
    from bit import bit_interface as app_module
    monkeypatch.setattr(app_module, "get_current_workbench_user", lambda: {
        "id": 711, "role_key": "employee", "permissions": ["order_print.view"], "access_version": 1,
    })
    client = app_module.app.test_client()
    response = client.post("/api/orders/zying-sync/upload", data={
        "file": (io.BytesIO(workbook([HEADERS, ["123", 2, "P1", "T1"]])), "订单.xlsx")})
    assert response.status_code == 403


def test_snapshot_sums_numbered_purchases_and_replaces_prior_snapshot():
    from bit.zying_order_excel import purchase_snapshot
    local = {"purchase_order": "OLD", "purchase_tracking": "OLD", "purchase_cost": "999.00"}
    buys = [{"no": "P1", "trace": "T1", "amount": "12.30", "cur": "CNY"},
            {"no": "P2", "trace": "T2", "amount": "7.20", "cur": "RMB"},
            {"no": "P1", "trace": "T3", "amount": "12.30"},
            {"no": "", "trace": "BAD", "amount": "999"}]
    for _ in range(2):
        local.update(purchase_snapshot(buys))
        assert local == {"purchase_order": "P1,P2", "purchase_tracking": "T1,T2,T3", "purchase_cost": "19.50"}
    local.update(purchase_snapshot([{"no": "P2", "amount": "5"}]))
    assert local == {"purchase_order": "P2", "purchase_tracking": "", "purchase_cost": "5.00"}
    local.update(purchase_snapshot([]))
    assert local == {"purchase_order": "", "purchase_tracking": "", "purchase_cost": "0.00"}


@pytest.mark.parametrize("buy", [
    {"no": "P1", "amount": "***"}, {"no": "P1"},
    {"no": "P1", "amount": "2", "cur": "USD"},
])
def test_snapshot_rejects_unreadable_or_foreign_purchase_amount(buy):
    from bit.zying_order_excel import purchase_snapshot
    with pytest.raises(ValueError):
        purchase_snapshot([buy])


def test_excel_duplicate_purchase_is_counted_once_and_unpurchased_amount_is_ignored():
    records = read_excel(workbook([HEADERS,
        ["123", 5, "P1", "T1"], ["123", 5, "P1", "T2"],
        ["123", 6, "P2", "T3"], ["123", "bad", None, "BAD"],
        ["124", 999, None, "BAD"],
    ]), "订单.xlsx")
    assert not any(row["error"] for row in records)
    assert records[0]["changes"] == {"purchase_order": "P1,P2", "purchase_tracking": "T1,T2,T3", "purchase_cost": "11.00"}
    assert records[1]["changes"] == {"purchase_order": "", "purchase_tracking": "", "purchase_cost": "0.00"}


def test_excel_requires_purchase_numbers_to_prove_cost_source():
    record = read_excel(workbook([["平台订单号", "成本"], ["123", 999]]), "订单.xlsx")[0]
    assert "请同时提供" in record["error"]
