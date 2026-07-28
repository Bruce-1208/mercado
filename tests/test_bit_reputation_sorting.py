import unittest
from unittest import mock

from bit import bit_mysql


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, _query, _params=None):
        return None

    def fetchone(self):
        return {"latest_submit_time": "2026-07-25 12:00:00"}

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)

    def close(self):
        return None


class ReputationSortingTests(unittest.TestCase):
    def test_latest_reputation_is_sorted_by_total_traffic(self):
        rows = [
            {
                "店铺名": "高单量低流量店",
                "站点": "墨西哥",
                "总单量": "999",
                "一周流量趋势": "[1, 2, 3]",
            },
            {
                "店铺名": "低单量高流量店",
                "站点": "巴西",
                "总单量": "1",
                "一周流量趋势": "[100, 200, 300]",
            },
        ]
        with (
            mock.patch.object(bit_mysql, "_ensure_column"),
            mock.patch.object(
                bit_mysql.pymysql,
                "connect",
                return_value=_Connection(rows),
            ),
        ):
            data = bit_mysql.get_latest_reputation_info()

        self.assertEqual(
            [row["店铺名"] for row in data["rows"]],
            ["低单量高流量店", "高单量低流量店"],
        )

    def test_parse_traffic_total_handles_empty_and_fallback_text(self):
        self.assertEqual(bit_mysql._parse_traffic_total(""), 0)
        self.assertEqual(bit_mysql._parse_traffic_total("流量: 10 / 20 / 30"), 60)
