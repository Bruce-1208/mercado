"""Exact-title queries for rights-holder reports, separate from fuzzy search."""
from erp import mercadolibre_collection_store as products
from erp import mercadolibre_store_link_store as links
from erp.short_transaction import run as short_transaction
from erp import product_title_index


def lock_matching_products(cursor, organization_key, title, fields):
    """Discover without locks; lock only primary keys and recheck current titles."""
    snapshot = "IF(JSON_VALID(`source_snapshot_json`), `source_snapshot_json`, '{}')"
    predicate = " OR ".join([
        "BINARY TRIM(`title`) = BINARY %s",
        *[f"BINARY TRIM(JSON_UNQUOTE(JSON_EXTRACT({snapshot}, '$.ai_original.{field}'))) = BINARY %s"
          for field in ("title_es", "title_pt")],
    ])
    scope = (organization_key, title, title, title)
    if product_title_index.available(cursor):
        # Hashes only select candidates. The locked read below still compares
        # every full title byte-for-byte, including translation and scope checks.
        branches = [
            f"SELECT id FROM `{products.PRODUCT_TABLE}` FORCE INDEX (`{index}`) "
            f"WHERE organization_key=%s AND `{column}`=UNHEX(MD5(%s))"
            for index, column in zip(product_title_index.INDEXES, product_title_index.COLUMNS)
        ]
        cursor.execute(' UNION ALL '.join(branches), (organization_key, title) * 3)
    else:
        # Older installations remain compatible until the explicit online
        # migration is applied; never leave runaway scans after socket timeout.
        cursor.execute(f"SELECT /*+ MAX_EXECUTION_TIME(10000) */ id FROM `{products.PRODUCT_TABLE}` WHERE organization_key=%s AND ({predicate})", scope)
    ids = sorted({row['id'] for row in cursor.fetchall() or []})
    if not ids:
        return []
    cursor.execute(
        f"SELECT {fields} FROM `{products.PRODUCT_TABLE}` FORCE INDEX (PRIMARY) "
        f"WHERE id IN ({','.join(['%s'] * len(ids))}) AND organization_key=%s "
        f"AND ({predicate}) ORDER BY id FOR UPDATE", tuple(ids) + scope)
    return list(cursor.fetchall() or [])


def mark_products_infringing(title, *, organization_key="wuhan-zeshun", connection_factory=None):
    title = str(title or "").strip()
    if not title:
        raise ValueError("权利人商品缺少标题")
    connection = (connection_factory or products._connect)()
    def update():
        with connection.cursor() as cursor:
            rows = lock_matching_products(cursor, organization_key, title, "id,title,review_status")
            changed = [row for row in rows if row.get("review_status") != "infringing"]
            if changed:
                ids = [row["id"] for row in changed]
                cursor.execute(
                    f"UPDATE `{products.PRODUCT_TABLE}` SET `review_status` = 'infringing' "
                    f"WHERE `id` IN ({', '.join(['%s'] * len(ids))})", tuple(ids),
                )
        return {"matched": len(rows), "changed": len(changed), "before": changed,
                "product_ids": [row["id"] for row in rows], "review_status": "infringing"}
    try:
        with connection.cursor() as cursor:
            products.ensure_collection_tables(cursor)
        connection.commit()
        return short_transaction(connection, update)
    except BaseException:
        try:
            connection.rollback()
        except Exception:
            pass  # A broken socket must not replace the original query error.
        raise
    finally:
        connection.close()


def same_title_links(title, organization_key, *, connection_factory=None):
    connection = (connection_factory or links._connect)()
    try:
        with connection.cursor() as cursor:
            links.ensure_store_link_table(cursor)
            indexed = product_title_index.links_available(cursor)
            index = f"FORCE INDEX (`{product_title_index.LINK_INDEX}`) " if indexed else ""
            candidate = "links.`rights_title_hash`=UNHEX(MD5(%s)) AND " if indexed else ""
            cursor.execute(
                f"SELECT /*+ MAX_EXECUTION_TIME(10000) */ links.*, tokens.`enabled` AS `store_enabled` "
                f"FROM `{links.STORE_LINK_TABLE}` AS links {index}"
                "INNER JOIN `mercado_store_tokens` AS tokens ON tokens.`id` = links.`token_id` "
                f"WHERE {candidate}BINARY TRIM(links.`title`) = BINARY %s "
                "AND COALESCE(NULLIF(NULLIF(tokens.`organization_key`, 'default'), ''), 'wuhan-zeshun') = %s "
                "ORDER BY links.`token_id`, links.`id`", ((title,) if indexed else ()) + (title, organization_key),
            )
            return list(cursor.fetchall() or [])
    finally:
        connection.close()
