from datetime import datetime, timedelta
from unittest import mock

from bit import bit_mysql


class _ReadCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, _query, _params=None):
        return None

    def fetchone(self):
        return {"latest_submit_time": "2026-07-30 12:00:00"}


class _ReadConnection:
    def cursor(self):
        return _ReadCursor()

    def close(self):
        return None


def _reputation_row(shop_name, site, color):
    return {
        "店铺名": shop_name,
        "站点": site,
        "声誉颜色": color,
        "总单量": "10",
        "投诉率": "1%",
        "延误率": "2%",
        "取消率": "3%",
        "增加或减少": "持平",
        "近七天变化率": "0%",
        "系统告警": "正常",
        "更新时间": "2026-07-29 10:00:00",
        "一周流量趋势": "[]",
    }


def test_reputation_rerun_replaces_selected_shop_and_keeps_other_shops():
    submit_time = "2026-07-30 12:00:00"
    collected = [
        [
            "店铺乙",
            "巴西",
            "绿色",
            "20",
            "0%",
            "0%",
            "0%",
            "上升",
            "5%",
            "正常",
            "2026-07-30 11:59:00",
            "[]",
            submit_time,
        ]
    ]

    merged = bit_mysql._merge_reputation_snapshot_rows(
        [
            _reputation_row("店铺甲", "墨西哥", "黄色"),
            _reputation_row("店铺乙", "巴西", "红色"),
        ],
        collected,
        [("店铺乙", "巴西")],
        submit_time,
    )

    assert [(row[0], row[1], row[2]) for row in merged] == [
        ("店铺甲", "墨西哥", "黄色"),
        ("店铺乙", "巴西", "绿色"),
    ]
    assert all(row[12] == submit_time for row in merged)


def test_infraction_rerun_replaces_selected_site_and_keeps_other_shops():
    submit_time = "2026-07-30 12:00:00"
    latest = [
        {
            "店铺名": "店铺甲",
            "站点": "墨西哥",
            "编号": "A-1",
            "标题": "甲侵权",
            "侵权时间": "2026-07-20",
            "执行时间": "2026-07-29 10:00:00",
            "类型": "侵权",
        },
        {
            "店铺名": "店铺乙",
            "站点": "巴西",
            "编号": "B-old",
            "标题": "旧侵权",
            "侵权时间": "2026-07-20",
            "执行时间": "2026-07-29 10:00:00",
            "类型": "侵权",
        },
    ]
    collected = [
        [
            "店铺乙",
            "巴西",
            "B-new",
            "新侵权",
            "2026-07-30",
            submit_time,
            "2026-07-30 11:59:00",
            "侵权",
        ]
    ]

    merged = bit_mysql._merge_infraction_snapshot_rows(
        latest,
        collected,
        [("店铺乙", "巴西")],
        submit_time,
    )

    assert [(row[0], row[1], row[2]) for row in merged] == [
        ("店铺甲", "墨西哥", "A-1"),
        ("店铺乙", "巴西", "B-new"),
    ]
    assert all(row[5] == submit_time for row in merged)


def test_infraction_zero_result_rerun_removes_only_selected_site():
    merged = bit_mysql._merge_infraction_snapshot_rows(
        [
            {
                "店铺名": "店铺甲",
                "站点": "墨西哥",
                "编号": "A-1",
                "标题": "甲侵权",
                "侵权时间": "2026-07-20",
                "执行时间": "2026-07-29 10:00:00",
                "类型": "侵权",
            },
            {
                "店铺名": "店铺乙",
                "站点": "巴西",
                "编号": "B-old",
                "标题": "旧侵权",
                "侵权时间": "2026-07-20",
                "执行时间": "2026-07-29 10:00:00",
                "类型": "侵权",
            },
        ],
        [],
        [("店铺乙", "巴西")],
        "2026-07-30 12:00:00",
    )

    assert [(row[0], row[1], row[2]) for row in merged] == [
        ("店铺甲", "墨西哥", "A-1"),
    ]


def test_active_snapshot_filter_removes_disabled_historical_shops():
    with mock.patch.object(
        bit_mysql,
        "_load_authorized_shop_sites",
        return_value=[{"店铺名": "店铺甲", "站点": "墨西哥"}],
    ):
        rows = bit_mysql._active_collection_snapshot_rows(
            [
                _reputation_row("店铺甲", "墨西哥", "绿色"),
                _reputation_row("已停用店铺", "巴西", "黄色"),
            ]
        )

    assert [(row["店铺名"], row["站点"]) for row in rows] == [
        ("店铺甲", "墨西哥"),
    ]


def test_reputation_without_snapshot_marker_uses_latest_rows_per_shop():
    rows = [
        _reputation_row("此前成功店铺", "墨西哥", "绿色"),
        _reputation_row("本次补跑店铺", "巴西", "黄色"),
    ]
    with (
        mock.patch.object(bit_mysql, "_ensure_column"),
        mock.patch.object(
            bit_mysql,
            "_latest_tracked_collection_snapshot",
            return_value=None,
        ),
        mock.patch.object(
            bit_mysql,
            "_latest_reputation_snapshot_rows",
            return_value=rows,
        ) as latest_rows,
        mock.patch.object(
            bit_mysql,
            "_active_collection_snapshot_rows",
            side_effect=lambda value: value,
        ),
        mock.patch.object(
            bit_mysql,
            "_get_latest_collection_task_status",
            return_value={},
        ),
        mock.patch.object(bit_mysql, "_load_authorized_shop_sites", return_value=[]),
        mock.patch.object(
            bit_mysql.pymysql,
            "connect",
            return_value=_ReadConnection(),
        ),
    ):
        data = bit_mysql.get_latest_reputation_info()

    latest_rows.assert_called_once()
    assert {row["店铺名"] for row in data["rows"]} == {
        "此前成功店铺",
        "本次补跑店铺",
    }


def test_infraction_without_snapshot_marker_uses_latest_rows_per_shop():
    recent_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = [
        {
            "店铺名": "此前成功店铺",
            "站点": "墨西哥",
            "编号": "A-1",
            "标题": "此前记录",
            "侵权时间": recent_date,
            "提交时间": "2026-07-29 12:00:00",
            "执行时间": "2026-07-29 11:59:00",
            "类型": "侵权",
        },
        {
            "店铺名": "本次补跑店铺",
            "站点": "巴西",
            "编号": "B-1",
            "标题": "补跑记录",
            "侵权时间": recent_date,
            "提交时间": "2026-07-30 12:00:00",
            "执行时间": "2026-07-30 11:59:00",
            "类型": "权利人",
        },
    ]
    with (
        mock.patch.object(bit_mysql, "_ensure_column"),
        mock.patch.object(
            bit_mysql,
            "_latest_tracked_collection_snapshot",
            return_value=None,
        ),
        mock.patch.object(
            bit_mysql,
            "_latest_infraction_snapshot_rows",
            return_value=rows,
        ) as latest_rows,
        mock.patch.object(
            bit_mysql,
            "_active_collection_snapshot_rows",
            side_effect=lambda value: value,
        ),
        mock.patch.object(
            bit_mysql,
            "_get_latest_infraction_task_status",
            return_value={},
        ),
        mock.patch.object(bit_mysql, "_load_authorized_shop_sites", return_value=[]),
        mock.patch.object(
            bit_mysql.pymysql,
            "connect",
            return_value=_ReadConnection(),
        ),
    ):
        data = bit_mysql.get_latest_infraction_info(30)

    latest_rows.assert_called_once()
    assert {row["店铺名"] for row in data["rows"]} == {
        "此前成功店铺",
        "本次补跑店铺",
    }
