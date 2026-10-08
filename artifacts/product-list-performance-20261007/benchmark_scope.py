"""Bounded read-only comparison. Run with python -m artifacts.product-list-performance-20261007.benchmark_scope."""
import hashlib
import json
import time
from pathlib import Path

from bit.bit_mysql import config, pymysql
from erp import mercadolibre_collection_store as store


class CaptureCursor:
    def __init__(self, queries):
        self.queries = queries
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def execute(self, sql, params=()):
        self.queries.append((sql, params))
    def fetchall(self):
        return []


class CaptureConnection:
    def __init__(self):
        self.queries = []
    def cursor(self):
        return CaptureCursor(self.queries)
    def commit(self):
        pass
    def close(self):
        pass


def comparison_queries(tokens, limit, offset):
    capture = CaptureConnection()
    store.list_product_items(token_ids=tokens, limit=limit, offset=offset,
                             connection_factory=lambda: capture)
    sql, params = capture.queries[-1]
    union_end = sql.index(") SELECT /*+")
    union = sql[len("WITH authorized_products AS ("):union_end]
    select_start = union_end + len(") ")
    fields_end = sql.index(" FROM (SELECT COUNT(*) AS total FROM authorized_products)")
    select = sql[select_start:fields_end].replace(", list_totals.`total` AS `__list_total`", "")
    product_join = f"LEFT JOIN `{store.PRODUCT_TABLE}` ON `{store.PRODUCT_TABLE}`.`id` = page_ids.`id` "
    joins = sql[sql.index(product_join):].replace("LEFT JOIN", "INNER JOIN", 1)
    count = f"SELECT /*+ MAX_EXECUTION_TIME(8000) */ COUNT(*) AS total FROM ({union}) AS authorized_products"
    page = (select + f" FROM (SELECT `id` FROM `{store.PRODUCT_TABLE}` WHERE `id` IN "
            f"(SELECT authorized_products.`id` FROM ({union}) AS authorized_products) "
            "ORDER BY `id` DESC LIMIT %s OFFSET %s) AS page_ids " + joins)
    return [("baseline_count", count, params[:-2]), ("baseline_page", page, params),
            ("combined_count_page", sql, params)]


def main():
    # Bypass schema/migration entry points: this benchmark executes SELECT only.
    store.ensure_collection_tables = lambda cursor: None
    connection = pymysql.connect(**dict(config, connect_timeout=3, read_timeout=10, write_timeout=5))
    reports = []
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DISTINCT token_id FROM erp_mercadolibre_store_links FORCE INDEX(uniq_erp_meli_store_link)")
            all_tokens = [row["token_id"] for row in cursor.fetchall()]
            connection.rollback()
            scopes = [("five_stores", [2, 4, 63, 65, 122]), ("all_linked_stores", all_tokens)]
            for scope, tokens in scopes:
                for round_no in range(2):
                    report = {"scope": scope, "tokens": len(tokens), "round": round_no, "limit": 100}
                    # Compare all three statements in the same MVCC snapshot;
                    # live sync/deletion can otherwise change totals mid-round.
                    cursor.execute("SET TRANSACTION READ ONLY")
                    connection.begin()
                    for name, sql, params in comparison_queries(tokens, 100, 0):
                        started = time.monotonic()
                        cursor.execute(sql, params)
                        rows = cursor.fetchall()
                        ids = [row["id"] for row in rows if row.get("id") is not None]
                        report[name] = {"ms": round((time.monotonic() - started) * 1000, 1),
                                        "rows": len(ids),
                                        "total": rows[0].get("__list_total", rows[0].get("total")) if rows else None,
                                        "ids_hash": hashlib.sha256(json.dumps(ids).encode()).hexdigest() if ids else None}
                    assert report["baseline_count"]["total"] == report["combined_count_page"]["total"]
                    assert report["baseline_page"]["ids_hash"] == report["combined_count_page"]["ids_hash"]
                    connection.rollback()
                    reports.append(report)
                    print(json.dumps(report), flush=True)
        Path(__file__).with_name("final-sql-comparison.json").write_text(json.dumps(reports, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
