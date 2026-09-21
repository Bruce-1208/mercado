"""Live Product Ads analysis across every authorized store and advertised link."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from bit.bit_runtime_lock import RUNTIME_LOCK_DIR


AD_METRICS = (
    "clicks", "prints", "cost", "cpc", "ctr", "direct_amount",
    "indirect_amount", "total_amount", "direct_units_quantity",
    "indirect_units_quantity", "units_quantity", "direct_items_quantity",
    "indirect_items_quantity", "advertising_items_quantity", "acos", "tacos",
    "sov", "cvr", "roas",
)
AD_GROUP_PAGE_SIZE = 800
AD_ITEM_PAGE_SIZE = 50
AD_ANALYSIS_STATE_PATH = Path(
    os.environ.get("BIT_AD_ANALYSIS_STATE_PATH")
    or (RUNTIME_LOCK_DIR / "ad_analysis_last_snapshot.json")
)


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
PAGE_WORKERS = _worker_count("MERCADO_AD_ANALYSIS_PAGE_WORKERS", 4, 8)
_api_slots = threading.BoundedSemaphore(API_CONCURRENCY)
_cache_lock = threading.Lock()
_snapshot_lock = threading.Lock()
_cache: dict[tuple[Any, ...], tuple[datetime, dict[str, Any]]] = {}


def _empty_snapshot() -> dict[str, Any]:
    return {
        "date_from": "",
        "date_to": "",
        "generated_at": "",
        "summary": {},
        "accounts": [],
        "links": [],
        "errors": [],
        "cached": True,
        "snapshot_available": False,
    }


def _load_snapshot(state_path=None) -> dict[str, Any]:
    path = Path(state_path or AD_ANALYSIS_STATE_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return _empty_snapshot()
    if not isinstance(payload, dict):
        return _empty_snapshot()
    result = _empty_snapshot()
    result.update(payload)
    for name in ("accounts", "links", "errors"):
        result[name] = [
            dict(row) for row in (result.get(name) or []) if isinstance(row, dict)
        ]
    result["snapshot_available"] = bool(result.get("generated_at"))
    result["cached"] = True
    return result


def _persist_snapshot(snapshot: dict[str, Any], state_path=None) -> bool:
    path = Path(state_path or AD_ANALYSIS_STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(
        f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    payload = copy.deepcopy(snapshot)
    payload["cached"] = True
    payload["snapshot_available"] = True
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
        return True
    except OSError as exc:
        logging.warning("无法保存广告分析上次结果：%s", exc)
        return False
    finally:
        try:
            temporary_path.unlink()
        except OSError:
            pass


def _api_call(function, *args, **kwargs):
    """Keep nested worker pools below Mercado's observed rate-limit ceiling."""
    with _api_slots:
        return function(*args, **kwargs)


def _normalize_ad_group_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    aliases = {
        "start": "active",
        "started": "active",
        "enable": "active",
        "enabled": "active",
        "activate": "active",
        "pause": "paused",
        "paused": "paused",
        "active": "active",
    }
    normalized = aliases.get(status)
    if normalized not in {"active", "paused"}:
        raise ValueError("广告操作状态只能是 active（启动）或 paused（暂停）")
    return normalized


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
    """Read a paginated API result, fetching known remaining pages in parallel."""
    first_page = dict(fetch_page(0) or {})
    first_batch = [dict(row) for row in first_page.get("results") or []]
    summary = (
        dict(first_page["metrics_summary"])
        if isinstance(first_page.get("metrics_summary"), dict)
        else {}
    )
    paging = first_page.get("paging") or {}
    total = _integer(paging.get("total"))
    first_offset = len(first_batch)

    if not first_batch or len(first_batch) < page_size:
        return first_batch, summary
    if total and first_offset >= total:
        return first_batch, summary

    # Mercado normally returns a total. When it does not, retain the old
    # sequential fallback because there is no safe way to know the offsets.
    if not total:
        rows = list(first_batch)
        offset = first_offset
        while True:
            page = dict(fetch_page(offset) or {})
            batch = [dict(row) for row in page.get("results") or []]
            rows.extend(batch)
            if not summary and isinstance(page.get("metrics_summary"), dict):
                summary = dict(page["metrics_summary"])
            offset += len(batch)
            if not batch or len(batch) < page_size:
                break
        return rows, summary

    offsets = list(range(first_offset, total, page_size))
    rows_by_offset: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=min(PAGE_WORKERS, len(offsets))) as executor:
        futures = {offset: executor.submit(fetch_page, offset) for offset in offsets}
        for offset in offsets:
            page = dict(futures[offset].result() or {})
            rows_by_offset[offset] = [dict(row) for row in page.get("results") or []]
            if not summary and isinstance(page.get("metrics_summary"), dict):
                summary = dict(page["metrics_summary"])

    rows = list(first_batch)
    for offset in offsets:
        rows.extend(rows_by_offset[offset])
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


def _store_assignment(token: dict, site_id: str) -> tuple[str, str]:
    settings = [
        row for row in (token.get("site_settings") or [])
        if str(row.get("site_id") or "").strip().upper() == site_id
    ]
    if not settings:
        settings = list(token.get("site_settings") or [])
    setting = settings[0] if settings else {}
    return (
        str(setting.get("salesperson") or "").strip(),
        str(setting.get("group_name") or "").strip(),
    )


def _advertiser_analysis(client, token: dict, advertiser: dict, date_from: str, date_to: str) -> dict:
    site_id = str(advertiser.get("site_id") or "").strip().upper()
    advertiser_id = _integer(advertiser.get("advertiser_id"))
    salesperson, group_name = _store_assignment(token, site_id)
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
                "salesperson": salesperson,
                "group_name": group_name,
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
        "salesperson": salesperson,
        "group_name": group_name,
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


def _filtered_snapshot(
    snapshot: dict[str, Any], token_ids: Iterable[int] | None
) -> dict[str, Any]:
    result = copy.deepcopy(snapshot)
    if token_ids is None:
        result["summary"] = _summary(
            result.get("accounts") or [],
            result.get("links") or [],
            result.get("errors") or [],
        )
        return result
    selected = {int(value) for value in token_ids or () if int(value or 0) > 0}
    for name in ("accounts", "links", "errors"):
        result[name] = [
            row for row in (result.get(name) or [])
            if _integer(row.get("token_id")) in selected
        ]
    result["summary"] = _summary(result["accounts"], result["links"], result["errors"])
    return result


def _merge_snapshot(
    previous: dict[str, Any], fresh: dict[str, Any], refreshed_token_ids: Iterable[int]
) -> dict[str, Any]:
    selected = {
        int(value) for value in refreshed_token_ids or () if int(value or 0) > 0
    }
    if not previous.get("snapshot_available") or (
        previous.get("date_from"), previous.get("date_to")
    ) != (fresh.get("date_from"), fresh.get("date_to")):
        result = copy.deepcopy(fresh)
        result["partial_snapshot"] = True
        return result
    result = copy.deepcopy(previous)
    for name in ("accounts", "links", "errors"):
        result[name] = [
            row for row in (result.get(name) or [])
            if _integer(row.get("token_id")) not in selected
        ] + copy.deepcopy(fresh.get(name) or [])
    result.update({
        "date_from": fresh.get("date_from") or previous.get("date_from") or "",
        "date_to": fresh.get("date_to") or previous.get("date_to") or "",
        "generated_at": fresh.get("generated_at") or previous.get("generated_at") or "",
        "cached": False,
        "snapshot_available": True,
        "partial_snapshot": False,
        "refreshed_token_ids": sorted(selected),
    })
    result["accounts"].sort(
        key=lambda row: (_float((row.get("metrics") or {}).get("cost")), row.get("store_name", "")),
        reverse=True,
    )
    result["links"].sort(
        key=lambda row: (_float((row.get("metrics") or {}).get("cost")), row.get("item_id", "")),
        reverse=True,
    )
    result["summary"] = _summary(result["accounts"], result["links"], result["errors"])
    return result


def _update_snapshot_group_status(rows: Iterable[dict[str, Any]], status: str) -> None:
    keys = {
        (
            _integer(row.get("token_id")),
            str(row.get("site_id") or "").strip().upper(),
            _integer(row.get("ad_group_id")),
        )
        for row in rows or ()
    }
    if not keys:
        return
    with _snapshot_lock:
        snapshot = _load_snapshot()
        if not snapshot.get("snapshot_available"):
            return
        changed = False
        for row in snapshot.get("links") or []:
            key = (
                _integer(row.get("token_id")),
                str(row.get("site_id") or "").strip().upper(),
                _integer(row.get("ad_group_id")),
            )
            if key in keys:
                row["ad_group_status"] = status.upper()
                row["status"] = status.upper()
                changed = True
        if changed:
            _persist_snapshot(snapshot)


def update_product_ads_ad_groups(
    rows: Iterable[dict[str, Any]], *, status: str
) -> dict[str, Any]:
    """Update the status of selected Product Ads groups.

    The analysis response can contain several link rows for one catalog/family
    group.  Collapse those rows before calling Mercado so a batch action changes
    each ad group once and reports failures at the same level as the API call.
    """
    from bit import bit_mysql
    from bit.bit_store_link_sync import _client_and_token

    target_status = _normalize_ad_group_status(status)
    requested_rows = list(rows or [])
    if not requested_rows:
        raise ValueError("请至少选择一个广告组")
    if len(requested_rows) > 500:
        raise ValueError("单次最多操作 500 条广告链接")

    groups: dict[tuple[int, str, int], dict[str, Any]] = {}
    for index, row in enumerate(requested_rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条广告选择无效")
        token_id = _integer(row.get("token_id"))
        site_id = str(row.get("site_id") or "").strip().upper()
        ad_group_id = _integer(row.get("ad_group_id"))
        campaign_id = _integer(row.get("campaign_id"))
        if token_id <= 0 or not site_id or ad_group_id <= 0 or campaign_id <= 0:
            raise ValueError(f"第 {index} 条广告缺少店铺、站点、活动或广告组编号")
        key = (token_id, site_id, ad_group_id)
        group = groups.setdefault(
            key,
            {
                "token_id": token_id,
                "site_id": site_id,
                "ad_group_id": ad_group_id,
                "campaign_id": campaign_id,
                "item_ids": [],
            },
        )
        item_id = str(row.get("item_id") or "").strip().upper()
        if item_id and item_id not in group["item_ids"]:
            group["item_ids"].append(item_id)

    client_cache: dict[int, Any] = {}
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for group in groups.values():
        token_id = int(group["token_id"])
        site_id = str(group["site_id"])
        try:
            cached_client = client_cache.get(token_id)
            if cached_client is None:
                token = dict(bit_mysql.get_mercado_store_token(token_id) or {})
                if not token:
                    raise ValueError("店铺授权不存在，请重新授权")
                cached_client = _client_and_token(token)[0]
                client_cache[token_id] = cached_client
            _api_call(
                cached_client.update_product_ads_ad_group,
                site_id,
                int(group["ad_group_id"]),
                int(group["campaign_id"]),
                target_status,
            )
            results.append({
                **group,
                "status": target_status,
                "result": "succeeded",
            })
        except Exception as exc:
            errors.append({
                **group,
                "status": target_status,
                "result": "failed",
                "message": str(exc),
            })

    # Do not serve a pre-action status snapshot after a successful write.
    with _cache_lock:
        _cache.clear()
    _update_snapshot_group_status(results, target_status)
    return {
        "status": target_status,
        "requested_count": len(requested_rows),
        "ad_group_count": len(groups),
        "success_count": len(results),
        "failure_count": len(errors),
        "results": results,
        "errors": errors,
    }


def collect_ad_analysis(
    *, date_from: str = "", date_to: str = "", token_ids: Iterable[int] | None = None,
    refresh_token_ids: Iterable[int] | None = None, force: bool = False,
) -> dict[str, Any]:
    """Return the last snapshot, optionally refreshing all or selected stores."""
    from bit import bit_mysql
    from bit.bit_store_link_sync import _client_and_token

    visible = (
        None if token_ids is None else
        tuple(sorted({int(value) for value in token_ids or () if int(value or 0) > 0}))
    )
    if not force:
        return _filtered_snapshot(_load_snapshot(), visible)

    start, end = _date_range(date_from, date_to)
    refresh_scope = (
        tuple(sorted({
            int(value) for value in refresh_token_ids or () if int(value or 0) > 0
        }))
        if refresh_token_ids is not None
        else visible
    )
    if refresh_scope is not None and not refresh_scope:
        return _filtered_snapshot(_load_snapshot(), visible)
    cache_key = (start, end, refresh_scope)
    now = datetime.now()

    summaries = (bit_mysql.list_mercado_store_tokens() or {}).get("rows") or []
    token_summaries = [
        row for row in summaries
        if bool(row.get("enabled", True))
        and (refresh_scope is None or int(row.get("id") or 0) in refresh_scope)
    ]
    token_details = bit_mysql.get_mercado_store_tokens(
        [int(row["id"]) for row in token_summaries]
    ) if token_summaries else {}
    tokens = [
        {
            **dict(token_details.get(int(row["id"])) or {}),
            "site_settings": copy.deepcopy(row.get("site_settings") or []),
        }
        for row in token_summaries
        if token_details.get(int(row["id"]))
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
        "snapshot_available": True,
        "partial_snapshot": False,
    }
    with _snapshot_lock:
        previous = _load_snapshot()
        if refresh_scope is not None:
            result = _merge_snapshot(previous, result, refresh_scope)
        if not (result.get("partial_snapshot") and previous.get("snapshot_available")):
            _persist_snapshot(result)
    with _cache_lock:
        _cache[cache_key] = (now, result)
    return _filtered_snapshot(result, visible)


__all__ = [
    "AD_METRICS",
    "collect_ad_analysis",
    "update_product_ads_ad_groups",
]
