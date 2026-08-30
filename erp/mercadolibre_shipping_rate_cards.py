"""Official Mercado Libre Global Selling shipping standards.

The authoritative rate cards for this cross-border workbench are the Global
Selling Cainiao announcements for shipments from China/Hong Kong, not the
domestic marketplace reputation rate cards. The announcements publish charges
directly in USD by destination, listing-price threshold, and billable weight.
"""

from __future__ import annotations

import json
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping


OFFICIAL_SHIPPING_RATE_CARD_TABLE = "erp_mercadolibre_official_shipping_rate_cards"
OFFICIAL_CONTENT_API = (
    "https://api.mercadolibre.com/cx/knowledge-middleware/contents/{content_id}"
)
OFFICIAL_REPUTATION_CODE = "cross_border_cainiao"
OFFICIAL_REPUTATION_LABEL = "跨境卖家 · 中国/香港发货 · Cainiao"

# Content IDs linked from Global Selling's official Costs and logistic
# information folder (content 17556). Uruguay currently has no published
# China/Hong Kong Cainiao rate card, so it must remain visibly unavailable.
SITE_METADATA = {
    "MLM": {
        "country_name": "墨西哥",
        "currency_id": "MXN",
        "price_threshold_local": 299.0,
        "content_id": 41817,
        "source_url": "https://global-selling.mercadolibre.com/help/41817",
    },
    "MLB": {
        "country_name": "巴西",
        "currency_id": "BRL",
        "price_threshold_local": 79.0,
        "content_id": 41814,
        "source_url": "https://global-selling.mercadolibre.com/help/41814",
    },
    "MLA": {
        "country_name": "阿根廷",
        "currency_id": "ARS",
        "price_threshold_local": 33000.0,
        "content_id": 42837,
        "source_url": "https://global-selling.mercadolibre.com/help/42837",
    },
    "MLC": {
        "country_name": "智利",
        "currency_id": "CLP",
        "price_threshold_local": 19990.0,
        "content_id": 38716,
        "source_url": "https://global-selling.mercadolibre.com/help/38716",
    },
    "MCO": {
        "country_name": "哥伦比亚",
        "currency_id": "COP",
        "price_threshold_local": 60000.0,
        "content_id": 37289,
        "source_url": "https://global-selling.mercadolibre.com/help/37289",
    },
    "MLU": {
        "country_name": "乌拉圭",
        "currency_id": "UYU",
        "price_threshold_local": None,
        "content_id": None,
        "source_url": "https://global-selling.mercadolibre.com/help/17556",
    },
}

_schema_lock = threading.Lock()
_schema_ready = False


def _connect() -> Any:
    import pymysql
    from bit.bit_mysql import config

    return pymysql.connect(**config)


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def ensure_official_shipping_rate_card_table(cursor: Any) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{OFFICIAL_SHIPPING_RATE_CARD_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `site_id` VARCHAR(16) NOT NULL,
            `country_name` VARCHAR(64) NOT NULL,
            `currency_id` VARCHAR(16) NOT NULL,
            `reputation_code` VARCHAR(32) NOT NULL,
            `reputation_label` VARCHAR(128) NULL,
            `rate_kind` VARCHAR(32) NOT NULL,
            `table_index` INT NOT NULL,
            `price_band_index` INT NOT NULL,
            `price_label` VARCHAR(128) NOT NULL,
            `price_min_local` DECIMAL(20,4) NULL,
            `price_max_local` DECIMAL(20,4) NULL,
            `weight_band_index` INT NOT NULL,
            `weight_label` VARCHAR(128) NOT NULL,
            `weight_min_g` DECIMAL(20,4) NULL,
            `weight_max_g` DECIMAL(20,4) NULL,
            `shipping_amount_local` DECIMAL(20,4) NOT NULL,
            `exchange_rate_to_usd` DECIMAL(24,12) NOT NULL,
            `shipping_amount_usd` DECIMAL(20,4) NOT NULL,
            `source_url` VARCHAR(512) NOT NULL,
            `source_payload_json` LONGTEXT NULL,
            `refreshed_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_official_shipping_band` (
                `site_id`, `reputation_code`, `rate_kind`,
                `price_band_index`, `weight_band_index`
            ),
            KEY `idx_erp_meli_official_shipping_lookup` (
                `site_id`, `rate_kind`, `weight_max_g`, `price_max_local`
            ),
            KEY `idx_erp_meli_official_shipping_refresh` (`refreshed_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def _ensure_schema(connection: Any) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with connection.cursor() as cursor:
            ensure_official_shipping_rate_card_table(cursor)
        connection.commit()
        _schema_ready = True


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


class _TableExtractor(HTMLParser):
    """Extract visible table cells without adding an HTML dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"style", "script"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"style", "script"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag in {"th", "td"} and self._cell is not None and self._row is not None:
            self._row.append(_clean_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and self._cell is not None:
            self._cell.append(data)


def extract_official_tables(content_html: str) -> list[list[list[str]]]:
    parser = _TableExtractor()
    parser.feed(str(content_html or ""))
    parser.close()
    return parser.tables


def _usd_amount(value: Any) -> float | None:
    match = re.search(
        r"(?:USD\s*)?([0-9]+(?:[.,][0-9]+)?)", _clean_text(value), re.I
    )
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return amount if math.isfinite(amount) and amount >= 0 else None


def _weight_range_g(value: Any) -> tuple[float | None, float | None]:
    text = _clean_text(value).lower().replace(",", ".")
    numbers = [float(number) for number in re.findall(r"\d+(?:\.\d+)?", text)]
    if not numbers:
        return None, None
    if any(marker in text for marker in ("beyond", "and above", "or above", "more than")):
        return numbers[0] * 1000, None
    if len(numbers) >= 2:
        return numbers[0] * 1000, numbers[1] * 1000
    return 0.0, numbers[0] * 1000


def parse_official_shipping_announcement(
    site_id: str,
    content_html: str,
) -> list[dict[str, Any]]:
    """Parse one current Global Selling Cainiao announcement."""

    site_id = str(site_id or "").upper()
    metadata = SITE_METADATA.get(site_id)
    if not metadata:
        raise ValueError(f"不支持的 Mercado 站点：{site_id}")
    if not metadata.get("content_id"):
        raise ValueError(f"{metadata['country_name']} 官方未公布跨境 Cainiao 运费标准")
    threshold = float(metadata["price_threshold_local"])
    rate_table = None
    for table in extract_official_tables(content_html):
        if len(table) >= 3 and any("weight" in cell.lower() for cell in table[0]):
            rate_table = table
            break
    if not rate_table:
        raise ValueError(f"{metadata['country_name']} 官方公告没有读取到有效运费表")

    condition_labels = rate_table[1]
    if len(condition_labels) < 2:
        raise ValueError(f"{metadata['country_name']} 官方公告缺少售价门槛列")
    rows: list[dict[str, Any]] = []
    for weight_band_index, raw_row in enumerate(rate_table[2:]):
        if len(raw_row) < 3:
            continue
        weight_label = _clean_text(raw_row[0])
        weight_min_g, weight_max_g = _weight_range_g(weight_label)
        if weight_min_g is None and weight_max_g is None:
            continue
        for price_band_index, rate_kind in enumerate(("above_threshold", "below_threshold")):
            amount_usd = _usd_amount(raw_row[price_band_index + 1])
            if amount_usd is None:
                continue
            above = rate_kind == "above_threshold"
            rows.append({
                "site_id": site_id,
                "country_name": metadata["country_name"],
                "currency_id": metadata["currency_id"],
                "reputation_code": OFFICIAL_REPUTATION_CODE,
                "reputation_label": OFFICIAL_REPUTATION_LABEL,
                "rate_kind": rate_kind,
                "table_index": 0,
                "price_band_index": price_band_index,
                "price_label": _clean_text(condition_labels[price_band_index]),
                "price_min_local": threshold if above else 0.0,
                "price_max_local": None if above else threshold,
                "weight_band_index": weight_band_index,
                "weight_label": weight_label,
                "weight_min_g": weight_min_g,
                "weight_max_g": weight_max_g,
                # Schema compatibility only; the authoritative amount is USD.
                "shipping_amount_local": amount_usd,
                "shipping_amount_usd": amount_usd,
                "source_url": metadata["source_url"],
            })
    if not rows:
        raise ValueError(f"{metadata['country_name']} 官方公告没有读取到有效运费金额")
    return rows


class OfficialShippingRateCardStore:
    def __init__(self, *, connection_factory=None) -> None:
        self.connection_factory = connection_factory or _connect

    def _connection(self) -> Any:
        connection = self.connection_factory()
        try:
            _ensure_schema(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def clear_site_rates(self, site_id: str) -> int:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM `{OFFICIAL_SHIPPING_RATE_CARD_TABLE}` WHERE `site_id` = %s",
                    (str(site_id or "").upper(),),
                )
                changed = max(0, int(cursor.rowcount or 0))
            connection.commit()
            return changed
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def replace_site_rates(
        self,
        site_id: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        exchange_rate_to_usd: float,
        source_payload: Mapping[str, Any] | None = None,
    ) -> int:
        site_id = str(site_id or "").upper()
        exchange_rate = float(exchange_rate_to_usd)
        if exchange_rate <= 0:
            raise ValueError("官方美元汇率无效")
        values = []
        refreshed_at = _now_text()
        payload_text = json.dumps(
            dict(source_payload or {}), ensure_ascii=False, separators=(",", ":")
        )
        for row in rows or []:
            amount_usd = float(row["shipping_amount_usd"])
            values.append((
                site_id,
                row["country_name"],
                row["currency_id"],
                row.get("reputation_code") or OFFICIAL_REPUTATION_CODE,
                row.get("reputation_label") or OFFICIAL_REPUTATION_LABEL,
                row["rate_kind"],
                int(row["table_index"]),
                int(row["price_band_index"]),
                row["price_label"],
                row.get("price_min_local"),
                row.get("price_max_local"),
                int(row["weight_band_index"]),
                row["weight_label"],
                row.get("weight_min_g"),
                row.get("weight_max_g"),
                amount_usd,
                exchange_rate,
                amount_usd,
                row["source_url"],
                payload_text,
                refreshed_at,
            ))
        if not values:
            raise ValueError(f"{site_id} 没有可写入的官方运费")
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                # Remove domestic-reputation rows too. A site must contain only
                # the current cross-border official announcement.
                cursor.execute(
                    f"DELETE FROM `{OFFICIAL_SHIPPING_RATE_CARD_TABLE}` WHERE `site_id` = %s",
                    (site_id,),
                )
                cursor.executemany(
                    f"""
                    INSERT INTO `{OFFICIAL_SHIPPING_RATE_CARD_TABLE}` (
                        `site_id`, `country_name`, `currency_id`,
                        `reputation_code`, `reputation_label`, `rate_kind`,
                        `table_index`, `price_band_index`, `price_label`,
                        `price_min_local`, `price_max_local`, `weight_band_index`,
                        `weight_label`, `weight_min_g`, `weight_max_g`,
                        `shipping_amount_local`, `exchange_rate_to_usd`,
                        `shipping_amount_usd`, `source_url`, `source_payload_json`,
                        `refreshed_at`
                    ) VALUES ({", ".join(["%s"] * 21)})
                    """,
                    values,
                )
            connection.commit()
            return len(values)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def match(
        self,
        *,
        site_id: str,
        price_local: float,
        billable_weight_g: float,
        free_shipping: bool = True,
    ) -> dict[str, Any] | None:
        site_id = str(site_id or "").upper()
        metadata = SITE_METADATA.get(site_id) or {}
        threshold = metadata.get("price_threshold_local")
        if threshold is None:
            return None
        kind = (
            "above_threshold"
            if float(price_local) >= float(threshold)
            else "below_threshold"
        )
        weight = float(billable_weight_g)
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT * FROM `{OFFICIAL_SHIPPING_RATE_CARD_TABLE}`
                    WHERE `site_id` = %s AND `reputation_code` = %s
                      AND `rate_kind` = %s
                      AND (`weight_min_g` IS NULL OR %s >= `weight_min_g`)
                      AND (`weight_max_g` IS NULL OR %s <= `weight_max_g`)
                    ORDER BY
                      CASE WHEN `weight_max_g` IS NULL THEN 1 ELSE 0 END,
                      `weight_max_g` ASC
                    LIMIT 1
                    """,
                    (site_id, OFFICIAL_REPUTATION_CODE, kind, weight, weight),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        finally:
            connection.close()

    def list_rates(self, site_id: str = "") -> dict[str, Any]:
        normalized_site = str(site_id or "").upper()
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                where = (
                    "WHERE `site_id` = %s AND `reputation_code` = %s"
                    if normalized_site
                    else "WHERE `reputation_code` = %s"
                )
                params = (
                    (normalized_site, OFFICIAL_REPUTATION_CODE)
                    if normalized_site
                    else (OFFICIAL_REPUTATION_CODE,)
                )
                cursor.execute(
                    f"""
                    SELECT * FROM `{OFFICIAL_SHIPPING_RATE_CARD_TABLE}`
                    {where}
                    ORDER BY FIELD(`site_id`, 'MLM','MLB','MLA','MLC','MCO','MLU'),
                             `weight_band_index`, `price_band_index`
                    """,
                    params,
                )
                rows = [dict(row) for row in (cursor.fetchall() or [])]
                cursor.execute(
                    f"""
                    SELECT `site_id`, COUNT(*) AS `row_count`,
                           COUNT(DISTINCT `weight_band_index`) AS `weight_band_count`,
                           MAX(`refreshed_at`) AS `refreshed_at`,
                           MAX(`exchange_rate_to_usd`) AS `exchange_rate_to_usd`
                    FROM `{OFFICIAL_SHIPPING_RATE_CARD_TABLE}`
                    WHERE `reputation_code` = %s
                    GROUP BY `site_id`
                    """,
                    (OFFICIAL_REPUTATION_CODE,),
                )
                summary_by_site = {
                    str(row.get("site_id") or ""): dict(row)
                    for row in (cursor.fetchall() or [])
                }
        finally:
            connection.close()
        summaries = []
        for key, metadata in SITE_METADATA.items():
            summary = summary_by_site.get(key) or {}
            exchange_rate = (
                float(summary["exchange_rate_to_usd"])
                if summary.get("exchange_rate_to_usd") is not None
                else None
            )
            official_available = bool(metadata.get("content_id"))
            summaries.append({
                "site_id": key,
                **metadata,
                "row_count": int(summary.get("row_count") or 0),
                "weight_band_count": int(summary.get("weight_band_count") or 0),
                "refreshed_at": str(summary.get("refreshed_at") or ""),
                "exchange_rate_to_usd": exchange_rate,
                "exchange_rate_from_usd": (
                    round(1.0 / exchange_rate, 8) if exchange_rate else None
                ),
                "official_available": official_available,
                "official_status": "available" if official_available else "not_published",
                "origin_label": "中国 / 香港",
                "carrier_label": "Cainiao",
            })
        return {"rows": rows, "sites": summaries, "site_id": normalized_site}

    def needs_refresh(self, *, max_age_hours: int = 24) -> bool:
        cutoff = datetime.now() - timedelta(hours=max(1, int(max_age_hours)))
        for site in self.list_rates().get("sites") or []:
            if not site.get("official_available"):
                continue
            try:
                refreshed_at = datetime.fromisoformat(str(site.get("refreshed_at") or ""))
            except ValueError:
                return True
            if refreshed_at < cutoff:
                return True
        return False


def _official_headers() -> dict[str, str]:
    return {
        "Accept-Language": "en-US",
        "Content-Type": "application/json",
        "User-Agent": "Zeshun-Mercado-Workbench/1.0",
        "x-meli-caller-id": "cx-knowledge-hub-lib",
    }


def _official_content_body() -> dict[str, Any]:
    return {
        "site_id": "CBT",
        "portal": "ML",
        "bu": "ML",
        "placeholders": {"user_id": "-1"},
    }


def _fetch_one_announcement(site_id: str, *, session: Any = None) -> dict[str, Any]:
    import requests

    metadata = SITE_METADATA[site_id]
    content_id = metadata.get("content_id")
    if not content_id:
        return {
            "site_id": site_id,
            "available": False,
            "title": "官方未公布跨境 Cainiao 运费标准",
            "content": "",
            "url": metadata["source_url"],
        }
    requester = session or requests
    response = requester.post(
        OFFICIAL_CONTENT_API.format(content_id=content_id),
        headers=_official_headers(),
        json=_official_content_body(),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping) or not str(data.get("content") or "").strip():
        raise ValueError(f"{metadata['country_name']} 官方公告内容为空")
    return {
        "site_id": site_id,
        "available": True,
        "content_id": int(content_id),
        "title": str(data.get("title") or ""),
        "content": str(data.get("content") or ""),
        "metadata": dict(data.get("metadata") or {}),
        "url": metadata["source_url"],
        "etag": str(response.headers.get("etag") or ""),
    }


def fetch_official_shipping_announcements(*, session: Any = None) -> dict[str, dict[str, Any]]:
    """Fetch the current official Global Selling announcements in parallel."""

    output: dict[str, dict[str, Any]] = {}
    available_sites = [key for key, value in SITE_METADATA.items() if value.get("content_id")]
    with ThreadPoolExecutor(max_workers=len(available_sites)) as executor:
        futures = {
            executor.submit(_fetch_one_announcement, site_id, session=session): site_id
            for site_id in available_sites
        }
        for future in as_completed(futures):
            site_id = futures[future]
            try:
                output[site_id] = future.result()
            except Exception as exc:
                output[site_id] = {
                    "site_id": site_id,
                    "available": True,
                    "title": "",
                    "content": "",
                    "url": SITE_METADATA[site_id]["source_url"],
                    "error": str(exc),
                }
    for site_id, metadata in SITE_METADATA.items():
        if not metadata.get("content_id"):
            output[site_id] = _fetch_one_announcement(site_id, session=session)
    return output


def refresh_official_shipping_rate_cards(
    client: Any = None,
    *,
    store: OfficialShippingRateCardStore | None = None,
    scraped_pages: Mapping[str, Mapping[str, Any]] | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Replace cached rows with current official cross-border standards."""

    if client is None:
        # Mercado Libre currently protects the currency-conversion endpoint
        # with OAuth even though the rate itself is public reference data.
        from erp.mercadolibre_profitability import (
            MercadoProfitabilityClient,
            active_store_token,
        )

        client = MercadoProfitabilityClient(active_store_token())
    rate_store = store or OfficialShippingRateCardStore()
    pages = dict(scraped_pages or fetch_official_shipping_announcements(session=session))
    sites = []
    errors = []
    unavailable_sites = []
    for site_id, metadata in SITE_METADATA.items():
        page = dict(pages.get(site_id) or {})
        if not metadata.get("content_id"):
            if hasattr(rate_store, "clear_site_rates"):
                rate_store.clear_site_rates(site_id)
            unavailable_sites.append({
                "site_id": site_id,
                "country_name": metadata["country_name"],
                "status": "not_published",
                "message": "Global Selling 官方未公布该站点跨境 Cainiao 运费标准",
            })
            continue
        try:
            if page.get("error"):
                raise RuntimeError(str(page["error"]))
            rows = parse_official_shipping_announcement(
                site_id, str(page.get("content") or "")
            )
            conversion = client.conversion_to_usd(metadata["currency_id"])
            count = rate_store.replace_site_rates(
                site_id,
                rows,
                exchange_rate_to_usd=float(conversion["ratio"]),
                source_payload={
                    "content_id": metadata["content_id"],
                    "title": page.get("title"),
                    "url": page.get("url") or metadata["source_url"],
                    "metadata": page.get("metadata") or {},
                    "etag": page.get("etag") or "",
                    "shipping_currency_id": "USD",
                    "origin": "China/Hong Kong",
                    "carrier": "Cainiao",
                    "conversion": conversion,
                },
            )
            sites.append({
                "site_id": site_id,
                "country_name": metadata["country_name"],
                "row_count": count,
                "weight_band_count": len(rows) // 2,
                "exchange_rate_to_usd": float(conversion["ratio"]),
                "content_id": metadata["content_id"],
            })
        except Exception as exc:
            errors.append({
                "site_id": site_id,
                "country_name": metadata["country_name"],
                "error": str(exc),
            })
    return {
        "sites": sites,
        "errors": errors,
        "unavailable_sites": unavailable_sites,
        "success_sites": len(sites),
        "failed_sites": len(errors),
        "unavailable_site_count": len(unavailable_sites),
        "refreshed_at": _now_text(),
    }


# Backward-compatible public name. It now fetches Global Selling announcements
# and no longer controls a browser.
scrape_official_shipping_tables = fetch_official_shipping_announcements


__all__ = [
    "OFFICIAL_CONTENT_API",
    "OFFICIAL_REPUTATION_CODE",
    "OFFICIAL_SHIPPING_RATE_CARD_TABLE",
    "OfficialShippingRateCardStore",
    "SITE_METADATA",
    "ensure_official_shipping_rate_card_table",
    "extract_official_tables",
    "fetch_official_shipping_announcements",
    "parse_official_shipping_announcement",
    "refresh_official_shipping_rate_cards",
    "scrape_official_shipping_tables",
]
