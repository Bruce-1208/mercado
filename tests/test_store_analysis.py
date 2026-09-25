from decimal import Decimal

import pytest

from bit.store_analysis import _dates, list_store_analysis, summarize_store_analysis


def test_store_analysis_ranks_sites_and_groups_second_level_categories():
    rows = [
        {"order_id": "1", "site_id": "MLM", "category_id": "MLM100", "total_amount": 30,
         "line_amount": 20, "order_line_amount": 30, "usd_rate": 1},
        {"order_id": "1", "site_id": "MLM", "category_id": "MLM101", "total_amount": 30,
         "line_amount": 10, "order_line_amount": 30, "usd_rate": 1},
        {"order_id": "2", "site_id": "MLM", "category_id": "MLM200", "total_amount": 10,
         "line_amount": 10, "order_line_amount": 10, "usd_rate": 1},
        {"order_id": "3", "site_id": "MLB", "category_id": "MLB300", "total_amount": 90,
         "line_amount": 90, "order_line_amount": 90, "usd_rate": Decimal("0.5")},
    ]
    paths = {
        "MLM100": [{"id": "root", "name": "根"}, {"id": "MLM10", "name": "家居"}, {"id": "MLM100", "name": "灯具"}],
        "MLM101": [{"id": "root", "name": "根"}, {"id": "MLM10", "name": "家居"}, {"id": "MLM101", "name": "家具"}],
        "MLM200": [{"id": "root", "name": "根"}, {"id": "MLM20", "name": "服饰"}],
        "MLB300": [{"id": "root", "name": "根"}, {"id": "MLB30", "name": "电子"}],
    }
    by_orders = summarize_store_analysis(rows, paths)
    assert [site["site_id"] for site in by_orders] == ["MLM", "MLB"]
    assert by_orders[0]["orders"] == 2
    assert by_orders[0]["gmv_usd"] == 40
    assert by_orders[0]["top_categories"][0] == {
        "category_id": "MLM10", "name": "家居", "orders": 1, "gmv_usd": 30.0,
    }
    by_gmv = summarize_store_analysis(rows, paths, metric="gmv")
    assert [site["site_id"] for site in by_gmv] == ["MLB", "MLM"]


def test_store_analysis_date_bounds_use_beijing_days():
    start, end = _dates("2026-09-01", "2026-09-01")
    assert start.isoformat(sep=" ") == "2026-08-31 16:00:00"
    assert end.isoformat(sep=" ") == "2026-09-01 16:00:00"
    with pytest.raises(ValueError):
        _dates("2026-09-02", "2026-09-01")


def test_store_analysis_respects_authorized_store_scope(monkeypatch):
    class Cursor:
        def __init__(self):
            self.sql = ""
            self.params = []

        def execute(self, sql, params):
            self.sql, self.params = sql, params

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    class Connection:
        def __init__(self):
            self.db_cursor = Cursor()

        def cursor(self):
            return self.db_cursor

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr("bit.store_analysis.pymysql.connect", lambda **_: connection)
    assert list_store_analysis("2026-09-01", "2026-09-02", allowed_token_ids=set())["sites"] == []
    assert connection.db_cursor.sql == ""
    with pytest.raises(PermissionError):
        list_store_analysis("2026-09-01", "2026-09-02", token_id=4, allowed_token_ids={3})
    list_store_analysis("2026-09-01", "2026-09-02", token_id=3,
                        salesperson="王", group_name="一组", allowed_token_ids={3})
    assert "o.`token_id` IN (%s)" in connection.db_cursor.sql
    assert "o.`token_id` = %s" in connection.db_cursor.sql
    assert connection.db_cursor.params[-3:] == [3, "王", "一组"]
