import re
from pathlib import Path

from bit import bit_interface, bit_mysql, bit_order_sync


def test_workbench_uses_minute_datetime_inputs_with_friendly_placeholders():
    template = Path(bit_interface.app.template_folder, "index.html").read_text(
        encoding="utf-8"
    )

    minute_filter_ids = (
        "order-start-date",
        "order-end-date",
        "order-sync-start-date",
        "order-sync-end-date",
        "mercado-date-from",
        "mercado-date-to",
        "order-print-date-from",
        "order-print-date-to",
        "after-sale-date-from",
        "after-sale-date-to",
        "profit-date-from",
        "profit-date-to",
    )
    for input_id in minute_filter_ids:
        tag = re.search(rf'<input[^>]*id="{input_id}"[^>]*>', template)
        assert tag, f"缺少分钟级时间筛选控件: {input_id}"
        assert 'class="minute-datetime"' in tag.group(0)
        assert 'type="datetime-local"' in tag.group(0)
        assert 'step="60"' in tag.group(0)
    assert "datetime-placeholder" in template
    assert "选择起始时间" in template
    assert "选择结束时间" in template


def test_minute_filter_end_includes_the_selected_minute():
    start_at, end_exclusive = bit_mysql._filter_datetime_bounds(
        "2026-08-27T09:15",
        "2026-08-27T10:30",
    )

    assert start_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-27 09:15:00"
    assert end_exclusive.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-27 10:31:00"


def test_legacy_date_filter_still_includes_the_entire_end_day():
    start_at, end_exclusive = bit_mysql._filter_datetime_bounds(
        "2026-08-01",
        "2026-08-27",
    )

    assert start_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-01 00:00:00"
    assert end_exclusive.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-28 00:00:00"


def test_manual_order_sync_converts_beijing_minutes_to_utc():
    start_text, end_text, start_at, end_at = bit_order_sync._date_range(
        "2026-08-27T09:15",
        "2026-08-27T10:30",
    )

    assert start_text == "2026-08-27T09:15"
    assert end_text == "2026-08-27T10:30"
    assert bit_order_sync._iso_millis(start_at) == "2026-08-27T01:15:00.000Z"
    assert bit_order_sync._iso_millis(end_at) == "2026-08-27T02:31:00.000Z"
