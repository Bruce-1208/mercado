from decimal import Decimal
import json
from pathlib import Path

import pytest
from PIL import Image

from bit import bit_zying_desktop_export_mysql as zying_export


def _row(**values):
    return tuple(values.get(header) for header in zying_export.EXPECTED_HEADERS)


def test_build_record_preserves_variants_and_types_numeric_values():
    common = {
        "id": 801623245,
        "key": "color",
        "theme": "Blue",
        "分销价": "1,234.50",
        "库存": 7,
        "标题": "Variant product",
    }
    first = zying_export.build_record(
        zying_export.EXPECTED_HEADERS,
        _row(**common, child="blue", SKU="SKU-BLUE"),
        category="一马当先",
        source_batch="batch-1",
        source_file="第01页.xlsx",
        source_page=1,
        source_row=2,
    )
    second = zying_export.build_record(
        zying_export.EXPECTED_HEADERS,
        _row(**common, child="red", SKU="SKU-RED"),
        category="一马当先",
        source_batch="batch-1",
        source_file="第01页.xlsx",
        source_page=1,
        source_row=3,
    )

    assert first["product_id"] == "801623245"
    assert first["distribution_price"] == Decimal("1234.50")
    assert first["inventory"] == Decimal("7")
    assert first["record_key"] != second["record_key"]
    assert first["content_hash"] != second["content_hash"]


def test_same_variant_has_stable_key_across_files_and_batches():
    values = _row(id="100", key="size", theme="XL", child="XL", SKU="SKU-XL")
    first = zying_export.build_record(
        zying_export.EXPECTED_HEADERS,
        values,
        category="一马当先",
        source_batch="old-batch",
        source_file="old.xlsx",
        source_page=1,
        source_row=2,
    )
    second = zying_export.build_record(
        zying_export.EXPECTED_HEADERS,
        values,
        category="一马当先",
        source_batch="new-batch",
        source_file="new.xlsx",
        source_page=12,
        source_row=43,
    )

    assert first["record_key"] == second["record_key"]


def test_parent_only_export_may_omit_variant_identity_headers():
    headers = tuple(
        header
        for header in zying_export.EXPECTED_HEADERS
        if header not in {"key", "theme", "child"}
    )
    row = tuple("100" if header == "id" else None for header in headers)

    record = zying_export.build_record(
        headers,
        row,
        category="全部分类",
        source_batch="parent-only",
        source_file="第01页.xlsx",
        source_page=1,
        source_row=2,
    )

    assert record["product_id"] == "100"
    assert record["variant_key"] is None
    assert record["variant_theme"] is None
    assert record["variant_child"] is None


def test_page_number_and_export_file_sorting(tmp_path):
    for name in ("一马当先_第12页.xlsx", "一马当先_第02页.xlsx", "一马当先_第01页.xlsx"):
        (tmp_path / name).touch()

    files = zying_export.discover_export_files(tmp_path)

    assert [path.name for path in files] == [
        "一马当先_第01页.xlsx",
        "一马当先_第02页.xlsx",
        "一马当先_第12页.xlsx",
    ]
    assert zying_export._page_from_file(Path("一马当先_第12页.xlsx")) == 12


@pytest.mark.parametrize(
    "table_name",
    ("1products", "products;DROP TABLE users", "products-name", ""),
)
def test_table_name_rejects_unsafe_identifiers(table_name):
    with pytest.raises(ValueError):
        zying_export._validate_table_name(table_name)


def test_upsert_updates_existing_record_instead_of_duplicating_it():
    sql = zying_export._upsert_sql("zying_desktop_products")

    assert sql.startswith("INSERT INTO `zying_desktop_products`")
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "`record_key`=VALUES(`record_key`)" not in sql
    assert "`content_hash`=VALUES(`content_hash`)" in sql


@pytest.mark.parametrize(
    ("texts", "expected"),
    (
        (["位置：全部分类 / 搜索", "共有 1 条记录"], 1),
        (["共有 29 条记录"], 29),
        (["没有找到任何分类"], None),
    ),
)
def test_parse_category_result_count(texts, expected):
    assert zying_export._parse_category_result_count(texts) == expected


@pytest.mark.parametrize(
    ("texts", "expected"),
    (
        (["共找到 5542 个产品"], 5542),
        (["共找到42个产品"], 42),
        (["没有找到任何产品"], None),
    ),
)
def test_parse_product_count(texts, expected):
    assert zying_export._parse_product_count(texts) == expected


def test_safe_filename_component_keeps_category_but_replaces_windows_separators():
    assert zying_export._safe_filename_component(" 一马/当先:* ") == "一马_当先__"


def test_checkbox_state_detection_uses_blue_checked_mark():
    unchecked = Image.new("RGB", (40, 40), "white")
    checked = unchecked.copy()
    for x in range(17, 24):
        checked.putpixel((x, 20), (10, 90, 160))

    assert not zying_export.ZyingDesktopExporter._checkbox_looks_checked(
        unchecked,
        (20, 20),
    )
    assert zying_export.ZyingDesktopExporter._checkbox_looks_checked(
        checked,
        (20, 20),
    )


def test_export_page_does_not_press_escape_before_filter_verification(tmp_path):
    exporter = object.__new__(zying_export.ZyingDesktopExporter)
    events = []
    exporter.pyautogui = type(
        "FakePyAutoGui",
        (),
        {"press": lambda self, key: events.append(("press", key))},
    )()
    exporter.activate = lambda: events.append(("activate", None))
    exporter._assert_filter_state = lambda context="": (_ for _ in ()).throw(
        RuntimeError("stop after filter check")
    )

    with pytest.raises(RuntimeError, match="stop after filter check"):
        exporter.export_page(1, tmp_path / "page.xlsx")

    assert ("press", "esc") not in events


def test_filter_verification_retries_transient_empty_salesperson(monkeypatch):
    exporter = object.__new__(zying_export.ZyingDesktopExporter)
    exporter.hwnd = 123
    exporter.salesperson = "精品区"
    exporter.expected_count = 17074
    exporter._child_texts = lambda hwnd: ["共找到 17074 个产品"]

    class Rectangle:
        left = 1065
        top = 63

    class Child:
        def __init__(self, value):
            self.value = value

        def rectangle(self):
            return Rectangle()

        def window_text(self):
            return self.value

    attempts = iter(([Child("")], [Child("精品区")]))

    class Window:
        def descendants(self):
            return next(attempts)

    class Desktop:
        def __init__(self, backend):
            assert backend == "uia"

        def window(self, handle):
            assert handle == 123
            return Window()

    import pywinauto

    monkeypatch.setattr(pywinauto, "Desktop", Desktop)
    monkeypatch.setattr(zying_export.time, "sleep", lambda seconds: None)

    assert exporter._assert_filter_state("test") == 17074


def test_reuse_current_filters_cli_option_is_declared():
    source = Path(zying_export.__file__).read_text(encoding="utf-8")

    assert '"--reuse-current-filters"' in source
    assert "exporter._filters_prepared = True" in source


def test_scaled_coordinates_follow_actual_window_size(monkeypatch):
    exporter = object.__new__(zying_export.ZyingDesktopExporter)
    exporter.hwnd = 123
    exporter.coordinates = zying_export.DesktopCoordinates()

    class User32:
        @staticmethod
        def GetWindowRect(hwnd, rect_pointer):
            rect = rect_pointer._obj
            rect.left, rect.top, rect.right, rect.bottom = 100, 50, 3940, 2114
            return True

    monkeypatch.setattr(
        zying_export.ctypes,
        "windll",
        type("Windll", (), {"user32": User32()})(),
    )

    assert exporter._scaled((960, 540)) == (2020, 1082)


def test_build_record_stores_all_export_filter_metadata():
    record = zying_export.build_record(
        zying_export.EXPECTED_HEADERS,
        _row(id="100"),
        category="全部分类",
        department="武汉泽顺",
        salesperson="精品区",
        collection_site="美客多",
        source_region="墨西哥",
        start_date="2026-01-01",
        end_date="2026-08-12",
        currency="美元",
        export_filters={"salesperson": "精品区", "currency": "美元"},
        source_batch="batch",
        source_file="page.xlsx",
        source_page=1,
        source_row=2,
    )

    assert record["export_department"] == "武汉泽顺"
    assert record["export_salesperson"] == "精品区"
    assert record["export_collection_site"] == "美客多"
    assert record["export_source_region"] == "墨西哥"
    assert record["export_end_date"] == "2026-08-12"
    assert record["export_currency"] == "美元"
    assert '"salesperson":"精品区"' in record["export_filters_json"]


def test_all_supported_filter_automation_ids_are_declared():
    source = Path(zying_export.__file__).read_text(encoding="utf-8")

    for automation_id in (
        "PID",
        "PKey",
        "PWord",
        "PKind",
        "PSection",
        "PLogin",
        "PSource",
        "PArea",
        "PDate",
        "PCur",
    ):
        assert f'"{automation_id}"' in source


def test_example_json_config_is_valid_and_has_combined_parameters():
    path = Path(zying_export.__file__).with_name("zying_export_config.example.json")
    config = json.loads(path.read_text(encoding="utf-8"))

    assert config["category"] == "全部分类"
    assert config["salesperson"] == "精品区"
    assert config["parent_only"] is True
    assert config["allow_count_drift"] is True
