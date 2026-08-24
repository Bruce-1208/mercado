"""Use the logged-in Zying frontend to collect a small API-first product sample.

The current Zying frontend signs requests with code loaded by the web app.  This
module deliberately calls that already-loaded frontend API from the authorized
browser session instead of copying the site's signing implementation into
Python.  The POC writes to its own table so it cannot pollute the legacy
``zying_product`` snapshot table.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path


if __package__ in (None, ""):
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

import pymysql
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait

from bit.bit_api import openBrowser, releaseBrowserLease
from bit.bit_mysql import config
from bit.bit_zying_caiji import (
    DEFAULT_ZYING_WINDOW_ID,
    LOGIN_SELECTOR,
    TITLE_SELECTOR,
    ZYING_PRODUCT_URL,
    _clean_text,
    _detail_category_reference,
    _format_money,
    _merge_category_record,
    _merge_detail_record,
)


DEFAULT_LIMIT = 100
DEFAULT_PAGE_SIZE = 60
MELI_PLATFORM_ID = 8
ZYING_SELECTION_URL = "https://meli.zying.net/#/goods/total"
POC_TABLE = "zying_selection_api_poc"


# The product page loads a small module whose exported object exposes
# ``handleProduct``.  Finding the object by capability instead of by a hashed
# filename keeps the bridge working when Vite publishes a new build.
FRONTEND_API_CALL_SCRIPT = r"""
const prefix = arguments[0];
const action = arguments[1];
const payload = arguments[2];
const done = arguments[arguments.length - 1];

(async () => {
  try {
    const findApi = async () => {
      if (
        window.__codexZyingProductApi &&
        typeof window.__codexZyingProductApi.handleProduct === 'function'
      ) {
        return window.__codexZyingProductApi;
      }

      const urls = new Set();
      for (const entry of performance.getEntriesByType('resource')) {
        if (entry.name && entry.name.includes('.js')) urls.add(entry.name);
      }
      for (const script of document.scripts) {
        if (script.src && script.src.includes('.js')) urls.add(script.src);
      }

      for (const url of urls) {
        let parsed;
        try {
          parsed = new URL(url, location.href);
        } catch (_) {
          continue;
        }
        if (parsed.origin !== location.origin) continue;

        let source = '';
        try {
          source = await fetch(parsed.href, {credentials: 'same-origin'}).then(
            response => response.ok ? response.text() : ''
          );
        } catch (_) {
          continue;
        }
        if (!source.includes('handleProduct:')) continue;

        try {
          const module = await import(parsed.href);
          const api = Object.values(module).find(
            value => value && typeof value.handleProduct === 'function'
          );
          if (api) {
            window.__codexZyingProductApi = api;
            return api;
          }
        } catch (_) {
          continue;
        }
      }
      throw new Error('没有找到智赢前端 handleProduct 接口模块');
    };

    const api = await findApi();
    const response = await api.handleProduct(prefix, action, payload);
    done({ok: true, response});
  } catch (error) {
    const responseData = error?.response?.data || error?.data || null;
    done({
      ok: false,
      error: responseData?.message || responseData?.msg || error?.message || String(error),
      status: error?.response?.status || error?.status || null,
      responseData,
    });
  }
})();
"""


FRONTEND_MELI_API_CALL_SCRIPT = r"""
const method = arguments[0];
const payload = arguments[1];
const done = arguments[arguments.length - 1];

(async () => {
  try {
    const findApi = async () => {
      if (
        window.__codexZyingMeliApi &&
        typeof window.__codexZyingMeliApi.getMeliItemsByPage === 'function'
      ) {
        return window.__codexZyingMeliApi;
      }

      const urls = new Set();
      for (const entry of performance.getEntriesByType('resource')) {
        if (entry.name && entry.name.includes('.js')) urls.add(entry.name);
      }
      for (const script of document.scripts) {
        if (script.src && script.src.includes('.js')) urls.add(script.src);
      }

      for (const url of urls) {
        let parsed;
        try {
          parsed = new URL(url, location.href);
        } catch (_) {
          continue;
        }
        if (parsed.origin !== location.origin) continue;

        let source = '';
        try {
          source = await fetch(parsed.href, {credentials: 'same-origin'}).then(
            response => response.ok ? response.text() : ''
          );
        } catch (_) {
          continue;
        }
        if (!source.includes('getMeliItemsByPage')) continue;

        try {
          const module = await import(parsed.href);
          const api = Object.values(module).find(
            value => value && typeof value.getMeliItemsByPage === 'function'
          );
          if (api) {
            window.__codexZyingMeliApi = api;
            return api;
          }
        } catch (_) {
          continue;
        }
      }
      throw new Error('没有找到智赢选品库前端接口模块');
    };

    const api = await findApi();
    if (typeof api[method] !== 'function') {
      throw new Error(`智赢选品库接口不存在：${method}`);
    }
    const response = await api[method](payload);
    done({ok: true, response});
  } catch (error) {
    const responseData = error?.response?.data || error?.data || null;
    done({
      ok: false,
      error: responseData?.message || responseData?.msg || error?.message || String(error),
      status: error?.response?.status || error?.status || null,
      responseData,
    });
  }
})();
"""


def _plain_title(value):
    return _clean_text(html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))))


def _call_frontend_api(driver, command, payload, timeout=90):
    """Call a signed Zying command through the frontend loaded in ``driver``."""
    if "." not in command:
        raise ValueError(f"智赢接口命令格式错误：{command!r}")
    prefix, action = command.rsplit(".", 1)
    driver.set_script_timeout(timeout)
    result = driver.execute_async_script(
        FRONTEND_API_CALL_SCRIPT,
        prefix,
        action,
        payload,
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"智赢接口 {command} 没有返回可识别结果：{result!r}")
    if not result.get("ok"):
        status = result.get("status")
        status_text = f"HTTP {status}，" if status else ""
        raise RuntimeError(
            f"智赢接口 {command} 请求失败：{status_text}"
            f"{result.get('error') or '未知错误'}"
        )
    response = result.get("response")
    if not isinstance(response, dict):
        raise RuntimeError(f"智赢接口 {command} 响应格式错误：{response!r}")
    return response


def _extract_list_response(response):
    data = response.get("data") or {}
    listing = data.get("list") or {}
    rows = listing.get("data") or []
    if not isinstance(rows, list):
        raise RuntimeError("智赢 sale.stat 返回的 data.list.data 不是数组")
    total = listing.get("maxcount")
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = None
    return rows, total


def _normalize_list_row(row, page_number, collected_at):
    product_id = _clean_text(row.get("id"))
    if not product_id:
        raise RuntimeError(f"智赢列表商品缺少 id：{row!r}")
    currency = _clean_text(row.get("cur"))
    price = row.get("cost")
    if price is None:
        price = row.get("price")
    return {
        "product_id": product_id,
        "main_image_url": _clean_text(row.get("thumb")),
        "title": _plain_title(row.get("title")),
        "sale_price": _format_money(price, currency),
        "currency": currency,
        "page_number": page_number,
        "collected_at": collected_at,
        "list_row": row,
        "detail_ok": False,
        "error_message": "",
    }


def fetch_product_index(api_call, limit=DEFAULT_LIMIT, page_size=DEFAULT_PAGE_SIZE):
    """Fetch up to ``limit`` unique products from ``sale.stat``."""
    limit = max(1, int(limit))
    page_size = max(1, min(int(page_size), 500))
    records = []
    seen = set()
    page_number = 1
    total = None
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    while len(records) < limit:
        response = api_call(
            "sale.stat",
            {
                "page": page_number,
                "pagesize": page_size,
                "word": "",
                "from": MELI_PLATFORM_ID,
            },
        )
        rows, response_total = _extract_list_response(response)
        if total is None and response_total is not None:
            total = response_total
        if not rows:
            break

        for row in rows:
            record = _normalize_list_row(row, page_number, collected_at)
            if record["product_id"] in seen:
                continue
            seen.add(record["product_id"])
            records.append(record)
            if len(records) >= limit:
                break

        print(
            f"智赢接口列表第 {page_number} 页：返回 {len(rows)} 条，"
            f"累计唯一产品 {len(records)}/{limit}，平台总数 {total or '未知'}",
            flush=True,
        )
        if len(rows) < page_size or (total is not None and page_number * page_size >= total):
            break
        page_number += 1

    return records, total


def _detail_row(response, product_id):
    rows = (response.get("data") or {}).get("root") or []
    if not rows:
        raise RuntimeError(f"产品 {product_id} 的 sale.detail 没有返回 root 数据")
    if not isinstance(rows[0], dict):
        raise RuntimeError(f"产品 {product_id} 的 sale.detail 数据格式错误")
    return rows[0]


def enrich_product_details(api_call, records):
    """Fetch detail and category payloads for every record, retaining failures."""
    category_cache = {}
    for index, record in enumerate(records, start=1):
        product_id = record["product_id"]
        try:
            detail_response = api_call("sale.detail", {"id": product_id})
            detail = _detail_row(detail_response, product_id)
            _merge_detail_record(record, record["list_row"], detail)
            record["detail_row"] = detail

            site, category_id = _detail_category_reference(detail)
            category = None
            if site and category_id:
                cache_key = (site, category_id)
                if cache_key not in category_cache:
                    category_response = api_call(
                        "meli_category.detail",
                        {"site": site, "id": category_id},
                    )
                    category_rows = (category_response.get("data") or {}).get("root") or []
                    category_cache[cache_key] = category_rows[0] if category_rows else None
                category = category_cache[cache_key]
                if category:
                    _merge_category_record(record, category)
            record["category_row"] = category
            record["detail_ok"] = True
            record["error_message"] = ""
        except Exception as exc:
            record["detail_ok"] = False
            record["error_message"] = str(exc)[:2000]

        print(
            f"智赢接口详情 {index}/{len(records)}：产品 {product_id}，"
            f"{'成功' if record['detail_ok'] else '失败'}",
            flush=True,
        )
    return records


def _call_meli_frontend_api(driver, method, payload, timeout=90):
    """Call the signed Meli selection-library client loaded by the web app."""
    driver.set_script_timeout(timeout)
    result = driver.execute_async_script(
        FRONTEND_MELI_API_CALL_SCRIPT,
        method,
        payload,
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"智赢选品库接口 {method} 没有返回可识别结果：{result!r}")
    if not result.get("ok"):
        status = result.get("status")
        status_text = f"HTTP {status}，" if status else ""
        raise RuntimeError(
            f"智赢选品库接口 {method} 请求失败：{status_text}"
            f"{result.get('error') or '未知错误'}"
        )
    response = result.get("response")
    if not isinstance(response, dict):
        raise RuntimeError(f"智赢选品库接口 {method} 响应格式错误：{response!r}")
    if response.get("code") not in (None, 200):
        raise RuntimeError(
            f"智赢选品库接口 {method} 业务失败："
            f"{response.get('msg') or response.get('message') or response.get('code')}"
        )
    return response


def _normalize_selection_row(row, page_number, site_id, collected_at):
    product_id = _clean_text(row.get("Sku"))
    if not product_id:
        raise RuntimeError(f"智赢选品库商品缺少 Sku：{row!r}")
    return {
        "site_id": _clean_text(site_id),
        "product_id": product_id,
        "title": _clean_text(row.get("Title")),
        "main_image_url": _clean_text(row.get("Thumb")),
        "product_url": _clean_text(row.get("Url")),
        "price": row.get("Price"),
        "net_income": row.get("Netproceed"),
        "orders": row.get("Orders"),
        "orders_7": row.get("Order_7"),
        "orders_14": row.get("Order_14"),
        "orders_30": row.get("Order_30"),
        "orders_7_rate": row.get("Order_7rate"),
        "orders_30_rate": row.get("Order_30rate"),
        "orders_90_rate": row.get("Order_90rate"),
        "brand": _clean_text(row.get("Brand")),
        "seller_id": _clean_text(row.get("Sellerid")),
        "seller_name": _clean_text(row.get("Sellername")),
        "comment_count": row.get("Comment"),
        "rating": row.get("Rate"),
        "stock": row.get("Stock"),
        "status": row.get("Status"),
        "storage_type": row.get("Storagetype"),
        "category_id": _clean_text(row.get("Cateid")),
        "category_path": _clean_text(row.get("CateFullName")),
        "uptime": row.get("Uptime"),
        "page_number": page_number,
        "collected_at": collected_at,
        "list_row": row,
        "detail_ok": False,
        "error_message": "",
    }


def fetch_selection_index(
    api_call,
    limit=DEFAULT_LIMIT,
    page_size=DEFAULT_PAGE_SIZE,
    site_id="1",
):
    """Fetch a unique sample from the current selection-library endpoint."""
    limit = max(1, int(limit))
    page_size = max(1, min(int(page_size), 500))
    records = []
    seen = set()
    page_number = 1
    total = None
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    while len(records) < limit:
        response = api_call(
            "getMeliItemsByPage",
            {"page": page_number, "pageSize": page_size},
        )
        data = response.get("data") or {}
        rows = data.get("Datas") or []
        if not isinstance(rows, list):
            raise RuntimeError("智赢选品库返回的 data.Datas 不是数组")
        if total is None:
            try:
                total = int(data.get("Total"))
            except (TypeError, ValueError):
                total = None
        if not rows:
            break

        for row in rows:
            record = _normalize_selection_row(
                row,
                page_number,
                site_id,
                collected_at,
            )
            key = (record["site_id"], record["product_id"])
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
            if len(records) >= limit:
                break

        print(
            f"智赢选品库第 {page_number} 页：返回 {len(rows)} 条，"
            f"累计唯一产品 {len(records)}/{limit}，当前查询总数 {total or '未知'}",
            flush=True,
        )
        if len(rows) < page_size or (total is not None and page_number * page_size >= total):
            break
        page_number += 1
    return records, total


def enrich_selection_details(api_call, records):
    for index, record in enumerate(records, start=1):
        try:
            response = api_call(
                "getMeliItemDetail",
                {"sku": record["product_id"]},
            )
            detail = response.get("data") or {}
            if not isinstance(detail, dict) or not detail.get("Sku"):
                raise RuntimeError("详情接口没有返回有效商品")
            record["detail_row"] = detail
            record["weight"] = detail.get("Weight")
            size = detail.get("Size") or []
            record["size"] = " x ".join(str(value) for value in size) if size else ""
            record["category_commission"] = detail.get("CatePremium")
            record["official"] = detail.get("Official")
            # Detail values win when present; list-only fields remain intact.
            for source, target in (
                ("Title", "title"),
                ("Thumb", "main_image_url"),
                ("Url", "product_url"),
                ("Price", "price"),
                ("Orders", "orders"),
                ("Brand", "brand"),
                ("Sellerid", "seller_id"),
                ("Sellername", "seller_name"),
                ("Comment", "comment_count"),
                ("Rate", "rating"),
                ("Stock", "stock"),
                ("Status", "status"),
                ("Storagetype", "storage_type"),
                ("Cateid", "category_id"),
                ("Uptime", "uptime"),
            ):
                if detail.get(source) is not None:
                    record[target] = detail[source]
            record["detail_ok"] = True
            record["error_message"] = ""
        except Exception as exc:
            record["detail_ok"] = False
            record["error_message"] = str(exc)[:2000]
        print(
            f"智赢选品库详情 {index}/{len(records)}：{record['product_id']}，"
            f"{'成功' if record['detail_ok'] else '失败'}",
            flush=True,
        )
    return records


def _ensure_poc_table(cursor):
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{POC_TABLE}` (
            `site_id` VARCHAR(32) NOT NULL,
            `product_id` VARCHAR(128) NOT NULL,
            `title` VARCHAR(1024) NULL,
            `main_image_url` TEXT NULL,
            `product_url` TEXT NULL,
            `price` DECIMAL(20, 4) NULL,
            `net_income` DECIMAL(20, 4) NULL,
            `orders` BIGINT NULL,
            `orders_7` BIGINT NULL,
            `orders_14` BIGINT NULL,
            `orders_30` BIGINT NULL,
            `orders_7_rate` DECIMAL(20, 6) NULL,
            `orders_30_rate` DECIMAL(20, 6) NULL,
            `orders_90_rate` DECIMAL(20, 6) NULL,
            `brand` VARCHAR(512) NULL,
            `seller_id` VARCHAR(128) NULL,
            `seller_name` VARCHAR(512) NULL,
            `comment_count` BIGINT NULL,
            `rating` DECIMAL(10, 4) NULL,
            `stock` BIGINT NULL,
            `status` INT NULL,
            `storage_type` INT NULL,
            `category_id` VARCHAR(64) NULL,
            `category_path` VARCHAR(2048) NULL,
            `uptime` DATETIME NULL,
            `weight` DECIMAL(20, 4) NULL,
            `size` VARCHAR(255) NULL,
            `category_commission` DECIMAL(20, 6) NULL,
            `official` TINYINT(1) NULL,
            `list_page` INT NULL,
            `detail_ok` TINYINT(1) NOT NULL DEFAULT 0,
            `error_message` TEXT NULL,
            `list_json` LONGTEXT NOT NULL,
            `detail_json` LONGTEXT NULL,
            `collected_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`site_id`, `product_id`),
            KEY `idx_zying_api_poc_detail_ok` (`detail_ok`),
            KEY `idx_zying_api_poc_updated_at` (`updated_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def persist_poc_records(records):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_poc_table(cursor)
            updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            values = []
            for record in records:
                values.append(
                    (
                        record.get("site_id"),
                        record.get("product_id"),
                        record.get("title"),
                        record.get("main_image_url"),
                        record.get("product_url"),
                        record.get("price"),
                        record.get("net_income"),
                        record.get("orders"),
                        record.get("orders_7"),
                        record.get("orders_14"),
                        record.get("orders_30"),
                        record.get("orders_7_rate"),
                        record.get("orders_30_rate"),
                        record.get("orders_90_rate"),
                        record.get("brand"),
                        record.get("seller_id"),
                        record.get("seller_name"),
                        record.get("comment_count"),
                        record.get("rating"),
                        record.get("stock"),
                        record.get("status"),
                        record.get("storage_type"),
                        record.get("category_id"),
                        record.get("category_path"),
                        record.get("uptime"),
                        record.get("weight"),
                        record.get("size"),
                        record.get("category_commission"),
                        None
                        if record.get("official") is None
                        else int(bool(record.get("official"))),
                        record.get("page_number"),
                        1 if record.get("detail_ok") else 0,
                        record.get("error_message"),
                        json.dumps(record.get("list_row") or {}, ensure_ascii=False),
                        json.dumps(record.get("detail_row"), ensure_ascii=False)
                        if record.get("detail_row") is not None
                        else None,
                        record.get("collected_at") or updated_at,
                        updated_at,
                    )
                )
            if values:
                cursor.executemany(
                    f"""
                    INSERT INTO `{POC_TABLE}` (
                        `site_id`, `product_id`, `title`, `main_image_url`,
                        `product_url`, `price`, `net_income`, `orders`, `orders_7`,
                        `orders_14`, `orders_30`, `orders_7_rate`, `orders_30_rate`,
                        `orders_90_rate`, `brand`, `seller_id`, `seller_name`,
                        `comment_count`, `rating`, `stock`, `status`, `storage_type`,
                        `category_id`, `category_path`, `uptime`, `weight`, `size`,
                        `category_commission`, `official`, `list_page`, `detail_ok`,
                        `error_message`, `list_json`, `detail_json`, `collected_at`,
                        `updated_at`
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    ON DUPLICATE KEY UPDATE
                        `title` = VALUES(`title`),
                        `main_image_url` = VALUES(`main_image_url`),
                        `product_url` = VALUES(`product_url`),
                        `price` = VALUES(`price`),
                        `net_income` = VALUES(`net_income`),
                        `orders` = VALUES(`orders`),
                        `orders_7` = VALUES(`orders_7`),
                        `orders_14` = VALUES(`orders_14`),
                        `orders_30` = VALUES(`orders_30`),
                        `orders_7_rate` = VALUES(`orders_7_rate`),
                        `orders_30_rate` = VALUES(`orders_30_rate`),
                        `orders_90_rate` = VALUES(`orders_90_rate`),
                        `brand` = VALUES(`brand`),
                        `seller_id` = VALUES(`seller_id`),
                        `seller_name` = VALUES(`seller_name`),
                        `comment_count` = VALUES(`comment_count`),
                        `rating` = VALUES(`rating`),
                        `stock` = VALUES(`stock`),
                        `status` = VALUES(`status`),
                        `storage_type` = VALUES(`storage_type`),
                        `category_id` = VALUES(`category_id`),
                        `category_path` = VALUES(`category_path`),
                        `uptime` = VALUES(`uptime`),
                        `weight` = VALUES(`weight`),
                        `size` = VALUES(`size`),
                        `category_commission` = VALUES(`category_commission`),
                        `official` = VALUES(`official`),
                        `list_page` = VALUES(`list_page`),
                        `detail_ok` = VALUES(`detail_ok`),
                        `error_message` = VALUES(`error_message`),
                        `list_json` = VALUES(`list_json`),
                        `detail_json` = VALUES(`detail_json`),
                        `collected_at` = VALUES(`collected_at`),
                        `updated_at` = VALUES(`updated_at`)
                    """,
                    values,
                )
        connection.commit()
        return len(values)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def collect_api_poc(
    limit=DEFAULT_LIMIT,
    page_size=DEFAULT_PAGE_SIZE,
    window_id=DEFAULT_ZYING_WINDOW_ID,
    include_details=True,
):
    limit = max(1, int(limit))
    if limit > 1000:
        raise ValueError("POC 单次最多允许采集 1000 条")
    started_at = time.time()
    browser_info = openBrowser(window_id)
    if not browser_info or not browser_info.get("data"):
        raise RuntimeError(f"打开 BitBrowser 窗口失败：{browser_info}")

    service = Service(browser_info["data"]["driver"])
    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", browser_info["data"]["http"])
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception:
        releaseBrowserLease(window_id)
        raise

    try:
        driver.get(ZYING_SELECTION_URL)

        def page_ready(current_driver):
            if current_driver.find_elements(
                By.CSS_SELECTOR,
                ".meli-select-goods-table, table tbody tr",
            ):
                return True
            if "#/login" in current_driver.current_url.casefold() or current_driver.find_elements(
                By.CSS_SELECTOR, LOGIN_SELECTOR
            ):
                raise RuntimeError(
                    "智赢尚未登录。请先在已打开的 BitBrowser 窗口完成登录，再重新运行。"
                )
            return False

        WebDriverWait(driver, 30).until(page_ready)
        site_id = driver.execute_script(
            "return localStorage.getItem('meliSiteikey') || '1';"
        )
        api_call = lambda method, payload: _call_meli_frontend_api(
            driver,
            method,
            payload,
        )
        records, total = fetch_selection_index(
            api_call,
            limit=limit,
            page_size=page_size,
            site_id=site_id,
        )
        if len(records) < limit:
            raise RuntimeError(
                f"智赢选品库只取得 {len(records)}/{limit} 个唯一产品，"
                f"当前查询总数 {total or '未知'}"
            )

        # Persist the index first.  A detail-stage interruption still leaves a
        # complete 100-ID checkpoint that can be rerun safely via UPSERT.
        persist_poc_records(records)
        if include_details:
            enrich_selection_details(api_call, records)
            persist_poc_records(records)
    finally:
        try:
            service.stop()
        finally:
            releaseBrowserLease(window_id)

    detail_success = sum(bool(record.get("detail_ok")) for record in records)
    return {
        "requested": limit,
        "collected": len(records),
        "platform_total": total,
        "detail_success": detail_success,
        "detail_failed": len(records) - detail_success if include_details else 0,
        "database_table": POC_TABLE,
        "elapsed_seconds": round(time.time() - started_at, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="智赢 API 优先采集 POC")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="POC 产品数量")
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help="sale.stat 每页请求数量",
    )
    parser.add_argument("--window-id", default=DEFAULT_ZYING_WINDOW_ID)
    parser.add_argument("--list-only", action="store_true", help="只验证列表，不请求详情")
    args = parser.parse_args()
    try:
        result = collect_api_poc(
            limit=args.limit,
            page_size=args.page_size,
            window_id=args.window_id,
            include_details=not args.list_only,
        )
    except (RuntimeError, ValueError) as exc:
        parser.exit(status=1, message=f"智赢 API POC 失败：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
