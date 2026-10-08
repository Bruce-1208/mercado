"""Inspect the store-link metadata index; create it only with --apply.

Run from the server's configured MySQL environment::

    python3 -m scripts.optimize_store_link_indexes --token-id 7 --token-id 9
    python3 -m scripts.optimize_store_link_indexes --token-id 7 --token-id 9 --apply

The default only reads schema metadata and EXPLAIN plans. Index creation uses
ALGORITHM=INPLACE, LOCK=NONE and never retries with a blocking/table-copy DDL.
Online DDL still needs brief metadata locks and adds disk/I/O load; its lock
wait is limited by --lock-wait-timeout. No application module runs this script.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TABLE = "erp_mercadolibre_store_links"
STORE_LINK_METADATA_INDEX = "idx_erp_meli_link_scoped_metadata"
INDEX = STORE_LINK_METADATA_INDEX
COLUMNS = ("token_id", "is_current", "site_id", "last_synced_at", "category_id")
DDL = (
    f"ALTER TABLE `{TABLE}` ADD INDEX `{INDEX}` "
    "(" + ", ".join(f"`{column}`" for column in COLUMNS) + "), "
    "ALGORITHM=INPLACE, LOCK=NONE"
)
SYSTEM_DATABASES = {"mysql", "information_schema", "performance_schema", "sys"}


class MaintenanceError(RuntimeError):
    """A preflight or postcondition failed; messages contain no credentials."""


def _index_definitions(cursor: Any) -> dict[str, dict[str, Any]]:
    cursor.execute(
        "SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART, INDEX_TYPE, IS_VISIBLE "
        "FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
        "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
        (TABLE,),
    )
    definitions: dict[str, dict[str, Any]] = {}
    for row in cursor.fetchall():
        name = str(row["INDEX_NAME"])
        index = definitions.setdefault(name, {
            "name": name, "columns": [], "prefix_lengths": [],
            "type": str(row["INDEX_TYPE"]).upper(),
            "visible": str(row["IS_VISIBLE"]).upper() == "YES",
        })
        index["columns"].append(row["COLUMN_NAME"])
        index["prefix_lengths"].append(row["SUB_PART"])
    return definitions


def _matching_index(definitions: dict[str, dict[str, Any]]) -> str | None:
    def matches(index: dict[str, Any]) -> bool:
        return (
            index["type"] == "BTREE"
            and index["visible"]
            and tuple(index["columns"][:len(COLUMNS)]) == COLUMNS
            and all(part is None for part in index["prefix_lengths"][:len(COLUMNS)])
        )

    if INDEX in definitions:
        if not matches(definitions[INDEX]):
            raise MaintenanceError("The proposed index name exists with a different or invisible definition; inspect it manually.")
        return INDEX
    if any(matches(index) for index in definitions.values()):
        raise MaintenanceError(
            "An equivalent index exists under another name. Inspect it and rename it "
            f"to {INDEX} before the application can recognize it; this script will not add a duplicate index."
        )
    return None


def _explain(cursor: Any, token_ids: tuple[int, ...]) -> list[dict[str, Any]]:
    if not token_ids:
        # Empty tables still receive useful plans using a harmless sample ID.
        cursor.execute(f"SELECT `token_id` FROM `{TABLE}` ORDER BY `token_id` LIMIT 1")
        row = cursor.fetchone() or {}
        token_ids = (int(row.get("token_id") or 1),)
    scope = "`token_id` IN (" + ", ".join("%s" for _ in token_ids) + ")"
    queries = {
        "sites": f"SELECT `site_id`, COUNT(*) AS `link_count` FROM `{TABLE}` "
                 f"WHERE `is_current`=1 AND {scope} "
                 "AND `site_id` IS NOT NULL AND `site_id`<>'' GROUP BY `site_id`",
        "summary": f"SELECT COUNT(*) AS `all_count`, "
                   "SUM(`is_current`=1) AS `current_count`, "
                   "COUNT(DISTINCT CASE WHEN `is_current`=1 THEN `token_id` END) AS `store_count`, "
                   "MAX(CASE WHEN `is_current`=1 THEN `last_synced_at` END) AS `last_synced_at` "
                   f"FROM `{TABLE}` WHERE {scope}",
        "categories": f"SELECT `category_id`, MIN(`id`) AS `sample_link_id`, "
                      f"COUNT(*) AS `link_count` FROM `{TABLE}` "
                      f"WHERE `is_current`=1 AND {scope} "
                      "AND `category_id` IS NOT NULL AND `category_id`<>'' GROUP BY `category_id`",
    }
    plans = []
    for name, sql in queries.items():
        cursor.execute("EXPLAIN " + sql, token_ids)
        plans.append({"query": name, "plan": list(cursor.fetchall())})
    return plans


def optimize(
    connection: Any, *, expected_database: str, apply: bool = False,
    token_ids: tuple[int, ...] = (), lock_wait_timeout: int = 5,
    emit_plan: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Inspect/apply using a caller-owned dedicated connection, never a web pool."""
    if not expected_database or expected_database.casefold() in SYSTEM_DATABASES:
        raise MaintenanceError("Configure an application MySQL database before running this script.")
    if not 1 <= lock_wait_timeout <= 60:
        raise MaintenanceError("Metadata lock wait must be between 1 and 60 seconds.")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in token_ids):
        raise MaintenanceError("Token IDs must be positive integers.")
    token_ids = tuple(sorted(set(token_ids)))
    with connection.cursor() as cursor:
        cursor.execute("SELECT DATABASE() AS database_name, VERSION() AS server_version")
        server = cursor.fetchone() or {}
        if server.get("database_name") != expected_database:
            raise MaintenanceError("The connected database does not match the configured application database.")
        version = str(server.get("server_version") or "")
        parsed = re.match(r"^(\d+)\.(\d+)", version)
        if not parsed or int(parsed[1]) < 8 or "mariadb" in version.casefold():
            raise MaintenanceError("This maintenance script requires MySQL 8.0 or newer.")
        cursor.execute(
            "SELECT ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH "
            "FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND TABLE_TYPE='BASE TABLE'",
            (TABLE,),
        )
        table = cursor.fetchone()
        if not table or str(table.get("ENGINE") or "").upper() != "INNODB":
            raise MaintenanceError("The store-link table is missing or does not use InnoDB.")
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
            (TABLE,),
        )
        columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}
        if not set((*COLUMNS, "id")).issubset(columns):
            raise MaintenanceError("The store-link table is missing columns required by this index.")
        definitions = _index_definitions(cursor)
        existing = _matching_index(definitions)
        report = {
            "database": expected_database, "server_version": version, "table": TABLE,
            "table_metadata": dict(table), "index_name": INDEX,
            "index_columns": list(COLUMNS), "existing_index": existing,
            "status": "already_present" if existing else "planned", "ddl": DDL,
            "lock_wait_timeout_seconds": lock_wait_timeout,
            "explain_before": _explain(cursor, token_ids),
        }
        if not apply or existing:
            return report
        if emit_plan is not None:
            emit_plan(report)
        # LOCK=NONE does not remove the brief metadata locks needed by online DDL.
        # Bound those waits, and fail rather than falling back to COPY/SHARED.
        cursor.execute("SET SESSION lock_wait_timeout=%s", (lock_wait_timeout,))
        cursor.execute(DDL)
        if _matching_index(_index_definitions(cursor)) is None:
            raise MaintenanceError("Index creation completed but its expected definition was not found.")
        report["status"] = "applied"
        report["explain_after"] = _explain(cursor, token_ids)
        return report


def _print_report(report: dict[str, Any]) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Create the missing index using online DDL.")
    parser.add_argument("--token-id", action="append", type=int, default=[], help="Scope the EXPLAIN plans; repeat for multiple stores.")
    parser.add_argument("--lock-wait-timeout", type=int, default=5, help="Maximum seconds to wait for each metadata lock (1–60).")
    args = parser.parse_args(argv)
    connection = None
    try:
        import pymysql
        from bit.db_pool import default_connection_config

        config = default_connection_config()
        database = str(config.get("database") or "")
        # A separate socket avoids occupying web-request connection capacity.
        config["autocommit"] = True
        # Building an index on the large listing table may exceed web-request
        # read timeouts. Keep this dedicated maintenance connection alive.
        if args.apply:
            config["read_timeout"] = max(int(config.get("read_timeout") or 0), 3600)
        connection = pymysql.connect(**config)
        report = optimize(
            connection, expected_database=database, apply=args.apply,
            token_ids=tuple(args.token_id), lock_wait_timeout=args.lock_wait_timeout,
            emit_plan=_print_report,
        )
        _print_report(report)
        return 0
    except Exception as exc:
        # Driver messages can contain connection/user details; never print them.
        message = str(exc) if isinstance(exc, MaintenanceError) else "MySQL maintenance failed; no blocking DDL fallback was attempted."
        error_code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        _print_report({"status": "error", "message": message, "mysql_error_code": error_code})
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
