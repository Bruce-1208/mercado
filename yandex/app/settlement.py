from __future__ import annotations

import io
import json
import zipfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any


def _rows_from_json(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("rows", "items", "data", "result", "records"):
        nested = value.get(key)
        rows = _rows_from_json(nested)
        if rows:
            return rows
    if value and all(not isinstance(item, (dict, list)) for item in value.values()):
        return [value]
    for nested in value.values():
        rows = _rows_from_json(nested)
        if rows:
            return rows
    return []


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def parse_payment_report_archive(payload: bytes, cached_order_ids: set[str] | None = None) -> dict[str, Any]:
    """Parse Yandex's JSON ZIP report while tolerating added sheets and columns."""
    cached = cached_order_ids or set()
    files: list[dict[str, Any]] = []
    by_order: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"order_id": "", "transaction_sum": Decimal(0), "line_count": 0, "sources": set()}
    )
    payment_status_totals: dict[str, Decimal] = defaultdict(Decimal)
    bank_orders: dict[str, dict[str, Any]] = {}
    parsed_sheets: list[tuple[str, list[dict[str, Any]]]] = []

    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("Yandex 返回的结算报表不是有效 JSON ZIP 文件") from exc

    with archive:
        entries = [entry for entry in archive.infolist() if not entry.is_dir() and entry.filename.lower().endswith(".json")]
        if not entries:
            raise ValueError("结算报表中没有 JSON 数据表")
        for entry in entries:
            if entry.file_size > 50 * 1024 * 1024:
                raise ValueError("结算报表单个数据表超过 50 MB")
            try:
                decoded = json.loads(archive.read(entry).decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"无法解析结算报表数据表：{entry.filename}") from exc
            rows = _rows_from_json(decoded)
            parsed_sheets.append((entry.filename.rsplit("/", 1)[-1], rows))
            file_sum = Decimal(0)
            preview: list[dict[str, Any]] = []
            for row in rows:
                amount = _decimal(row.get("transactionSum", row.get("TRANSACTION_SUM")))
                file_sum += amount
                if len(preview) < 250:
                    preview.append(row)
            files.append({
                "name": entry.filename.rsplit("/", 1)[-1],
                "row_count": len(rows),
                "transaction_sum": float(file_sum),
                "rows": preview,
            })

    payment_sheets = [
        (name, rows) for name, rows in parsed_sheets
        if name.lower() == "transaction_date.json"
    ]
    summary_sheets = payment_sheets or parsed_sheets
    for name, rows in summary_sheets:
        for row in rows:
            amount = _decimal(row.get("transactionSum", row.get("TRANSACTION_SUM")))
            status = str(row.get("paymentStatus", row.get("PAYMENT_STATUS", "未分类")) or "未分类")
            payment_status_totals[status] += amount
            order_id = str(row.get("orderId", row.get("ORDER_ID", "")) or "").strip()
            if order_id:
                aggregate = by_order[order_id]
                aggregate["order_id"] = order_id
                aggregate["transaction_sum"] += amount
                aggregate["line_count"] += 1
                aggregate["sources"].add(name)
            bank_order_id = str(row.get("bankOrderId", row.get("BANK_ORDER_ID", "")) or "").strip()
            bank_sum = row.get("bankSum", row.get("BANK_SUM"))
            if bank_order_id and bank_sum not in (None, ""):
                bank_orders.setdefault(bank_order_id, {
                    "bank_order_id": bank_order_id,
                    "date": row.get("bankOrderDate", row.get("BANK_ORDER_DATE", "")),
                    "sum": float(_decimal(bank_sum)),
                })

    summary_line_count = sum(len(rows) for _, rows in summary_sheets)
    summary_transaction_sum = sum(
        (_decimal(row.get("transactionSum", row.get("TRANSACTION_SUM"))) for _, rows in summary_sheets for row in rows),
        Decimal(0),
    )

    order_rows = []
    for order_id, item in by_order.items():
        order_rows.append({
            "order_id": order_id,
            "transaction_sum": float(item["transaction_sum"]),
            "line_count": item["line_count"],
            "sources": sorted(item["sources"]),
            "cached": order_id in cached,
        })
    order_rows.sort(key=lambda item: item["order_id"], reverse=True)
    return {
        "files": files,
        "stats": {
            "line_count": summary_line_count,
            "transaction_sum": float(summary_transaction_sum),
            "summary_source": "transaction_date.json" if payment_sheets else "all_data_sheets_fallback",
            "order_count": len(order_rows),
            "cached_order_count": sum(1 for item in order_rows if item["cached"]),
            "uncached_order_count": sum(1 for item in order_rows if not item["cached"]),
            "bank_order_count": len(bank_orders),
            "bank_orders": list(bank_orders.values())[:500],
            "payment_status_totals": [
                {"status": key, "transaction_sum": float(value)}
                for key, value in sorted(payment_status_totals.items())
            ],
            "orders": order_rows[:1000],
        },
    }
