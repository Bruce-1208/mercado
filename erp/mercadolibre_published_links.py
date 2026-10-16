"""Project confirmed publications into store links before reporting success."""
from collections.abc import Mapping
import re

from erp.mercadolibre_store_link_store import (
    replace_store_snapshot, invalidate_store_link_metadata_cache,
)


def save_published_store_links(token, publication, *, site_id, product, quantity, client):
    from bit.bit_store_link_sync import _public_item_url
    from bit.store_link_item_lock import store_link_item_locks

    result = publication.get("result") or {}
    payload = publication.get("payload") or {}
    parents = (result.get("user_products") or [result]) if isinstance(result, Mapping) else result
    payloads = payload if isinstance(payload, list) else [payload]
    items = {}
    for index, parent in enumerate(parents):
        if not isinstance(parent, Mapping):
            continue
        candidates = list(parent.get("site_items") or [])
        if re.fullmatch(rf"{re.escape(site_id)}\d+", str(parent.get("id") or "")):
            candidates.append(parent)
        if not candidates:
            parent_id = (parent.get("siteless_user_product_id") or parent.get("id")
                         or parent.get("user_product_id") or parent.get("parent_user_product_id"))
            if parent_id:
                if str(parent_id).startswith("CBT") and not str(parent_id).startswith("CBTU"):
                    mapped = client.request("GET", f"/items/{parent_id}/marketplace_items")
                else:
                    mapped = client.request("GET", f"/marketplace/user-products/{parent_id}/mapping")
                for entry in mapped if isinstance(mapped, list) else [mapped]:
                    if isinstance(entry, Mapping):
                        candidates.extend(entry.get("site_items") or entry.get("marketplace_items") or [entry])
        base = payloads[index] if index < len(payloads) and isinstance(payloads[index], Mapping) else {}
        for candidate in candidates:
            if not isinstance(candidate, Mapping) or candidate.get("error"):
                continue
            item_id = str(candidate.get("item_id") or candidate.get("id") or "")
            if not re.fullmatch(rf"{re.escape(site_id)}\d+", item_id):
                continue
            item = {key: base[key] for key in ("title", "pictures", "attributes", "category_id", "listing_type_id", "seller_custom_field") if key in base}
            item.update(title=item.get("title") or product.get("title") or "", available_quantity=quantity,
                        status="active", site_id=site_id)
            proceeds = base.get("global_net_proceeds") or base.get("net_proceeds")
            if proceeds is not None and not isinstance(proceeds, (dict, list)):
                item["net_proceeds"] = {"amount": proceeds, "currency_id": "USD"}
            item.update(candidate)
            item.update(id=item_id, site_id=site_id)
            item["permalink"] = candidate.get("permalink") or _public_item_url(item_id, site_id)
            items[item_id] = item
    if not items:
        raise ValueError("平台暂未返回目标站点商品编号，店铺链接尚未写入")
    with store_link_item_locks(int(token["id"]), list(items), owner="publish-store-links"):
        saved = replace_store_snapshot(token, items.values(), finalize=False)
    invalidate_store_link_metadata_cache()
    return saved
