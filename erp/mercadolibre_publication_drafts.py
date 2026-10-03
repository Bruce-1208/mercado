"""Append-only draft revisions in the shared workbench database."""
import json


def draft_storage(action, *, revision="", draft=None, product_id=0, token_id=0,
                  site_id="", owner_id=0, connection_factory=None):
    from erp.mercadolibre_collection_store import _connect
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS erp_meli_attribute_drafts (
                revision CHAR(64) PRIMARY KEY, product_id BIGINT NOT NULL,
                token_id BIGINT NOT NULL, site_id VARCHAR(8) NOT NULL,
                owner_id BIGINT NOT NULL, draft_json LONGTEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_product_target (product_id, token_id, site_id)
            ) CHARACTER SET utf8mb4""")
            if action == "save":
                cursor.execute("""INSERT INTO erp_meli_attribute_drafts
                    (revision,product_id,token_id,site_id,owner_id,draft_json)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (draft["revision"], int(product_id), int(token_id), str(site_id), int(owner_id),
                     json.dumps(draft, ensure_ascii=False, allow_nan=False)))
                result = {"revision": draft["revision"]}
            elif action == "get":
                cursor.execute("""SELECT draft_json FROM erp_meli_attribute_drafts
                    WHERE revision=%s AND product_id=%s AND token_id=%s AND site_id=%s AND owner_id=%s""",
                    (revision, int(product_id), int(token_id), str(site_id), int(owner_id)))
                row = cursor.fetchone()
                if not row:
                    raise ValueError("上架草稿不存在或不属于当前用户及目标账号，请重新预检")
                result = json.loads(row["draft_json"])
            else:
                raise ValueError("不支持的草稿操作")
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def shared_draft_storage(action, **kwargs):
    from bit import bit_db_api
    if bit_db_api.DB_MODE == "mysql":
        return draft_storage(action, **kwargs)
    return bit_db_api._request("POST", "/api/db/mercado-products/attribute-drafts",
                              json={"action": action, **kwargs})
