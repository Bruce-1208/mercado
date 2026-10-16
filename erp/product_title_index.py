"""Indexed exact-title candidates; full values are rechecked before writes."""
from erp import mercadolibre_collection_store as products
from erp import mercadolibre_store_link_store as links

COLUMNS = ('rights_title_hash', 'rights_title_es_hash', 'rights_title_pt_hash')
INDEXES = tuple('idx_' + name for name in COLUMNS)
LINK_INDEX = 'idx_rights_link_title_hash'


def links_available(cursor):
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s",
        (links.STORE_LINK_TABLE, LINK_INDEX))
    return (cursor.fetchone() or {}).get('n') == 1


def available(cursor):
    cursor.execute(
        "SELECT COUNT(DISTINCT INDEX_NAME) AS n FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME IN (%s,%s,%s)",
        (products.PRODUCT_TABLE,) + INDEXES)
    return (cursor.fetchone() or {}).get('n') == 3


def install(connection):
    """Explicit online migration, never run DDL in a worker's business transaction."""
    with connection.cursor() as cursor:
        cursor.execute(f"SHOW COLUMNS FROM `{products.PRODUCT_TABLE}`")
        columns = {row['Field'] for row in cursor.fetchall()}
        snapshot = "IF(JSON_VALID(`source_snapshot_json`), `source_snapshot_json`, '{}')"
        expressions = ['`title`'] + [
            f"JSON_UNQUOTE(JSON_EXTRACT({snapshot}, '$.ai_original.{field}'))"
            for field in ('title_es', 'title_pt')]
        additions = [f"ADD COLUMN `{name}` BINARY(16) GENERATED ALWAYS AS "
                     f"(UNHEX(MD5(TRIM({expression})))) VIRTUAL INVISIBLE"
                     for name, expression in zip(COLUMNS, expressions) if name not in columns]
        if additions:
            cursor.execute(f"ALTER TABLE `{products.PRODUCT_TABLE}` " + ', '.join(additions) +
                           ', ALGORITHM=INPLACE, LOCK=NONE')
        # Internal binary keys must never leak through existing SELECT * APIs.
        cursor.execute(f"SHOW FULL COLUMNS FROM `{products.PRODUCT_TABLE}`")
        visible = [row['Field'] for row in cursor.fetchall()
                   if row['Field'] in COLUMNS and 'INVISIBLE' not in row.get('Extra', '')]
        if visible:
            cursor.execute(f"ALTER TABLE `{products.PRODUCT_TABLE}` " + ', '.join(
                f"ALTER COLUMN `{name}` SET INVISIBLE" for name in visible))
        cursor.execute(f"SHOW INDEX FROM `{products.PRODUCT_TABLE}`")
        indexes = {row['Key_name'] for row in cursor.fetchall()}
        additions = [f"ADD INDEX `{index}` (`organization_key`, `{column}`)"
                     for index, column in zip(INDEXES, COLUMNS) if index not in indexes]
        if additions:
            cursor.execute(f"ALTER TABLE `{products.PRODUCT_TABLE}` " + ', '.join(additions) +
                           ', ALGORITHM=INPLACE, LOCK=NONE')
    connection.commit()


def install_links(connection):
    with connection.cursor() as cursor:
        cursor.execute(f"SHOW COLUMNS FROM `{links.STORE_LINK_TABLE}`")
        if 'rights_title_hash' not in {row['Field'] for row in cursor.fetchall()}:
            cursor.execute(f"ALTER TABLE `{links.STORE_LINK_TABLE}` ADD COLUMN "
                           "`rights_title_hash` BINARY(16) GENERATED ALWAYS AS "
                           "(UNHEX(MD5(TRIM(`title`)))) VIRTUAL INVISIBLE, ALGORITHM=INPLACE, LOCK=NONE")
        cursor.execute(f"SHOW FULL COLUMNS FROM `{links.STORE_LINK_TABLE}`")
        if any(row['Field'] == 'rights_title_hash' and 'INVISIBLE' not in row.get('Extra', '')
               for row in cursor.fetchall()):
            cursor.execute(f"ALTER TABLE `{links.STORE_LINK_TABLE}` "
                           "ALTER COLUMN `rights_title_hash` SET INVISIBLE")
        if not links_available(cursor):
            cursor.execute(f"ALTER TABLE `{links.STORE_LINK_TABLE}` ADD INDEX "
                           f"`{LINK_INDEX}` (`rights_title_hash`), ALGORITHM=INPLACE, LOCK=NONE")
    connection.commit()
