"""Persist missing AI-original net proceeds from captured 1688 prices.

Preview by default; --apply fills only NULL amounts and preserves manual
overrides, review status, and generated content. No AI or marketplace calls.
"""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def missing_net_proceeds(row):
    from erp.mercadolibre_collection_store import _default_ai_original_net_proceeds, _loads

    if row.get("source_type") != "ai_original" or row.get("net_proceeds_usd") is not None:
        return None
    snapshot = _loads(row.get("source_snapshot_json"), {})
    if not isinstance(snapshot, dict):
        return None
    net = _default_ai_original_net_proceeds(
        snapshot, fallback_price=(row.get("price")
            if str(row.get("currency_id") or "").upper() == "CNY" else None),
    )
    return float(net) if net is not None and net > 0 else None


def repair(connection, *, apply=False):
    changes = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM erp_mercadolibre_products "
                "WHERE source_type='ai_original' AND net_proceeds_usd IS NULL "
                "ORDER BY id" + (" FOR UPDATE" if apply else "")
            )
            for row in cursor.fetchall():
                net = missing_net_proceeds(row)
                if net is None or net <= 0:
                    continue
                change = {"id": row["id"], "before": None, "net_proceeds_usd": net}
                if apply:
                    cursor.execute(
                        "UPDATE erp_mercadolibre_products SET net_proceeds_usd=%s "
                        "WHERE id=%s AND source_type='ai_original' AND net_proceeds_usd IS NULL",
                        (net, row["id"]),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(f"商品 {row['id']} 的净收益保存未确认")
                    cursor.execute(
                        "SELECT net_proceeds_usd FROM erp_mercadolibre_products WHERE id=%s",
                        (row["id"],),
                    )
                    if cursor.fetchone()["net_proceeds_usd"] != Decimal(str(net)):
                        raise RuntimeError(f"商品 {row['id']} 的净收益回读不一致")
                changes.append(change)
        if apply:
            connection.commit()
        else:
            connection.rollback()
        return {"applied": apply, "changes": changes}
    except BaseException:
        connection.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    from erp.mercadolibre_collection_store import _connect_list

    connection = _connect_list()
    try:
        print(json.dumps(repair(connection, apply=args.apply), ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
