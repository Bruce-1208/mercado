"""Live Product Ads analysis across every authorized store and advertised link."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Iterable


AD_METRICS = (
    "clicks", "prints", "cost", "cpc", "ctr", "direct_amount",
    "indirect_amount", "total_amount", "direct_units_quantity",
    "indirect_units_quantity", "units_quantity", "direct_items_quantity",
    "indirect_items_quantity", "advertising_items_quantity", "acos", "tacos",
    "sov", "cvr", "roas",
)
AD_GROUP_PAGE_SIZE = 800
AD_ITEM_PAGE_SIZE = 50
CACHE_SECONDS = 300


def _worker_count(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


TOKEN_WORKERS = _worker_count("MERCADO_AD_ANALYSIS_TOKEN_WORKERS", 32, 64)
ACCOUNT_WORKERS = _worker_count("MERCADO_AD_ANALYSIS_ACCOUNT_WORKERS", 16, 32)
GROUP_EXPANSION_WORKERS = _worker_count(
    "MERCADO_AD_ANALYSIS_GROUP_WORKERS", 12, 32
)
API_CONCURRENCY = _worker_count(
    "MERCADO_AD_ANALYSIS_API_CONCURRENCY", 16, 64
)
_api_slots = threading.BoundedSemaphore(API_CONCURRENCY)
_cache_lock = threading.Lock()
_cache: dict[tuple[Any, ...], tuple[datetime, dict[str, Any]]] = {}


def _api_call(function, *args, **kwargs):
    """Keep nested worker pools below Mercado's observed rate-limit ceiling."""
    with _api_slots:
        return function(*args, **kwargs)


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _metrics(value: Any) -> dict[str, int | float]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, int | float] = {}
    integer_fields = {
        "clicks", "prints", "direct_units_quantity", "indirect_units_quantity",
        "units_quantity", "direct_items_quantity", "indirect_items_quantity",
        "advertising_items_quantity", "organic_units_quantity", "organic_items_quantity",
    }
    for name in AD_METRICS:
        result[name] = _integer(source.get(name)) if name in integer_fields else _float(source.get(name))
    return result


def _derived_metrics(metrics: dict[str, Any]) -> dict[str, int | float]:
    result = _metrics(metrics)
    cost = _float(result["cost"])
    revenue = _float(result["total_amount"])
    clicks = _integer(result["clicks"])
    prints = _integer(result["prints"])
    units = _integer(result["units_quantity"])
    if not _float(result["roas"]):
        result["roas"] = revenue / cost if cost else 0.0
    if not _float(result["acos"]):
        result["acos"] = cost / revenue * 100 if revenue else 0.0
    if not _float(result["ctr"]):
        result["ctr"] = clicks / prints * 100 if prints else 0.0
    if not _float(result["cvr"]):
        result["cvr"] = units / clicks * 100 if clicks else 0.0
    if not _float(result["cpc"]):
        result["cpc"] = cost / clicks if clicks else 0.0
    return result


def _sum_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, int | float]:
    totals = {name: 0 for name in AD_METRICS}
    for row in rows:
        metrics = row.get("metrics") if "metrics" in row else row
        for name in AD_METRICS:
            totals[name] += _float((metrics or {}).get(name))
    return _derived_metrics(totals)


def _date_range(date_from: str = "", date_to: str = "") -> tuple[str, str]:
    today = date.today()
    end = date.fromisoformat(str(date_to).strip()) if str(date_to).strip() else today
    start = date.fromisoformat(str(date_from).strip()) if str(date_from).strip() else end - timedelta(days=29)
    if end > today:
        raise ValueError("广告分析结束日期不能晚于今天")
    if start > end:
        raise ValueError("广告分析开始日期不能晚于结束日期")
    if (end - start).days >= 90:
        raise ValueError("广告分析一次最多查询 90 天")
    return start.isoformat(), end.isoformat()


def _pages(fetch_page, *, page_size: int) -> tuple[list[dict], dict]:
    offset = 0
    rows: list[dict] = []
    summary: dict = {}
    while True:
        page = dict(fetch_page(offset) or {})
        batch = [dict(row) for row in page.get("results") or []]
        rows.extend(batch)
        if not summary and isinstance(page.get("metrics_summary"), dict):
            summary = dict(page["metrics_summary"])
        paging = page.get("paging") or {}
        total = _integer(paging.get("total"))
        offset += len(batch)
        if not batch or (total and offset >= total) or len(batch) < page_size:
            break
    return rows, summary


def _public_item_url(item_id: str) -> str:
    item_id = str(item_id or "").strip().upper()
    bases = {
        "MLM": "https://articulo.mercadolibre.com.mx",
        "MLB": "https://produto.mercadolivre.com.br",
        "MLC": "https://articulo.mercadolibre.cl",
        "MCO": "https://articulo.mercadolibre.com.co",
        "MLA": "https://articulo.mercadolibre.com.ar",
        "MLU": "https://articulo.mercadolibre.com.uy",
        "MPE": "https://articulo.mercadolibre.com.pe",
        "MEC": "https://articulo.mercadolibre.com.ec",
    }
    prefix = item_id[:3]
    digits = item_id[3:]
    return f"{bases[prefix]}/{prefix}-{digits}-_JM" if prefix in bases and digits.isdigit() else ""


def _campaigns(client, site_id: str, advertiser_id: int) -> dict[int, dict]:
    try:
        rows, _ = _pages(
            lambda offset: _api_call(
                client.search_product_ads_campaigns,
                site_id, advertiser_id, limit=50, offset=offset
            ),
            page_size=50,
        )
    except Exception as exc:
        # Mercado returns 404, rather than an empty collection, for an
        # advertiser that is enabled but has not created a campaign yet.
        if "advertiser_campaigns_not_found" in str(exc):
            return {}
        raise
    return {_integer(row.get("id")): row for row in rows if _integer(row.get("id"))}


def _campaign_groups(
    client, site_id: str, advertiser_id: int, campaign_id: int,
    date_from: str, date_to: str,
) -> tuple[list[dict], dict]:
    return _pages(
        lambda offset: _api_call(
            client.search_product_ads_ad_groups_metrics,
            site_id,
            advertiser_id,
            date_from=date_from,
            date_to=date_to,
            metrics=AD_METRICS,
            limit=AD_GROUP_PAGE_SIZE,
            offset=offset,
            metrics_summary=True,
            campaign_id=campaign_id,
        ),
        page_size=AD_GROUP_PAGE_SIZE,
    )


def _group_items(client, site_id: str, group: dict, date_from: str, date_to: str) -> list[dict]:
    group_type = str(group.get("ad_group_type") or "ITEM").upper()
    if group_type == "ITEM":
        item_id = str(group.get("ad_group_external_id") or "").strip().upper()
        return [{
            "item_id": item_id,
            "title": group.get("title") or "",
            "status": group.get("status") or "",
            "metrics": group.get("metrics") or {},
            "catalog_listing": bool(group.get("catalog_listing")),
        }] if item_id else []
    rows, _ = _pages(
        lambda offset: _api_call(
            client.list_product_ads_ad_group_ads,
            site_id,
            _integer(group.get("id")),
            date_from=date_from,
            date_to=date_to,
            metrics=AD_METRICS,
            limit=AD_ITEM_PAGE_SIZE,
            offset=offset,
        ),
        page_size=AD_ITEM_PAGE_SIZE,
    )
    return rows


def _advertiser_analysis(client, token: dict, advertiser: dict, date_from: str, date_to: str) -> dict:
    site_id = str(advertiser.get("site_id") or "").strip().upper()
    advertiser_id = _integer(advertiser.get("advertiser_id"))
    campaigns = _campaigns(client, site_id, advertiser_id)
    campaign_results: list[tuple[list[dict], dict]] = []
    if campaigns:
        with ThreadPoolExecutor(max_workers=min(len(campaigns), 8)) as executor:
            futures = [
                executor.submit(
                    _campaign_groups,
                    client,
                    site_id,
                    advertiser_id,
                    campaign_id,
                    date_from,
                    date_to,
                )
                for campaign_id in campaigns
            ]
            for future in as_completed(futures):
                campaign_results.append(future.result())

    # The unfiltered endpoint includes every eligible store product and rejects
    # pagination after 10,000 rows.  Campaign filters keep the response limited
    # to products that are actually advertised.
    groups_by_id: dict[int, dict] = {}
    metric_rows: list[dict] = []
    for campaign_groups, campaign_summary in campaign_results:
        for group in campaign_groups:
            group_id = _integer(group.get("id"))
            if group_id:
                groups_by_id[group_id] = group
        metric_rows.append({
            "metrics": campaign_summary or _sum_metrics(campaign_groups)
        })
    groups = list(groups_by_id.values())
    summary = _sum_metrics(metric_rows) if metric_rows else {}

    links: list[dict] = []
    expand_groups = [group for group in groups if str(group.get("ad_group_type") or "ITEM").upper() != "ITEM"]
    expanded: dict[int, list[dict]] = {}
    expansion_errors: list[dict] = []
    if expand_groups:
        with ThreadPoolExecutor(
            max_workers=min(GROUP_EXPANSION_WORKERS, len(expand_groups))
        ) as executor:
            futures = {
                executor.submit(_group_items, client, site_id, group, date_from, date_to): group
                for group in expand_groups
            }
            for future in as_completed(futures):
                group = futures[future]
                group_id = _integer(group.get("id"))
                try:
                    expanded[group_id] = future.result()
                except Exception as exc:
                    expanded[group_id] = []
                    expansion_errors.append({
                        "token_id": _integer(token.get("id")),
                        "store_name": str(token.get("display_name") or token.get("nickname") or ""),
                        "site_id": site_id,
                        "advertiser_id": advertiser_id,
                        "ad_group_id": group_id,
                        "message": f"广告组链接展开失败：{exc}",
                    })

    for group in groups:
        group_id = _integer(group.get("id"))
        campaign_id = _integer(group.get("campaign_id"))
        campaign = campaigns.get(campaign_id, {})
        items = expanded.get(group_id)
        if items is None:
            items = _group_items(client, site_id, group, date_from, date_to)
        for item in items:
            item_id = str(item.get("item_id") or item.get("ad_group_external_id") or "").strip().upper()
            if not item_id:
                continue
            links.append({
                "token_id": _integer(token.get("id")),
                "store_name": str(token.get("display_name") or token.get("nickname") or token.get("id") or ""),
                "site_id": site_id,
                "advertiser_id": advertiser_id,
                "advertiser_name": str(advertiser.get("advertiser_name") or ""),
                "account_name": str(advertiser.get("account_name") or ""),
                "item_id": item_id,
                "title": str(item.get("title") or group.get("title") or ""),
                "permalink": _public_item_url(item_id),
                "status": str(item.get("status") or group.get("status") or "").upper(),
                "catalog_listing": bool(item.get("catalog_listing", group.get("catalog_listing"))),
                "campaign_id": campaign_id,
                "campaign_name": str(campaign.get("name") or ""),
                "campaign_status": str(campaign.get("status") or "").upper(),
                "campaign_budget": _float(campaign.get("budget")),
                "currency_id": str(campaign.get("currency_id") or ""),
                "ad_group_id": group_id,
                "ad_group_type": str(group.get("ad_group_type") or "ITEM").upper(),
                "ad_group_status": str(group.get("status") or "").upper(),
                "metrics": _derived_metrics(item.get("metrics") or group.get("metrics") or {}),
            })

    currency_id = next(
        (str(row.get("currency_id") or "") for row in campaigns.values() if row.get("currency_id")),
        "",
    )
    account_metrics = _derived_metrics(summary) if summary else _sum_metrics(groups)
    active_campaigns = sum(
        1 for row in campaigns.values() if str(row.get("status") or "").lower() == "active"
    )
    account = {
        "token_id": _integer(token.get("id")),
        "store_name": str(token.get("display_name") or token.get("nickname") or token.get("id") or ""),
        "site_id": site_id,
        "advertiser_id": advertiser_id,
        "advertiser_name": str(advertiser.get("advertiser_name") or ""),
        "account_name": str(advertiser.get("account_name") or ""),
        "currency_id": currency_id,
        "campaign_count": len(campaigns),
        "active_campaign_count": active_campaigns,
        "ad_group_count": len(groups),
        "link_count": len(links),
        "metrics": account_metrics,
    }
    return {"account": account, "links": links, "errors": expansion_errors}


def _summary(accounts: list[dict], links: list[dict], errors: list[dict]) -> dict:
    currencies: dict[str, list[dict]] = {}
    for account in accounts:
        currencies.setdefault(str(account.get("currency_id") or "未标明币种"), []).append(account)
    money = []
    for currency_id, rows in sorted(currencies.items()):
        metrics = _sum_metrics(rows)
        money.append({"currency_id": currency_id, "metrics": metrics})
    additive = _sum_metrics(accounts)
    additive.update({
        "account_count": len(accounts),
        "link_count": len(links),
        "store_count": len({row.get("token_id") for row in accounts}),
        "error_count": len(errors),
        "currencies": money,
    })
    return additive


def collect_ad_analysis(
    *, date_from: str = "", date_to: str = "", token_ids: Iterable[int] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Collect a read-only live snapshot for every enabled, selected Mercado token."""
    from bit import bit_mysql
    from bit.bit_store_link_sync import _client_and_token

    start, end = _date_range(date_from, date_to)
    selected = (
        None if token_ids is None else
        tuple(sorted({int(value) for value in token_ids or () if int(value or 0) > 0}))
    )
    cache_key = (start, end, selected)
    now = datetime.now()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if not force and cached and (now - cached[0]).total_seconds() < CACHE_SECONDS:
            result = dict(cached[1])
            result["cached"] = True
            return result

    summaries = (bit_mysql.list_mercado_store_tokens() or {}).get("rows") or []
    tokens = [
        dict(bit_mysql.get_mercado_store_token(int(row["id"])) or {})
        for row in summaries
        if bool(row.get("enabled", True))
        and (selected is None or int(row.get("id") or 0) in selected)
    ]
    accounts: list[dict] = []
    links: list[dict] = []
    errors: list[dict] = []

    def discover_advertisers(token: dict):
        store_name = str(token.get("display_name") or token.get("nickname") or token.get("id") or "")
        try:
            client, token = _client_and_token(token)
            advertisers = _api_call(client.get_product_ads_advertisers)
        except Exception as exc:
            return None, token, [], [{
                "token_id": _integer(token.get("id")),
                "store_name": store_name,
                "message": str(exc),
            }]
        if not advertisers:
            return client, token, [], [{
                "token_id": _integer(token.get("id")),
                "store_name": store_name,
                "message": "未开通 Product Ads",
            }]
        return client, token, [dict(row) for row in advertisers], []

    advertiser_jobs: list[tuple[Any, dict, dict]] = []
    if tokens:
        with ThreadPoolExecutor(max_workers=min(TOKEN_WORKERS, len(tokens))) as executor:
            futures = [executor.submit(discover_advertisers, token) for token in tokens]
            for future in as_completed(futures):
                client, token, advertisers, discovery_errors = future.result()
                errors.extend(discovery_errors)
                advertiser_jobs.extend(
                    (client, token, advertiser) for advertiser in advertisers
                )

    def analyze_advertiser(client, token: dict, advertiser: dict):
        store_name = str(
            token.get("display_name") or token.get("nickname") or token.get("id") or ""
        )
        try:
            return _advertiser_analysis(client, token, advertiser, start, end), None
        except Exception as exc:
            return None, {
                "token_id": _integer(token.get("id")),
                "store_name": store_name,
                "site_id": str(advertiser.get("site_id") or ""),
                "advertiser_id": _integer(advertiser.get("advertiser_id")),
                "message": str(exc),
            }

    if advertiser_jobs:
        with ThreadPoolExecutor(
            max_workers=min(ACCOUNT_WORKERS, len(advertiser_jobs))
        ) as executor:
            futures = [
                executor.submit(analyze_advertiser, client, token, advertiser)
                for client, token, advertiser in advertiser_jobs
            ]
            for future in as_completed(futures):
                result, error = future.result()
                if error:
                    errors.append(error)
                    continue
                if not result:
                    continue
                accounts.append(result["account"])
                links.extend(result["links"])
                errors.extend(result.get("errors") or [])

    accounts.sort(key=lambda row: (_float(row["metrics"].get("cost")), row.get("store_name", "")), reverse=True)
    links.sort(key=lambda row: (_float(row["metrics"].get("cost")), row.get("item_id", "")), reverse=True)
    result = {
        "date_from": start,
        "date_to": end,
        "generated_at": now.replace(microsecond=0).isoformat(sep=" "),
        "summary": _summary(accounts, links, errors),
        "accounts": accounts,
        "links": links,
        "errors": errors,
        "cached": False,
    }
    with _cache_lock:
        _cache[cache_key] = (now, result)
    return dict(result)


__all__ = ["AD_METRICS", "collect_ad_analysis"]
