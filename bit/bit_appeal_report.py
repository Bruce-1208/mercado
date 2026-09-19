"""Build and email a six-hour execution report for automatic appeals."""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Iterable

from bit.bit_appeal_state import STATUS_LABELS, SUCCESS_STATUSES


DEFAULT_REPORT_HOURS = 6
DEFAULT_REPORT_RECIPIENT = "1013459852@qq.com"
DEFAULT_RECORD_LIMIT = 500
DEFAULT_SEND_HOURS = (10, 14)
CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
SKIPPED_STATUSES = frozenset(("no_data",))
GROUP_APPEAL_MARKER = "组"
_scheduler_guard = threading.Lock()
_scheduler_thread = None
_scheduler_stop_event = threading.Event()


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
    return {"total": 0, "success": 0, "failed": 0, "no_data": 0, "ai_sent": 0}


def _increment_bucket(bucket, classification, ai_sent=0):
    bucket["total"] += 1
    bucket[classification] += 1
    bucket["ai_sent"] += max(0, int(ai_sent or 0))


def _confirmed_send_count(row, status):
    execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
    metrics = execution.get("metrics") if isinstance(execution.get("metrics"), dict) else {}
    value = metrics.get("sent_confirmed")
    try:
        if value is not None:
            return max(0, int(value))
    except (TypeError, ValueError):
        pass
    return 1 if status in SUCCESS_STATUSES else 0


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
    login_failures = []
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
        ai_sent = _confirmed_send_count(row, status)
        for bucket in (
            overall,
            executor_stats[executor],
            site_stats[site],
            type_stats[appeal_type],
        ):
            _increment_bucket(bucket, classification, ai_sent)

        if classification == "failed":
            failure = {
                "time": row["_appeal_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "executor": executor,
                "shop": str(row.get("shop_name") or "店铺未记录").strip(),
                "site": site,
                "appeal_type": appeal_type,
                "status": STATUS_LABELS.get(status, str(row.get("status") or status)),
                "reason": _failure_reason(row, status),
            }
            failures.append(failure)
            if status == "login_required":
                login_failures.append(failure)

    attempted = overall["success"] + overall["failed"]
    success_rate = (overall["success"] / attempted * 100) if attempted else None
    failures.sort(key=lambda item: item["time"], reverse=True)
    login_failures.sort(key=lambda item: item["time"], reverse=True)
    return {
        "since": since.strftime("%Y-%m-%d %H:%M:%S"),
        "until": until.strftime("%Y-%m-%d %H:%M:%S"),
        "overall": {
            **overall,
            "attempted": attempted,
            "success_rate": success_rate,
        },
        "executors": dict(sorted(
            executor_stats.items(), key=lambda item: (-item[1]["failed"], item[0])
        )),
        "sites": dict(sorted(
            site_stats.items(),
            key=lambda item: (-item[1]["ai_sent"], -item[1]["total"], item[0]),
        )),
        "appeal_types": dict(sorted(
            type_stats.items(),
            key=lambda item: (-item[1]["ai_sent"], -item[1]["total"], item[0]),
        )),
        "failures": failures,
        "login_failures": login_failures,
    }


def summarize_current_infractions(payload, site_names=None):
    """Aggregate the current API infringement snapshot by Mercado site."""
    site_names = dict(site_names or {})
    sites = defaultdict(lambda: {"infraction": 0, "rights_holder": 0, "total": 0})
    for key, value in dict((payload or {}).get("counts") or {}).items():
        site_id = str(key[1] if isinstance(key, tuple) and len(key) > 1 else key).upper()
        if not site_id:
            continue
        bucket = sites[f"{site_names.get(site_id, site_id)}（{site_id}）"]
        bucket["infraction"] += int((value or {}).get("infraction_count") or 0)
        bucket["rights_holder"] += int((value or {}).get("rights_holder_count") or 0)
        bucket["total"] = bucket["infraction"] + bucket["rights_holder"]
    ordered = dict(sorted(sites.items(), key=lambda item: (-item[1]["total"], item[0])))
    return {
        "last_synced_at": (payload or {}).get("last_synced_at"),
        "total": sum(item["total"] for item in ordered.values()),
        "sites": ordered,
    }


def load_current_infraction_summary():
    from bit.bit_db_api import get_current_infraction_counts_by_token_site
    from erp.mercadolibre_infraction_store import SITE_NAMES

    payload = get_current_infraction_counts_by_token_site(days=100) or {}
    return summarize_current_infractions(payload, SITE_NAMES)


def _rate_text(bucket):
    attempted = int(bucket.get("success") or 0) + int(bucket.get("failed") or 0)
    if not attempted:
        return "无可计算执行"
    return f"{int(bucket.get('success') or 0) / attempted * 100:.1f}%"


def render_report(summary, *, truncated=False):
    overall = summary["overall"]
    infractions = summary.get("infractions") or {}
    lines = [
        "美客多自动申诉六小时执行报告",
        f"统计时段：{summary['since']} 至 {summary['until']}",
        "",
        "【一眼看懂】",
        f"当前待处理侵权：{infractions.get('total', '读取失败')} 个",
        f"AI 确认发送成功：{overall['ai_sent']} 次",
        f"登录失效：{len(summary.get('login_failures') or [])} 个站点任务",
        f"其他失败/需处理：{max(0, overall['failed'] - len(summary.get('login_failures') or []))} 个站点任务",
        f"无可申诉数据：{overall['no_data']} 个站点任务",
        "",
        "一、当前待处理侵权（按总数从多到少）",
    ]
    if infractions.get("error"):
        lines.append(f"- 读取失败：{infractions['error']}")
    elif infractions.get("sites"):
        for site, bucket in infractions["sites"].items():
            lines.append(
                f"- {site}：{bucket['total']} 个（普通侵权 {bucket['infraction']}，"
                f"权利人案件 {bucket['rights_holder']}）"
            )
        if infractions.get("last_synced_at"):
            lines.append(f"- 快照最近同步：{infractions['last_synced_at']}")
    else:
        lines.append("- 当前没有待处理侵权")

    lines.extend((
        "",
        "二、最近 6 小时 AI 申诉发送（按成功次数从多到少）",
        f"站点任务：{overall['total']} 个；实际执行 {overall['attempted']} 个；"
        f"执行成功率：{_rate_text(overall)}",
        "说明：成功表示申诉话术已成功发送；不代表平台已经批准申诉。",
    ))
    if truncated:
        lines.append("数据提醒：数据库仅返回最新 500 条记录，本时段结果可能不完整。")
    if summary["sites"]:
        for site, bucket in summary["sites"].items():
            lines.append(
                f"- {site}：AI 发送成功 {bucket['ai_sent']} 次；站点任务 {bucket['total']} 个，"
                f"失败/需处理 {bucket['failed']} 个，无数据 {bucket['no_data']} 个"
            )
    else:
        lines.append("- 本时段没有执行站点")

    lines.extend(("", "三、登录失效（优先处理）"))
    login_failures = summary.get("login_failures") or []
    if not login_failures:
        lines.append("- 本时段没有登录失效")
    else:
        for item in login_failures[:100]:
            lines.append(
                f"- {item['time']}｜{item['executor']}｜{item['shop']}｜"
                f"{item['site']}｜{item['reason']}"
            )

    lines.extend(("", "四、其他申诉失败/需处理"))
    failures = [item for item in summary["failures"] if item not in login_failures]
    if not failures:
        lines.append("- 本时段没有其他申诉失败")
    else:
        for item in failures[:100]:
            lines.append(
                f"- {item['time']}｜{item['executor']}｜{item['shop']}｜"
                f"{item['site']}｜{item['appeal_type']}｜{item['status']}｜{item['reason']}"
            )
        if len(failures) > 100:
            lines.append(f"- 其余 {len(failures) - 100} 条失败记录已省略")

    lines.extend(("", "五、执行主机汇总（失败数从多到少）"))
    if not summary["executors"]:
        lines.append("- 本时段没有执行主机记录")
    else:
        for executor, bucket in summary["executors"].items():
            lines.append(
                f"- {executor}：AI 发送成功 {bucket['ai_sent']} 次；"
                f"失败/需处理 {bucket['failed']} 个，无数据 {bucket['no_data']} 个"
            )
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
    try:
        summary["infractions"] = load_current_infraction_summary()
    except Exception as exc:
        logging.exception("读取申诉报告侵权快照失败")
        summary["infractions"] = {"total": None, "sites": {}, "error": str(exc)}
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


def _configured_send_hours(value=None):
    value = os.environ.get("BIT_APPEAL_REPORT_SEND_HOURS", "10,14") if value is None else value
    hours = set()
    for part in str(value or "").split(","):
        try:
            hour = int(part.strip())
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            hours.add(hour)
    return tuple(sorted(hours)) or DEFAULT_SEND_HOURS


def next_report_run(now=None, send_hours=None):
    now = now or datetime.now(CHINA_TIMEZONE)
    hours = tuple(send_hours or _configured_send_hours())
    for day_offset in (0, 1):
        day = (now + timedelta(days=day_offset)).date()
        for hour in hours:
            candidate = datetime.combine(day, datetime_time(hour=hour), tzinfo=now.tzinfo)
            if candidate > now:
                return candidate
    raise RuntimeError("无法计算下一次申诉报告发送时间")


def _report_scheduler_loop(stop_event=None):
    stop_event = stop_event or _scheduler_stop_event
    recipient = str(
        os.environ.get("BIT_APPEAL_REPORT_RECIPIENT", DEFAULT_REPORT_RECIPIENT)
    ).strip() or DEFAULT_REPORT_RECIPIENT
    while not stop_event.is_set():
        now = datetime.now(CHINA_TIMEZONE)
        scheduled_at = next_report_run(now)
        wait_seconds = max(0.0, (scheduled_at - now).total_seconds())
        logging.info("下一封申诉汇总邮件计划于 %s 发送", scheduled_at.isoformat())
        if stop_event.wait(wait_seconds):
            break
        try:
            result = send_recent_report(hours=DEFAULT_REPORT_HOURS, recipient=recipient)
            if result["sent"]:
                logging.info("申诉汇总邮件已发送至 %s", recipient)
            else:
                logging.error("申诉汇总邮件发送失败：%s", recipient)
        except Exception:
            logging.exception("生成或发送申诉汇总邮件失败")


def start_appeal_report_scheduler():
    global _scheduler_thread
    enabled = str(os.environ.get("BIT_APPEAL_REPORT_ENABLED", "1")).strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return False
    with _scheduler_guard:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return True
        _scheduler_stop_event.clear()
        _scheduler_thread = threading.Thread(
            target=_report_scheduler_loop,
            name="appeal-summary-email-scheduler",
            daemon=True,
        )
        _scheduler_thread.start()
        return True


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
