import unittest
from datetime import datetime, timedelta
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
    def test_latest_infraction_counts_are_grouped_by_shop_site_and_type(self):
        recent_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        old_date = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
        rows = [
            {"店铺名": "店铺甲", "站点": "墨西哥", "类型": "侵权", "侵权时间": recent_date},
            {"店铺名": "店铺甲", "站点": "墨西哥", "类型": "权利人", "侵权时间": recent_date},
            {"店铺名": "店铺甲", "站点": "墨西哥", "类型": "权利人", "侵权时间": recent_date},
            {"店铺名": "店铺甲", "站点": "巴西", "类型": "侵权", "侵权时间": recent_date},
            {"店铺名": "店铺甲", "站点": "墨西哥", "类型": "侵权", "侵权时间": old_date},
        ]
        with (
            mock.patch.object(
                bit_mysql,
                "_latest_infraction_snapshot_rows",
                return_value=rows,
            ),
            mock.patch.object(
                bit_mysql,
                "_active_collection_snapshot_rows",
                side_effect=lambda value, **_kwargs: value,
            ),
        ):
            counts = bit_mysql._latest_infraction_counts_by_shop_site(
                object(),
                recent_days=30,
            )

        self.assertEqual(
            counts[("店铺甲", "墨西哥")],
            {"侵权数量": 1, "权利人数量": 2},
        )
        self.assertEqual(
            counts[("店铺甲", "巴西")],
            {"侵权数量": 1, "权利人数量": 0},
        )

    def test_latest_reputation_is_sorted_by_site_seven_day_orders(self):
        rows = [
            {
                "店铺名": "多站点店铺",
                "站点": "墨西哥",
                "总单量": "10",
                "七天单量": 120,
                "一周流量趋势": "[60, 60]",
            },
            {
                "店铺名": "单站点店铺",
                "站点": "巴西",
                "总单量": "999",
                "七天单量": 200,
                "一周流量趋势": "[200]",
            },
            {
                "店铺名": "多站点店铺",
                "站点": "阿根廷",
                "总单量": "20",
                "七天单量": 100,
                "一周流量趋势": "[50, 50]",
            },
            {"店铺名": "未知单量店铺", "站点": "墨西哥", "总单量": 9999,
             "七天单量": None, "一周流量趋势": "[9999]"},
            {"店铺名": "零单量店铺", "站点": "巴西", "总单量": 9999,
             "七天单量": 0, "一周流量趋势": "[9999]"},
        ]
        with (
            mock.patch.object(bit_mysql, "_ensure_column"),
            mock.patch.object(
                bit_mysql,
                "_load_authorized_shop_sites",
                return_value=[
                    {"店铺名": "多站点店铺", "站点": "墨西哥"},
                    {"店铺名": "多站点店铺", "站点": "阿根廷"},
                    {"店铺名": "单站点店铺", "站点": "巴西"},
                    {"店铺名": "未知单量店铺", "站点": "墨西哥"},
                    {"店铺名": "零单量店铺", "站点": "巴西"},
                ],
            ),
            mock.patch.object(
                bit_mysql,
                "_latest_infraction_counts_by_shop_site",
                return_value={},
            ) as infraction_counts,
            mock.patch.object(
                bit_mysql.pymysql,
                "connect",
                return_value=_Connection(rows),
            ),
        ):
            data = bit_mysql.get_latest_reputation_info()

        self.assertEqual(
            [(row["店铺名"], row["站点"]) for row in data["rows"]],
            [
                ("单站点店铺", "巴西"),
                ("多站点店铺", "墨西哥"),
                ("多站点店铺", "阿根廷"),
                ("零单量店铺", "巴西"),
                ("未知单量店铺", "墨西哥"),
            ],
        )
        self.assertEqual([row["orders_current_7d"] for row in data["rows"]], [200, 120, 100, 0, None])
        self.assertTrue(all(row["侵权数量"] == 0 for row in data["rows"]))
        self.assertTrue(all(row["权利人数量"] == 0 for row in data["rows"]))
        self.assertTrue(all(row["侵权统计天数"] == 100 for row in data["rows"]))
        infraction_counts.assert_called_once_with(
            mock.ANY,
            recent_days=100,
            active_targets={
                ("多站点店铺", "墨西哥"),
                ("多站点店铺", "阿根廷"),
                ("单站点店铺", "巴西"),
                ("未知单量店铺", "墨西哥"),
                ("零单量店铺", "巴西"),
            },
        )

    def test_latest_collection_task_status_keeps_latest_site_result(self):
        class StatusCursor:
            def __init__(self):
                self.params = None

            def execute(self, _query, params=None):
                self.params = params

            def fetchall(self):
                return [
                    {
                        "name": "店铺甲",
                        "site": "墨西哥",
                        "isSuccess": "失败：页面结构不匹配",
                        "datetime": "2026-07-29 10:00:00",
                    },
                    {
                        "name": "店铺甲",
                        "site": "墨西哥",
                        "isSuccess": "成功",
                        "datetime": "2026-07-29 09:00:00",
                    },
                    {
                        "name": "店铺甲",
                        "site": "巴西",
                        "isSuccess": "成功",
                        "datetime": "2026-07-29 09:30:00",
                    },
                ]

        cursor = StatusCursor()
        status = bit_mysql._get_latest_collection_task_status(
            cursor,
            "获取声誉信息",
        )

        self.assertEqual(cursor.params, ("获取声誉信息",))
        self.assertEqual(status[("店铺甲", "墨西哥")]["状态"], "失败：页面结构不匹配")
        self.assertEqual(status[("店铺甲", "巴西")]["状态"], "成功")

    def test_parse_traffic_total_handles_empty_and_fallback_text(self):
        self.assertEqual(bit_mysql._parse_traffic_total(""), 0)
        self.assertEqual(bit_mysql._parse_traffic_total("流量: 10 / 20 / 30"), 60)
