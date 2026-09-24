"""Read a Zhiying export and retrieve validated shipment measurements.

Writes JSON for spreadsheet authoring. Never reads the exported weight/size fields.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bit import bit_mysql
from mercado_api.client import MercadoLibreClient
from scripts.zying_official_measurements_api import _refresh_if_needed, validated_measurement


def clean_id(value):
    if value is None:
        return ""
    value = str(value).strip()
    return value[:-2] if value.endswith(".0") and value[:-2].isdigit() else value


def read_source(path):
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = book.active.values
        headers = next(rows)
        columns = {str(name).strip(): i for i, name in enumerate(headers)}
        required = ("id", "编号", "产品id", "公司店铺")
        if any(key not in columns for key in required):
            raise ValueError("导出表缺少 id、编号、产品id 或公司店铺列")
        records = []
        for row in rows:
            sales = clean_id(row[columns["id"]])
            order = clean_id(row[columns["编号"]])
            if not sales or not order:
                continue
            records.append({
                "sales_number": sales,
                "order_number": order,
                "product_number": clean_id(row[columns["产品id"]]),
                "store_name": str(row[columns["公司店铺"]] or "").strip(),
            })
        return records
    finally:
        book.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()
    records = read_source(args.input)
    stores = {}
    for store in bit_mysql.list_mercado_store_tokens()["rows"]:
        if store.get("enabled"):
            stores.setdefault(str(store["display_name"]).strip(), []).append(int(store["id"]))

    clients = {}
    cache = {}
    counts = Counter()
    for i, record in enumerate(records, 1):
        store_name = re.sub(r"\([^)]*\)$", "", record["store_name"]).strip()
        token_ids = stores.get(store_name, [])
        if len(token_ids) != 1:
            record["status"] = "store_not_connected" if not token_ids else "ambiguous_store"
            counts[record["status"]] += 1
            continue
        token_id = token_ids[0]
        key = (token_id, record["order_number"])
        try:
            if token_id not in clients:
                token = _refresh_if_needed(bit_mysql.get_mercado_store_token(token_id))
                clients[token_id] = MercadoLibreClient(str(token["access_token"]))
            client = clients[token_id]
            if key not in cache:
                pack = client.request("GET", f'/marketplace/orders/pack/{record["order_number"]}')
                if str(pack.get("id")) != record["order_number"]:
                    raise ValueError("包裹号不匹配")
                shipment_id = clean_id((pack.get("shipment") or {}).get("id"))
                if not shipment_id:
                    raise ValueError("包裹没有运单号")
                data = client.request(
                    "GET", f"/marketplace/shipments/{shipment_id}/compensation_costs",
                    params={"weight_unit": "g", "dimensions_unit": "cm"},
                )
                package = data.get("package") or {}
                declared_weight, declared_size = validated_measurement(
                    {"package": {"validated": package.get("declared") or {}}}
                )
                actual_weight, actual_size = validated_measurement(data)
                costs = data.get("costs") or {}
                compensation = float(costs.get("compensation") or 0)
                cache[key] = {
                    "declared_weight_g": declared_weight,
                    "declared_dimensions_cm": declared_size,
                    "actual_weight_g": actual_weight,
                    "actual_dimensions_cm": actual_size,
                    "compensation": compensation,
                    "currency": costs.get("currency_id"),
                }
            record.update(cache[key])
            record["status"] = "adjusted" if record["compensation"] > 0 else "no_adjustment"
        except Exception as exc:
            record["status"] = "api_unavailable"
            record["error"] = str(exc)[:250]
        counts[record["status"]] += 1
        if i % 25 == 0:
            print(json.dumps({"processed": i, "total": len(records), "counts": counts}, ensure_ascii=True), flush=True)

    adjusted = [record for record in records if record["status"] == "adjusted"]
    payload = {"source": args.input.name, "records": adjusted, "counts": dict(counts)}
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output_json": str(args.output_json), "counts": counts}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
