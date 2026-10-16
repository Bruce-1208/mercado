"""Optional, database-maintained pack text for exact existing LIKE semantics.

No request performs DDL. Deploy the maintenance script during a write pause;
unmigrated databases keep the original JSON expression.
"""
import re
import threading
import time

import pymysql

TABLE = "mercado_synced_orders"
COLUMN = "_pack_search_value"
EXPRESSION = "NULLIF(JSON_UNQUOTE(JSON_EXTRACT(`raw_json`, '$.pack_id')), 'null')"
DEFINITION = ("LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin "
              f"GENERATED ALWAYS AS ({EXPRESSION}) STORED")
_CACHE = {}
_LOCK = threading.Lock()


def normalized(expression):
    # INFORMATION_SCHEMA may escape quotes and add charset introducers. Keep
    # quoted JSON paths/literals case-sensitive, including their whitespace.
    tokens = re.findall(r"'(?:''|[^'])*'|`[^`]*`|[A-Za-z_][A-Za-z_0-9]*|\S",
                        str(expression).replace("\\'", "'"))
    return tuple(token if token.startswith("'") else token.strip("`").lower()
                 for token in tokens if token.lower() != "_utf8mb4")


def inspect(cursor):
    cursor.execute(
        "SELECT GENERATION_EXPRESSION, DATA_TYPE, COLLATION_NAME, EXTRA "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME=%s AND COLUMN_NAME=%s", (TABLE, COLUMN))
    row = cursor.fetchone()
    if row and (normalized(row["GENERATION_EXPRESSION"]) != normalized(EXPRESSION)
                or row["DATA_TYPE"] != "longtext"
                or row["COLLATION_NAME"] != "utf8mb4_bin"
                or "STORED GENERATED" not in row["EXTRA"]):
        raise ValueError("Order search projection has an incompatible definition")
    return bool(row)


def pack_sql(cursor):
    fallback = EXPRESSION.replace("`raw_json`", "synced.`raw_json`")
    connection = getattr(cursor, "connection", None)
    if not isinstance(connection, pymysql.connections.Connection):
        return fallback
    key = (connection.host, connection.port, connection.unix_socket,
           connection.user, connection.db)
    with _LOCK:
        expires, present = _CACHE.get(key, (0, False))
        if expires <= time.monotonic():
            present = inspect(cursor)
            if len(_CACHE) >= 32:
                _CACHE.clear()
            _CACHE[key] = (time.monotonic() + 60, present)
    return f"synced.`{COLUMN}`" if present else fallback
