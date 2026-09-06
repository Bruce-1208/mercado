"""Build and email a six-hour execution report for automatic appeals."""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable

from bit.bit_appeal_state import STATUS_LABELS


DEFAULT_REPORT_HOURS = 6
DEFAULT_REPORT_RECIPIENT = "1013459852@qq.com"
DEFAULT_RECORD_LIMIT = 500
SUCCESS_STATUSES = frozenset(("replied",))
SKIPPED_STATUSES = frozenset(("no_data",))
GROUP_APPEAL_MARKER = "组"


def _parse_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_group_record(row):
    if str(row.get("record_scope") or "").strip().lower() == "group":
        return True
    appeal_type = str(row.get("appeal_type") or "")
    return GROUP_APPEAL_MARKER in appeal_type and "第" in appeal_type and "/" in appeal_type


def _execution_status(row):
    execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
    status = str(
        execution.get("execution_status")
        or execution.get("status")
        or ""
    ).strip()
    if status:
        return status

    stored_status = str(row.get("status") or "").strip()
    reverse_labels = {label: key for key, label in STATUS_LABELS.items()}
    if stored_status in reverse_labels:
        return reverse_labels[stored_status]
    if stored_status == "未登录":
        return "login_required"
    if str(row.get("error") or "").strip():
        return "failed"
    return "unknown"


def _executor_label(row):
    executor = row.get("executor") if isinstance(row.get("executor"), dict) else {}
    target = str(executor.get("execution_target") or "").strip().lower()
    role = str(executor.get("runtime_role") or "").strip().lower()
    hostname = str(executor.get("hostname") or "").strip()
    if target == "local" or role == "client":
        label = "本机比特浏览器"
    elif target == "server" or role == "server":
        label = "服务器比特浏览器"
    else:
        label = "执行端未记录"
    return f"{label}（{hostname}）" if hostname else label


def _failure_reason(row, status):
    execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
    for value in (
        execution.get("error"),
        row.get("error"),
        execution.get("message"),
        row.get("status"),
        row.get("ai_summary"),
    ):
        text = str(value or "").strip()
        if text:
            text = text.split("Stacktrace:", 1)[0].strip()
            text = text.replace("\r", " ").replace("\n", "；")
            return text if len(text) <= 300 else text[:300] + "…"
    return STATUS_LABELS.get(status, status or "原因未记录")


def _new_bucket():
    return {"total": 0, "success": 0, "failed": 0, "no_data": 0}


def _increment_bucket(bucket, classification):
    bucket["total"] += 1
    bucket[classification] += 1


def summarize_appeal_records(rows: Iterable[dict], *, since, until):
    selected = []
    for original in rows or ():
        row = dict(original or {})
        if _is_group_record(row):
            continue
        appeal_time = _parse_datetime(row.get("appeal_time") or row.get("created_at"))
        if appeal_time is None:
            continue
        comparison_since = since
        comparison_until = until
        if appeal_time.tzinfo is not None and since.tzinfo is None:
            comparison_since = since.replace(tzinfo=appeal_time.tzinfo)
            comparison_until = until.replace(tzinfo=appeal_time.tzinfo)
        elif appeal_time.tzinfo is None and since.tzinfo is not None:
            appeal_time = appeal_time.replace(tzinfo=since.tzinfo)
        if comparison_since <= appeal_time < comparison_until:
            row["_appeal_time"] = appeal_time
            selected.append(row)

    executor_stats = defaultdict(_new_bucket)
    site_stats = defaultdict(_new_bucket)
    type_stats = defaultdict(_new_bucket)
    failures = []
    overall = _new_bucket()

    for row in selected:
        status = _execution_status(row)
        if status in SUCCESS_STATUSES:
            classification = "success"
        elif status in SKIPPED_STATUSES:
            classification = "no_data"
        else:
            classification = "failed"

        executor = _executor_label(row)
        site = str(row.get("site") or "站点未记录").strip() or "站点未记录"
        appeal_type = str(row.get("appeal_type") or "类型未记录").strip() or "类型未记录"
        for bucket in (
            overall,
            executor_stats[executor],
            site_stats[site],
            type_stats[appeal_type],
        ):
            _increment_bucket(bucket, classification)

        if classification == "failed":
            failures.append({
                "time": row["_appeal_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "executor": executor,
                "shop": str(row.get("shop_name") or "店铺未记录").strip(),
                "site": site,
                "appeal_type": appeal_type,
                "status": STATUS_LABELS.get(status, str(row.get("status") or status)),
                "reason": _failure_reason(row, status),
            })

    attempted = overall["success"] + overall["failed"]
    success_rate = (overall["success"] / attempted * 100) if attempted else None
    failures.sort(key=lambda item: item["time"], reverse=True)
    return {
        "since": since.strftime("%Y-%m-%d %H:%M:%S"),
        "until": until.strftime("%Y-%m-%d %H:%M:%S"),
        "overall": {
            **overall,
            "attempted": attempted,
            "success_rate": success_rate,
        },
        "executors": dict(sorted(executor_stats.items())),
        "sites": dict(sorted(site_stats.items())),
        "appeal_types": dict(sorted(type_stats.items())),
        "failures": failures,
    }


def _rate_text(bucket):
    attempted = int(bucket.get("success") or 0) + int(bucket.get("failed") or 0)
    if not attempted:
        return "无可计算执行"
    return f"{int(bucket.get('success') or 0) / attempted * 100:.1f}%"


def render_report(summary, *, truncated=False):
    overall = summary["overall"]
    lines = [
        "美客多自动申诉六小时执行报告",
        f"统计时段：{summary['since']} 至 {summary['until']}",
        "",
        "一、整体情况",
        f"站点级记录：{overall['total']} 条",
        f"实际执行：{overall['attempted']} 条",
        f"执行成功：{overall['success']} 条",
        f"执行失败/需处理：{overall['failed']} 条",
        f"无可申诉数据：{overall['no_data']} 条",
        f"执行成功率：{_rate_text(overall)}",
        "说明：成功表示 AI 客服已回复；不代表平台已经批准申诉。",
    ]
    if truncated:
        lines.append("数据提醒：数据库仅返回最新 500 条记录，本时段结果可能不完整。")

    lines.extend(("", "二、执行端"))
    if summary["executors"]:
        for executor, bucket in summary["executors"].items():
            lines.append(
                f"- {executor}：{bucket['total']} 条，成功 {bucket['success']} 条，"
                f"失败/需处理 {bucket['failed']} 条，无数据 {bucket['no_data']} 条，"
                f"成功率 {_rate_text(bucket)}"
            )
    else:
        lines.append("- 本时段没有自动申诉执行记录")

    lines.extend(("", "三、站点"))
    if summary["sites"]:
        for site, bucket in summary["sites"].items():
            lines.append(
                f"- {site}：{bucket['total']} 条，成功 {bucket['success']} 条，"
                f"失败/需处理 {bucket['failed']} 条，无数据 {bucket['no_data']} 条，"
                f"成功率 {_rate_text(bucket)}"
            )
    else:
        lines.append("- 本时段没有执行站点")

    lines.extend(("", "四、失败站点与原因"))
    failures = summary["failures"]
    if not failures:
        lines.append("- 本时段没有站点执行失败")
    else:
        for item in failures[:100]:
            lines.append(
                f"- {item['time']}｜{item['executor']}｜{item['shop']}｜"
                f"{item['site']}｜{item['appeal_type']}｜{item['status']}｜{item['reason']}"
            )
        if len(failures) > 100:
            lines.append(f"- 其余 {len(failures) - 100} 条失败记录已省略")
    return "\n".join(lines)


def load_recent_appeal_records(limit=DEFAULT_RECORD_LIMIT):
    from bit.bit_db_api import get_ai_appeal_records

    payload = get_ai_appeal_records(limit) or {}
    return list(payload.get("rows") or ()), int(payload.get("total") or 0)


def build_recent_report(*, hours=DEFAULT_REPORT_HOURS, now=None):
    now = now or datetime.now()
    hours = max(1, int(hours))
    rows, returned_total = load_recent_appeal_records(DEFAULT_RECORD_LIMIT)
    since = now - timedelta(hours=hours)
    summary = summarize_appeal_records(rows, since=since, until=now)
    known_times = [
        value
        for row in rows
        if (value := _parse_datetime(row.get("appeal_time") or row.get("created_at")))
    ]
    oldest_time = min(known_times) if known_times else None
    comparable_since = since
    if oldest_time is not None and oldest_time.tzinfo is not None and since.tzinfo is None:
        comparable_since = since.replace(tzinfo=oldest_time.tzinfo)
    truncated = bool(
        returned_total >= DEFAULT_RECORD_LIMIT
        and oldest_time is not None
        and oldest_time > comparable_since
    )
    return summary, render_report(summary, truncated=truncated)


def send_recent_report(*, hours=DEFAULT_REPORT_HOURS, recipient=DEFAULT_REPORT_RECIPIENT):
    from bit.bit_send_mail import send_info

    summary, body = build_recent_report(hours=hours)
    rate = _rate_text(summary["overall"])
    subject = (
        f"美客多自动申诉 {hours} 小时报告｜成功率 {rate}｜"
        f"失败 {summary['overall']['failed']} 条"
    )
    sent = bool(send_info(subject, body, receiver_email=recipient))
    return {"sent": sent, "recipient": recipient, "subject": subject, "summary": summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成或发送自动申诉执行报告")
    parser.add_argument("--hours", type=int, default=DEFAULT_REPORT_HOURS)
    parser.add_argument(
        "--recipient",
        default=os.environ.get("BIT_APPEAL_REPORT_RECIPIENT", DEFAULT_REPORT_RECIPIENT),
    )
    parser.add_argument("--send", action="store_true", help="通过现有 QQ SMTP 配置发送")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出执行结果")
    args = parser.parse_args(argv)

    if args.send:
        result = send_recent_report(hours=args.hours, recipient=args.recipient)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result["subject"])
            print("邮件已发送" if result["sent"] else "邮件发送失败")
        return 0 if result["sent"] else 1

    summary, body = build_recent_report(hours=args.hours)
    print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
