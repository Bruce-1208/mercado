"""Apply the ERP rights-holder rule whenever authoritative reports are saved."""
import hashlib

from bit import bit_mysql
from bit.bit_runtime_lock import InterProcessLock
from bit.bit_store_link_sync import _client_and_token
from bit.bit_store_link_remote_update import (
    _delete_one_link, _is_site_listing_id, _site_listing_retention_reason,
)
from erp.rights_holder_policy import mark_products_infringing, same_title_links
from erp.store_link_audit import audited, record


@audited
def process_rights_holder_product(token, title, reports, *, max_sold_quantity=5, before_write=None, on_result=None):
    organization = str(token.get("organization_key") or "wuhan-zeshun").strip().lower()
    if organization == "default":
        organization = "wuhan-zeshun"
    key = hashlib.sha256(f"{organization}\0{title}".encode()).hexdigest()
    lease = InterProcessLock("rights-holder-product-" + key, owner="rights-holder-policy")
    if not lease.acquire(timeout=30):
        raise RuntimeError("同名权利人商品正在处理，下次同步重试")
    try:
        result = {"title": title, "source_token_id": token["id"], "reports": reports,
                  "max_deleted_sales": max_sold_quantity, "links": [], "errors": []}
        if before_write:
            before_write()
        result["products"] = mark_products_infringing(title, organization_key=organization)
        if on_result:
            on_result({"stage": "标记同名产品侵权", "result": result["products"]})
        known = same_title_links(title, organization)
        targets = [row for row in known if row.get("is_current", 1) and row.get("status") != "deleted"]
        # Reports may arrive before the first store-link inventory sync.
        missing = {str(row.get("item_id") or "") for row in reports} - {
            str(row.get("item_id") or "") for row in known
            if row.get("is_current", 1) or row.get("status") == "deleted"}
        tokens = {}
        if missing:
            from erp.mercadolibre_store_link_store import replace_store_snapshot
            try:
                full_token = bit_mysql.get_mercado_store_token(int(token["id"]))
                client, full_token = _client_and_token(dict(full_token or {}))
                tokens[int(token["id"])] = full_token
                for item_id in sorted(missing):
                    item = client.get_marketplace_item(item_id)
                    if str(item.get("id") or "").upper() != item_id.upper():
                        raise ValueError("平台返回的权利人商品编号不匹配")
                    if str(item.get("title") or "").strip() == title:
                        replace_store_snapshot(full_token, [item], finalize=False)
                targets = [row for row in same_title_links(title, organization)
                           if row.get("is_current", 1) and row.get("status") != "deleted"]
            except Exception as exc:
                result["errors"].append(f"补齐权利人链接失败：{exc}")
        for row in targets:
            token_id = int(row["token_id"])
            item_id = str(row.get("item_id") or "").strip().upper()
            if not bool(row.get("store_enabled", 1)):
                outcome = {"link_id": row["id"], "item_id": item_id,
                           "status": "retained", "errors": [],
                           "retention_reason": "店铺已关闭，已跳过平台删除"}
            elif not _is_site_listing_id(item_id, row.get("site_id")):
                # CBT is a Global parent, not a deletable site listing. Keeping it
                # is expected and must not turn an otherwise successful cleanup
                # into a permanently failing retry loop.
                outcome = {"link_id": row["id"], "item_id": item_id,
                           "status": "retained", "errors": [],
                           "retention_reason": _site_listing_retention_reason(item_id)}
            else:
                try:
                    if token_id not in tokens:
                        full_token = bit_mysql.get_mercado_store_token(token_id)
                        _, tokens[token_id] = _client_and_token(dict(full_token or {}))
                    if before_write:
                        before_write()
                    outcome = _delete_one_link(row, tokens[token_id], required_title=title, max_sold_quantity=max_sold_quantity)
                except Exception as exc:
                    outcome = {"link_id": row["id"], "item_id": row["item_id"],
                               "status": "error", "errors": [str(exc)]}
            outcome.update(token_id=token_id, salesperson=row.get("salesperson") or "",
                           store_name=row.get("store_name") or "")
            result["links"].append(outcome)
            if on_result:
                on_result({"stage": "删除链接", **outcome})
            record("rights_holder_link_result", "completed" if outcome["status"] in {"success", "retained"} else "failed",
                   {"title": title, **outcome})
        result["status"] = "partial" if result["errors"] or any(
            row["status"] not in {"success", "retained"} for row in result["links"]) else "success"
        return result
    finally:
        lease.release()


def apply_rights_holder_records(token, records):
    """Reports are persisted first; automation-center owns all automatic writes.

    The server worker scans persisted reports, so disabling cleanup cannot be
    bypassed by infraction sync, and opening the page is never required.
    """
    return None
