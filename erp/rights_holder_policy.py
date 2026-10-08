"""Exact-title queries for rights-holder reports, separate from fuzzy search."""
from erp import mercadolibre_collection_store as products
from erp import mercadolibre_store_link_store as links


def mark_products_infringing(title, *, connection_factory=None):
    title = str(title or "").strip()
    if not title:
        raise ValueError("权利人商品缺少标题")
    connection = (connection_factory or products._connect)()
    try:
        with connection.cursor() as cursor:
            products.ensure_collection_tables(cursor)
            snapshot = "IF(JSON_VALID(`source_snapshot_json`), `source_snapshot_json`, '{}')"
            predicate = " OR ".join([
                "BINARY TRIM(`title`) = BINARY %s",
                *[f"BINARY TRIM(JSON_UNQUOTE(JSON_EXTRACT({snapshot}, '$.ai_original.{field}'))) = BINARY %s"
                  for field in ("title_es", "title_pt")],
            ])
            cursor.execute(
                f"SELECT `id`, `title`, `review_status` FROM `{products.PRODUCT_TABLE}` "
                f"WHERE ({predicate}) FOR UPDATE", (title, title, title),
            )
            rows = list(cursor.fetchall() or [])
            changed = [row for row in rows if row.get("review_status") != "infringing"]
            if changed:
                ids = [row["id"] for row in changed]
                cursor.execute(
                    f"UPDATE `{products.PRODUCT_TABLE}` SET `review_status` = 'infringing' "
                    f"WHERE `id` IN ({', '.join(['%s'] * len(ids))})", tuple(ids),
                )
        connection.commit()
        return {"matched": len(rows), "changed": len(changed), "before": changed,
                "product_ids": [row["id"] for row in rows], "review_status": "infringing"}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def same_title_links(title, organization_key, *, connection_factory=None):
    connection = (connection_factory or links._connect)()
    try:
        with connection.cursor() as cursor:
            links.ensure_store_link_table(cursor)
            cursor.execute(
                f"SELECT links.* FROM `{links.STORE_LINK_TABLE}` AS links "
                "INNER JOIN `mercado_store_tokens` AS tokens ON tokens.`id` = links.`token_id` "
                "WHERE BINARY TRIM(links.`title`) = BINARY %s "
                "AND COALESCE(NULLIF(NULLIF(tokens.`organization_key`, 'default'), ''), 'wuhan-zeshun') = %s "
                "ORDER BY links.`token_id`, links.`id`", (title, organization_key),
            )
            return list(cursor.fetchall() or [])
    finally:
        connection.close()
