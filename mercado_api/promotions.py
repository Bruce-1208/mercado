"""Mercado Libre Global Selling promotion API adapter.

The adapter keeps the official v2 headers in one place and deliberately treats
promotion mutations as submit-once operations.  A connection error after a
POST/PUT/DELETE is ambiguous, so callers must re-read the item state before a
human retries it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

import requests

from mercado_api.client import MercadoAPIError, MercadoLibreClient


WRITABLE_TYPES = frozenset({"DEAL", "MARKETPLACE_CAMPAIGN", "PRICE_DISCOUNT"})
READ_ONLY_TYPES = frozenset({"SELLER_CAMPAIGN", "DOD", "LIGHTNING", "CLEARANCE"})


def _required(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label}不能为空")
    return normalized


def _positive_price(value: Any) -> float:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("活动价必须是有效数字") from exc
    if number <= 0:
        raise ValueError("活动价必须大于 0")
    return float(number.quantize(Decimal("0.01")))


@dataclass(frozen=True)
class PromotionIdentity:
    """Verified API caller identity for endpoints that require it."""

    client_id: str = ""
    caller_id: str = ""


class MercadoPromotionsClient:
    def __init__(
        self,
        client: MercadoLibreClient,
        *,
        client_id: str = "",
        caller_id: str = "",
    ) -> None:
        self.client = client
        self.identity = PromotionIdentity(
            str(client_id or "").strip(), str(caller_id or "").strip()
        )

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {"version": "v2"}
        if self.identity.client_id:
            headers["X-Client-Id"] = self.identity.client_id
        if self.identity.caller_id:
            headers["X-Caller-Id"] = self.identity.caller_id
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def list_promotions_page(
        self, user_id: str, *, limit: int = 50, search_after: str = ""
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": max(1, min(50, int(limit or 50)))}
        if search_after:
            params["searchAfter"] = str(search_after)
        return dict(
            self.client.request(
                "GET",
                f"/marketplace/seller-promotions/users/{_required(user_id, '子账号 user_id')}",
                params=params,
                headers=self._headers(),
            )
            or {}
        )

    def iter_promotions(self, user_id: str) -> Iterator[dict[str, Any]]:
        cursor = ""
        seen_cursors: set[str] = set()
        while True:
            page = self.list_promotions_page(user_id, search_after=cursor)
            results = list(page.get("results") or [])
            for row in results:
                if isinstance(row, dict):
                    yield dict(row)
            paging = page.get("paging") or {}
            next_cursor = str(
                paging.get("searchAfter") or paging.get("search_after") or ""
            ).strip()
            if not results or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def get_promotion(
        self, user_id: str, promotion_id: str, promotion_type: str
    ) -> dict[str, Any]:
        return dict(
            self.client.request(
                "GET",
                f"/marketplace/seller-promotions/promotions/{_required(promotion_id, '活动编号')}",
                params={
                    "user_id": _required(user_id, "子账号 user_id"),
                    "promotion_type": _required(promotion_type, "活动类型").upper(),
                },
                headers=self._headers(),
            )
            or {}
        )

    def list_items_page(
        self,
        user_id: str,
        promotion_id: str,
        *,
        limit: int = 50,
        search_after: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "user_id": _required(user_id, "子账号 user_id"),
            "limit": max(1, min(50, int(limit or 50))),
        }
        if search_after:
            params["searchAfter"] = str(search_after)
        return dict(
            self.client.request(
                "GET",
                f"/marketplace/seller-promotions/promotions/{_required(promotion_id, '活动编号')}/items",
                params=params,
                headers=self._headers(),
            )
            or {}
        )

    def iter_items(self, user_id: str, promotion_id: str) -> Iterator[dict[str, Any]]:
        cursor = ""
        seen_cursors: set[str] = set()
        while True:
            page = self.list_items_page(user_id, promotion_id, search_after=cursor)
            results = list(page.get("results") or [])
            for row in results:
                if isinstance(row, dict):
                    yield dict(row)
            paging = page.get("paging") or {}
            next_cursor = str(
                paging.get("searchAfter") or paging.get("search_after") or ""
            ).strip()
            if not results or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def get_item_promotions(self, user_id: str, item_id: str) -> dict[str, Any]:
        return dict(
            self.client.request(
                "GET",
                f"/marketplace/seller-promotions/items/{_required(item_id, '商品编号')}",
                params={"user_id": _required(user_id, "子账号 user_id")},
                headers=self._headers(),
            )
            or {}
        )

    def enroll_item(
        self,
        user_id: str,
        item_id: str,
        *,
        promotion_id: str,
        promotion_type: str,
        deal_price: Any = None,
        start_date: str = "",
        finish_date: str = "",
    ) -> dict[str, Any]:
        promotion_type = _required(promotion_type, "活动类型").upper()
        if promotion_type not in WRITABLE_TYPES:
            raise ValueError(f"{promotion_type} 当前仅支持查看，不能报名")
        payload: dict[str, Any] = {"promotion_type": promotion_type}
        if promotion_id:
            payload["promotion_id"] = str(promotion_id)
        if promotion_type in {"DEAL", "PRICE_DISCOUNT"}:
            payload["deal_price"] = _positive_price(deal_price)
        if promotion_type == "PRICE_DISCOUNT":
            payload["start_date"] = _required(start_date, "开始日期")
            payload["finish_date"] = _required(finish_date, "结束日期")
        if promotion_type != "PRICE_DISCOUNT" and not promotion_id:
            raise ValueError("活动编号不能为空")
        return self._mutation_once(
            "POST",
            f"/marketplace/seller-promotions/items/{_required(item_id, '商品编号')}",
            params={"user_id": _required(user_id, "子账号 user_id")},
            payload=payload,
        )

    def withdraw_item(
        self,
        user_id: str,
        item_id: str,
        *,
        promotion_id: str,
        promotion_type: str,
        offer_id: str = "",
    ) -> dict[str, Any]:
        promotion_type = _required(promotion_type, "活动类型").upper()
        if promotion_type not in WRITABLE_TYPES:
            raise ValueError(f"{promotion_type} 当前仅支持查看，不能退出")
        params: dict[str, Any] = {
            "user_id": _required(user_id, "子账号 user_id"),
            "promotion_type": promotion_type,
        }
        if promotion_id:
            params["promotion_id"] = str(promotion_id)
        if offer_id:
            params["offer_id"] = str(offer_id)
        return self._mutation_once(
            "DELETE",
            f"/marketplace/seller-promotions/items/{_required(item_id, '商品编号')}",
            params=params,
        )

    def _mutation_once(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit once, allowing only one unambiguous 401 refresh retry."""
        refreshed = False
        while True:
            headers = self._headers(json_body=payload is not None)
            headers["Authorization"] = f"Bearer {self.client.access_token}"
            try:
                response = self.client.session.request(
                    method,
                    f"{self.client.BASE_URL}{path}",
                    params=params,
                    headers=headers,
                    json=payload,
                    timeout=self.client.timeout,
                )
            except requests.RequestException as exc:
                raise MercadoAPIError(
                    f"{method} {path} 连接中断，结果未知；请先同步官方状态，禁止直接重试"
                ) from exc
            if response.status_code == 401 and not refreshed:
                self.client._refresh_access_token()
                refreshed = True
                continue
            if not response.ok:
                raise MercadoAPIError(
                    f"{method} {path} 失败 ({response.status_code}): {response.text[:1000]}"
                )
            if response.status_code == 204 or not getattr(response, "content", b""):
                return {}
            data = response.json()
            return dict(data or {}) if isinstance(data, dict) else {"result": data}
