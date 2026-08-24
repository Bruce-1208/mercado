import os
from pathlib import Path

from openpyxl import Workbook

from bit import bit_mysql, bit_update_orders


def _write_orders(path, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["id", "编号", "时间", "状态", "标题", "买家名称"])
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def test_folder_import_keeps_last_order_from_latest_file(tmp_path):
    old_file = tmp_path / "订单-旧.xlsx"
    new_file = tmp_path / "订单-新.xlsx"
    _write_orders(
        old_file,
        [
            ["order-1", "sale-1", "2026/08/01 10:00:00", "待处理", "旧标题", "旧买家"],
            [None, None, None, None, None, None],
        ],
    )
    _write_orders(
        new_file,
        [
            ["order-1", "sale-1", "2026/08/01 10:00:00", "已完成", "新标题", "新买家"],
            ["order-2", "sale-2", "2026/08/02 11:00:00", "待处理", "订单二", "买家二"],
        ],
    )
    os.utime(old_file, (100, 100))
    os.utime(new_file, (200, 200))
    inserted = []

    result = bit_update_orders.update_order_mysql(
        tmp_path,
        insert_func=lambda rows: inserted.extend(rows),
    )

    assert result["files_processed"] == 2
    assert result["raw_rows"] == 3
    assert result["duplicate_rows"] == 1
    assert result["unique_orders"] == 2
    assert result["imported_orders"] == 2
    assert [row[0] for row in inserted] == ["order-1", "order-2"]
    assert inserted[0][5] == "已完成"
    assert inserted[0][16] == "新标题"
    assert inserted[0][22] == "新买家"


def test_order_insert_is_upsert_with_database_unique_constraint():
    import inspect

    insert_source = inspect.getsource(bit_mysql.insert_orders)
    migration_source = inspect.getsource(bit_mysql._ensure_orders_unique_id)

    assert "ON DUPLICATE KEY UPDATE" in insert_source
    assert "ADD UNIQUE KEY `uniq_orders_id` (`id`)" in migration_source
    assert "ROW_NUMBER() OVER" in migration_source


def test_discovery_recurses_and_ignores_temporary_workbooks(tmp_path):
    nested = tmp_path / "子目录"
    nested.mkdir()
    _write_orders(nested / "订单.xlsx", [["1", "sale", None, "状态", "标题", "买家"]])
    _write_orders(tmp_path / "~$订单.xlsx", [["2", "sale", None, "状态", "标题", "买家"]])
    (tmp_path / "说明.txt").write_text("not excel", encoding="utf-8")

    files = bit_update_orders.discover_order_excel_files(tmp_path)

    assert files == [nested / "订单.xlsx"]
