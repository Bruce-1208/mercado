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
from typing import Any, Callable, Mapping

import requests

from erp.mercadolibre_profitability_cache import DatabaseProfitabilityCache
from erp.mercadolibre_shipping_rate_cards import (
    SITE_METADATA,
    OfficialShippingRateCardStore,
)


API_BASE_URL = "https://api.mercadolibre.com"
DEFAULT_LISTING_TYPE_ID = "gold_special"
FIXED_COLLECTION_COMMISSION_RATE = 0.15
FIXED_COLLECTION_PROFITABILITY_SOURCE = "fixed_commission_15_pct"
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

    def __init__(self, message: str, *, snapshot: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.snapshot = dict(snapshot or {})


_cache_lock = threading.RLock()
_cache: dict[str, tuple[float, Any]] = {}


class _InFlightCall:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


_singleflight_lock = threading.Lock()
_singleflight_calls: dict[tuple[Any, ...], _InFlightCall] = {}


def _run_singleflight(key: tuple[Any, ...], callback: Callable[[], Any]) -> Any:
    """Run one concurrent call per key and forget it immediately afterwards."""

    with _singleflight_lock:
        flight = _singleflight_calls.get(key)
        leader = flight is None
        if flight is None:
            flight = _InFlightCall()
            _singleflight_calls[key] = flight

    if not leader:
        flight.event.wait()
        if flight.error is not None:
            raise flight.error
        return flight.result

    try:
        result = callback()
    except BaseException as exc:
        with _singleflight_lock:
            flight.error = exc
            _singleflight_calls.pop(key, None)
            flight.event.set()
        raise
    else:
        with _singleflight_lock:
            flight.result = result
            _singleflight_calls.pop(key, None)
            flight.event.set()
        return result


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
    """Use only ZYing's actual product weight for the shipping quote.

    ``volumetric_weight_kg`` remains in the signature for callers and stored
    snapshots that still carry dimensions, but it must not affect freight.
    """

    actual = _positive(actual_weight_g)
    if actual is None:
        return None
    return round(actual, 4)


def actual_weight_from_row(row: Mapping[str, Any]) -> float | None:
    """Return a genuine actual weight, never a legacy volumetric substitute."""

    basis = str(row.get("weight_basis") or "").strip().lower()
    if basis in {
        "calculated_volumetric",
        "legacy_unknown",
        "plugin_volumetric_fallback",
    }:
        return None
    return _positive(row.get("weight_g"))


def shipping_dimensions_parameter(row: Mapping[str, Any]) -> str:
    """Build an actual-weight-only quote parameter for the API fallback."""

    actual = actual_weight_from_row(row)
    if actual is None:
        raise MercadoProfitabilityError("缺少智赢实际重量，暂时无法计算运费")
    billable = calculate_billable_weight_g(actual, row.get("volumetric_weight_kg"))
    assert billable is not None

    def text(value: float) -> str:
        # The shipping-options endpoint accepts integer centimetres/grams.
        # Always round upward so formatting is valid without underquoting.
        return str(max(1, math.ceil(value)))

    # The API fallback can derive volumetric weight from the dimensions string.
    # Send neutral 1 cm dimensions so this workflow remains actual-weight-only.
    return f"1x1x1,{text(billable)}"


def estimate_rate_card_shipping(
    row: Mapping[str, Any],
    *,
    rate_rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None = None,
    shipping_rate_store: Any = None,
) -> dict[str, Any]:
    """Match the fixed Cainiao table immediately, without an API request.

    ``rate_rows`` lets a collection task load the small fixed table once and
    perform every per-item lookup in memory.  Supplying ``shipping_rate_store``
    is useful to callers that prefer one database lookup.
    """

    price = _positive(row.get("price"))
    actual_weight_g = actual_weight_from_row(row)
    if price is None or actual_weight_g is None:
        return {}
    try:
        site_id = _site_id(row)
    except MercadoProfitabilityError:
        return {}
    local_currency = SUPPORTED_SITE_CURRENCIES.get(site_id)
    currency_id = str(row.get("currency_id") or "").strip().upper()
    # The fixed table's price boundary is in the marketplace's local currency.
    # International listings may display USD; every stored rate row carries
    # the daily local→USD ratio, so convert in memory without an API request.
    price_local = price
    if not local_currency:
        return {}
    if currency_id == "USD" and local_currency != "USD":
        stored_exchange_rate = next(
            (
                _positive(candidate.get("exchange_rate_to_usd"))
                for candidate in (rate_rows or ())
                if str(candidate.get("site_id") or "").upper() == site_id
                and _positive(candidate.get("exchange_rate_to_usd")) is not None
            ),
            None,
        )
        if stored_exchange_rate is None:
            return {}
        price_local = price / stored_exchange_rate
    elif currency_id != local_currency:
        return {}

    free_shipping = source_free_shipping(row)
    if free_shipping is None:
        free_shipping = True
    matched: Mapping[str, Any] | None = None
    if rate_rows is not None:
        metadata = SITE_METADATA.get(site_id) or {}
        threshold = _positive(metadata.get("price_threshold_local"))
        if threshold is None:
            return {}
        rate_kind = (
            "above_threshold" if price_local >= threshold else "below_threshold"
        )
        matches = []
        for candidate in rate_rows:
            if str(candidate.get("site_id") or "").upper() != site_id:
                continue
            if str(candidate.get("rate_kind") or "") != rate_kind:
                continue
            minimum = _number(candidate.get("weight_min_g"))
            maximum = _number(candidate.get("weight_max_g"))
            if minimum is not None and actual_weight_g < minimum:
                continue
            if maximum is not None and actual_weight_g > maximum:
                continue
            matches.append(candidate)
        if matches:
            matched = min(
                matches,
                key=lambda candidate: (
                    _number(candidate.get("weight_max_g")) is None,
                    _number(candidate.get("weight_max_g")) or float("inf"),
                ),
            )
    else:
        store = shipping_rate_store or OfficialShippingRateCardStore()
        matched = store.match(
            site_id=site_id,
            price_local=price_local,
            billable_weight_g=actual_weight_g,
            free_shipping=free_shipping,
        )
    amount_usd = _positive((matched or {}).get("shipping_amount_usd"))
    if amount_usd is None:
        return {}
    return {
        "shipping_fee_local": round(amount_usd, 4),
        "shipping_currency_id": "USD",
        "shipping_fee_usd": round(amount_usd, 2),
        "billable_weight_g": actual_weight_g,
        "shipping_api_billable_weight_g": actual_weight_g,
        "shipping_weight_rule": (
            f"{'free_shipping' if free_shipping else 'buyer_pays_shipping'}:"
            "actual_weight_only:official_global_selling_cainiao_rate_card"
        ),
        "source_free_shipping": free_shipping,
        "profitability_source": (
            "mercadolibre_global_selling_cainiao_rate_card_immediate_database_cache"
        ),
    }


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

        queries = tuple(dict.fromkeys(candidate for candidate in candidates if candidate))

        def predict() -> dict[str, str]:
            payload: Any = []
            for query in queries:
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

        return _run_singleflight(("category", site_id, queries), predict)

    def conversion_to_usd(
        self,
        currency_id: str,
        *,
        refresh_if_stale: bool = False,
    ) -> dict[str, Any]:
        """Read the fixed database rate, refreshing it only in daily maintenance."""

        currency_id = str(currency_id or "USD").upper()
        if currency_id == "USD":
            return {
                "ratio": 1.0,
                "creation_date": _now_text(),
                "valid_until": None,
            }
        cache_key = f"currency:{currency_id}:USD"
        # A daily refresh must inspect the database deadline instead of being
        # hidden by the process cache. Normal product calculations use the
        # process cache and then the latest database value, even if its daily
        # refresh is temporarily late.
        payload = (
            _cache_get(cache_key)
            if self.cache_store is not None and not refresh_if_stale
            else None
        )
        if payload is None:
            def resolve_rate() -> Any:
                persisted = None
                if self.cache_store is not None:
                    try:
                        persisted = self.cache_store.get_exchange_rate(
                            currency_id,
                            "USD",
                            fresh_only=refresh_if_stale,
                        )
                    except TypeError:
                        # Keep lightweight/custom cache implementations compatible.
                        persisted = self.cache_store.get_exchange_rate(
                            currency_id, "USD"
                        )
                if persisted:
                    resolved = {
                        "ratio": persisted.get("rate"),
                        "creation_date": persisted.get("source_created_at")
                        or persisted.get("refreshed_at"),
                        "valid_until": persisted.get("source_valid_until"),
                        "cache_source": "database_daily_cache",
                    }
                else:
                    resolved = self._get(
                        "/currency_conversions/search",
                        params={"from": currency_id, "to": "USD"},
                    )
                    if self.cache_store is not None:
                        self.cache_store.put_exchange_rate(
                            currency_id, "USD", resolved
                        )
                if self.cache_store is not None:
                    _cache_set(cache_key, resolved, 24 * 60 * 60)
                return resolved

            payload = _run_singleflight(
                (
                    "exchange-rate-daily-refresh"
                    if refresh_if_stale
                    else "exchange-rate-database-read",
                    currency_id,
                    "USD",
                ),
                resolve_rate,
            )
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
            actual_weight_from_row(row), row.get("volumetric_weight_kg")
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
        def fetch_quote() -> dict[str, Any]:
            # The flight leader performs the cache lookup as well as the API
            # fallback, so concurrent callers share both the miss and result.
            cached = (
                self.cache_store.get_commission(**quote)
                if self.cache_store is not None
                else None
            )
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
                raise MercadoProfitabilityError(
                    f"官网未返回刊登类型 {listing_type_id} 的分类佣金"
                )
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

        flight_key = (
            "commission",
            tuple((key, quote[key]) for key in sorted(quote)),
        )
        return _run_singleflight(flight_key, fetch_quote)

    def shipping_from_rate_card(
        self, row: Mapping[str, Any], price: float, *, free_shipping: bool,
    ) -> dict[str, Any] | None:
        """Read the official database table without requiring a category or store."""

        if self.shipping_rate_store is None:
            return None
        billable_weight_g = calculate_billable_weight_g(
            actual_weight_from_row(row), row.get("volumetric_weight_kg")
        )
        if billable_weight_g is None:
            raise MercadoProfitabilityError("缺少智赢实际重量，暂时无法计算运费")
        site_id = _site_id(row)
        local_currency = SUPPORTED_SITE_CURRENCIES.get(site_id)
        currency_id = str(row.get("currency_id") or "USD").upper()
        price_local = price
        if local_currency and local_currency != currency_id:
            price_local = (
                price * float(self.conversion_to_usd(currency_id)["ratio"])
                / float(self.conversion_to_usd(local_currency)["ratio"])
            )
        matched = self.shipping_rate_store.match(
            site_id=site_id, price_local=price_local,
            billable_weight_g=billable_weight_g, free_shipping=free_shipping,
        )
        if not matched:
            return None
        return {
            "amount": float(matched["shipping_amount_usd"]),
            "currency_id": "USD",
            "api_billable_weight_g": billable_weight_g,
            "rate_source": "official_global_selling_cainiao_rate_card",
            "rate_kind": str(matched.get("rate_kind") or ""),
            "rate_price_label": str(matched.get("price_label") or ""),
            "rate_weight_label": str(matched.get("weight_label") or ""),
            "refreshed_at": matched.get("refreshed_at"),
            "free_shipping": free_shipping,
        }

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
        matched = self.shipping_from_rate_card(row, price, free_shipping=free_shipping)
        if matched:
            return matched
        if self.shipping_rate_store is not None:
            # Production quotes are sourced from the maintained Global Selling
            # table. Falling back to the dimensions-based endpoint would make
            # Mercado recalculate volumetric weight and violate this workflow's
            # actual-weight-only rule.
            raise MercadoProfitabilityError(
                "固定运费表未命中该站点、售价或实际重量区间"
            )
        dimensions = shipping_dimensions_parameter(row)
        # Only the API fallback needs an authorized marketplace and category.
        if not category_id:
            raise MercadoProfitabilityError("缺少商品分类，数据库运费表未命中，无法查询接口运费")
        marketplace = marketplace or self.marketplace(_site_id(row))
        child_user_id = str(marketplace.get("user_id") or "")
        if not child_user_id:
            raise MercadoProfitabilityError("授权店铺站点缺少子账号编号")
        if source_free_shipping(row) is None:
            free_shipping = self.source_listing_free_shipping(row)
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
        cached = self.cache_store.get_shipping(**quote) if self.cache_store else None
        if cached:
            return {**cached, "free_shipping": free_shipping}
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
            # The endpoint may echo a volumetric billable weight.  This
            # workflow deliberately records the actual weight used in our
            # quote instead, while retaining the raw response in ``payload``.
            "api_billable_weight_g": actual_weight_from_row(row),
            "rate_source": "official_shipping_options_api",
            "free_shipping": free_shipping,
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
        site_id = _site_id(row)
        currency_id = str(row.get("currency_id") or "USD").upper()
        listing_type_id = DEFAULT_LISTING_TYPE_ID
        use_fixed_collection_commission = str(
            row.get("profitability_source") or ""
        ).strip().lower().startswith(FIXED_COLLECTION_PROFITABILITY_SOURCE)
        snapshot: dict[str, Any] = {
            "listing_type_id": listing_type_id,
            "listing_type_name": "Classic",
            "billable_weight_g": calculate_billable_weight_g(
                actual_weight_from_row(row), row.get("volumetric_weight_kg")
            ),
            "net_proceeds_usd": None,
            "profitability_updated_at": _now_text(),
            "profitability_source": PROFITABILITY_SOURCE,
        }
        errors = []
        try:
            snapshot.update(self.pricing(row))
        except Exception as exc:
            errors.append(f"售价换算：{exc}")

        category_id = str(row.get("category_id") or "").strip()
        if not use_fixed_collection_commission:
            if category_id:
                snapshot.update(category_id=category_id, category_name=row.get("category_name") or "")
            else:
                try:
                    title = str(row.get("title") or "").strip()
                    if not title:
                        raise MercadoProfitabilityError("商品缺少标题，无法预测分类")
                    snapshot.update(self.category(site_id, title, row=row))
                    category_id = snapshot["category_id"]
                except Exception as exc:
                    errors.append(f"分类：{exc}")

        if use_fixed_collection_commission:
            snapshot.update(
                category_id=category_id or None,
                category_name=row.get("category_name") or None,
            )
            try:
                conversion = self.conversion_to_usd(currency_id)
                exchange_rate = float(conversion["ratio"])
                commission_local = round(price * FIXED_COLLECTION_COMMISSION_RATE, 2)
                sale_price_usd = snapshot.get("sale_price_usd")
                commission_usd = (
                    round(
                        float(sale_price_usd) * FIXED_COLLECTION_COMMISSION_RATE + 1e-9,
                        2,
                    )
                    if sale_price_usd is not None
                    else round(commission_local * exchange_rate, 2)
                )
                snapshot.update(
                    listing_type_name="固定佣金",
                    commission_rate=FIXED_COLLECTION_COMMISSION_RATE * 100,
                    commission_amount_local=commission_local,
                    commission_currency_id=currency_id,
                    commission_amount_usd=commission_usd,
                )
            except Exception as exc:
                errors.append(f"固定佣金：{exc}")
        elif category_id:
            try:
                # The Classic remote quote is shared reference data, so a cached
                # commission does not need a live marketplace/account lookup.
                commission = self.commission(
                    site_id, category_id, price, listing_type_id,
                    currency_id=currency_id, row=row,
                )
                commission_currency = commission["currency_id"] or currency_id
                snapshot.update(
                    listing_type_name=commission.get("listing_type_name") or "Classic",
                    commission_rate=commission.get("rate"),
                    commission_amount_local=commission["amount"],
                    commission_currency_id=commission_currency,
                    commission_amount_usd=None,
                )
                rate = float(self.conversion_to_usd(commission_currency)["ratio"])
                snapshot["commission_amount_usd"] = round(float(commission["amount"]) * rate, 2)
            except Exception as exc:
                errors.append(f"佣金：{exc}")

        # Shipping from the official table only depends on site, local price
        # band and weight. A failed category/commission must not discard it.
        try:
            free_shipping = source_free_shipping(row)
            shipping = self.shipping(
                {}, row, category_id, price, listing_type_id,
                free_shipping=True if free_shipping is None else free_shipping,
            )
            free_shipping = shipping.get("free_shipping", free_shipping)
            shipping_currency = shipping["currency_id"] or currency_id
            snapshot.update(
                shipping_fee_local=shipping["amount"],
                shipping_currency_id=shipping_currency,
                shipping_fee_usd=None,
                shipping_api_billable_weight_g=shipping.get("api_billable_weight_g"),
                shipping_weight_rule=(
                    f"{'free_shipping' if free_shipping else 'buyer_pays_shipping'}:"
                    "actual_weight_only:"
                    f"{shipping.get('rate_source') or 'official_shipping_options_api'}"
                ),
                source_free_shipping=free_shipping,
            )
            rate = float(self.conversion_to_usd(shipping_currency)["ratio"])
            snapshot["shipping_fee_usd"] = round(float(shipping["amount"]) * rate, 2)
            if shipping.get("rate_source") == "official_global_selling_cainiao_rate_card":
                snapshot["profitability_source"] = (
                    "mercadolibre_global_selling_cainiao_rate_card_daily_database_cache"
                )
        except Exception as exc:
            errors.append(f"运费：{exc}")

        if not errors:
            snapshot["net_proceeds_usd"] = calculate_net_proceeds_usd(
                snapshot.get("sale_price_usd"), snapshot.get("commission_amount_usd"),
                snapshot.get("shipping_fee_usd"),
            )
        if use_fixed_collection_commission:
            snapshot["profitability_source"] = FIXED_COLLECTION_PROFITABILITY_SOURCE
        snapshot["profitability_error"] = "；".join(errors)[:2000]
        if errors:
            raise MercadoProfitabilityError(snapshot["profitability_error"], snapshot=snapshot)
        return snapshot


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
        if isinstance(exc, MercadoProfitabilityError) and exc.snapshot:
            result.update(exc.snapshot)
            return result
        try:
            result.update(calculator.pricing(result))
        except Exception:
            pass
        result.update(
            net_proceeds_usd=None,
            profitability_updated_at=_now_text(),
            profitability_source=PROFITABILITY_SOURCE,
            profitability_error=str(exc)[:2000],
        )
    return result


def refresh_supported_exchange_rates(
    client: MercadoProfitabilityClient,
) -> dict[str, dict[str, Any]]:
    """Refresh stale daily rows; fresh database values cause no API request."""
    return {
        site_id: client.conversion_to_usd(currency_id, refresh_if_stale=True)
        for site_id, currency_id in SUPPORTED_SITE_CURRENCIES.items()
    }


__all__ = [
    "DEFAULT_LISTING_TYPE_ID",
    "LIGHT_PACKAGE_LIMIT_G",
    "MercadoProfitabilityClient",
    "MercadoProfitabilityError",
    "SUPPORTED_SITE_CURRENCIES",
    "active_store_token",
    "actual_weight_from_row",
    "calculate_billable_weight_g",
    "calculate_net_proceeds_usd",
    "enrich_profitability",
    "estimate_rate_card_shipping",
    "refresh_supported_exchange_rates",
    "shipping_dimensions_parameter",
    "source_free_shipping",
]
