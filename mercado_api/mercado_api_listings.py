"""Download all Mercado Libre Global Selling listings into SQLite.

The public entry point is :func:`sync_listings`.  It only requires an access
token; the seller id is resolved through ``/users/me`` and the database schema
is created automatically.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests


API_BASE_URL = "https://api.mercadolibre.com"
DEFAULT_DATABASE = "mercado_api_listings.db"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class MercadoAPIError(RuntimeError):
    """Raised when Mercado Libre returns an unusable API response."""


@dataclass(frozen=True)
class SyncResult:
    """Summary returned after one completed synchronization."""

    seller_id: str
    database_path: str
    discovered: int
    stored: int
    failed: int
    sync_run_id: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _normalise_token(access_token: str) -> str:
    token = (access_token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise ValueError("access_token 不能为空")
    return token


def _attribute_value(attributes: Any, attribute_id: str) -> Optional[str]:
    if not isinstance(attributes, list):
        return None
    for attribute in attributes:
        if not isinstance(attribute, Mapping) or attribute.get("id") != attribute_id:
            continue
        value = attribute.get("value_name")
        if value is not None:
            return str(value)
        values = attribute.get("values")
        if isinstance(values, list):
            names = [str(item.get("name")) for item in values if isinstance(item, Mapping) and item.get("name")]
            return ", ".join(names) or None
    return None


def _first_picture(item: Mapping[str, Any]) -> Optional[str]:
    pictures = item.get("pictures")
    if isinstance(pictures, list) and pictures:
        first = pictures[0]
        if isinstance(first, Mapping):
            return first.get("secure_url") or first.get("url")
    thumbnail = item.get("secure_thumbnail") or item.get("thumbnail")
    return str(thumbnail) if thumbnail else None


class MercadoLibreClient:
    """Small authenticated client for the listing resources used here."""

    def __init__(
        self,
        access_token: str,
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.access_token = _normalise_token(access_token)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
                "User-Agent": "mercado-api-listings/1.0",
            }
        )

    def close(self) -> None:
        self.session.close()

    def _request_json(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        url = f"{API_BASE_URL}{path}"
        last_error: Optional[BaseException] = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request("GET", url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(self.backoff_seconds * (2**attempt))
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after is not None else self.backoff_seconds * (2**attempt)
                except ValueError:
                    delay = self.backoff_seconds * (2**attempt)
                time.sleep(min(max(delay, 0.0), 60.0))
                continue

            try:
                payload = response.json()
            except ValueError as exc:
                raise MercadoAPIError(
                    f"Mercado Libre API 返回了非 JSON 内容（HTTP {response.status_code}，{path}）"
                ) from exc

            if not 200 <= response.status_code < 300:
                if isinstance(payload, Mapping):
                    message = payload.get("message") or payload.get("error") or _json_dump(payload)
                else:
                    message = str(payload)
                raise MercadoAPIError(f"Mercado Libre API 请求失败（HTTP {response.status_code}，{path}）：{message}")
            return payload

        raise MercadoAPIError(f"无法连接 Mercado Libre API（{path}）：{last_error}") from last_error

    def get_current_user(self) -> Dict[str, Any]:
        payload = self._request_json("/users/me")
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise MercadoAPIError("/users/me 响应中缺少店铺 id")
        return payload

    def list_all_listing_ids(self, seller_id: str, *, page_size: int = 100) -> List[str]:
        """Return every listing id using scan/scroll pagination."""

        path = f"/marketplace/users/{seller_id}/items/search"
        params: Dict[str, Any] = {"search_type": "scan", "limit": page_size}
        listing_ids: List[str] = []
        seen_ids = set()
        previous_page: Optional[Tuple[str, ...]] = None

        while True:
            payload = self._request_json(path, params=params)
            if not isinstance(payload, Mapping):
                raise MercadoAPIError("listing 搜索接口返回格式不正确")

            results = payload.get("results") or []
            if not isinstance(results, list):
                raise MercadoAPIError("listing 搜索结果中的 results 不是列表")
            if not results:
                break

            new_count = 0
            for item_id in results:
                item_id = str(item_id)
                if item_id not in seen_ids:
                    seen_ids.add(item_id)
                    listing_ids.append(item_id)
                    new_count += 1

            current_page = tuple(str(item_id) for item_id in results)
            if new_count == 0 and current_page == previous_page:
                raise MercadoAPIError("scan 分页连续返回相同结果，已停止以避免死循环")
            previous_page = current_page

            scroll_id = payload.get("scroll_id")
            paging = payload.get("paging") if isinstance(payload.get("paging"), Mapping) else {}
            total = int(paging.get("total") or 0)
            if total and len(listing_ids) >= total:
                break
            if not scroll_id:
                if len(results) < page_size:
                    break
                raise MercadoAPIError("scan 分页响应缺少 scroll_id，无法确认是否已获取全部 listing")
            scroll_id = str(scroll_id)
            params = {"search_type": "scan", "limit": page_size, "scroll_id": scroll_id}

        return listing_ids

    def get_listing_details(
        self, listing_ids: Sequence[str], *, batch_size: int = 20
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        """Fetch full item bodies with the official global-listing multiget."""

        details: List[Dict[str, Any]] = []
        failures: Dict[str, str] = {}

        for batch in _chunks(list(listing_ids), max(1, min(batch_size, 20))):
            payload = self._request_json("/items", params={"ids": ",".join(batch)})
            if not isinstance(payload, list):
                raise MercadoAPIError("listing 批量详情接口返回格式不正确")

            returned_ids = set()
            for entry in payload:
                if not isinstance(entry, Mapping):
                    continue
                body = entry.get("body")
                code = int(entry.get("code") or 0)
                if code == 200 and isinstance(body, dict) and body.get("id") is not None:
                    item_id = str(body["id"])
                    returned_ids.add(item_id)
                    details.append(body)
                    continue

                item_id = None
                if isinstance(body, Mapping) and body.get("id") is not None:
                    item_id = str(body["id"])
                if item_id is None and len(batch) == 1:
                    item_id = batch[0]
                message = body.get("message") if isinstance(body, Mapping) else None
                if item_id:
                    returned_ids.add(item_id)
                    failures[item_id] = f"HTTP {code}: {message or '无法获取 listing 详情'}"

            for item_id in batch:
                if item_id not in returned_ids:
                    failures[item_id] = "批量详情响应中没有返回该 listing"

        return details, failures


SCHEMA = """
CREATE TABLE IF NOT EXISTS mercado_sellers (
    seller_id TEXT PRIMARY KEY,
    nickname TEXT,
    site_id TEXT,
    raw_json TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mercado_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    stored_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS mercado_listings (
    seller_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    site_id TEXT,
    title TEXT,
    category_id TEXT,
    domain_id TEXT,
    currency_id TEXT,
    price NUMERIC,
    original_price NUMERIC,
    available_quantity INTEGER,
    sold_quantity INTEGER,
    status TEXT,
    sub_status_json TEXT NOT NULL DEFAULT '[]',
    listing_type_id TEXT,
    condition TEXT,
    buying_mode TEXT,
    permalink TEXT,
    thumbnail TEXT,
    catalog_product_id TEXT,
    seller_custom_field TEXT,
    seller_sku TEXT,
    start_time TEXT,
    stop_time TEXT,
    date_created TEXT,
    last_updated TEXT,
    health NUMERIC,
    automatic_relist INTEGER,
    fetch_status TEXT NOT NULL DEFAULT 'ok',
    fetch_error TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,
    first_synced_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (seller_id, item_id),
    FOREIGN KEY (seller_id) REFERENCES mercado_sellers(seller_id)
);

CREATE INDEX IF NOT EXISTS idx_mercado_listings_status
    ON mercado_listings(seller_id, status, is_current);
CREATE INDEX IF NOT EXISTS idx_mercado_listings_sku
    ON mercado_listings(seller_id, seller_sku);

CREATE TABLE IF NOT EXISTS mercado_listing_variations (
    seller_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    variation_id TEXT NOT NULL,
    seller_sku TEXT,
    price NUMERIC,
    available_quantity INTEGER,
    sold_quantity INTEGER,
    picture_ids_json TEXT NOT NULL DEFAULT '[]',
    attributes_json TEXT NOT NULL DEFAULT '[]',
    attribute_combinations_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    PRIMARY KEY (seller_id, item_id, variation_id),
    FOREIGN KEY (seller_id, item_id) REFERENCES mercado_listings(seller_id, item_id)
        ON DELETE CASCADE
);
"""


class ListingsDatabase:
    """SQLite persistence for sellers, listings, variations and sync history."""

    def __init__(self, database_path: os.PathLike[str] | str) -> None:
        self.path = Path(database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def upsert_seller(self, seller: Mapping[str, Any], synced_at: str) -> str:
        seller_id = str(seller["id"])
        self.connection.execute(
            """
            INSERT INTO mercado_sellers (seller_id, nickname, site_id, raw_json, last_synced_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(seller_id) DO UPDATE SET
                nickname=excluded.nickname,
                site_id=excluded.site_id,
                raw_json=excluded.raw_json,
                last_synced_at=excluded.last_synced_at
            """,
            (seller_id, seller.get("nickname"), seller.get("site_id"), _json_dump(seller), synced_at),
        )
        self.connection.commit()
        return seller_id

    def start_run(self, seller_id: str, started_at: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO mercado_sync_runs (seller_id, started_at, status) VALUES (?, ?, 'running')",
            (seller_id, started_at),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        discovered: int = 0,
        stored: int = 0,
        failed: int = 0,
        error_message: Optional[str] = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE mercado_sync_runs
               SET finished_at=?, status=?, discovered_count=?, stored_count=?,
                   failed_count=?, error_message=?
             WHERE id=?
            """,
            (_utc_now(), status, discovered, stored, failed, error_message, run_id),
        )
        self.connection.commit()

    def replace_current_snapshot(
        self,
        seller_id: str,
        details: Sequence[Mapping[str, Any]],
        failures: Mapping[str, str],
        synced_at: str,
    ) -> None:
        connection = self.connection
        try:
            connection.execute("BEGIN")
            connection.execute("UPDATE mercado_listings SET is_current=0 WHERE seller_id=?", (seller_id,))

            for item in details:
                self._upsert_listing(seller_id, item, synced_at)
                item_id = str(item["id"])
                connection.execute(
                    "DELETE FROM mercado_listing_variations WHERE seller_id=? AND item_id=?",
                    (seller_id, item_id),
                )
                variations = item.get("variations")
                if isinstance(variations, list):
                    for variation in variations:
                        if isinstance(variation, Mapping) and variation.get("id") is not None:
                            self._insert_variation(seller_id, item_id, variation, synced_at)

            for item_id, error in failures.items():
                self._upsert_listing(
                    seller_id,
                    {"id": item_id, "_fetch_error": error},
                    synced_at,
                    fetch_status="error",
                    fetch_error=error,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _upsert_listing(
        self,
        seller_id: str,
        item: Mapping[str, Any],
        synced_at: str,
        *,
        fetch_status: str = "ok",
        fetch_error: Optional[str] = None,
    ) -> None:
        values = (
            seller_id,
            str(item["id"]),
            item.get("site_id"),
            item.get("title"),
            item.get("category_id"),
            item.get("domain_id"),
            item.get("currency_id"),
            item.get("price"),
            item.get("original_price"),
            item.get("available_quantity"),
            item.get("sold_quantity"),
            item.get("status"),
            _json_dump(item.get("sub_status") or []),
            item.get("listing_type_id"),
            item.get("condition"),
            item.get("buying_mode"),
            item.get("permalink"),
            _first_picture(item),
            item.get("catalog_product_id"),
            item.get("seller_custom_field"),
            _attribute_value(item.get("attributes"), "SELLER_SKU"),
            item.get("start_time"),
            item.get("stop_time"),
            item.get("date_created"),
            item.get("last_updated"),
            item.get("health"),
            int(bool(item.get("automatic_relist"))) if item.get("automatic_relist") is not None else None,
            fetch_status,
            fetch_error,
            synced_at,
            synced_at,
            _json_dump(item),
        )
        self.connection.execute(
            """
            INSERT INTO mercado_listings (
                seller_id, item_id, site_id, title, category_id, domain_id, currency_id,
                price, original_price, available_quantity, sold_quantity, status,
                sub_status_json, listing_type_id, condition, buying_mode, permalink,
                thumbnail, catalog_product_id, seller_custom_field, seller_sku, start_time,
                stop_time, date_created, last_updated, health, automatic_relist,
                fetch_status, fetch_error, is_current, first_synced_at, last_synced_at, raw_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, 1, ?, ?, ?
            )
            ON CONFLICT(seller_id, item_id) DO UPDATE SET
                site_id=COALESCE(excluded.site_id, mercado_listings.site_id),
                title=COALESCE(excluded.title, mercado_listings.title),
                category_id=COALESCE(excluded.category_id, mercado_listings.category_id),
                domain_id=COALESCE(excluded.domain_id, mercado_listings.domain_id),
                currency_id=COALESCE(excluded.currency_id, mercado_listings.currency_id),
                price=COALESCE(excluded.price, mercado_listings.price),
                original_price=COALESCE(excluded.original_price, mercado_listings.original_price),
                available_quantity=COALESCE(excluded.available_quantity, mercado_listings.available_quantity),
                sold_quantity=COALESCE(excluded.sold_quantity, mercado_listings.sold_quantity),
                status=COALESCE(excluded.status, mercado_listings.status),
                sub_status_json=CASE WHEN excluded.fetch_status='ok' THEN excluded.sub_status_json ELSE mercado_listings.sub_status_json END,
                listing_type_id=COALESCE(excluded.listing_type_id, mercado_listings.listing_type_id),
                condition=COALESCE(excluded.condition, mercado_listings.condition),
                buying_mode=COALESCE(excluded.buying_mode, mercado_listings.buying_mode),
                permalink=COALESCE(excluded.permalink, mercado_listings.permalink),
                thumbnail=COALESCE(excluded.thumbnail, mercado_listings.thumbnail),
                catalog_product_id=COALESCE(excluded.catalog_product_id, mercado_listings.catalog_product_id),
                seller_custom_field=COALESCE(excluded.seller_custom_field, mercado_listings.seller_custom_field),
                seller_sku=COALESCE(excluded.seller_sku, mercado_listings.seller_sku),
                start_time=COALESCE(excluded.start_time, mercado_listings.start_time),
                stop_time=COALESCE(excluded.stop_time, mercado_listings.stop_time),
                date_created=COALESCE(excluded.date_created, mercado_listings.date_created),
                last_updated=COALESCE(excluded.last_updated, mercado_listings.last_updated),
                health=COALESCE(excluded.health, mercado_listings.health),
                automatic_relist=COALESCE(excluded.automatic_relist, mercado_listings.automatic_relist),
                fetch_status=excluded.fetch_status,
                fetch_error=excluded.fetch_error,
                is_current=1,
                last_synced_at=excluded.last_synced_at,
                raw_json=CASE WHEN excluded.fetch_status='ok' THEN excluded.raw_json ELSE mercado_listings.raw_json END
            """,
            values,
        )

    def _insert_variation(
        self,
        seller_id: str,
        item_id: str,
        variation: Mapping[str, Any],
        synced_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO mercado_listing_variations (
                seller_id, item_id, variation_id, seller_sku, price, available_quantity,
                sold_quantity, picture_ids_json, attributes_json,
                attribute_combinations_json, raw_json, last_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seller_id,
                item_id,
                str(variation["id"]),
                _attribute_value(variation.get("attributes"), "SELLER_SKU"),
                variation.get("price"),
                variation.get("available_quantity"),
                variation.get("sold_quantity"),
                _json_dump(variation.get("picture_ids") or []),
                _json_dump(variation.get("attributes") or []),
                _json_dump(variation.get("attribute_combinations") or []),
                _json_dump(variation),
                synced_at,
            ),
        )


def sync_listings(
    access_token: str,
    database_path: os.PathLike[str] | str = DEFAULT_DATABASE,
    *,
    session: Optional[requests.Session] = None,
    timeout: float = 30.0,
    max_retries: int = 4,
) -> SyncResult:
    """Synchronize every listing available to ``access_token`` into SQLite.

    Existing rows are updated in place. Listings absent from the latest complete
    scan remain for history but get ``is_current = 0``. Access tokens are never
    written to the database.
    """

    client = MercadoLibreClient(
        access_token,
        session=session,
        timeout=timeout,
        max_retries=max_retries,
    )
    database: Optional[ListingsDatabase] = None
    run_id: Optional[int] = None
    discovered = stored = failed = 0

    try:
        seller = client.get_current_user()
        synced_at = _utc_now()
        database = ListingsDatabase(database_path)
        seller_id = database.upsert_seller(seller, synced_at)
        run_id = database.start_run(seller_id, synced_at)

        listing_ids = client.list_all_listing_ids(seller_id)
        discovered = len(listing_ids)
        details, failures = client.get_listing_details(listing_ids)
        stored = len(details)
        failed = len(failures)
        database.replace_current_snapshot(seller_id, details, failures, synced_at)
        database.finish_run(
            run_id,
            status="success" if not failures else "partial",
            discovered=discovered,
            stored=stored,
            failed=failed,
        )
        return SyncResult(
            seller_id=seller_id,
            database_path=str(database.path),
            discovered=discovered,
            stored=stored,
            failed=failed,
            sync_run_id=run_id,
        )
    except Exception as exc:
        if database is not None and run_id is not None:
            database.finish_run(
                run_id,
                status="failed",
                discovered=discovered,
                stored=stored,
                failed=failed,
                error_message=str(exc),
            )
        raise
    finally:
        client.close()
        if database is not None:
            database.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把美客多店铺的所有 listings 同步到 SQLite")
    parser.add_argument("--token", help="Access token；省略时从环境变量读取或安全提示输入")
    parser.add_argument("--database", default=DEFAULT_DATABASE, help=f"SQLite 文件（默认：{DEFAULT_DATABASE}）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    token = args.token or os.getenv("MERCADO_ACCESS_TOKEN")
    if not token:
        token = getpass.getpass("请输入美客多店铺 access token（输入内容不会显示）：")

    try:
        result = sync_listings(token, args.database)
    except (MercadoAPIError, ValueError, sqlite3.Error) as exc:
        print(f"同步失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"同步完成：店铺 {result.seller_id}，发现 {result.discovered} 条，"
        f"成功 {result.stored} 条，失败 {result.failed} 条。\n"
        f"数据库：{result.database_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
