"""Create or update a dedicated Product Ads campaign for one store link."""

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
