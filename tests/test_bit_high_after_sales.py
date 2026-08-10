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


def test_high_after_sale_query_supports_period_and_rate_sort(monkeypatch):
    cursor = _FakeCursor([
        {
            "product_id": 123,
            "title": "测试产品",
            "category": "分类",
            "image": "",
            "sites": "MX",
            "total_orders": 20,
            "total_quantity": Decimal("25"),
            "after_sale_orders": 5,
            "after_sale_quantity": Decimal("6"),
            "latest_after_sale_time": "2026-08-05 12:00:00",
            "after_sale_rate": Decimal("24.00"),
            "_total_alert_products": 8,
            "_all_after_sale_quantity": Decimal("18"),
            "_all_total_quantity": Decimal("90"),
        }
    ])
    connection = _FakeConnection(cursor)
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: connection)

    result = bit_mysql.get_high_after_sale_alerts(
        sort_by="after_sale_rate",
        search="123",
        date_from="2026-08-01",
        date_to="2026-08-05",
        limit=10,
    )

    sql, params = cursor.calls[0]
    assert "`时间` >= %s" in sql
    assert "`时间` < %s" in sql
    assert "ORDER BY `after_sale_rate` DESC" in sql
    assert params == (
        "2026-08-01 00:00:00",
        "2026-08-06 00:00:00",
        "%123%",
        "%123%",
        10,
    )
    assert result["summary"] == {
        "alert_products": 8,
        "after_sale_quantity": 18,
        "total_quantity": 90,
        "after_sale_rate": 20.0,
    }
    assert result["rows"][0]["product_id"] == "123"
    assert result["rows"][0]["after_sale_rate"] == 24.0
    assert connection.closed is True


def test_high_after_sale_query_rejects_reversed_period():
    with pytest.raises(ValueError, match="开始日期不能晚于结束日期"):
        bit_mysql.get_high_after_sale_alerts(
            date_from="2026-08-06",
            date_to="2026-08-01",
        )


def test_high_after_sale_api_passes_filters(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_interface, "get_current_workbench_user", _viewer)
    monkeypatch.setattr(
        bit_interface,
        "db_get_high_after_sale_alerts",
        lambda **kwargs: calls.append(kwargs) or {"summary": {}, "rows": []},
    )
    client = bit_interface.app.test_client()

    response = client.get(
        "/api/order-analysis/high-after-sales"
        "?sort_by=after_sale_rate&date_from=2026-08-01&date_to=2026-08-05&search=abc"
    )

    assert response.status_code == 200
    assert calls == [{
        "sort_by": "after_sale_rate",
        "sort_dir": "desc",
        "search": "abc",
        "date_from": "2026-08-01",
        "date_to": "2026-08-05",
        "limit": 100,
    }]


def test_db_api_proxies_high_after_sale_query(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {},
    )

    bit_db_api.get_high_after_sale_alerts(
        sort_by="after_sale_rate",
        date_from="2026-08-01",
        date_to="2026-08-05",
    )

    assert calls[0][0:2] == ("GET", "/api/db/orders/high-after-sales")
    assert calls[0][2]["params"]["date_from"] == "2026-08-01"
    assert calls[0][2]["params"]["date_to"] == "2026-08-05"


def test_order_analysis_page_contains_high_after_sale_controls():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert "高售后告警" in template
    assert 'id="after-sale-date-from"' in template
    assert 'id="after-sale-date-to"' in template
    assert "sortHighAfterSaleAlerts('after_sale_quantity')" in template
    assert "sortHighAfterSaleAlerts('after_sale_rate')" in template
