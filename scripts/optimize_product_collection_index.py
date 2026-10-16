"""Install the collection-to-product lookup index outside request transactions.

Run: python -m scripts.optimize_product_collection_index [--apply]
"""
import argparse
import json
import time

from scripts.optimize_product_search_index import TABLE, inspect

INDEX = "idx_erp_meli_product_collection"
COLUMNS = ["collection_item_id"]


def optimize(connection, *, apply=False):
    with connection.cursor() as cursor:
        cursor.execute("SET SESSION lock_wait_timeout=5")
        before = inspect(cursor, INDEX, COLUMNS)
        start = time.monotonic()
        if apply and not before:
            cursor.execute(
                f"ALTER TABLE `{TABLE}` ADD INDEX `{INDEX}` (`collection_item_id`), "
                "ALGORITHM=INPLACE, LOCK=NONE"
            )
        return {
            "index": INDEX, "before": before,
            "after": inspect(cursor, INDEX, COLUMNS),
            "seconds": round(time.monotonic() - start, 3),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    import pymysql
    from bit.bit_mysql import config

    connection = pymysql.connect(**dict(config, read_timeout=1800, autocommit=True))
    try:
        print(json.dumps(optimize(connection, apply=args.apply)))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
