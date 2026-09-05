"""Safe catalogue media for order rows, without guessing storefront URLs.

getOfferMappings returns offer.pictures (main image first), mediaFiles.pictures,
mapping.marketSku and showcaseUrls. Showcase links are product-level B2C/B2B
links, not campaign IDs; never turn an offerId into a guessed product URL.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


MARKET_STOREFRONT_HOSTS = frozenset({"market.yandex.ru", "www.market.yandex.ru"})


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def safe_image_url(value: Any) -> str | None:
    """Accept only explicit web URLs; no browser URL-parser ambiguities."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 8192 or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    if "\\" in value or "\x7f" in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        # Reading .port validates malformed/out-of-range ports as well.
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
    except ValueError:
        return None
    return value


def safe_product_url(value: Any) -> str | None:
    url = safe_image_url(value)
    if url is None:
        return None
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in MARKET_STOREFRONT_HOSTS
            or parsed.port not in {None, 443}):
        return None
    return url


def _positive_id(value: Any) -> str | None:
    # Keep platform IDs as strings so the browser cannot round large integers.
    if isinstance(value, bool) or value is None:
        return None
    value = str(value)
    return (str(int(value)) if len(value) <= 32 and value.isascii() and value.isdigit()
            and int(value) > 0 else None)


def _showcase_url(record: dict[str, Any], campaign_id: int | None) -> str | None:
    expected_campaign = _positive_id(campaign_id)
    campaign_rows = _list(record.get("campaigns"))
    known_campaigns = {
        identity for row in campaign_rows
        if (identity := _positive_id(_dict(row).get("campaignId"))) is not None
    }
    if expected_campaign and known_campaigns and expected_campaign not in known_campaigns:
        return None
    matching: list[str] = []
    general: list[str] = []
    for raw in _list(record.get("showcaseUrls")):
        row = _dict(raw)
        # B2B belongs to business.market.yandex.ru, not the customer storefront.
        if str(row.get("showcaseType") or "").upper() != "B2C":
            continue
        url = safe_product_url(row.get("showcaseUrl"))
        if url is None:
            continue
        # Current schema has no campaignId. Honour one if later supplied, but
        # never confuse it with a public seller ID or rewrite URL query values.
        if row.get("campaignId") is not None:
            if expected_campaign and _positive_id(row.get("campaignId")) == expected_campaign:
                matching.append(url)
        else:
            general.append(url)
    return next(iter(matching or general), None)


def build_order_item_media(
    item: dict[str, Any], catalogue: dict[str, Any] | None = None,
    *, campaign_id: int | None = None,
) -> dict[str, Any]:
    """Return a common media contract for order.items and finance.items."""
    catalogue = _dict(catalogue)
    candidates = [item.get("image_url"), item.get("pictureUrl"), item.get("imageUrl")]
    candidates.extend(_list(item.get("pictures")))
    candidates.extend(_list(catalogue.get("pictures")))
    candidates.extend(_list(_dict(catalogue.get("mediaFiles")).get("pictures")))
    pictures: list[str] = []
    for candidate in candidates:
        value = _dict(candidate).get("url") if isinstance(candidate, dict) else candidate
        url = safe_image_url(value)
        if url is not None and url not in pictures:
            pictures.append(url)
    # Existing explicit order URLs remain a fallback only. Catalogue links are
    # authoritative for this business SKU and keep all official URL parameters.
    product_url = _showcase_url(catalogue, campaign_id)
    source = "showcase" if product_url else None
    if product_url is None:
        product_url = safe_product_url(item.get("product_url"))
        previous_source = item.get("product_url_source")
        source = (previous_source if isinstance(previous_source, str)
                  and previous_source in {"showcase", "order"} else "order") if product_url else None
    market_sku = (
        _positive_id(_dict(catalogue.get("mapping")).get("marketSku"))
        or _positive_id(item.get("market_sku"))
        or _positive_id(item.get("marketSku"))
    )
    return {
        "image_url": pictures[0] if pictures else None,
        "pictures": pictures,
        "product_url": product_url,
        "product_url_source": source,
        "market_sku": market_sku,
    }
