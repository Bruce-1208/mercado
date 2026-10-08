"""Recover actual weights and atomically backfill missing collection costs.

Dry-run by default. Pass --apply to save; the report preserves previous values.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bit.workbench_services import child_environment
from bit.mercado_product_data import ProductDataService
from erp import mercadolibre_collection_store as store
from erp.mercadolibre_profitability import (
    actual_weight_from_row, estimate_collection_profitability_from_rate_card,
)
from erp.mercadolibre_shipping_rate_cards import OfficialShippingRateCardStore
from erp.mercadolibre_batch_collector import parse_plugin_metrics


def stored_actual_weight(row):
    snapshot = json.loads(row.get("plugin_snapshot_json") or "{}")
    dom = str(snapshot.get("dom_text") or "")
    ocr = str(snapshot.get("ocr_text") or "")
    if ocr and float(snapshot.get("ocr_confidence") or 0) < .95:
        ocr = ""
    metrics = parse_plugin_metrics(" ".join((dom, ocr)))
    return actual_weight_from_row(metrics)


def repair_products(*, apply=False, report_path):
    """Use each collected product's current inputs, preserving manual edits."""
    connection = store._connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM `{store.PRODUCT_TABLE}` FORCE INDEX(idx_erp_meli_product_source) "
                "WHERE `source_type` = 'collected' "
                "AND (`shipping_fee_usd` IS NULL OR `net_proceeds_usd` IS NULL) ORDER BY `id` DESC"
            )
            rows = list(cursor.fetchall())
        connection.commit()
    finally:
        connection.close()
    rates = tuple(OfficialShippingRateCardStore().list_rates().get("rows") or [])
    report = {"apply": apply, "scanned": len(rows), "saved": 0, "ready": 0, "items": []}
    output = Path(report_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    prepared = []
    for row in rows:
        estimate = estimate_collection_profitability_from_rate_card(row, rate_rows=rates)
        entry = {"id": row["id"], "source_item_id": row["source_item_id"]}
        report["items"].append(entry)
        if estimate.get("net_proceeds_usd") is None:
            entry["reason"] = "缺少实重或未命中官方运费表"
            continue
        entry.update(before={column: row.get(column) for column in store.PROFITABILITY_COLUMNS}, after=estimate)
        report["ready"] += 1
        snapshot = {
            **row, **estimate,
            "_expected_profitability_inputs": {
                column: row.get(column) for column in (
                    "updated_at", "price", "currency_id", "weight_g", "weight_basis",
                    "category_id", "title", "source_snapshot_json", "description_text",
                )
            },
        }
        prepared.append((entry, snapshot))
    # Preserve all before-images before applying the first change.
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if apply:
        for index, (entry, snapshot) in enumerate(prepared, start=1):
            try:
                entry["saved"] = store.update_item_profitability(snapshot["source_item_id"], snapshot)
                report["saved"] += int(entry["saved"])
            except Exception as exc:
                entry["error"] = str(exc)
            if index % 50 == 0:
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
                print(f"Products {index}/{len(prepared)}; saved {report['saved']}", flush=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return report


def repair(*, apply=False, report_path):
    os.environ.update({
        key: value for key, value in child_environment("maintenance").items()
        if key.startswith("ZESHUN_MERCADO_")
    })
    connection = store._connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM `{store.COLLECTION_TABLE}` "
                "WHERE `shipping_fee_usd` IS NULL OR `net_proceeds_usd` IS NULL "
                "ORDER BY `id` DESC"
            )
            rows = list(cursor.fetchall())
            cursor.execute(
                f"SELECT p.`source_item_id`, p.`weight_g`, p.`weight_basis`, "
                "p.`package_length_cm`, p.`package_width_cm`, p.`package_height_cm`, "
                f"p.`volumetric_weight_kg` FROM `{store.PRODUCT_TABLE}` p "
                f"JOIN `{store.COLLECTION_TABLE}` c ON c.`source_item_id` = p.`source_item_id` "
                "WHERE (c.`shipping_fee_usd` IS NULL OR c.`net_proceeds_usd` IS NULL) "
                "AND p.`weight_g` > 0"
            )
            existing_weights = {
                row["source_item_id"]: row for row in cursor.fetchall()
                if actual_weight_from_row(row) is not None
            }
        connection.commit()
    finally:
        connection.close()
    rate_rows = tuple(OfficialShippingRateCardStore().list_rates().get("rows") or [])
    service = ProductDataService()
    report = {"apply": apply, "scanned": len(rows), "saved": 0, "ready": 0, "items": []}
    output = Path(report_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    def save_report():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        temporary.replace(output)

    try:
        for offset in range(0, len(rows), 50):
            batch = rows[offset:offset + 50]
            missing_weights = [row for row in batch if actual_weight_from_row(row) is None]
            fetched = {}
            if missing_weights:
                try:
                    response = service.query([
                        {"sku": row["source_item_id"], "price": float(row["price"] or 0)}
                        for row in missing_weights
                    ])
                    fetched = {item["sku"]: item.get("product") for item in response["items"]}
                except Exception as exc:
                    report.setdefault("query_errors", []).append(str(exc))
            for old in batch:
                row = dict(old)
                measurements = {}
                info = fetched.get(row["source_item_id"]) or {}
                if actual_weight_from_row(row) is None and info.get("weight_g"):
                    measurements = {"weight_g": info["weight_g"], "weight_basis": "server_actual"}
                    size = info.get("size_cm")
                    if size and len(size) == 3 and all(value and value > 0 for value in size):
                        measurements.update(zip(
                            ("package_length_cm", "package_width_cm", "package_height_cm"), size,
                        ))
                        measurements["volumetric_weight_kg"] = info["volumetric_weight_g"] / 1000
                    row.update(measurements)
                elif actual_weight_from_row(row) is None:
                    # The same listing may already have an actual-weight
                    # snapshot in the product list even if the source retired it.
                    existing = existing_weights.get(row["source_item_id"])
                    if existing:
                        measurements = {
                            column: value for column, value in existing.items()
                            if column != "source_item_id" and value is not None
                        }
                        row.update(measurements)
                    else:
                        weight = stored_actual_weight(row)
                        if weight is not None:
                            measurements = {"weight_g": weight, "weight_basis": "plugin_actual"}
                            row.update(measurements)
                estimate = estimate_collection_profitability_from_rate_card(row, rate_rows=rate_rows)
                entry = {"id": old["id"], "source_item_id": old["source_item_id"]}
                report["items"].append(entry)
                if estimate.get("net_proceeds_usd") is None:
                    entry["reason"] = (
                        "商品数据未返回可用实重" if actual_weight_from_row(row) is None
                        else "售价、币种或重量区间未命中官方运费表"
                    )
                    continue
                changes = {**measurements, **{
                    column: value for column, value in estimate.items()
                    if column in store.PROFITABILITY_COLUMNS
                }}
                entry.update(before={column: old.get(column) for column in changes}, after=changes)
                report["ready"] += 1
                save_report()  # Preserve the before-image before any write.
                if not apply:
                    continue
                connection = store._connect()
                try:
                    with connection.cursor() as cursor:
                        assignments = ", ".join(f"`{column}` = %s" for column in changes)
                        cursor.execute(
                            f"UPDATE `{store.COLLECTION_TABLE}` SET {assignments} "
                            "WHERE `id` = %s AND `updated_at` <=> %s "
                            "AND (`shipping_fee_usd` IS NULL OR `net_proceeds_usd` IS NULL) "
                            f"AND {store._purchase_profitability_guard_sql()}",
                            tuple(changes.values()) + (old["id"], old["updated_at"]),
                        )
                        saved = cursor.rowcount > 0
                        if saved:
                            cursor.execute(
                                f"UPDATE `{store.PRODUCT_TABLE}` SET {assignments} "
                                "WHERE `collection_item_id` = %s AND `source_item_id` = %s "
                                "AND `source_type` = 'collected' AND `price` <=> %s "
                                "AND `currency_id` <=> %s "
                                f"AND {store._purchase_profitability_guard_sql()} "
                                "AND (`weight_g` IS NULL OR `weight_g` = %s) "
                                f"AND NOT EXISTS (SELECT 1 FROM `{store.COLLECTION_TABLE}` newer "
                                "WHERE newer.`source_item_id` = %s AND newer.`id` > %s)",
                                tuple(changes.values()) + (
                                    old["id"], old["source_item_id"], old["price"], old["currency_id"],
                                    row["weight_g"], old["source_item_id"], old["id"],
                                ),
                            )
                    connection.commit()
                    entry["saved"] = saved
                    report["saved"] += int(saved)
                except Exception as exc:
                    connection.rollback()
                    entry["error"] = str(exc)
                finally:
                    connection.close()
            save_report()
            print(f"Checked {min(offset + 50, len(rows))}/{len(rows)}; ready {report['ready']}; saved {report['saved']}", flush=True)
    finally:
        service.close()
        save_report()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--products", action="store_true", help="Repair collected products instead of the collection box")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    operation = repair_products if args.products else repair
    result = operation(apply=args.apply, report_path=args.report)
    print(json.dumps({key: value for key, value in result.items() if key != "items"}, ensure_ascii=False))
