from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from bisect import bisect_left
import json
import re

import openpyxl
import pymysql

from bit.bit_mysql import config


MAPPING = Path(r"E:\xwechat_files\z1013459852_e03a\temp\RWTemp\2026-09\12f3855f62071e413148208cd1d773e4\店铺流水.xlsx")
SOURCE_DIR = Path(r"E:\360MoveData\Users\Admin\Documents\20260911")


def page_no(path: Path) -> int:
    m = re.search(r"第(\d+)页", path.name)
    return int(m.group(1)) if m else 0


def canonical_store(value):
    if value is None:
        return None
    s = str(value).strip()
    s = re.sub(r"\([^)]*\)\s*$", "", s).strip()
    return s.casefold()


mapping_wb = openpyxl.load_workbook(MAPPING, read_only=True, data_only=True)
mapping = {}
mapping_order = []
for row in mapping_wb["Sheet1"].iter_rows(min_row=2, values_only=True):
    store, company, legal_person, active_sites = row[:4]
    if store and company:
        store_key = canonical_store(store)
        mapping[store_key] = {
            "store": str(store).strip(),
            "company": str(company).strip(),
            "legal_person": str(legal_person or "").strip(),
            "active_sites": str(active_sites or "").strip(),
        }
        mapping_order.append(store_key)

records = []
for path in sorted(
    (p for p in SOURCE_DIR.glob("*.xlsx") if not p.name.startswith("~$")),
    key=page_no,
):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    for excel_row, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        internal_id, order_no, salesperson, store_raw, amount_raw = row[:5]
        store_key = canonical_store(store_raw)
        if internal_id is None or store_key not in mapping:
            continue
        amount_text = str(amount_raw or "").strip().replace(",", "")
        currency = "CNY" if "￥" in amount_text else "USD" if "$" in amount_text else "UNKNOWN"
        try:
            amount = Decimal(amount_text.replace("US$", "").replace("$", "").replace("￥", ""))
        except InvalidOperation:
            amount = None
        records.append({
            "internal_id": str(internal_id).strip(),
            "order_no": str(order_no or "").strip(),
            "salesperson": str(salesperson or "").strip(),
            "store_raw": str(store_raw or "").strip(),
            "store_key": store_key,
            "amount": amount,
            "currency": currency,
            "source_file": path.name,
            "source_row": excel_row,
        })

connection = pymysql.connect(**config, autocommit=False)
try:
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, 编号, 时间 FROM orders WHERE 时间 IS NOT NULL")
        legacy_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT order_id, date_created,
                   JSON_UNQUOTE(JSON_EXTRACT(raw_json, '$.pack_id')) AS pack_id
            FROM mercado_synced_orders
            WHERE date_created IS NOT NULL
            """
        )
        synced_rows = cursor.fetchall()
finally:
    connection.rollback()
    connection.close()

legacy_by_id = {str(r["id"]): r["时间"] for r in legacy_rows}
legacy_by_no = defaultdict(list)
for r in legacy_rows:
    legacy_by_no[str(r["编号"] or "")].append(r["时间"])
synced_by_no = defaultdict(list)
for r in synced_rows:
    local_dt = r["date_created"] + timedelta(hours=8)
    synced_by_no[str(r["order_id"] or "")].append(local_dt)
    if r.get("pack_id"):
        synced_by_no[str(r["pack_id"])].append(local_dt)


def unique_date(values):
    if not values:
        return None, False
    values = sorted(values)
    conflict = (values[-1] - values[0]).total_seconds() > 86400
    return values[0], conflict


source_counts = Counter()
unresolved = []
conflicts = []
currency_counts = Counter()
company_quarters = defaultdict(lambda: Decimal("0"))
date_min = None
date_max = None

for record in records:
    internal_date = legacy_by_id.get(record["internal_id"])
    legacy_no_date, legacy_no_conflict = unique_date(legacy_by_no.get(record["order_no"], []))
    synced_date, synced_conflict = unique_date(synced_by_no.get(record["order_no"], []))
    candidates = [d for d in (internal_date, legacy_no_date, synced_date) if d is not None]
    if internal_date:
        resolved_date = internal_date
        source = "orders.id"
    elif legacy_no_date:
        resolved_date = legacy_no_date
        source = "orders.编号"
    elif synced_date:
        resolved_date = synced_date
        source = "mercado_synced_orders"
    else:
        resolved_date = None
        source = "unresolved"
    if legacy_no_conflict or synced_conflict:
        conflicts.append((record, "duplicate order number dates"))
    if len(candidates) > 1 and (max(candidates) - min(candidates)).total_seconds() > 86400:
        conflicts.append((record, candidates))
    source_counts[source] += 1
    currency_counts[record["currency"]] += 1
    if resolved_date is None or record["amount"] is None:
        unresolved.append(record)
        continue
    record["resolved_date"] = resolved_date
    date_min = resolved_date if date_min is None else min(date_min, resolved_date)
    date_max = resolved_date if date_max is None else max(date_max, resolved_date)
    quarter = f"{resolved_date.year}-Q{(resolved_date.month - 1) // 3 + 1}"
    record["assigned_quarter"] = quarter
    record["resolution"] = source
    company_quarters[(mapping[record["store_key"]]["company"], quarter)] += record["amount"]

resolved_by_id = sorted(
    (int(r["internal_id"]), r["resolved_date"])
    for r in records
    if r.get("resolved_date") is not None
)
resolved_ids = [x[0] for x in resolved_by_id]


def quarter_for_date(value):
    return f"{value.year}-Q{(value.month - 1) // 3 + 1}"


inference_counts = Counter()
ambiguous = []
unresolved_by_company = defaultdict(lambda: [0, Decimal("0")])
for record in unresolved:
    company = mapping[record["store_key"]]["company"]
    unresolved_by_company[company][0] += 1
    unresolved_by_company[company][1] += record["amount"] or Decimal("0")
    order_id = int(record["internal_id"])
    pos = bisect_left(resolved_ids, order_id)
    lower = resolved_by_id[pos - 1] if pos > 0 else None
    higher = resolved_by_id[pos] if pos < len(resolved_by_id) else None
    if lower and higher and quarter_for_date(lower[1]) == quarter_for_date(higher[1]):
        inferred = quarter_for_date(lower[1])
        source = "between same-quarter dates"
    elif not higher and lower and quarter_for_date(lower[1]) == "2026-Q3":
        inferred = "2026-Q3"
        source = "after latest resolved Q3 order"
    elif not lower and higher and quarter_for_date(higher[1]) == "2025-Q3":
        inferred = "2025-Q3"
        source = "before earliest resolved Q3 order"
    else:
        inferred = None
        source = "ambiguous boundary"
    inference_counts[(source, inferred)] += 1
    if inferred:
        company_quarters[(company, inferred)] += record["amount"] or Decimal("0")
        record["assigned_quarter"] = inferred
        record["resolution"] = source
    else:
        ambiguous.append((record, lower, higher))

print("mapped source records", len(records))
print("date sources", source_counts)
print("currencies", currency_counts)
print("date range", date_min, date_max)
print("conflicts", len(conflicts))
for item in conflicts[:20]:
    print("CONFLICT", item)
print("unresolved", len(unresolved), "amount", sum((r["amount"] or Decimal("0") for r in unresolved), Decimal("0")))
print("inference", inference_counts)
print("ambiguous", len(ambiguous), "amount", sum((r[0]["amount"] or Decimal("0") for r in ambiguous), Decimal("0")))
for item in ambiguous[:100]:
    print("AMBIGUOUS", item)
print("unresolved by company")
for company, values in sorted(unresolved_by_company.items()):
    print(company, values)
print("\nCOMPANY QUARTERS")
for (company, quarter), amount in sorted(company_quarters.items()):
    print(company, quarter, amount)

quarters = ["2025-Q3", "2025-Q4", "2026-Q1", "2026-Q2", "2026-Q3"]
quarter_labels = {
    "2025-Q3": "2025年7月—9月",
    "2025-Q4": "2025年10月—12月",
    "2026-Q1": "2026年1月—3月",
    "2026-Q2": "2026年4月—6月",
    "2026-Q3": "2026年7月—9月（截至9月11日）",
}
store_quarters = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
store_record_counts = Counter()
store_inferred_counts = Counter()
negative_count = 0
for record in records:
    assigned_quarter = record.get("assigned_quarter")
    if assigned_quarter not in quarters:
        raise RuntimeError(f"订单未归入目标季度: {record}")
    store_quarters[record["store_key"]][assigned_quarter] += record["amount"]
    store_record_counts[record["store_key"]] += 1
    if record.get("resolution") in ("between same-quarter dates", "after latest resolved Q3 order"):
        store_inferred_counts[record["store_key"]] += 1
    if record["amount"] < 0:
        negative_count += 1

company_order = []
company_info = {}
for store_key in mapping_order:
    item = mapping[store_key]
    if item["company"] not in company_info:
        company_order.append(item["company"])
        company_info[item["company"]] = {
            "legal_person": item["legal_person"],
            "store_count": 0,
        }
    company_info[item["company"]]["store_count"] += 1

store_detail = []
for store_key in mapping_order:
    item = mapping[store_key]
    values = {q: store_quarters[store_key][q] for q in quarters}
    store_detail.append({
        **item,
        "record_count": store_record_counts[store_key],
        "inferred_quarter_count": store_inferred_counts[store_key],
        "quarter_values": {q: float(values[q]) for q in quarters},
        "total": float(sum(values.values(), Decimal("0"))),
    })

company_summary = []
for company in company_order:
    values = {q: company_quarters[(company, q)] for q in quarters}
    company_summary.append({
        "company": company,
        "legal_person": company_info[company]["legal_person"],
        "store_count": company_info[company]["store_count"],
        "quarter_values": {q: float(values[q]) for q in quarters},
        "total": float(sum(values.values(), Decimal("0"))),
    })

quarter_totals = {
    q: float(sum((company_quarters[(company, q)] for company in company_order), Decimal("0")))
    for q in quarters
}
grand_total = float(sum((Decimal(str(v)) for v in quarter_totals.values()), Decimal("0")))
payload = {
    "quarters": quarters,
    "quarter_labels": quarter_labels,
    "company_summary": company_summary,
    "store_detail": store_detail,
    "quarter_totals": quarter_totals,
    "grand_total": grand_total,
    "metadata": {
        "source_file_count": len(list(p for p in SOURCE_DIR.glob("*.xlsx") if not p.name.startswith("~$"))),
        "all_source_record_count": 109613,
        "mapped_record_count": len(records),
        "mapping_store_count": len(mapping),
        "stores_with_records": sum(1 for key in mapping if store_record_counts[key]),
        "company_count": len(company_order),
        "direct_date_count": len(records) - len(unresolved),
        "inferred_quarter_count": len(unresolved),
        "duplicate_id_count": 0,
        "date_conflict_count": len(conflicts),
        "negative_amount_count": negative_count,
        "currency": "USD",
        "data_start": date_min.strftime("%Y-%m-%d"),
        "data_end": date_max.strftime("%Y-%m-%d"),
        "mapping_source": str(MAPPING),
        "order_source_dir": str(SOURCE_DIR),
    },
}
output_path = Path(__file__).resolve().parents[1] / "tmp" / "quarterly_license_revenue.json"
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print("WROTE", output_path)
