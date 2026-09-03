"""Official Mercado Libre fee, shipping and net-proceeds estimates.

The estimator is intended for source listings that do not belong to the
authorized seller yet.  It therefore uses the official pre-publication APIs:
category prediction, listing prices, currency conversion, and the seller's
shipping-options quote.
"""

from __future__ import annotations

import math
import json
import unicodedata
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Mapping

import requests

from erp.mercadolibre_profitability_cache import DatabaseProfitabilityCache
from erp.mercadolibre_shipping_rate_cards import OfficialShippingRateCardStore


API_BASE_URL = "https://api.mercadolibre.com"
DEFAULT_LISTING_TYPE_ID = "gold_special"
PROFITABILITY_SOURCE = "mercadolibre_official_api_daily_database_cache"
LIGHT_PACKAGE_LIMIT_G = 500.0
SUPPORTED_SITE_CURRENCIES = {
    "MLM": "MXN",
    "MLB": "BRL",
    "MLA": "ARS",
    "MLC": "CLP",
    "MCO": "COP",
    "MLU": "UYU",
}


class MercadoProfitabilityError(RuntimeError):
    """An official profitability estimate could not be produced."""


_cache_lock = threading.RLock()
_cache: dict[str, tuple[float, Any]] = {}


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "si", "sí"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def source_free_shipping(row: Mapping[str, Any]) -> bool | None:
    """Read the source listing's shipping mode from current and stored snapshots."""

    def loaded(value: Any) -> Mapping[str, Any]:
        if isinstance(value, Mapping):
            return value
        try:
            parsed = json.loads(str(value or "{}"))
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}

    candidates: list[Any] = [
        row.get("source_free_shipping"),
        row.get("free_shipping"),
    ]
    shipping_rule = str(row.get("shipping_weight_rule") or "").strip().lower()
    if shipping_rule.startswith("free_shipping:"):
        candidates.append(True)
    elif shipping_rule.startswith("buyer_pays_shipping:"):
        candidates.append(False)
    for raw_source in (
        row.get("source"),
        row.get("source_json"),
        row.get("source_snapshot_json"),
    ):
        source = loaded(raw_source)
        nested_source = loaded(source.get("source"))
        for value in (source, nested_source):
            shipping = loaded(value.get("shipping"))
            candidates.extend((
                value.get("source_free_shipping"),
                value.get("free_shipping"),
                shipping.get("free_shipping"),
            ))
    for candidate in candidates:
        normalized = _boolean(candidate)
        if normalized is not None:
            return normalized
    return None


def calculate_billable_weight_g(
    actual_weight_g: Any,
    volumetric_weight_kg: Any = None,
) -> float | None:
    """Use the Global Selling rule: greater of gross and volumetric weight."""

    actual = _positive(actual_weight_g)
    if actual is None:
        return None
    volumetric_kg = _positive(volumetric_weight_kg)
    volumetric_g = volumetric_kg * 1000 if volumetric_kg is not None else 0.0
    return round(max(actual, volumetric_g), 4)


def shipping_dimensions_parameter(row: Mapping[str, Any]) -> str:
    """Build the official quote parameter from declared package dimensions."""

    actual = _positive(row.get("weight_g"))
    if actual is None:
        raise MercadoProfitabilityError("缺少智赢实际重量，暂时无法计算运费")
    billable = calculate_billable_weight_g(actual, row.get("volumetric_weight_kg"))
    assert billable is not None

    height = _positive(row.get("package_height_cm")) or 1.0
    width = _positive(row.get("package_width_cm")) or 1.0
    length = _positive(row.get("package_length_cm")) or 1.0

    def text(value: float) -> str:
        # The shipping-options endpoint accepts integer centimetres/grams.
        # Always round upward so formatting is valid without underquoting.
        return str(max(1, math.ceil(value)))

    return f"{text(height)}x{text(width)}x{text(length)},{text(billable)}"


def calculate_net_proceeds_usd(
    sale_price_usd: Any,
    commission_amount_usd: Any,
    shipping_fee_usd: Any,
) -> float:
    values = tuple(_number(value) for value in (
        sale_price_usd,
        commission_amount_usd,
        shipping_fee_usd,
    ))
    if any(value is None for value in values):
        raise MercadoProfitabilityError("售价、佣金或运费不完整，无法计算净收益")
    return round(values[0] - values[1] - values[2], 2)  # type: ignore[operator]


def _cache_get(key: str) -> Any:
    with _cache_lock:
        expires_at, value = _cache.get(key, (0.0, None))
        if expires_at > time.time():
            return value
        _cache.pop(key, None)
    return None


def _cache_set(key: str, value: Any, ttl_seconds: int) -> Any:
    with _cache_lock:
        _cache[key] = (time.time() + max(1, int(ttl_seconds)), value)
    return value


def _site_id(row: Mapping[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), Mapping) else {}
    item_id = str(row.get("source_item_id") or source.get("id") or "").upper()
    site_id = str(source.get("site_id") or item_id[:3] or "").upper()
    if len(site_id) != 3 or not site_id.startswith("ML"):
        raise MercadoProfitabilityError("无法识别商品所属国家站点")
    return site_id


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def active_store_token() -> dict[str, Any]:
    """Return a usable server-side token, refreshing it before expiry."""

    from bit import bit_mysql
    from bit import mercado_tokens

    summaries = [
        row
        for row in ((bit_mysql.list_mercado_store_tokens() or {}).get("rows") or [])
        if bool(row.get("enabled", True))
    ]
    if not summaries:
        raise MercadoProfitabilityError("没有可用的 Mercado Libre 授权店铺")
    now = datetime.now()
    summaries.sort(
        key=lambda row: (
            _parse_datetime(row.get("expires_at")) or datetime.min,
            int(row.get("id") or 0),
        ),
        reverse=True,
    )
    summary = summaries[0]
    token_id = int(summary.get("id") or 0)
    expires_at = _parse_datetime(summary.get("expires_at"))
    if expires_at is not None and expires_at <= now + timedelta(minutes=10):
        try:
            mercado_tokens.refresh_and_save(
                token_id,
                get_token=bit_mysql.get_mercado_store_token,
                update_token=bit_mysql.update_mercado_store_token,
                record_error=bit_mysql.record_mercado_store_token_error,
            )
        except Exception as exc:
            if expires_at <= now:
                raise MercadoProfitabilityError(f"授权已过期且自动刷新失败：{exc}") from exc
    token = dict(bit_mysql.get_mercado_store_token(token_id) or {})
    if not token.get("access_token") or not token.get("meli_user_id"):
        raise MercadoProfitabilityError("授权店铺缺少 Access Token 或用户编号")
    return token


class MercadoProfitabilityClient:
    def __init__(
        self,
        token: Mapping[str, Any],
        *,
        http: requests.Session | None = None,
        timeout: int = 30,
        cache_store: Any = None,
        shipping_rate_store: Any = None,
    ) -> None:
        self.token = dict(token)
        self.http = http or requests.Session()
        self.timeout = timeout
        self.cache_store = (
            DatabaseProfitabilityCache()
            if cache_store is None
            else (None if cache_store is False else cache_store)
        )
        # Explicit/in-memory cache stores are primarily used by tests and
        # one-off callers. Only enable the database rate-card lookup by
        # default together with the normal production cache.
        self.shipping_rate_store = (
            OfficialShippingRateCardStore()
            if shipping_rate_store is None and cache_store is None
            else (None if shipping_rate_store in (None, False) else shipping_rate_store)
        )
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token.get('access_token') or ''}",
        }

    def _get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        response = self.http.get(
            f"{API_BASE_URL}{path}",
            headers=self.headers,
            params=dict(params or {}),
            timeout=self.timeout,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MercadoProfitabilityError(
                f"Mercado Libre 官网接口返回无法识别的数据（HTTP {response.status_code}）"
            ) from exc
        if not response.ok:
            message = payload.get("message") if isinstance(payload, Mapping) else payload
            raise MercadoProfitabilityError(
                f"Mercado Libre 官网接口失败（HTTP {response.status_code}）：{message}"
            )
        return payload

    def marketplace(self, site_id: str) -> dict[str, Any]:
        root_user_id = str(self.token.get("meli_user_id") or "")
        cache_key = f"marketplaces:{root_user_id}"
        payload = _cache_get(cache_key) if self.cache_store is not None else None
        if payload is None:
            payload = self._get(f"/marketplace/users/{root_user_id}")
            if self.cache_store is not None:
                _cache_set(cache_key, payload, 6 * 60 * 60)
        marketplaces = payload.get("marketplaces") if isinstance(payload, Mapping) else []
        for marketplace in marketplaces or []:
            if str(marketplace.get("site_id") or "").upper() == site_id:
                return dict(marketplace)
        raise MercadoProfitabilityError(f"授权店铺未开通 {site_id} 站点")

    def category(
        self,
        site_id: str,
        title: str,
        *,
        row: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        row = dict(row or {})
        candidates = [str(title or "").strip()]

        # Cross-border titles are frequently truncated before the actual
        # product noun.  Use the already-captured specifications/description as
        # a conservative second query when the official predictor returns no
        # category for the raw title.
        context = " ".join(
            str(row.get(key) or "")
            for key in (
                "title",
                "description_text",
                "description_json",
                "page_snapshot_json",
            )
        )
        context = "".join(
            character
            for character in unicodedata.normalize("NFKD", context).lower()
            if not unicodedata.combining(character)
        )
        if any(term in context for term in (
            "cantidad de disfraces", "cosplay", "disfraz", "costume",
        )):
            candidates.append("cosplay anime")
        elif any(term in context for term in (
            "action figure", "figura de accion", "model toy", "muneca",
        )):
            candidates.append("figura de accion anime")

        payload: Any = []
        for query in dict.fromkeys(candidate for candidate in candidates if candidate):
            payload = self._get(
                f"/sites/{site_id}/domain_discovery/search",
                params={"q": query},
            )
            if isinstance(payload, list) and payload:
                break
        if not isinstance(payload, list) or not payload:
            raise MercadoProfitabilityError("官网没有预测出对应商品分类")
        category = payload[0]
        category_id = str(category.get("category_id") or "")
        if not category_id:
            raise MercadoProfitabilityError("官网分类预测结果缺少分类编号")
        return {
            "category_id": category_id,
            "category_name": str(category.get("category_name") or ""),
        }

    def conversion_to_usd(self, currency_id: str) -> dict[str, Any]:
        currency_id = str(currency_id or "USD").upper()
        if currency_id == "USD":
            return {
                "ratio": 1.0,
                "creation_date": _now_text(),
                "valid_until": None,
            }
        cache_key = f"currency:{currency_id}:USD"
        payload = _cache_get(cache_key) if self.cache_store is not None else None
        if payload is None:
            persisted = (
                self.cache_store.get_exchange_rate(currency_id, "USD")
                if self.cache_store is not None
                else None
            )
            if persisted:
                payload = {
                    "ratio": persisted.get("rate"),
                    "creation_date": persisted.get("source_created_at")
                    or persisted.get("refreshed_at"),
                    "valid_until": persisted.get("source_valid_until"),
                    "cache_source": "database_daily_cache",
                }
            else:
                payload = self._get(
                    "/currency_conversions/search",
                    params={"from": currency_id, "to": "USD"},
                )
                if self.cache_store is not None:
                    self.cache_store.put_exchange_rate(currency_id, "USD", payload)
            if self.cache_store is not None:
                _cache_set(cache_key, payload, 60 * 60)
        ratio = _positive(payload.get("ratio") if isinstance(payload, Mapping) else None)
        if ratio is None:
            raise MercadoProfitabilityError(f"官网未返回 {currency_id} 到 USD 的汇率")
        return {
            "ratio": ratio,
            "creation_date": payload.get("creation_date"),
            "valid_until": payload.get("valid_until"),
        }

    def commission(
        self,
        site_id: str,
        category_id: str,
        price: float,
        listing_type_id: str,
        *,
        currency_id: str = "",
        marketplace: Mapping[str, Any] | None = None,
        row: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        marketplace = dict(marketplace or {})
        row = dict(row or {})
        logistic_type = str(marketplace.get("logistic_type") or "remote")
        shipping_mode = "me2"
        billable_weight_g = calculate_billable_weight_g(
            row.get("weight_g"), row.get("volumetric_weight_kg")
        )
        quote = {
            "site_id": site_id,
            "category_id": category_id,
            "listing_type_id": listing_type_id,
            "price": price,
            "currency_id": str(currency_id or "").upper(),
            "logistic_type": logistic_type,
            "shipping_mode": shipping_mode,
            "billable_weight_g": billable_weight_g,
        }
        cached = self.cache_store.get_commission(**quote) if self.cache_store else None
        if cached:
            return cached
        params: dict[str, Any] = {
            "price": price,
            "category_id": category_id,
            "listing_type_id": listing_type_id,
            "logistic_type": logistic_type,
            "shipping_modes": shipping_mode,
        }
        if currency_id:
            params["currency_id"] = str(currency_id).upper()
        if billable_weight_g is not None:
            params["billable_weight"] = billable_weight_g
        payload = self._get(
            f"/sites/{site_id}/listing_prices",
            params=params,
        )
        choices = (
            payload
            if isinstance(payload, list)
            else ([payload] if isinstance(payload, Mapping) else [])
        )
        selected = next(
            (
                choice for choice in choices
                if str(choice.get("listing_type_id") or "") == listing_type_id
            ),
            None,
        )
        if not selected:
            raise MercadoProfitabilityError(f"官网未返回刊登类型 {listing_type_id} 的分类佣金")
        details = selected.get("sale_fee_details") or {}
        amount = _number(selected.get("sale_fee_amount"))
        if amount is None:
            raise MercadoProfitabilityError("官网分类佣金缺少金额")
        value = {
            "amount": amount,
            "currency_id": str(selected.get("currency_id") or ""),
            "rate": _number(details.get("percentage_fee")),
            "fixed_fee": _number(details.get("fixed_fee")),
            "financing_add_on_fee": _number(details.get("financing_add_on_fee")),
            "listing_type_name": str(selected.get("listing_type_name") or ""),
            "payload": selected,
        }
        if self.cache_store is not None:
            self.cache_store.put_commission(quote, value)
        return value

    def shipping(
        self,
        marketplace: Mapping[str, Any],
        row: Mapping[str, Any],
        category_id: str,
        price: float,
        listing_type_id: str,
        *,
        free_shipping: bool,
    ) -> dict[str, Any]:
        child_user_id = str(marketplace.get("user_id") or "")
        if not child_user_id:
            raise MercadoProfitabilityError("授权店铺站点缺少子账号编号")
        dimensions = shipping_dimensions_parameter(row)
        logistic_type = str(marketplace.get("logistic_type") or "remote")
        shipping_mode = "me2"
        quote = {
            "site_id": str(marketplace.get("site_id") or _site_id(row)).upper(),
            "marketplace_user_id": child_user_id,
            "category_id": category_id,
            "listing_type_id": listing_type_id,
            "price": price,
            "dimensions": dimensions,
            "logistic_type": logistic_type,
            "shipping_mode": shipping_mode,
            "free_shipping": bool(free_shipping),
        }
        billable_weight_g = calculate_billable_weight_g(
            row.get("weight_g"), row.get("volumetric_weight_kg")
        )
        if self.shipping_rate_store is not None and billable_weight_g is not None:
            try:
                matched = self.shipping_rate_store.match(
                    site_id=quote["site_id"],
                    price_local=price,
                    billable_weight_g=billable_weight_g,
                    free_shipping=bool(free_shipping),
                )
            except Exception:
                matched = None
            if matched:
                return {
                    # Global Selling publishes the Cainiao charge directly in
                    # USD. Never convert a domestic reputation rate into USD
                    # and present that derived value as an official standard.
                    "amount": float(matched["shipping_amount_usd"]),
                    "currency_id": "USD",
                    "api_billable_weight_g": billable_weight_g,
                    "rate_source": "official_global_selling_cainiao_rate_card",
                    "rate_kind": str(matched.get("rate_kind") or ""),
                    "rate_price_label": str(matched.get("price_label") or ""),
                    "rate_weight_label": str(matched.get("weight_label") or ""),
                    "refreshed_at": matched.get("refreshed_at"),
                }
        cached = self.cache_store.get_shipping(**quote) if self.cache_store else None
        if cached:
            return cached
        payload = self._get(
            f"/users/{child_user_id}/shipping_options/free",
            params={
                "dimensions": dimensions,
                "verbose": "true",
                "item_price": price,
                "listing_type_id": listing_type_id,
                "mode": shipping_mode,
                "condition": "new",
                "logistic_type": logistic_type,
                "free_shipping": "true" if free_shipping else "false",
                "category_id": category_id,
            },
        )
        coverage = payload.get("coverage") if isinstance(payload, Mapping) else {}
        country = coverage.get("all_country") if isinstance(coverage, Mapping) else {}
        amount = _number(country.get("list_cost") if isinstance(country, Mapping) else None)
        if amount is None:
            raise MercadoProfitabilityError("官网未返回全国运费")
        value = {
            "amount": amount,
            "currency_id": str(country.get("currency_id") or ""),
            "api_billable_weight_g": _number(country.get("billable_weight")),
            "rate_source": "official_shipping_options_api",
            "payload": payload,
        }
        if self.cache_store is not None:
            self.cache_store.put_shipping(quote, value)
        return value

    def source_listing_free_shipping(self, row: Mapping[str, Any]) -> bool:
        explicit = source_free_shipping(row)
        if explicit is not None:
            return explicit
        item_id = str(row.get("source_item_id") or "").strip().upper()
        if not item_id:
            return True
        cache_key = f"source-free-shipping:{item_id}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return bool(cached)
        try:
            payload = self._get(f"/items/{item_id}", params={"attributes": "shipping"})
            shipping = payload.get("shipping") if isinstance(payload, Mapping) else {}
            detected = _boolean(
                shipping.get("free_shipping") if isinstance(shipping, Mapping) else None
            )
        except Exception:
            detected = None
        # Unknown listings stay conservative: charge the free-shipping seller cost.
        value = True if detected is None else detected
        return bool(_cache_set(cache_key, value, 24 * 60 * 60))

    def pricing(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Convert the product price even when fee or shipping quotes later fail."""

        price = _positive(row.get("price"))
        if price is None:
            raise MercadoProfitabilityError("商品缺少有效售价")
        currency_id = str(row.get("currency_id") or "USD").upper()
        conversion = self.conversion_to_usd(currency_id)
        exchange_rate = float(conversion["ratio"])
        return {
            "sale_price_usd": round(price * exchange_rate, 2),
            "exchange_rate_to_usd": exchange_rate,
            "exchange_rate_updated_at": conversion.get("creation_date"),
        }

    def estimate(self, row: Mapping[str, Any]) -> dict[str, Any]:
        price = _positive(row.get("price"))
        if price is None:
            raise MercadoProfitabilityError("商品缺少有效售价")
        title = str(row.get("title") or "").strip()
        if not title:
            raise MercadoProfitabilityError("商品缺少标题，无法预测分类")
        site_id = _site_id(row)
        currency_id = str(row.get("currency_id") or "USD").upper()
        pricing = self.pricing(row)
        # The workbench always publishes with the Classic plan.  Source listing
        # types (for example Premium/gold_pro) must not affect our commission.
        listing_type_id = DEFAULT_LISTING_TYPE_ID
        marketplace = self.marketplace(site_id)
        category_id = str(row.get("category_id") or "").strip()
        category = (
            {
                "category_id": category_id,
                "category_name": str(row.get("category_name") or ""),
            }
            if category_id
            else self.category(site_id, title, row=row)
        )
        commission = self.commission(
            site_id,
            category["category_id"],
            price,
            listing_type_id,
            currency_id=currency_id,
            marketplace=marketplace,
            row=row,
        )
        free_shipping = self.source_listing_free_shipping(row)
        shipping = self.shipping(
            marketplace,
            row,
            category["category_id"],
            price,
            listing_type_id,
            free_shipping=free_shipping,
        )
        exchange_rate = float(pricing["exchange_rate_to_usd"])
        commission_currency = commission["currency_id"] or currency_id
        shipping_currency = shipping["currency_id"] or currency_id
        commission_rate = exchange_rate
        if commission_currency != currency_id:
            commission_rate = float(self.conversion_to_usd(commission_currency)["ratio"])
        shipping_rate = exchange_rate
        if shipping_currency != currency_id:
            shipping_rate = float(self.conversion_to_usd(shipping_currency)["ratio"])
        sale_price_usd = float(pricing["sale_price_usd"])
        commission_amount_usd = round(float(commission["amount"]) * commission_rate, 2)
        shipping_fee_usd = round(float(shipping["amount"]) * shipping_rate, 2)
        billable_weight = calculate_billable_weight_g(
            row.get("weight_g"), row.get("volumetric_weight_kg")
        )
        return {
            **pricing,
            "category_id": category["category_id"],
            "category_name": category["category_name"],
            "listing_type_id": listing_type_id,
            "listing_type_name": commission.get("listing_type_name") or "",
            "commission_rate": commission.get("rate"),
            "commission_amount_local": commission["amount"],
            "commission_currency_id": commission_currency,
            "commission_amount_usd": commission_amount_usd,
            "shipping_fee_local": shipping["amount"],
            "shipping_currency_id": shipping_currency,
            "shipping_fee_usd": shipping_fee_usd,
            "billable_weight_g": billable_weight,
            "shipping_api_billable_weight_g": shipping.get("api_billable_weight_g"),
            "shipping_weight_rule": (
                f"{'free_shipping' if free_shipping else 'buyer_pays_shipping'}:"
                "global_selling_max_gross_or_volumetric:"
                f"{shipping.get('rate_source') or 'official_shipping_options_api'}"
            ),
            "source_free_shipping": free_shipping,
            "net_proceeds_usd": calculate_net_proceeds_usd(
                sale_price_usd, commission_amount_usd, shipping_fee_usd
            ),
            "profitability_updated_at": _now_text(),
            "profitability_source": (
                "mercadolibre_global_selling_cainiao_rate_card_daily_database_cache"
                if shipping.get("rate_source")
                == "official_global_selling_cainiao_rate_card"
                else PROFITABILITY_SOURCE
            ),
            "profitability_error": "",
        }


def enrich_profitability(
    row: Mapping[str, Any],
    *,
    token: Mapping[str, Any] | None = None,
    client: MercadoProfitabilityClient | None = None,
) -> dict[str, Any]:
    result = dict(row)
    calculator = client or MercadoProfitabilityClient(token or active_store_token())
    try:
        result.update(calculator.estimate(result))
    except Exception as exc:
        try:
            result.update(calculator.pricing(result))
        except Exception:
            pass
        result.update(
            profitability_updated_at=_now_text(),
            profitability_source=PROFITABILITY_SOURCE,
            profitability_error=str(exc)[:2000],
        )
    return result


def refresh_supported_exchange_rates(
    client: MercadoProfitabilityClient,
) -> dict[str, dict[str, Any]]:
    """Ensure every selectable marketplace currency has a fresh daily row."""
    return {
        site_id: client.conversion_to_usd(currency_id)
        for site_id, currency_id in SUPPORTED_SITE_CURRENCIES.items()
    }


__all__ = [
    "DEFAULT_LISTING_TYPE_ID",
    "LIGHT_PACKAGE_LIMIT_G",
    "MercadoProfitabilityClient",
    "MercadoProfitabilityError",
    "SUPPORTED_SITE_CURRENCIES",
    "active_store_token",
    "calculate_billable_weight_g",
    "calculate_net_proceeds_usd",
    "enrich_profitability",
    "refresh_supported_exchange_rates",
    "shipping_dimensions_parameter",
    "source_free_shipping",
]
