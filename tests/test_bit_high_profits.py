from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest

from bit import bit_db_api, bit_interface, bit_mysql


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    @contextmanager
    def cursor(self):
        yield self._cursor

    def close(self):
        self.closed = True


def _viewer():
    return {
        "id": 1,
        "username": "viewer",
        "permissions": ["order_analysis.view"],
        "access_version": 1,
    }


def test_high_profit_query_supports_period_and_rate_sort(monkeypatch):
    cursor = _FakeCursor([
        {
            "product_id": 123,
            "title": "测试产品",
            "category": "分类",
            "image": "",
            "sites": "MX",
            "order_count": 20,
            "total_quantity": Decimal("25"),
            "total_income": Decimal("2000.00"),
            "total_profit": Decimal("500.00"),
            "latest_order_time": "2026-08-05 12:00:00",
            "profit_rate": Decimal("25.00"),
            "_total_products": 8,
            "_all_total_income": Decimal("6000.00"),
            "_all_total_profit": Decimal("1200.00"),
        }
    ])
    connection = _FakeConnection(cursor)
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: connection)

    result = bit_mysql.get_high_profit_products(
        sort_by="profit_rate",
        search="123",
        date_from="2026-08-01",
        date_to="2026-08-05",
        limit=10,
    )

    sql, params = cursor.calls[0]
    assert "`时间` >= %s" in sql
    assert "`时间` < %s" in sql
    assert "SUM(COALESCE(`利润`, 0))" in sql
    assert "ORDER BY `profit_rate` DESC" in sql
    assert params == (
        "2026-08-01 00:00:00",
        "2026-08-06 00:00:00",
        "%123%",
        "%123%",
        10,
    )
    assert result["summary"] == {
        "profitable_products": 8,
        "total_income": 6000.0,
        "total_profit": 1200.0,
        "profit_rate": 20.0,
    }
    assert result["rows"][0]["product_id"] == "123"
    assert result["rows"][0]["profit_rate"] == 25.0
    assert connection.closed is True


def test_high_profit_query_rejects_reversed_period():
    with pytest.raises(ValueError, match="开始日期不能晚于结束日期"):
        bit_mysql.get_high_profit_products(
            date_from="2026-08-06",
            date_to="2026-08-01",
        )


def test_high_profit_api_passes_filters(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_interface, "get_current_workbench_user", _viewer)
    monkeypatch.setattr(
        bit_interface,
        "db_get_high_profit_products",
        lambda **kwargs: calls.append(kwargs) or {"summary": {}, "rows": []},
    )
    client = bit_interface.app.test_client()

    response = client.get(
        "/api/order-analysis/high-profits"
        "?sort_by=profit_rate&date_from=2026-08-01&date_to=2026-08-05&search=abc"
    )

    assert response.status_code == 200
    assert calls == [{
        "sort_by": "profit_rate",
        "sort_dir": "desc",
        "search": "abc",
        "date_from": "2026-08-01",
        "date_to": "2026-08-05",
        "limit": 100,
    }]


def test_db_api_proxies_high_profit_query(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {},
    )

    bit_db_api.get_high_profit_products(
        sort_by="profit_rate",
        date_from="2026-08-01",
        date_to="2026-08-05",
    )

    assert calls[0][0:2] == ("GET", "/api/db/orders/high-profits")
    assert calls[0][2]["params"]["date_from"] == "2026-08-01"
    assert calls[0][2]["params"]["date_to"] == "2026-08-05"


def test_order_analysis_page_contains_high_profit_controls():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert "高利润产品" in template
    assert 'id="profit-date-from"' in template
    assert 'id="profit-date-to"' in template
    assert "sortHighProfitProducts('total_profit')" in template
    assert "sortHighProfitProducts('profit_rate')" in template
