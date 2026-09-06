from datetime import datetime, timedelta

from bit import bit_appeal_ai
from bit import bit_appeal_report as report
from bit import bit_send_mail


def test_six_hour_report_groups_executor_site_and_failure_reason():
    until = datetime(2026, 9, 6, 18, 0, 0)
    rows = [
        {
            "appeal_time": "2026-09-06 17:00:00",
            "appeal_type": "侵权",
            "shop_name": "店铺甲",
            "site": "墨西哥",
            "status": "已收到回复，结果待确认",
            "execution": {"execution_status": "replied"},
            "executor": {
                "runtime_role": "server",
                "execution_target": "server",
                "hostname": "appeal-server-1",
            },
        },
        {
            "appeal_time": "2026-09-06 16:00:00",
            "appeal_type": "投诉",
            "shop_name": "店铺乙",
            "site": "巴西",
            "status": "执行失败",
            "error": "客服入口未找到",
            "execution": {"execution_status": "failed", "error": "客服入口未找到"},
            "executor": {
                "runtime_role": "client",
                "execution_target": "local",
                "hostname": "sales-pc",
            },
        },
        {
            "appeal_time": "2026-09-06 15:00:00",
            "appeal_type": "取消率",
            "shop_name": "店铺丙",
            "site": "阿根廷",
            "status": "无可申诉数据",
            "execution": {"execution_status": "no_data"},
            "executor": {
                "runtime_role": "server",
                "execution_target": "server",
                "hostname": "appeal-server-1",
            },
        },
        {
            "appeal_time": "2026-09-06 17:00:00",
            "appeal_type": "侵权-第1/2组",
            "shop_name": "店铺甲",
            "site": "墨西哥",
            "record_scope": "group",
            "execution": {"execution_status": "replied"},
        },
        {
            "appeal_time": "2026-09-06 11:59:59",
            "appeal_type": "侵权",
            "shop_name": "过期店铺",
            "site": "智利",
            "execution": {"execution_status": "failed"},
        },
    ]

    summary = report.summarize_appeal_records(
        rows,
        since=until - timedelta(hours=6),
        until=until,
    )

    assert summary["overall"] == {
        "total": 3,
        "success": 1,
        "failed": 1,
        "no_data": 1,
        "attempted": 2,
        "success_rate": 50.0,
    }
    assert summary["executors"]["服务器比特浏览器（appeal-server-1）"]["total"] == 2
    assert summary["executors"]["本机比特浏览器（sales-pc）"]["failed"] == 1
    assert summary["sites"]["墨西哥"]["success"] == 1
    assert summary["sites"]["巴西"]["failed"] == 1
    assert summary["failures"][0]["reason"] == "客服入口未找到"

    body = report.render_report(summary)
    assert "执行成功率：50.0%" in body
    assert "服务器比特浏览器（appeal-server-1）" in body
    assert "店铺乙｜巴西｜投诉｜执行失败｜客服入口未找到" in body
    assert "成功表示 AI 客服已回复；不代表平台已经批准申诉" in body


def test_report_with_no_attempts_does_not_claim_zero_percent():
    now = datetime(2026, 9, 6, 18, 0, 0)
    summary = report.summarize_appeal_records([], since=now - timedelta(hours=6), until=now)

    assert summary["overall"]["success_rate"] is None
    assert "执行成功率：无可计算执行" in report.render_report(summary)


def test_send_recent_report_uses_requested_recipient(monkeypatch):
    now = datetime(2026, 9, 6, 18, 0, 0)
    summary = report.summarize_appeal_records([], since=now - timedelta(hours=6), until=now)
    monkeypatch.setattr(report, "build_recent_report", lambda **_kwargs: (summary, "报告正文"))
    sent = {}

    def fake_send(subject, body, **kwargs):
        sent.update(subject=subject, body=body, **kwargs)
        return True

    monkeypatch.setattr(bit_send_mail, "send_info", fake_send)

    result = report.send_recent_report(hours=6, recipient="1013459852@qq.com")

    assert result["sent"] is True
    assert sent["receiver_email"] == "1013459852@qq.com"
    assert sent["body"] == "报告正文"
    assert "6 小时报告" in sent["subject"]


def test_appeal_record_captures_executor_hostname(monkeypatch):
    monkeypatch.setenv("BIT_RUNTIME_ROLE", "client")
    monkeypatch.setattr(bit_appeal_ai.socket, "gethostname", lambda: "appeal-pc")

    assert bit_appeal_ai.appeal_executor_metadata() == {
        "runtime_role": "client",
        "execution_target": "local",
        "hostname": "appeal-pc",
    }
