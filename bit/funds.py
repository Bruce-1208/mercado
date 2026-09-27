"""Funds view totals and validation for append-only manual snapshots."""
import re
from datetime import datetime
from decimal import Decimal


def summarize_funds(data, group_name=""):
    from bit.bit_mysql import _parse_currency_decimal
    result = dict(data or {})
    rows = result.get("rows") or []
    result["owners"] = sorted({row.get("店铺归属人") or "未分配" for row in rows})
    result["groups"] = sorted({row.get("店铺组") or "未分组" for row in rows})
    rows = [row for row in rows if not group_name or (row.get("店铺组") or "未分组") == group_name]
    result.update(rows=rows, total=len(rows), shop_total=len({row["店铺名"] for row in rows}),
                  latest_submit_time=max((str(row.get("提交时间") or "") for row in rows), default=""))
    for output, field in (("released_total", "已释放美元"), ("pending_total", "未释放美元")):
        result[output] = f'{sum((_parse_currency_decimal(row.get(field)) for row in rows), Decimal("0")):,.2f}'
    return result


def manual_funds_row(data, visible_rows):
    shop = str(data.get("shop_name") or "").strip()
    site = str(data.get("site") or "").strip()
    if not any(row.get("店铺名") == shop and row.get("站点") == site for row in visible_rows):
        raise ValueError("店铺或站点不存在，或没有操作权限")
    amounts = []
    for key in ("released", "pending"):
        value = str(data.get(key, "")).strip()
        if not re.fullmatch(r"-?\d{1,12}(?:\.\d{1,2})?", value):
            raise ValueError("请填写有效美元金额，最多两位小数；无金额请填 0")
        amounts.append(f"{Decimal(value):.2f}")
    return [shop, site, *amounts, "手动更新", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "手动录入/修改"]
