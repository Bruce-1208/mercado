"""Sales by Mercado Libre site and selected official category level."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from collections import OrderedDict, defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from bit.bit_mysql import config, pymysql
from erp.mercadolibre_category_tree import category_paths_for_ids
from erp.mercadolibre_translation import translate_texts


SITE_NAMES = {
    "MLM": "墨西哥", "MLB": "巴西", "MLA": "阿根廷",
    "MLC": "智利", "MCO": "哥伦比亚", "MLU": "乌拉圭",
}
_CATEGORY_LEVELS = range(1, 7)
_ANALYSIS_CACHE = OrderedDict()
_ANALYSIS_CACHE_LOCK = threading.Lock()
_ANALYSIS_REQUEST_LOCKS = [threading.Lock() for _ in range(16)]
_ANALYSIS_CACHE_SECONDS = 60
_ANALYSIS_CACHE_LIMIT = 128
_ANALYSIS_CACHE_DIR = Path(__file__).resolve().parents[1] / ".data" / "store-analysis-cache"
_CATEGORY_TRANSLATION_LOCK = threading.Lock()
_CATEGORY_TRANSLATION_CACHE: dict[tuple[str, str], str] = {}


def _normalize_category_level(category_level=2):
    if category_level in (None, ""):
        category_level = 2
    try:
        level = int(category_level)
    except (TypeError, ValueError) as exc:
        raise ValueError("类目层级请选择 1 至 6 级") from exc
    if level not in _CATEGORY_LEVELS:
        raise ValueError("类目层级请选择 1 至 6 级")
    return level


def _category_source_language(category_id):
    site_id = str(category_id or "").strip().upper()[:3]
    if site_id == "MLB":
        return "pt-BR"
    if site_id == "CBT":
        return "en"
    return "es"


def _translate_category_names(paths, category_level):
    """Translate the selected category names once and reuse them across requests."""
    labels_by_id = {}
    names_by_language = defaultdict(set)
    for item_category_id, path in paths.items():
        if len(path) < category_level:
            continue
        node = path[category_level - 1]
        category_id = str(node.get("id") or "").strip().upper()
        name = str(node.get("name") or "").strip()
        if not category_id or not name:
            continue
        language = _category_source_language(item_category_id)
        labels_by_id[category_id] = (language, name)
        names_by_language[language].add(name)

    translated_by_label = {}
    for language, names in names_by_language.items():
        pending = []
        with _CATEGORY_TRANSLATION_LOCK:
            for name in sorted(names):
                cached = _CATEGORY_TRANSLATION_CACHE.get((language, name))
                if cached:
                    translated_by_label[(language, name)] = cached
                else:
                    pending.append(name)
        for offset in range(0, len(pending), 100):
            batch = pending[offset:offset + 100]
            translated = translate_texts(batch, language, "zh-CN")
            with _CATEGORY_TRANSLATION_LOCK:
                for source_name, chinese_name in zip(batch, translated):
                    _CATEGORY_TRANSLATION_CACHE[(language, source_name)] = chinese_name
                    translated_by_label[(language, source_name)] = chinese_name
                if len(_CATEGORY_TRANSLATION_CACHE) > 10000:
                    _CATEGORY_TRANSLATION_CACHE.clear()
                    _CATEGORY_TRANSLATION_CACHE.update(translated_by_label)

    return {
        category_id: translated_by_label.get((language, name), "")
        for category_id, (language, name) in labels_by_id.items()
    }


def _dates(start_date: str, end_date: str):
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("请选择有效的开始和结束日期") from exc
    if end < start or end - start > timedelta(days=365):
        raise ValueError("结束日期不能早于开始日期，统计区间最长 366 天")
    # Synced order timestamps are stored as UTC; the workbench uses Beijing dates.
    return (datetime.combine(start, datetime.min.time()) - timedelta(hours=8),
            datetime.combine(end + timedelta(days=1), datetime.min.time()) - timedelta(hours=8))


def summarize_store_analysis(rows, paths, *, metric="orders", category_level=2,
                              category_names_zh=None):
    """Aggregate order lines without counting a multi-item order twice per site."""
    category_level = _normalize_category_level(category_level)
    category_names_zh = category_names_zh or {}
    sites = {}
    for row in rows:
        site_id = str(row.get("site_id") or "").upper()
        if not site_id:
            continue
        site = sites.setdefault(site_id, {
            "site_id": site_id, "site_name": SITE_NAMES.get(site_id, site_id),
            "order_ids": set(), "gmv_usd": Decimal(0), "unconverted_orders": set(),
            "categories": defaultdict(lambda: {"order_ids": set(), "gmv_usd": Decimal(0)}),
        })
        order_id = str(row.get("order_id") or "")
        site["order_ids"].add(order_id)
        rate = row.get("usd_rate")
        amount = Decimal(str(row.get("total_amount") or 0))
        line_amount = Decimal(str(row.get("line_amount") or 0))
        order_line_amount = Decimal(str(row.get("order_line_amount") or 0))
        if rate is None and amount:
            site["unconverted_orders"].add(order_id)
        # The order's total is allocated by item value, preserving the order GMV.
        line_gmv = amount * line_amount / order_line_amount if order_line_amount > 0 else Decimal(0)
        usd_gmv = line_gmv * Decimal(str(rate)) if rate is not None else Decimal(0)
        site["gmv_usd"] += usd_gmv
        category_id = str(row.get("category_id") or "").strip().upper()
        path = paths.get(category_id) or []
        selected = path[category_level - 1] if len(path) >= category_level else None
        category_key = str(selected.get("id") or "").strip().upper() if selected else "__unknown__"
        category = site["categories"][category_key]
        category["original_name"] = str(selected.get("name") or category_key) if selected else "未识别分类"
        category["name_zh"] = category_names_zh.get(category_key, "") if selected else ""
        category["order_ids"].add(order_id)
        category["gmv_usd"] += usd_gmv

    result = []
    for site in sites.values():
        categories = []
        for key, value in site["categories"].items():
            if key == "__unknown__":
                continue
            category = {
                "category_id": key,
                "name": value["name_zh"] or (
                    value["original_name"]
                    if any("\u3400" <= character <= "\u9fff" for character in value["original_name"])
                    else "分类名称暂不可用"
                ),
                "orders": len(value["order_ids"]),
                "gmv_usd": round(float(value["gmv_usd"]), 2),
            }
            if value["name_zh"]:
                category["name_zh"] = value["name_zh"]
                category["original_name"] = value["original_name"]
            categories.append(category)
        categories.sort(key=lambda item: (-item["gmv_usd"] if metric == "gmv" else -item["orders"], item["name"]))
        known_top = categories[:10]
        total_metric = float(site["gmv_usd"]) if metric == "gmv" else len(site["order_ids"])
        # One order can contain multiple categories. Pie values therefore use
        # category order appearances; the site order count remains unique.
        category_total = sum(item["gmv_usd"] if metric == "gmv" else item["orders"] for item in categories)
        unknown = site["categories"].get("__unknown__")
        unknown_orders = len(unknown["order_ids"]) if unknown else 0
        unknown_gmv = float(unknown["gmv_usd"]) if unknown else 0
        if unknown:
            category_total += float(unknown["gmv_usd"]) if metric == "gmv" else len(unknown["order_ids"])
        other_value = max(0, category_total - sum(item["gmv_usd"] if metric == "gmv" else item["orders"] for item in known_top))
        result.append({
            "site_id": site["site_id"], "site_name": site["site_name"],
            "orders": len(site["order_ids"]), "gmv_usd": round(float(site["gmv_usd"]), 2),
            "unconverted_orders": len(site["unconverted_orders"]),
            "top_categories": known_top, "other_value": round(other_value, 2),
            "unrecognized_category_orders": unknown_orders,
            "unrecognized_category_gmv_usd": round(unknown_gmv, 2),
            "category_total": round(category_total, 2), "metric_total": round(total_metric, 2),
        })
    result.sort(key=lambda item: (-item["gmv_usd"] if metric == "gmv" else -item["orders"], item["site_id"]))
    return result


def summarize_order_trend(rows, start_date, end_date):
    """Seven Beijing days ending at the selected end date, all order statuses."""
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    counts = {str(row["order_date"]): int(row["orders"]) for row in rows}
    days = []
    for offset in range(6, -1, -1):
        day = end - timedelta(days=offset)
        count = counts.get(day.isoformat(), 0) if day >= start else None
        previous_day = day - timedelta(days=1)
        previous = counts.get(previous_day.isoformat(), 0) if previous_day >= start else None
        change = None if count is None or previous in (None, 0) else round((count - previous) / previous * 100, 2)
        days.append({"date": day.isoformat(), "orders": count, "previous_orders": previous,
                     "change_rate": change})
    return {"total_orders": sum(counts.values()), "days": days}


def _analysis_cache_path(key):
    serialized = json.dumps(key, ensure_ascii=False, separators=(",", ":"), default=list)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return _ANALYSIS_CACHE_DIR / f"{digest}.json"


def _read_persisted_analysis(key):
    try:
        payload = json.loads(_analysis_cache_path(key).read_text(encoding="utf-8"))
        result = payload.get("result")
        if not isinstance(result, dict) or not result.get("computed_at"):
            return None
        return result
    except (OSError, ValueError, TypeError):
        return None


def _write_persisted_analysis(key, result):
    target = _analysis_cache_path(key)
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as stream:
            temporary = stream.name
            json.dump({"result": result}, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, target)
        temporary = None
        cache_files = sorted(target.parent.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in cache_files[_ANALYSIS_CACHE_LIMIT:]:
            stale.unlink(missing_ok=True)
    except OSError:
        # Disk caching is an optimization; a successful analysis should still be returned.
        pass
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def list_store_analysis(start_date, end_date, *, metric="orders", salesperson="", group_name="",
                        token_id=None, allowed_token_ids=None, category_level=2, refresh=False):
    """Reuse the most recent result for an authorized query until explicitly refreshed."""
    category_level = _normalize_category_level(category_level)
    if metric not in {"orders", "gmv"}:
        raise ValueError("排序指标无效")
    _dates(start_date, end_date)
    token_id = int(token_id) if token_id is not None else None
    scope = tuple(sorted(int(value) for value in allowed_token_ids)) if allowed_token_ids is not None else None
    if token_id is not None:
        if token_id <= 0:
            raise ValueError("店铺参数无效")
        if scope is not None and token_id not in scope:
            raise PermissionError("无权查看所选店铺")
    key = ("order-trend-v1", start_date, end_date, metric, salesperson, group_name, token_id, scope, category_level)
    # Coalesce concurrent identical requests without serializing all analysis work.
    with _ANALYSIS_REQUEST_LOCKS[hash(key) % len(_ANALYSIS_REQUEST_LOCKS)]:
        if not refresh:
            with _ANALYSIS_CACHE_LOCK:
                cached = _ANALYSIS_CACHE.get(key)
                if cached and cached[0] > time.monotonic():
                    _ANALYSIS_CACHE.move_to_end(key)
                    return deepcopy(cached[1])
            persisted = _read_persisted_analysis(key)
            if persisted is not None:
                with _ANALYSIS_CACHE_LOCK:
                    _ANALYSIS_CACHE[key] = (time.monotonic() + _ANALYSIS_CACHE_SECONDS, deepcopy(persisted))
                    _ANALYSIS_CACHE.move_to_end(key)
                    while len(_ANALYSIS_CACHE) > _ANALYSIS_CACHE_LIMIT:
                        _ANALYSIS_CACHE.popitem(last=False)
                return persisted
        result = _compute_store_analysis(
            start_date, end_date, metric=metric, salesperson=salesperson,
            group_name=group_name, token_id=token_id,
            allowed_token_ids=scope, category_level=category_level,
        )
        result["computed_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        _write_persisted_analysis(key, result)
        with _ANALYSIS_CACHE_LOCK:
            _ANALYSIS_CACHE[key] = (time.monotonic() + _ANALYSIS_CACHE_SECONDS, deepcopy(result))
            _ANALYSIS_CACHE.move_to_end(key)
            while len(_ANALYSIS_CACHE) > _ANALYSIS_CACHE_LIMIT:
                _ANALYSIS_CACHE.popitem(last=False)
        return result


def _compute_store_analysis(start_date, end_date, *, metric="orders", salesperson="", group_name="",
                        token_id=None, allowed_token_ids=None, category_level=2):
    if metric not in {"orders", "gmv"}:
        raise ValueError("排序指标无效")
    category_level = _normalize_category_level(category_level)
    start_utc, end_utc = _dates(start_date, end_date)
    if allowed_token_ids is not None and not allowed_token_ids:
        return {"sites": [], "order_trend": summarize_order_trend([], start_date, end_date), "start_date": start_date, "end_date": end_date,
                "metric": metric, "category_level": category_level}
    if token_id is not None:
        token_id = int(token_id)
        if token_id <= 0:
            raise ValueError("店铺参数无效")
        if allowed_token_ids is not None and token_id not in allowed_token_ids:
            raise PermissionError("无权查看所选店铺")

    clauses = ["o.`date_created` >= %s", "o.`date_created` < %s",
               "COALESCE(o.`status`, '') NOT IN ('cancelled', 'invalid')"]
    params = [start_utc, end_utc]
    if allowed_token_ids is not None:
        ids = sorted(allowed_token_ids)
        clauses.append(f"o.`token_id` IN ({','.join(['%s'] * len(ids))})")
        params.extend(ids)
    if token_id is not None:
        clauses.append("o.`token_id` = %s")
        params.append(token_id)
    if salesperson:
        clauses.append("COALESCE(settings.`salesperson`, '') = %s")
        params.append(salesperson)
    if group_name:
        clauses.append("COALESCE(settings.`group_name`, '') = %s")
        params.append(group_name)

    # Count directly from orders so missing items/categories and every status are included.
    trend_clauses = [clause for clause in clauses if "o.`status`" not in clause]
    trend_sql = f"""
        SELECT DATE(DATE_ADD(o.`date_created`, INTERVAL 8 HOUR)) AS order_date,
               COUNT(DISTINCT o.`order_id`) AS orders
        FROM `mercado_synced_orders` AS o
        LEFT JOIN `mercado_store_site_settings` AS settings
          ON settings.`token_id` = o.`token_id` AND settings.`site_id` = o.`site_id`
        WHERE {' AND '.join(trend_clauses)}
        GROUP BY DATE(DATE_ADD(o.`date_created`, INTERVAL 8 HOUR))
    """

    # Category comes from the order payload when present, then the synchronized
    # listing. Historical listings remain useful even after they are delisted.
    sql = f"""
        WITH order_lines AS (
            SELECT o.`order_id`, o.`token_id`, o.`site_id`, o.`date_created`,
                   o.`total_amount`, o.`paid_amount`, o.`currency_id`,
                   COALESCE(NULLIF(o.`amount_currency_id`, ''),
                       CASE UPPER(COALESCE(o.`site_id`, ''))
                           WHEN 'MLM' THEN 'MXN' WHEN 'MLB' THEN 'BRL'
                           WHEN 'MLA' THEN 'ARS' WHEN 'MLC' THEN 'CLP'
                           WHEN 'MCO' THEN 'COP' WHEN 'MLU' THEN 'UYU'
                           ELSE NULLIF(o.`currency_id`, '') END, 'USD') AS `amount_currency_id`,
                   COALESCE(NULLIF(items.`category_id`, ''), links.`category_id`) AS `category_id`,
                   GREATEST(COALESCE(items.`quantity`, 1), 1)
                     * GREATEST(COALESCE(NULLIF(items.`unit_price`, 0),
                                          NULLIF(items.`full_unit_price`, 0), 1), 0) AS `line_amount`
            FROM `mercado_synced_orders` AS o
            LEFT JOIN `mercado_store_site_settings` AS settings
              ON settings.`token_id` = o.`token_id` AND settings.`site_id` = o.`site_id`
            CROSS JOIN JSON_TABLE(
                IF(JSON_VALID(o.`raw_json`), o.`raw_json`, JSON_OBJECT('order_items', JSON_ARRAY())),
                '$.order_items[*]' COLUMNS (
                    `item_id` VARCHAR(64) PATH '$.item.id',
                    `category_id` VARCHAR(64) PATH '$.item.category_id',
                    `quantity` INT PATH '$.quantity',
                    `unit_price` DECIMAL(20,4) PATH '$.unit_price',
                    `full_unit_price` DECIMAL(20,4) PATH '$.full_unit_price'
                )
            ) AS items
            LEFT JOIN `erp_mercadolibre_store_links` AS links
              ON links.`token_id` = o.`token_id` AND links.`item_id` = items.`item_id`
            WHERE {' AND '.join(clauses)}
        ), apportioned AS (
            SELECT order_lines.*,
                   SUM(order_lines.`line_amount`) OVER (PARTITION BY order_lines.`order_id`) AS `order_line_amount`
            FROM order_lines
        )
        SELECT apportioned.`order_id`, apportioned.`site_id`, apportioned.`category_id`,
               apportioned.`total_amount`, apportioned.`line_amount`, apportioned.`order_line_amount`,
               CASE
                 WHEN UPPER(apportioned.`amount_currency_id`) = 'USD' THEN 1
                 WHEN UPPER(COALESCE(apportioned.`currency_id`, '')) = 'USD'
                   AND apportioned.`total_amount` > 0 AND apportioned.`paid_amount` > 0
                   THEN apportioned.`paid_amount` / apportioned.`total_amount`
                 ELSE COALESCE(daily_rate.`rate`, current_rate.`rate`)
               END AS `usd_rate`
        FROM apportioned
        LEFT JOIN `erp_mercadolibre_exchange_rate_daily` AS daily_rate
          ON daily_rate.`from_currency_id` = UPPER(apportioned.`amount_currency_id`)
         AND daily_rate.`to_currency_id` = 'USD'
         AND daily_rate.`rate_date` = (
             SELECT MAX(history.`rate_date`) FROM `erp_mercadolibre_exchange_rate_daily` AS history
             WHERE history.`from_currency_id` = daily_rate.`from_currency_id`
               AND history.`to_currency_id` = 'USD'
               AND history.`rate_date` <= DATE(DATE_ADD(apportioned.`date_created`, INTERVAL 8 HOUR))
         )
        LEFT JOIN `erp_mercadolibre_exchange_rates` AS current_rate
          ON current_rate.`from_currency_id` = UPPER(apportioned.`amount_currency_id`)
         AND current_rate.`to_currency_id` = 'USD'
    """
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            cursor.execute(trend_sql, params)
            trend_rows = cursor.fetchall()
    finally:
        connection.close()
    category_ids = sorted({str(row.get("category_id") or "").strip().upper() for row in rows if row.get("category_id")})
    paths = category_paths_for_ids(category_ids) if category_ids else {}
    # Rank before translating so only visible labels incur translation work.
    # Original names provide a stable tie breaker for equal sales totals.
    original_names = {
        str(path[category_level - 1].get("id") or "").strip().upper(): str(path[category_level - 1].get("name") or "")
        for path in paths.values() if len(path) >= category_level
    }
    sites = summarize_store_analysis(rows, paths, metric=metric, category_level=category_level,
                                     category_names_zh=original_names)
    visible_ids = {category["category_id"] for site in sites for category in site["top_categories"]}
    visible_paths = {key: path for key, path in paths.items()
                     if len(path) >= category_level
                     and str(path[category_level - 1].get("id") or "").strip().upper() in visible_ids}
    translation_warning = ""
    try:
        category_names_zh = _translate_category_names(visible_paths, category_level)
    except Exception:
        category_names_zh = {}
        translation_warning = "中文类目名称暂不可用，请运行 python3 scripts/install_argos_translation_models.py 安装本地翻译模型。"
    for site in sites:
        for category in site["top_categories"]:
            name = category_names_zh.get(category["category_id"], "")
            original = category.pop("name_zh", "")
            category["name"] = name or (original if any("\u3400" <= c <= "\u9fff" for c in original) else "分类名称暂不可用")
            if name:
                category["name_zh"] = name
    return {"sites": sites, "order_trend": summarize_order_trend(trend_rows, start_date, end_date),
            "start_date": start_date, "end_date": end_date, "metric": metric,
            "category_level": category_level,
            "category_translation_warning": translation_warning}
