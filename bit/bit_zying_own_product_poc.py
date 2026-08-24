"""Collect a small, authorized sample from the Zying ERP product library.

The ERP frontend owns the request-signing implementation.  This module attaches
to the already logged-in BitBrowser window and calls the product page's own
``handleProduct`` client.  Credentials and tokens are never copied to Python.

The proof of concept writes to ``zying_own_product_api_poc``.  It is deliberately
separate from both the legacy ``zying_product`` snapshot and the public
selection-library proof of concept.
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
from selenium.webdriver.support.wait import WebDriverWait

from bit.bit_api import openBrowser, releaseBrowserLease
from bit.bit_mysql import config
from bit.bit_zying_caiji import DEFAULT_ZYING_WINDOW_ID


# The self-owned library is served by the Seller application.  The similarly
# styled erp.zying.net application can belong to a different/empty tenant.
ERP_PRODUCT_URL = "https://seller.zying.net/#/"
POC_TABLES = {
    "sale": "zying_own_product_api_poc",
    "spider": "zying_own_spider_api_poc",
}
SOURCE_LABELS = {
    "sale": "产品列表",
    "spider": "采集列表",
}
DEFAULT_LIMIT = 100
DEFAULT_PAGE_SIZE = 60


# Umi/webpack keeps the product client private inside its module runtime.  A
# harmless runtime callback exposes webpack's require function.  Loading chunk
# 3132 gives us module 13184, whose ``handleProduct`` method uses the newer async
# request signer required by the ``sale`` service.  The generic request wrapper
# still works for ``spider`` today but is rejected by ``sale`` with ``BizError``.
FRONTEND_ERP_API_CALL_SCRIPT = r"""
const command = arguments[0];
const payload = arguments[1];
const done = arguments[arguments.length - 1];

(async () => {
  try {
    if (!window.__codexZyingErpRequire) {
      const chunks = window.webpackChunkzying;
      if (!chunks || typeof chunks.push !== 'function') {
        throw new Error('ERP webpack runtime is not ready');
      }
      const runtimeId = 900000000 + Math.floor(Math.random() * 99999999);
      chunks.push([
        [runtimeId],
        {},
        function(require) { window.__codexZyingErpRequire = require; }
      ]);
    }

    if (!command || !command.includes('.')) {
      throw new Error(`Invalid ERP command: ${command}`);
    }
    const separator = command.lastIndexOf('.');
    const prefix = command.slice(0, separator);
    const action = command.slice(separator + 1);
    const require = window.__codexZyingErpRequire;
    await require.e(3132);
    const productModule = require(13184);
    const productApi = productModule && (productModule.Z || productModule.default);
    if (!productApi || typeof productApi.handleProduct !== 'function') {
      throw new Error('Seller handleProduct module is not loaded');
    }
    const response = await productApi.handleProduct(prefix, action, payload);
    done({ok: true, response});
  } catch (error) {
    const responseData = error?.response?.data ?? error?.data ?? null;
    let responseText = '';
    try {
      responseText = JSON.stringify(responseData);
    } catch (_) {
      responseText = String(responseData);
    }
    done({
      ok: false,
      status: error?.response?.status || error?.status || null,
      error: responseData?.message || responseData?.msg || error?.message || String(error),
      responseData,
      responseText,
      errorKeys: error && typeof error === 'object' ? Object.keys(error) : [],
    });
  }
})();
"""


def _clean_text(value):
    return " ".join(str(value or "").split())


def _plain_title(value):
    return _clean_text(html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))))


def _response_payload(response):
    """Accept both the ERP client's direct payload and a server envelope."""
    if not isinstance(response, dict):
        raise RuntimeError(f"ERP returned an invalid response: {response!r}")
    if isinstance(response.get("data"), dict):
        data = response["data"]
        if "list" in data or "root" in data:
            return data
    return response


def _extract_list_response(response):
    payload = _response_payload(response)
    listing = payload.get("list") or {}
    rows = listing.get("data") or []
    if not isinstance(rows, list):
        raise RuntimeError("sale.stat did not return list.data as an array")
    try:
        total = int(listing.get("maxcount"))
    except (TypeError, ValueError):
        total = None
    return rows, total


def _extract_detail_response(response, product_id):
    payload = _response_payload(response)
    rows = payload.get("root") or []
    if not rows or not isinstance(rows[0], dict):
        raise RuntimeError(f"sale.detail returned no data for product {product_id}")
    return rows[0]


def _normalize_list_row(row, page_number, collected_at):
    product_id = _clean_text(row.get("id"))
    if not product_id:
        raise RuntimeError(f"ERP product list row is missing id: {row!r}")
    return {
        "product_id": product_id,
        "title": _plain_title(row.get("title")),
        "main_image_url": _clean_text(row.get("thumb")),
        "currency": _clean_text(row.get("cur")),
        "cost": row.get("cost") if row.get("cost") is not None else row.get("price"),
        "list_page": page_number,
        "list_row": row,
        "detail_row": None,
        "detail_ok": False,
        "error_message": "",
        "collected_at": collected_at,
    }


def fetch_product_index(
    api_call,
    limit=DEFAULT_LIMIT,
    page_size=DEFAULT_PAGE_SIZE,
    source="sale",
):
    """Fetch unique rows without mixing the ``sale`` and ``spider`` sources."""
    limit = max(1, int(limit))
    page_size = max(1, min(int(page_size), 500))
    records = []
    seen = set()
    total = None
    page_number = 1
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    while len(records) < limit:
        response = api_call(
            f"{source}.stat",
            {"page": page_number, "pagesize": page_size, "word": ""},
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
        label = SOURCE_LABELS.get(source, source)
        print(
            f"ERP {label}第 {page_number} 页：返回 {len(rows)} 条，"
            f"累计 {len(records)}/{limit}，列表总数 {total or '未知'}",
            flush=True,
        )
        if len(rows) < page_size:
            break
        if total is not None and page_number * page_size >= total:
            break
        page_number += 1
    return records, total


def _merge_detail(record, detail):
    record["detail_row"] = detail
    record["title_json"] = detail.get("sale_title")
    record["sku"] = detail.get("sale_sku")
    record["currency"] = _clean_text(detail.get("sale_cur")) or record["currency"]
    if detail.get("sale_cost") is not None:
        record["cost"] = detail.get("sale_cost")
    record["net_income"] = detail.get("sale_netproceed")
    record["weight"] = detail.get("sale_weight")
    size = detail.get("sale_size") or []
    record["dimensions"] = json.dumps(size, ensure_ascii=False) if size else ""
    record["category_local_id"] = detail.get("sale_localid")
    record["review_code"] = detail.get("sale_stat")
    images = detail.get("sale_pic") or []
    if images:
        record["main_image_url"] = _clean_text(images[0])

    try:
        attrs = json.loads(detail.get("sale_attrs") or "{}")
    except (TypeError, ValueError):
        attrs = {}
    category = next((value for value in attrs.values() if isinstance(value, dict)), {})
    record["category_site"] = category.get("site") or detail.get("sale_area")
    record["category_external_id"] = category.get("kindid")
    record["detail_ok"] = True
    record["error_message"] = ""
    return record


def enrich_product_details(api_call, records, source="sale"):
    for index, record in enumerate(records, start=1):
        try:
            response = api_call(f"{source}.detail", {"id": record["product_id"]})
            detail = _extract_detail_response(response, record["product_id"])
            _merge_detail(record, detail)
        except Exception as exc:
            record["detail_ok"] = False
            record["error_message"] = str(exc)[:2000]
        print(
            f"ERP 自有产品详情 {index}/{len(records)}：{record['product_id']} "
            f"{'成功' if record['detail_ok'] else '失败'}",
            flush=True,
        )
    return records


def _call_frontend_api(driver, command, payload, timeout=90):
    driver.set_script_timeout(timeout)
    result = driver.execute_async_script(
        FRONTEND_ERP_API_CALL_SCRIPT,
        command,
        payload,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        if isinstance(result, dict):
            status = f"HTTP {result.get('status')} " if result.get("status") else ""
            message = result.get("error") or "unknown frontend error"
            if result.get("responseData") is not None:
                response_text = json.dumps(
                    result["responseData"], ensure_ascii=False, default=str
                )
                message = f"{message}; response={response_text[:2000]}"
            elif result.get("responseText"):
                message = f"{message}; response={result['responseText'][:2000]}"
            if result.get("responseText") == '""':
                message = "server returned an empty response body"
        else:
            status = ""
            message = repr(result)
        raise RuntimeError(f"ERP {command} request failed: {status}{message}")
    return result.get("response")


def _ensure_table(cursor, table_name):
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{table_name}` (
            `product_id` VARCHAR(128) NOT NULL,
            `title` VARCHAR(2048) NULL,
            `title_json` LONGTEXT NULL,
            `main_image_url` TEXT NULL,
            `sku` VARCHAR(512) NULL,
            `currency` VARCHAR(32) NULL,
            `cost` VARCHAR(128) NULL,
            `net_income` VARCHAR(128) NULL,
            `weight` VARCHAR(128) NULL,
            `dimensions` VARCHAR(512) NULL,
            `category_local_id` VARCHAR(128) NULL,
            `category_site` VARCHAR(64) NULL,
            `category_external_id` VARCHAR(128) NULL,
            `review_code` VARCHAR(64) NULL,
            `list_page` INT NULL,
            `detail_ok` TINYINT(1) NOT NULL DEFAULT 0,
            `error_message` TEXT NULL,
            `list_json` LONGTEXT NOT NULL,
            `detail_json` LONGTEXT NULL,
            `collected_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`product_id`),
            KEY `idx_zying_own_poc_detail_ok` (`detail_ok`),
            KEY `idx_zying_own_poc_updated_at` (`updated_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def persist_records(records, table_name):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_table(cursor, table_name)
            updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            values = [
                (
                    record.get("product_id"),
                    record.get("title"),
                    record.get("title_json"),
                    record.get("main_image_url"),
                    record.get("sku"),
                    record.get("currency"),
                    record.get("cost"),
                    record.get("net_income"),
                    record.get("weight"),
                    record.get("dimensions"),
                    record.get("category_local_id"),
                    record.get("category_site"),
                    record.get("category_external_id"),
                    record.get("review_code"),
                    record.get("list_page"),
                    1 if record.get("detail_ok") else 0,
                    record.get("error_message"),
                    json.dumps(record.get("list_row") or {}, ensure_ascii=False),
                    json.dumps(record.get("detail_row"), ensure_ascii=False)
                    if record.get("detail_row") is not None
                    else None,
                    record.get("collected_at") or updated_at,
                    updated_at,
                )
                for record in records
            ]
            if values:
                cursor.executemany(
                    f"""
                    INSERT INTO `{table_name}` (
                        `product_id`, `title`, `title_json`, `main_image_url`, `sku`,
                        `currency`, `cost`, `net_income`, `weight`, `dimensions`,
                        `category_local_id`, `category_site`, `category_external_id`,
                        `review_code`, `list_page`, `detail_ok`, `error_message`,
                        `list_json`, `detail_json`, `collected_at`, `updated_at`
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON DUPLICATE KEY UPDATE
                        `title`=VALUES(`title`), `title_json`=VALUES(`title_json`),
                        `main_image_url`=VALUES(`main_image_url`), `sku`=VALUES(`sku`),
                        `currency`=VALUES(`currency`), `cost`=VALUES(`cost`),
                        `net_income`=VALUES(`net_income`), `weight`=VALUES(`weight`),
                        `dimensions`=VALUES(`dimensions`),
                        `category_local_id`=VALUES(`category_local_id`),
                        `category_site`=VALUES(`category_site`),
                        `category_external_id`=VALUES(`category_external_id`),
                        `review_code`=VALUES(`review_code`), `list_page`=VALUES(`list_page`),
                        `detail_ok`=VALUES(`detail_ok`),
                        `error_message`=VALUES(`error_message`),
                        `list_json`=VALUES(`list_json`), `detail_json`=VALUES(`detail_json`),
                        `collected_at`=VALUES(`collected_at`), `updated_at`=VALUES(`updated_at`)
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


def collect_poc(
    limit=DEFAULT_LIMIT,
    page_size=DEFAULT_PAGE_SIZE,
    window_id=DEFAULT_ZYING_WINDOW_ID,
    include_details=True,
    source="sale",
):
    limit = max(1, int(limit))
    if limit > 1000:
        raise ValueError("The proof of concept is limited to 1000 products per run")
    started_at = time.time()
    browser_info = openBrowser(window_id)
    if not browser_info or not browser_info.get("data"):
        raise RuntimeError(f"Could not open BitBrowser window: {browser_info}")

    service = Service(browser_info["data"]["driver"])
    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", browser_info["data"]["http"])
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception:
        releaseBrowserLease(window_id)
        raise

    try:
        driver.get(ERP_PRODUCT_URL)

        def logged_in(current_driver):
            has_token = current_driver.execute_script(
                "return !!localStorage.getItem('token');"
            )
            if "#/login" in current_driver.current_url.casefold() or not has_token:
                return False
            return True

        try:
            WebDriverWait(driver, 20).until(logged_in)
        except Exception as exc:
            raise RuntimeError(
                "智赢 ERP 尚未登录。请在已打开的 BitBrowser 中登录 "
                "https://erp.zying.net/，然后重新运行。"
            ) from exc

        # Route chunks (including module 13184) load asynchronously.
        api_call = lambda command, payload: _call_frontend_api(driver, command, payload)
        last_error = None
        for _ in range(20):
            try:
                records, total = fetch_product_index(
                    api_call,
                    limit,
                    page_size,
                    source=source,
                )
                break
            except RuntimeError as exc:
                last_error = exc
                if "not loaded" not in str(exc) and "not ready" not in str(exc):
                    raise
                time.sleep(0.5)
        else:
            raise last_error or RuntimeError("ERP product page did not become ready")

        if include_details:
            enrich_product_details(api_call, records, source=source)
        table_name = POC_TABLES[source]
        persisted = persist_records(records, table_name)
        return {
            "requested": limit,
            "collected": len(records),
            "unique_products": len({row["product_id"] for row in records}),
            "detail_ok": sum(1 for row in records if row.get("detail_ok")),
            "detail_failed": sum(1 for row in records if not row.get("detail_ok")),
            "reported_total": total,
            "persisted": persisted,
            "source": source,
            "table": table_name,
            "elapsed_seconds": round(time.time() - started_at, 2),
        }
    finally:
        service.stop()
        releaseBrowserLease(window_id)


def main():
    parser = argparse.ArgumentParser(description="Validate Zying ERP own-product APIs")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--window-id", default=DEFAULT_ZYING_WINDOW_ID)
    parser.add_argument("--no-details", action="store_true")
    parser.add_argument("--source", choices=sorted(POC_TABLES), default="sale")
    args = parser.parse_args()
    result = collect_poc(
        limit=args.limit,
        page_size=args.page_size,
        window_id=args.window_id,
        include_details=not args.no_details,
        source=args.source,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
