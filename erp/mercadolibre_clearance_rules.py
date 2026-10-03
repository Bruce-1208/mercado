"""Conservative, side-effect-free evaluation of semi-managed SKU clearance.

The sales rules are an OR; the exemptions are also an OR.  A positive risk
requires complete sales windows and evidence excluding every exemption.
Dates without a timezone use the merchant-facing Asia/Shanghai calendar.
Platform workflow dates are explicit facts, never inferred from risk history.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


MERCHANT_TIMEZONE = ZoneInfo("Asia/Shanghai")
STATUS_LABELS = {
    "not_applicable": "不适用",
    "normal": "未触发",
    "exempt": "已豁免",
    "pending_data": "数据待补全",
    "risk": "清退告警",
}


def is_semi_managed_group(group_name: object) -> bool:
    """Identify configured semi-managed groups, never by a store's name.

    Group names such as ``半托管一组`` are supported.  Explicit negative
    labels (including ``非 半托管`` and ``非-半托管``) are excluded.
    """
    name = str(group_name or "").strip()
    return "半托管" in name and not re.search(r"非[\s_\-]*半托管", name)


def _now_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    # The evaluator's now argument represents UTC, including naive test clocks.
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_date(value: object) -> datetime | None:
    """Parse ISO dates/datetimes; date-only and naive values use Shanghai."""
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, time.min)
        elif isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MERCHANT_TIMEZONE)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _count(value: object) -> int | None:
    # bool is an int subclass, but is not an observed quantity.
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def evaluate_clearance(row: Mapping, now: datetime | None = None) -> dict:
    """Return a JSON-safe assessment for one SKU.

    Required evidence is supplied in ``row``: group_name, status,
    available_quantity, sales_7d, sales_14d, sales_complete, visits_30d,
    visits_complete, first_inbound_at, inbound_history_confirmed,
    restock_after_stockout and last_restock_at.  Unknown or invalid values
    remain unknown; zero counts are not used as substitutes for missing data.

    Reasons and details are Chinese strings suitable for displaying directly;
    missing_fields contains input field names.  A proven exemption is enough
    even if other sales/exemption evidence is missing.  Unknown inventory
    always stays pending, because eligibility itself has not been established.
    """
    clock = _now_utc(now)
    result = {
        "status": "pending_data",
        "status_label": STATUS_LABELS["pending_data"],
        "trigger_reasons": [],
        "exemption_reasons": [],
        "missing_fields": [],
        "summary": "",
        "details": [],
        "checked_at": _iso(clock),
    }

    def finish(status: str, summary: str) -> dict:
        result["status"] = status
        result["status_label"] = STATUS_LABELS[status]
        result["summary"] = summary
        return result

    def missing(field: str, detail: str = "") -> None:
        if field not in result["missing_fields"]:
            result["missing_fields"].append(field)
        if detail:
            result["details"].append(detail)

    if not is_semi_managed_group(row.get("group_name")):
        return finish("not_applicable", "仅对已归入半托管分组的店铺判断清退风险")
    # A failed live inventory read may leave an inactive/OOS cached listing.
    # That cache is not evidence that the current SKU is outside the rule.
    if "inventory_complete" in row and row.get("inventory_complete") is not True:
        missing("inventory_complete", "当前平台商品状态及库存读取尚未确认完整")
        return finish("pending_data", "当前平台状态及库存未确认，暂无法判断清退风险")
    if str(row.get("status") or "").strip().lower() != "active":
        return finish("not_applicable", "商品当前不在售，不适用在售商品清退规则")

    quantity = _count(row.get("available_quantity"))
    if quantity == 0:
        return finish("not_applicable", "商品当前无可售库存，不进入在售库存清退判断")
    if quantity is None:
        missing("available_quantity", "可售库存尚未确认")

    sales_7d = _count(row.get("sales_7d"))
    sales_14d = _count(row.get("sales_14d"))
    sales_confirmed = row.get("sales_complete") is True
    if not sales_confirmed:
        missing("sales_complete", "近 7 天及近 14 天销量采集尚未确认完整")
    if sales_7d is None:
        missing("sales_7d", "缺少有效的近 7 天销量")
    if sales_14d is None:
        missing("sales_14d", "缺少有效的近 14 天销量")
    consistent_sales = not (sales_7d is not None and sales_14d is not None and sales_7d > sales_14d)
    if not consistent_sales:
        missing("sales_7d")
        missing("sales_14d", "近 7 天销量大于近 14 天销量，需核实销售窗口数据")
    reliable_sales = sales_confirmed and sales_7d is not None and sales_14d is not None and consistent_sales
    if sales_confirmed and consistent_sales:
        if sales_7d == 0:
            result["trigger_reasons"].append("零销售：过去 7 天销量为 0 件")
        if sales_14d is not None and sales_14d < 7:
            result["trigger_reasons"].append(f"低动销：过去 14 天销量 {sales_14d} 件，低于 7 件")
    if reliable_sales:
        result["details"].append(f"过去 7 天销量 {sales_7d} 件；过去 14 天销量 {sales_14d} 件")
    if quantity is not None and reliable_sales and not result["trigger_reasons"]:
        return finish("normal", "近 7 天及近 14 天销量均未触发清退条件")

    first_inbound = _parse_date(row.get("first_inbound_at"))
    if row.get("inbound_history_confirmed") is not True:
        missing("inbound_history_confirmed", "首次入仓历史尚未核实，不能排除新品期豁免")
    if first_inbound is None or first_inbound > clock:
        missing("first_inbound_at", "首次入仓日期缺失、无效或晚于判断时间")
    elif row.get("inbound_history_confirmed") is True:
        inbound_age = clock - first_inbound
        if inbound_age < timedelta(days=21):
            result["exemption_reasons"].append("新品期：首次入仓未满 21 天")
        else:
            result["details"].append("已核实首次入仓满 21 天，不适用新品期豁免")

    restock = row.get("restock_after_stockout")
    if restock is True:
        last_restock = _parse_date(row.get("last_restock_at"))
        if last_restock is None or last_restock > clock:
            missing("last_restock_at", "缺货后补货入仓日期缺失、无效或晚于判断时间")
        elif clock - last_restock < timedelta(days=14):
            result["exemption_reasons"].append("新入库：缺货后的补货批次入仓未满 14 天")
        else:
            result["details"].append("缺货后的补货批次入仓已满 14 天，不适用新入库豁免")
    elif restock is False:
        result["details"].append("已确认不存在缺货后补货的入仓豁免")
    else:
        missing("restock_after_stockout", "缺货后补货记录尚未确认")

    visits = _count(row.get("visits_30d"))
    if row.get("visits_complete") is not True:
        missing("visits_complete", "过去 30 天访问量采集尚未确认完整")
    if visits is None:
        missing("visits_30d", "缺少有效的过去 30 天累计访问量")
    elif row.get("visits_complete") is True:
        result["details"].append(f"过去 30 天累计访问 {visits} 次，日均 {visits / 30:.2f} 次")
        if visits <= 300:
            result["exemption_reasons"].append("低曝光：过去 30 天日均访问量不超过 10")

    if quantity is None:
        return finish("pending_data", "可售库存未确认，暂无法判断是否适用清退规则")
    if result["exemption_reasons"]:
        return finish("exempt", "满足清退豁免条件，当前不进入清退流程")
    if not reliable_sales or result["missing_fields"]:
        return finish("pending_data", "判断所需数据待补全，暂不能确认清退风险")
    return finish("risk", "触发清退条件且无已满足的豁免，应核实平台通知并处理")


# A descriptive alias for callers that prefer the rule-specific name.
evaluate_clearance_rules = evaluate_clearance


def evaluate_workflow(facts: Mapping, alert_status: str, now: datetime | None = None) -> dict:
    """Compute deadlines only from confirmed platform dates.

    ``notice_at`` gives a 3-day merchant-choice window, ``promotion_start_at``
    a 21-day promotion period, and ``return_order_at`` a 5-day pickup window.
    Actual return orders take priority even when sales have recovered.  No
    return order, promotion or notice is synthesized from a risk assessment.
    Invalid dates are ignored; dates claiming a future notice/order are ignored.
    Future promotion start dates are accepted as a platform schedule.
    ``days_remaining`` is the ceiling of exact time remaining and can be negative.
    """
    clock = _now_utc(now)
    notice = _parse_date(facts.get("notice_at"))
    promotion = _parse_date(facts.get("promotion_start_at"))
    return_order = _parse_date(facts.get("return_order_at"))
    if notice is not None and notice > clock:
        notice = None
    if return_order is not None and return_order > clock:
        return_order = None

    def response(stage: str, summary: str, deadline: datetime | None = None) -> dict:
        return {
            "stage": stage,
            "summary": summary,
            "deadline_at": _iso(deadline) if deadline is not None else None,
            "days_remaining": math.ceil((deadline - clock).total_seconds() / 86400) if deadline is not None else None,
        }

    if return_order is not None:
        deadline = return_order + timedelta(days=5)
        if clock >= deadline:
            return response("pickup_overdue", "平台退仓单提货期限已到，逾期未提有销毁风险，请立即核实并处理", deadline)
        return response("pickup_due", "平台已下发退仓单，需在下发后 5 天内完成提货", deadline)

    recovered = alert_status in {"normal", "exempt"}
    recovery_note = "；当前销量已达标或满足豁免，请向平台核实是否退出清仓" if recovered else ""
    if promotion is not None and promotion <= clock:
        deadline = promotion + timedelta(days=21)
        if clock >= deadline:
            if alert_status == "risk":
                return response("promotion_due", "促销期满，核实平台退仓单；收到实际退仓单后再计算提货期限", deadline)
            if recovered:
                return response("promotion_complete", "促销期满，当前销量已达标或满足豁免，请核实平台退出清仓状态", deadline)
            return response("promotion_review", "促销期满，判断数据待核实，请核实平台清仓处理结果", deadline)
        return response("promotion", "平台折扣促销进行中，可选择退出促销并退仓" + recovery_note, deadline)

    if notice is not None:
        deadline = notice + timedelta(days=3)
        if clock >= deadline:
            return response("notice_due", "平台通知后的 3 天自主退仓选择期已到，请核实商品是否进入促销" + recovery_note, deadline)
        return response("notice", "已收到平台通知，可在收到邮件后 3 天内选择自主退仓" + recovery_note, deadline)
    if promotion is not None:
        return response("promotion_scheduled", "平台已记录促销开始时间，等待促销开始" + recovery_note, promotion)
    if alert_status == "risk":
        return response("none", "尚未记录平台通知，告警本身不代表平台已启动清退流程")
    return response("none", "尚未记录平台清退通知或退仓单")
