"""Inspect order search projection; --apply requires a planned write pause.

MySQL rebuilds the table for a STORED generated column. This script never runs
from an HTTP request. Reads remain available, but writes pause during COPY;
allow disk space for a second copy of the table. Existing rows and subsequent
raw_json updates are maintained by MySQL, without backfill freshness gaps.

python -m scripts.optimize_order_search_projection --database NAME [--apply]
"""
import argparse
import json
import time

from bit.order_search_projection import COLUMN, DEFINITION, TABLE, inspect


def optimize(connection, *, database, apply=False):
    with connection.cursor() as cursor:
        cursor.execute("SELECT DATABASE() AS db")
        actual = cursor.fetchone()["db"]
        if actual != database or actual in {"mysql", "sys", "information_schema", "performance_schema"}:
            raise ValueError("Explicit application database must match the connection")
        cursor.execute("SET SESSION lock_wait_timeout=5")
        before = inspect(cursor)
        start = time.monotonic()
        if apply and not before:
            cursor.execute(f"ALTER TABLE `{TABLE}` ADD COLUMN `{COLUMN}` {DEFINITION}, "
                           "ALGORITHM=COPY, LOCK=SHARED")
        return {"database": actual, "present_before": before,
                "present_after": inspect(cursor), "applied": bool(apply and not before),
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "deployment_requirement": "COPY rebuild: pause writes and reserve table-copy disk space"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    import pymysql
    from bit.db_pool import default_connection_config
    config = default_connection_config()
    config.update(autocommit=True, read_timeout=1800)
    connection = pymysql.connect(**config)
    try:
        print(json.dumps(optimize(connection, database=args.database, apply=args.apply)))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
