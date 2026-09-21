"""Create or update Product Ads campaigns for store links."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _number(name, value, *, minimum, maximum=None):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name}必须是有效数字") from exc
    if number < Decimal(str(minimum)) or (
        maximum is not None and number > Decimal(str(maximum))
    ):
        suffix = f"，且不能大于 {maximum}" if maximum is not None else ""
        raise ValueError(f"{name}不能小于 {minimum}{suffix}")
    return float(number)


def _find_campaign(client, site_id, advertiser_id, name):
    offset = 0
    while offset < 1000:
        page = client.search_product_ads_campaigns(site_id, advertiser_id, limit=50, offset=offset)
        results = list(page.get("results") or [])
        for campaign in results:
            if str(campaign.get("name") or "").strip() == name and str(
                campaign.get("status") or ""
            ).lower() != "deleted":
                return dict(campaign)
        total = int((page.get("paging") or {}).get("total") or len(results))
        offset += len(results)
        if not results or offset >= total:
            break
    return None


def advertise_store_link(link_id, *, budget, roas_target, campaign_name=""):
    from bit import bit_mysql
    from bit.bit_store_link_sync import _client_and_token
    from erp.mercadolibre_store_link_store import get_store_links_by_ids

    daily_budget = _number("每日预算", budget, minimum="0.01")
    target_roas = _number("目标 ROAS", roas_target, minimum=1, maximum=35)
    row = get_store_links_by_ids([link_id])[0]
    if not row.get("is_current", True) or str(row.get("status") or "").lower() != "active":
        raise ValueError("只有当前在售的商品链接可以投放广告")
    site_id = str(row.get("site_id") or "").strip().upper()
    item_id = str(row.get("item_id") or "").strip().upper()
    if not site_id or site_id == "CBT" or not item_id:
        raise ValueError("商品站点或商品编号不完整，无法投放广告")

    token = dict(bit_mysql.get_mercado_store_token(int(row["token_id"])) or {})
    if not token:
        raise ValueError("店铺授权不存在，请重新授权")
    client, _ = _client_and_token(token)

    advertisers = client.get_product_ads_advertisers()
    advertiser = next(
        (entry for entry in advertisers if str(entry.get("site_id") or "").upper() == site_id),
        None,
    )
    if not advertiser:
        raise ValueError(f"该店铺尚未开通 {site_id} 站点的 Product Ads")
    advertiser_id = int(advertiser["advertiser_id"])

    ad_group_page = client.search_product_ads_ad_groups(site_id, advertiser_id, item_id)
    ad_groups = list(ad_group_page.get("results") or [])
    if not ad_groups:
        raise ValueError("美客多未返回该商品的广告组，商品可能暂不具备投放资格")
    ad_group = next(
        (entry for entry in ad_groups if str(entry.get("ad_group_external_id") or "").upper() == item_id),
        ad_groups[0],
    )
    ad_group_id = int(ad_group["id"])
    previous_campaign_id = int(ad_group.get("campaign_id") or 0)

    name = str(campaign_name or "").strip() or f"链接广告-{item_id}"
    if len(name) > 100:
        raise ValueError("广告活动名称不能超过 100 个字符")
    payload = {
        "name": name,
        "status": "active",
        "budget": daily_budget,
        "strategy": "profitability",
        "channel": "marketplace",
        "roas_target": target_roas,
    }
    campaign = _find_campaign(client, site_id, advertiser_id, name)
    if campaign:
        campaign_id = int(campaign["id"])
        update_payload = {key: value for key, value in payload.items() if key != "channel"}
        campaign = client.update_product_ads_campaign(site_id, campaign_id, update_payload)
        created = False
    else:
        campaign = client.create_product_ads_campaign(site_id, advertiser_id, payload)
        campaign_id = int(campaign.get("id") or 0)
        if not campaign_id:
            raise RuntimeError("美客多创建广告活动后未返回活动编号")
        created = True

    client.activate_product_ads_ad_group(site_id, ad_group_id, campaign_id)
    return {
        "link_id": int(link_id),
        "item_id": item_id,
        "site_id": site_id,
        "advertiser_id": advertiser_id,
        "campaign_id": campaign_id,
        "campaign_name": name,
        "campaign_created": created,
        "ad_group_id": ad_group_id,
        "ad_group_type": str(ad_group.get("ad_group_type") or "ITEM").upper(),
        "previous_campaign_id": previous_campaign_id,
        "budget": daily_budget,
        "roas_target": target_roas,
        "currency_id": campaign.get("currency_id"),
    }


def advertise_store_links(link_ids, *, budget, roas_target, campaign_name=""):
    """Put multiple store links into Product Ads campaigns grouped by account/site.

    Mercado's Product Ads campaigns cannot span advertisers or sites.  A selection
    that contains multiple accounts is therefore split into one campaign per
    ``(token_id, site_id)`` pair while keeping the user supplied campaign name.
    Invalid/ineligible links are reported individually so one bad listing does not
    prevent the remaining selection from being activated.
    """

    from bit import bit_mysql
    from bit.bit_store_link_sync import _client_and_token
    from erp.mercadolibre_store_link_store import get_store_links_by_ids

    daily_budget = _number("每日预算", budget, minimum="0.01")
    target_roas = _number("目标 ROAS", roas_target, minimum=1, maximum=35)
    name = str(campaign_name or "").strip() or "批量链接广告"
    if len(name) > 100:
        raise ValueError("广告活动名称不能超过 100 个字符")

    rows = get_store_links_by_ids(link_ids)
    grouped = {}
    errors = []
    for row in rows:
        link_id = int(row["id"])
        item_id = str(row.get("item_id") or "").strip().upper()
        site_id = str(row.get("site_id") or "").strip().upper()
        if not row.get("is_current", True) or str(row.get("status") or "").lower() != "active":
            errors.append({"link_id": link_id, "item_id": item_id, "message": "只有当前在售的商品链接可以投放广告"})
            continue
        if not site_id or site_id == "CBT" or not item_id:
            errors.append({"link_id": link_id, "item_id": item_id, "message": "商品站点或商品编号不完整，无法投放广告"})
            continue
        grouped.setdefault((int(row["token_id"]), site_id), []).append((row, item_id))

    results = []
    campaigns = []
    client_cache = {}
    for (token_id, site_id), group_rows in grouped.items():
        try:
            if token_id not in client_cache:
                token = dict(bit_mysql.get_mercado_store_token(token_id) or {})
                if not token:
                    raise ValueError("店铺授权不存在，请重新授权")
                client, _ = _client_and_token(token)
                client_cache[token_id] = (client, list(client.get_product_ads_advertisers()))
            client, advertisers = client_cache[token_id]
            advertiser = next(
                (entry for entry in advertisers if str(entry.get("site_id") or "").upper() == site_id),
                None,
            )
            if not advertiser:
                raise ValueError(f"该店铺尚未开通 {site_id} 站点的 Product Ads")
            advertiser_id = int(advertiser["advertiser_id"])
        except Exception as exc:
            for row, item_id in group_rows:
                errors.append({"link_id": int(row["id"]), "item_id": item_id, "message": str(exc)})
            continue

        prepared = []
        for row, item_id in group_rows:
            try:
                page = client.search_product_ads_ad_groups(site_id, advertiser_id, item_id)
                ad_groups = list(page.get("results") or [])
                if not ad_groups:
                    raise ValueError("美客多未返回该商品的广告组，商品可能暂不具备投放资格")
                ad_group = next(
                    (entry for entry in ad_groups if str(entry.get("ad_group_external_id") or "").upper() == item_id),
                    ad_groups[0],
                )
                prepared.append((row, item_id, ad_group))
            except Exception as exc:
                errors.append({"link_id": int(row["id"]), "item_id": item_id, "message": str(exc)})

        if not prepared:
            continue

        try:
            payload = {
                "name": name,
                "status": "active",
                "budget": daily_budget,
                "strategy": "profitability",
                "channel": "marketplace",
                "roas_target": target_roas,
            }
            campaign = _find_campaign(client, site_id, advertiser_id, name)
            if campaign:
                campaign_id = int(campaign["id"])
                update_payload = {key: value for key, value in payload.items() if key != "channel"}
                campaign = client.update_product_ads_campaign(site_id, campaign_id, update_payload)
                created = False
            else:
                campaign = client.create_product_ads_campaign(site_id, advertiser_id, payload)
                campaign_id = int(campaign.get("id") or 0)
                if not campaign_id:
                    raise RuntimeError("美客多创建广告活动后未返回活动编号")
                created = True
        except Exception as exc:
            for row, item_id, _ad_group in prepared:
                errors.append({"link_id": int(row["id"]), "item_id": item_id, "message": str(exc)})
            continue

        activated = {}
        for row, item_id, ad_group in prepared:
            ad_group_id = int(ad_group["id"])
            try:
                if ad_group_id not in activated:
                    client.activate_product_ads_ad_group(site_id, ad_group_id, campaign_id)
                    activated[ad_group_id] = True
                results.append({
                    "link_id": int(row["id"]),
                    "item_id": item_id,
                    "site_id": site_id,
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign_id,
                    "campaign_name": name,
                    "ad_group_id": ad_group_id,
                    "ad_group_type": str(ad_group.get("ad_group_type") or "ITEM").upper(),
                    "previous_campaign_id": int(ad_group.get("campaign_id") or 0),
                })
            except Exception as exc:
                errors.append({"link_id": int(row["id"]), "item_id": item_id, "message": str(exc)})

        campaigns.append({
            "token_id": token_id,
            "store_name": str(group_rows[0][0].get("store_name") or ""),
            "site_id": site_id,
            "advertiser_id": advertiser_id,
            "campaign_id": campaign_id,
            "campaign_name": name,
            "campaign_created": created,
            "currency_id": campaign.get("currency_id"),
            "ad_group_count": len(activated),
        })

    return {
        "requested_count": len(rows),
        "success_count": len(results),
        "failure_count": len(errors),
        "campaign_count": len(campaigns),
        "activated_ad_group_count": sum(int(row["ad_group_count"]) for row in campaigns),
        "budget": daily_budget,
        "roas_target": target_roas,
        "campaign_name": name,
        "campaigns": campaigns,
        "results": results,
        "errors": errors,
    }
