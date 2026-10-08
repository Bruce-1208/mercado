"""Apply the ERP rights-holder rule whenever authoritative reports are saved."""
import hashlib

from bit import bit_mysql
from bit.bit_runtime_lock import InterProcessLock
from bit.bit_store_link_sync import _client_and_token
from bit.bit_store_link_remote_update import _delete_one_link
from erp.rights_holder_policy import mark_products_infringing, same_title_links
from erp.store_link_audit import audited, record


@audited
def process_rights_holder_product(token, title, reports):
    organization = str(token.get("organization_key") or "wuhan-zeshun").strip().lower()
    if organization == "default":
        organization = "wuhan-zeshun"
    key = hashlib.sha256(f"{organization}\0{title}".encode()).hexdigest()
    lease = InterProcessLock("rights-holder-product-" + key, owner="rights-holder-policy")
    if not lease.acquire(timeout=30):
        raise RuntimeError("同名权利人商品正在处理，下次同步重试")
    try:
        result = {"title": title, "source_token_id": token["id"], "reports": reports,
                  "max_deleted_sales": 5, "links": [], "errors": []}
        result["products"] = mark_products_infringing(title)
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
            try:
                if token_id not in tokens:
                    full_token = bit_mysql.get_mercado_store_token(token_id)
                    _, tokens[token_id] = _client_and_token(dict(full_token or {}))
                outcome = _delete_one_link(row, tokens[token_id], required_title=title, max_sold_quantity=5)
            except Exception as exc:
                outcome = {"link_id": row["id"], "item_id": row["item_id"],
                           "status": "error", "errors": [str(exc)]}
            outcome.update(token_id=token_id, salesperson=row.get("salesperson") or "",
                           store_name=row.get("store_name") or "")
            result["links"].append(outcome)
            record("rights_holder_link_result", "completed" if outcome["status"] in {"success", "retained"} else "failed",
                   {"title": title, **outcome})
        result["status"] = "partial" if result["errors"] or any(
            row["status"] not in {"success", "retained"} for row in result["links"]) else "success"
        return result
    finally:
        lease.release()


def apply_rights_holder_records(token, records):
    groups, errors = {}, []
    for source in records:
        if source.get("source_type") != "rights_holder" or source.get("reason_code") == "LEGACY_PAGE":
            continue
        if not str(source.get("item_id") or "").strip():
            continue
        if source.get("resolution_status") == "appeal_success":
            continue
        if not source.get("is_current", True) and source.get("resolution_status") != "appeal_failed":
            continue
        row = {key: source.get(key) for key in (
            "source_id", "item_id", "title", "rights_holder", "reason", "salesperson", "site_id")}
        title = str(source.get("title") or "").strip()
        if not title:
            try:
                full_token = bit_mysql.get_mercado_store_token(int(token["id"]))
                client, _ = _client_and_token(dict(full_token or {}))
                item = client.get_marketplace_item(row["item_id"])
                if str(item.get("id") or "").upper() != str(row["item_id"]).upper():
                    raise ValueError("平台商品编号不匹配")
                title = str(item.get("title") or "").strip()
                if not title:
                    raise ValueError("平台未提供商品标题")
            except Exception as exc:
                record("rights_holder_link_result", "failed", {"report": row, "error": str(exc)})
                errors.append(str(exc))
                continue
        groups.setdefault(title, []).append(row)
    for title, reports in groups.items():
        try:
            result = process_rights_holder_product(token, title, reports)
            if result["status"] != "success":
                errors.append(f"{title}：部分处理失败，下次同步重试")
        except Exception as exc:
            errors.append(f"{title}：{exc}")
    if errors:
        raise RuntimeError("权利人侵权处理：" + "；".join(errors))
