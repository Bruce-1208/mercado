"""Fetch product main-image links from source URLs over a direct connection.

The HTTP session deliberately ignores environment and Windows proxy settings.
For Mercado Libre sources, an optional ``MELI_ACCESS_TOKEN`` lets the collector
use the official Items resource when public pages require account verification.
Results are checkpointed to MySQL after every product, so reruns resume safely.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


if __package__ in (None, ""):
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, ValueError):
    pass

import pymysql
import requests
from bs4 import BeautifulSoup

from bit.bit_mysql import config as mysql_config


DEFAULT_TABLE = "zying_desktop_products"
DEFAULT_CATEGORY = "一马当先"
DEFAULT_TOKEN_ENV = "MELI_ACCESS_TOKEN"
MERCADO_HOST_SUFFIXES = (
    "mercadolivre.com.br",
    "mercadolibre.com.mx",
    "mercadolibre.com.co",
    "mercadolibre.com.ar",
    "mercadolibre.cl",
)

IMAGE_COLUMNS = {
    "main_image_url": "TEXT NULL",
    "main_image_fetch_status": "VARCHAR(32) NULL",
    "main_image_fetch_method": "VARCHAR(32) NULL",
    "main_image_final_url": "TEXT NULL",
    "main_image_error": "TEXT NULL",
    "main_image_fetched_at": "DATETIME NULL",
}


class SourceVerificationRequired(RuntimeError):
    pass


class SourceAuthenticationRequired(RuntimeError):
    pass


def _validate_table_name(table_name: str) -> str:
    table_name = str(table_name or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", table_name):
        raise ValueError(f"不安全的 MySQL 表名：{table_name!r}")
    return table_name


def build_direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,es-MX;q=0.8,es;q=0.7",
        }
    )
    return session


def source_item_id(source_url: str) -> str | None:
    parsed = urlparse(str(source_url or ""))
    host = parsed.netloc.casefold()
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in MERCADO_HOST_SUFFIXES):
        return None
    match = re.search(r"/(M[A-Z]{2})-?(\d+)", parsed.path, re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(1).upper()}{match.group(2)}"


def _valid_image_url(value) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _json_images(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in {"image", "images", "thumbnail", "contenturl"}:
                if isinstance(child, str):
                    image = _valid_image_url(child)
                    if image:
                        yield image
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, str):
                            image = _valid_image_url(item)
                            if image:
                                yield image
                        else:
                            yield from _json_images(item)
                else:
                    yield from _json_images(child)
            else:
                yield from _json_images(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_images(child)


def extract_main_image_from_html(html_text: str) -> str | None:
    soup = BeautifulSoup(html_text or "", "html.parser")
    for selector in (
        'meta[property="og:image"]',
        'meta[property="og:image:secure_url"]',
        'meta[name="twitter:image"]',
        'meta[name="twitter:image:src"]',
    ):
        tag = soup.select_one(selector)
        image = _valid_image_url(tag.get("content") if tag else None)
        if image:
            return image

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text() or "null")
        except (TypeError, ValueError):
            continue
        image = next(_json_images(data), None)
        if image:
            return image

    for selector, attribute in (
        (".ui-pdp-gallery img", "data-zoom"),
        (".ui-pdp-gallery img", "src"),
        ("main img", "src"),
    ):
        for tag in soup.select(selector):
            image = _valid_image_url(tag.get(attribute))
            if image:
                return image
    return None


def _looks_like_verification(final_url: str, html_text: str) -> bool:
    path = urlparse(final_url).path.casefold()
    if "/gz/account-verification" in path:
        return True
    lowered = (html_text or "").casefold()
    return any(
        marker in lowered
        for marker in (
            "suspicious-traffic-frontend",
            "negative_traffic",
            "_____tmd_____/punish",
            '"action":"captcha"',
        )
    )


def _fetch_from_mercado_api(
    session: requests.Session,
    item_id: str,
    access_token: str,
    timeout: float,
):
    response = session.get(
        f"https://api.mercadolibre.com/items/{item_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if response.status_code in {401, 403}:
        raise SourceAuthenticationRequired(
            f"Mercado Items API 鉴权失败（HTTP {response.status_code}）；"
            "请更新 MELI_ACCESS_TOKEN"
        )
    response.raise_for_status()
    data = response.json()
    pictures = data.get("pictures") or []
    image = None
    if pictures and isinstance(pictures[0], dict):
        image = pictures[0].get("secure_url") or pictures[0].get("url")
    image = _valid_image_url(image or data.get("secure_thumbnail") or data.get("thumbnail"))
    if not image:
        raise RuntimeError(f"Mercado Items API 未返回图片：{item_id}")
    return {
        "image_url": image,
        "method": "mercado_items_api",
        "final_url": response.url,
    }


def fetch_source_main_image(
    session: requests.Session,
    source_url: str,
    *,
    access_token: str | None = None,
    timeout: float = 30,
):
    item_id = source_item_id(source_url)
    if item_id and access_token:
        return _fetch_from_mercado_api(session, item_id, access_token, timeout)

    response = session.get(source_url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    if _looks_like_verification(response.url, response.text):
        host = urlparse(source_url).netloc.casefold()
        if host == "detail.1688.com" or host.endswith(".1688.com"):
            raise SourceVerificationRequired(
                "1688 来源页要求人机验证；请在非 VPN 网络的浏览器中通过验证后续跑"
            )
        raise SourceVerificationRequired(
            "来源页面要求 Mercado Libre 账号验证；请配置 MELI_ACCESS_TOKEN"
        )
    image = extract_main_image_from_html(response.text)
    if not image:
        raise RuntimeError("来源页面中没有找到主图字段")
    return {
        "image_url": image,
        "method": "source_page",
        "final_url": response.url,
    }


def ensure_image_columns(cursor, table_name: str):
    table_name = _validate_table_name(table_name)
    cursor.execute(
        """
        SELECT `column_name` AS `column_name`
        FROM `information_schema`.`columns`
        WHERE `table_schema`=DATABASE() AND `table_name`=%s
        """,
        (table_name,),
    )
    existing = {row["column_name"] for row in cursor.fetchall()}
    for column, definition in IMAGE_COLUMNS.items():
        if column not in existing:
            cursor.execute(
                f"ALTER TABLE `{table_name}` ADD COLUMN `{column}` {definition}"
            )

    cursor.execute(
        """
        SELECT COUNT(*) AS `count`
        FROM `information_schema`.`statistics`
        WHERE `table_schema`=DATABASE() AND `table_name`=%s
          AND `index_name`='idx_zying_desktop_image_status'
        """,
        (table_name,),
    )
    if not int((cursor.fetchone() or {}).get("count") or 0):
        cursor.execute(
            f"CREATE INDEX `idx_zying_desktop_image_status` "
            f"ON `{table_name}` (`main_image_fetch_status`)"
        )


def _load_products(cursor, table_name, category, source_batch, limit, force):
    pending_filter = "" if force else (
        "AND (`main_image_fetch_status` IS NULL "
        "OR `main_image_fetch_status` <> 'ok')"
    )
    limit_sql = " LIMIT %s" if limit is not None else ""
    batch_filter = ""
    params = [category]
    if source_batch:
        batch_filter = "AND `source_batch`=%s"
        params.append(source_batch)
    if limit is not None:
        params.append(int(limit))
    cursor.execute(
        f"""
        SELECT `product_id`, MIN(`source_url`) AS `source_url`,
               MIN(`source_page`) AS `source_page`
        FROM `{_validate_table_name(table_name)}`
        WHERE `export_category`=%s
          {batch_filter}
          AND `source_url` IS NOT NULL AND `source_url` <> ''
          {pending_filter}
        GROUP BY `product_id`
        ORDER BY `source_page`, `product_id`
        {limit_sql}
        """,
        tuple(params),
    )
    return cursor.fetchall()


def _checkpoint_product(
    cursor,
    table_name,
    category,
    source_batch,
    product_id,
    *,
    image_url,
    status,
    method,
    final_url,
    error,
):
    cursor.execute(
        f"""
        UPDATE `{_validate_table_name(table_name)}`
        SET `main_image_url`=%s,
            `main_image_fetch_status`=%s,
            `main_image_fetch_method`=%s,
            `main_image_final_url`=%s,
            `main_image_error`=%s,
            `main_image_fetched_at`=%s
        WHERE `export_category`=%s
          AND (%s IS NULL OR `source_batch`=%s)
          AND `product_id`=%s
        """,
        (
            image_url,
            status,
            method,
            final_url,
            error,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            category,
            source_batch,
            source_batch,
            product_id,
        ),
    )


def fetch_images_to_mysql(
    *,
    table_name=DEFAULT_TABLE,
    category=DEFAULT_CATEGORY,
    source_batch=None,
    limit=None,
    delay=0.4,
    timeout=30,
    force=False,
    token_env=DEFAULT_TOKEN_ENV,
    connection_factory=None,
):
    table_name = _validate_table_name(table_name)
    connection_factory = connection_factory or (lambda: pymysql.connect(**mysql_config))
    connection = connection_factory()
    session = build_direct_session()
    access_token = str(os.environ.get(token_env) or "").strip() or None
    started = time.time()
    counts = {
        "ok": 0,
        "verification_required": 0,
        "authentication_required": 0,
        "error": 0,
    }
    processed = 0
    aborted_reason = None
    try:
        with connection.cursor() as cursor:
            ensure_image_columns(cursor, table_name)
            connection.commit()
            products = _load_products(
                cursor,
                table_name,
                category,
                source_batch,
                limit,
                force,
            )
            for record in products:
                product_id = str(record["product_id"])
                source_url = str(record["source_url"])
                image_url = method = final_url = error = None
                try:
                    result = fetch_source_main_image(
                        session,
                        source_url,
                        access_token=access_token,
                        timeout=timeout,
                    )
                    image_url = result["image_url"]
                    method = result["method"]
                    final_url = result["final_url"]
                    status = "ok"
                except SourceVerificationRequired as exc:
                    status = "verification_required"
                    error = str(exc)[:2000]
                except SourceAuthenticationRequired as exc:
                    status = "authentication_required"
                    error = str(exc)[:2000]
                except Exception as exc:
                    status = "error"
                    error = f"{type(exc).__name__}: {exc}"[:2000]

                _checkpoint_product(
                    cursor,
                    table_name,
                    category,
                    source_batch,
                    product_id,
                    image_url=image_url,
                    status=status,
                    method=method,
                    final_url=final_url,
                    error=error,
                )
                connection.commit()
                counts[status] += 1
                processed += 1
                print(
                    f"主图 {processed}/{len(products)}：{product_id} {status}",
                    flush=True,
                )

                if status == "verification_required" and not access_token:
                    aborted_reason = (
                        "来源页要求人机或账号验证；请在非 VPN 网络完成验证。"
                        "Mercado Libre 可设置 MELI_ACCESS_TOKEN 后断点续跑"
                    )
                    break
                if status == "authentication_required":
                    aborted_reason = (
                        "MELI_ACCESS_TOKEN 无效或已过期；更新令牌后可断点续跑"
                    )
                    break
                if delay > 0:
                    time.sleep(delay)

            cursor.execute(
                f"""
                SELECT COUNT(DISTINCT CASE
                           WHEN `main_image_fetch_status`='ok' THEN `product_id`
                       END) AS `with_image`,
                       COUNT(DISTINCT CASE
                           WHEN `main_image_fetch_status`='verification_required'
                           THEN `product_id`
                       END) AS `verification_required`,
                       COUNT(DISTINCT CASE
                           WHEN `main_image_fetch_status`='authentication_required'
                           THEN `product_id`
                       END) AS `authentication_required`,
                       COUNT(DISTINCT `product_id`) AS `total_products`
                FROM `{table_name}`
                WHERE `export_category`=%s
                  AND (%s IS NULL OR `source_batch`=%s)
                """,
                (category, source_batch, source_batch),
            )
            database_summary = cursor.fetchone() or {}
        return {
            "table": table_name,
            "category": category,
            "source_batch": source_batch,
            "selected": len(products),
            "processed": processed,
            "counts": counts,
            "database": database_summary,
            "network_mode": "direct_no_proxy",
            "token_configured": bool(access_token),
            "aborted_reason": aborted_reason,
            "elapsed_seconds": round(time.time() - started, 2),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        session.close()
        connection.close()


def main():
    parser = argparse.ArgumentParser(
        description="通过非代理直连获取来源产品主图并回填 MySQL",
    )
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--category", default=DEFAULT_CATEGORY)
    parser.add_argument("--source-batch")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--delay", type=float, default=0.4)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")
    if args.delay < 0:
        parser.error("--delay 不能小于 0")
    result = fetch_images_to_mysql(
        table_name=args.table,
        category=args.category,
        source_batch=args.source_batch,
        limit=args.limit,
        delay=args.delay,
        timeout=args.timeout,
        force=args.force,
        token_env=args.token_env,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
