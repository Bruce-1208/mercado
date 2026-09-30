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
    assert "COUNT(DISTINCT o.`order_id`)" in connection.db_cursor.sql
    assert "o.`status`" not in connection.db_cursor.sql
    assert "JSON_TABLE" not in connection.db_cursor.sql
    assert "o.`token_id` IN (%s)" in connection.db_cursor.sql
    assert "o.`token_id` = %s" in connection.db_cursor.sql
    assert connection.db_cursor.params[-3:] == [3, "王", "一组"]


@pytest.fixture(autouse=True)
def clear_analysis_cache(monkeypatch, tmp_path):
    from bit import store_analysis
    monkeypatch.setattr(store_analysis, "_ANALYSIS_CACHE_DIR", tmp_path)
    store_analysis._ANALYSIS_CACHE.clear()
    yield
    store_analysis._ANALYSIS_CACHE.clear()


def test_analysis_cache_expires_and_isolates_filters_and_permissions(monkeypatch):
    from bit import store_analysis
    calls = []
    clock = [100]

    def compute(*args, **kwargs):
        calls.append(kwargs)
        return {"sites": [{"orders": len(calls)}]}

    monkeypatch.setattr(store_analysis, "_compute_store_analysis", compute)
    monkeypatch.setattr(store_analysis.time, "monotonic", lambda: clock[0])
    kwargs = dict(allowed_token_ids={1, 2})
    result = list_store_analysis("2026-09-01", "2026-09-07", **kwargs)
    result["sites"][0]["orders"] = 999
    assert list_store_analysis("2026-09-01", "2026-09-07", **kwargs)["sites"][0]["orders"] == 1
    assert len(calls) == 1
    list_store_analysis("2026-09-01", "2026-09-07", allowed_token_ids={1})
    list_store_analysis("2026-09-01", "2026-09-07", metric="gmv", **kwargs)
    list_store_analysis("2026-09-01", "2026-09-07", category_level=3, **kwargs)
    assert len(calls) == 4
    clock[0] += 61
    list_store_analysis("2026-09-01", "2026-09-07", **kwargs)
    assert len(calls) == 4
    list_store_analysis("2026-09-01", "2026-09-07", refresh=True, **kwargs)
    assert len(calls) == 5


def test_analysis_only_translates_displayed_categories(monkeypatch):
    from bit import store_analysis
    rows = [{"order_id": str(i), "site_id": "MLM", "category_id": f"MLM{i}",
             "total_amount": 1, "line_amount": 1, "order_line_amount": 1, "usd_rate": 1}
            for i in range(8)]
    paths = {f"MLM{i}": [{"id": "MLM100", "name": "root"},
                          {"id": f"MLM{i}", "name": f"Category {i}"}] for i in range(8)}

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, sql, *_): self.is_trend = "AS order_date" in sql
        def fetchall(self): return [] if self.is_trend else rows

    class Connection:
        def cursor(self): return Cursor()
        def close(self): pass

    translated = []
    def translate(selected_paths, level):
        translated.extend(selected_paths)
        return {path[level - 1]["id"]: "中文分类" for path in selected_paths.values()}

    monkeypatch.setattr(store_analysis.pymysql, "connect", lambda **_: Connection())
    monkeypatch.setattr(store_analysis, "category_paths_for_ids", lambda _: paths)
    monkeypatch.setattr(store_analysis, "_translate_category_names", translate)
    data = list_store_analysis("2026-09-01", "2026-09-07")
    site = data["sites"][0]
    assert len(translated) == 8
    assert site["orders"] == 8
    assert site["category_total"] == 8
    assert site["other_value"] == 0
    assert all(category["name_zh"] == "中文分类" for category in site["top_categories"])


def test_analysis_coalesces_concurrent_identical_requests(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from bit import store_analysis
    started, release = Event(), Event()
    calls = []

    def compute(*args, **kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(3)
        return {"sites": []}

    monkeypatch.setattr(store_analysis, "_compute_store_analysis", compute)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(list_store_analysis, "2026-09-01", "2026-09-07")
        try:
            assert started.wait(3)
            second = executor.submit(list_store_analysis, "2026-09-01", "2026-09-07")
        finally:
            release.set()
        assert first.result() == second.result()
        assert first.result()["sites"] == []
    assert len(calls) == 1


def test_order_trend_fills_days_and_handles_zero_and_range_boundaries():
    from bit.store_analysis import summarize_order_trend
    result = summarize_order_trend([
        {"order_date": "2026-09-01", "orders": 4},
        {"order_date": "2026-09-02", "orders": 6},
        {"order_date": "2026-09-04", "orders": 3},
    ], "2026-09-01", "2026-09-04")
    assert result["total_orders"] == 13
    assert len(result["days"]) == 7
    assert result["days"][0]["orders"] is None
    assert result["days"][-3]["change_rate"] == 50
    assert result["days"][-2]["orders"] == 0
    assert result["days"][-2]["change_rate"] == -100
    assert result["days"][-1]["change_rate"] is None
    assert result["days"][-1]["previous_orders"] == 0
