"""Mercado Libre Global Selling HTTP 客户端及 API 分页封装。"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

LOGGER = logging.getLogger(__name__)


class MercadoAPIError(RuntimeError):
    """API 请求、重试或 token 刷新失败时抛出的统一异常。"""


class TokenStore:
    """持久化 OAuth token。

    Mercado Libre 的 refresh token 使用一次后会被替换，因此每次刷新后必须
    立即保存服务端返回的新 token。
    """

    def __init__(self, path: str | Path):
        """创建 token 存储器，``path`` 指向 JSON 文件。"""
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        """读取已保存的 token；文件不存在时返回空字典。"""
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, token_data: dict[str, Any]) -> None:
        """原子写入 token，避免程序中断后留下半个 JSON 文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(token_data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


class MercadoLibreClient:
    """封装认证、重试、订单分页和 Listing 批量查询。"""

    BASE_URL = "https://api.mercadolibre.com"

    def __init__(self, access_token: str, *, refresh_token: str | None = None,
                 client_id: str | None = None, client_secret: str | None = None,
                 token_store: TokenStore | None = None, timeout: int = 30,
                 session: requests.Session | None = None):
        """初始化客户端。

        ``session`` 参数主要用于连接复用，也方便在测试中注入模拟会话。
        若 token 文件已有数据，以文件内最后一次刷新后的 token 为准。
        """
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_store = token_store
        self.timeout = timeout
        if session is None:
            self.session = requests.Session()
            # Scheduled/local-agent jobs must not depend on a desktop proxy
            # process that may be stopped or restarted independently.
            self.session.trust_env = False
        else:
            self.session = session
        if token_store:
            saved = token_store.load()
            self.access_token = saved.get("access_token", self.access_token)
            self.refresh_token = saved.get("refresh_token", self.refresh_token)

    def _refresh_access_token(self) -> None:
        """用 refresh token 换取新凭证，并立刻持久化返回结果。"""
        if not all((self.refresh_token, self.client_id, self.client_secret)):
            raise MercadoAPIError("access token 已失效，且未配置完整的 refresh token/client id/client secret")
        response = self.session.post(
            f"{self.BASE_URL}/oauth/token",
            data={"grant_type": "refresh_token", "client_id": self.client_id,
                  "client_secret": self.client_secret, "refresh_token": self.refresh_token},
            timeout=self.timeout,
        )
        if not response.ok:
            raise MercadoAPIError(f"刷新 token 失败 ({response.status_code}): {response.text[:500]}")
        data = response.json()
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        if self.token_store:
            self.token_store.save(data)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        max_attempts: int = 4,
    ) -> Any:
        """发送已认证请求，并处理 token 失效、限流及临时服务端错误。

        401 每次请求最多触发一次 token 刷新；网络中断、429 和 5xx 使用退避
        等待，避免瞬时故障导致整个定时同步任务直接退出。
        """
        url = path if path.startswith("http") else f"{self.BASE_URL}{path}"
        refreshed = False
        attempts = max(1, min(4, int(max_attempts or 1)))
        for attempt in range(attempts):
            try:
                request_headers = dict(headers or {})
                request_headers["Authorization"] = f"Bearer {self.access_token}"
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    headers=request_headers,
                    json=json_body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt < attempts - 1:
                    delay = min(2**attempt, 8)
                    LOGGER.warning("API 网络请求中断，%s 秒后重试：%s", delay, exc)
                    time.sleep(delay)
                    continue
                raise MercadoAPIError(f"{method} {path} 网络请求多次失败：{exc}") from exc
            if response.status_code == 401 and not refreshed:
                self._refresh_access_token()
                refreshed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < attempts - 1:
                    delay = min(float(response.headers.get("Retry-After", 2**attempt)), 30)
                    LOGGER.warning("API 暂时不可用 (%s)，%.1f 秒后重试", response.status_code, delay)
                    time.sleep(delay)
                    continue
            if not response.ok:
                raise MercadoAPIError(f"{method} {path} 失败 ({response.status_code}): {response.text[:1000]}")
            if response.status_code == 204 or getattr(response, "content", None) == b"":
                return {}
            return response.json()
        raise MercadoAPIError(f"{method} {path} 多次重试后仍失败")

    def upload_item_clip(self, item_id: str, stream, filename: str, sites: list[dict]) -> dict[str, Any]:
        """Submit once: an ambiguous timeout must not create duplicate clips."""
        if not sites:
            raise ValueError("必须明确指定视频发布站点")
        stream.seek(0)
        try:
            response = self.session.post(
                f"{self.BASE_URL}/marketplace/items/{item_id}/clips/upload",
                headers={"Authorization": f"Bearer {self.access_token}"},
                data={"sites": json.dumps(sites)},
                files={"file": (filename, stream, "application/octet-stream")},
                timeout=(30, 300),
            )
        except requests.RequestException as exc:
            raise MercadoAPIError("视频上传连接中断，结果未知，请先在美客多后台确认后再重试") from exc
        if not response.ok:
            raise MercadoAPIError(f"美客多视频上传失败 ({response.status_code}): {response.text[:1000]}")
        result = response.json()
        if not isinstance(result, dict) or result.get("status") != "accepted" or not result.get("clip_uuid"):
            raise MercadoAPIError("美客多返回了未确认的上传结果，请到后台核实")
        return result

    def update_global_item(self, item_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Update a Global Selling marketplace listing through /global/items."""

        item_id = str(item_id or "").strip().upper()
        if not item_id:
            raise ValueError("item_id 不能为空")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("changes 不能为空")
        result = self.request(
            "PUT",
            f"/global/items/{item_id}",
            json_body=changes,
        )
        return dict(result or {})

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        max_attempts: int = 4,
    ) -> bytes:
        """发送认证请求并返回二进制内容，供官方 PDF 等文件接口使用。"""
        url = path if path.startswith("http") else f"{self.BASE_URL}{path}"
        refreshed = False
        attempts = max(1, min(4, int(max_attempts or 1)))
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt < attempts - 1:
                    delay = min(2**attempt, 8)
                    LOGGER.warning("API 文件请求中断，%s 秒后重试：%s", delay, exc)
                    time.sleep(delay)
                    continue
                raise MercadoAPIError(f"{method} {path} 网络请求多次失败：{exc}") from exc
            if response.status_code == 401 and not refreshed and self.refresh_token:
                self._refresh_access_token()
                refreshed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < attempts - 1:
                    delay = min(float(response.headers.get("Retry-After", 2**attempt)), 30)
                    LOGGER.warning("API 文件暂时不可用 (%s)，%.1f 秒后重试", response.status_code, delay)
                    time.sleep(delay)
                    continue
            if not response.ok:
                raise MercadoAPIError(
                    f"{method} {path} 失败 ({response.status_code}): {response.text[:1000]}"
                )
            return bytes(response.content)
        raise MercadoAPIError(f"{method} {path} 多次重试后仍失败")

    @staticmethod
    def _order_ids(results: Iterable[dict[str, Any]]) -> Iterator[str]:
        """提取真实订单 ID；购物车结果的顶层 ID 可能是 pack ID，不能直接使用。"""
        seen: set[str] = set()
        for result in results:
            nested = result.get("orders") or []
            for order in nested if nested else [result]:
                order_id = order.get("id")
                if order_id is not None and str(order_id) not in seen:
                    seen.add(str(order_id))
                    yield str(order_id)

    def iter_order_ids(self, seller_id: str, **filters: Any) -> Iterator[str]:
        """按 offset 遍历指定 Seller 的全部订单 ID。

        ``filters`` 原样传递给订单搜索接口，可用于增量同步的
        ``last_updated.from`` 等官方过滤条件。
        """
        offset, limit = 0, 50
        while True:
            page = self.search_order_ids_page(
                seller_id,
                offset=offset,
                limit=limit,
                **filters,
            )
            yield from page["order_ids"]
            offset = int(page["next_offset"])
            if not page["result_count"] or offset >= int(page["total"]):
                break

    def search_order_ids_page(
        self,
        seller_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
        **filters: Any,
    ) -> dict[str, Any]:
        """Read one resumable order-search page and expose the next top-level offset."""
        offset = max(0, int(offset or 0))
        limit = max(1, min(50, int(limit or 50)))
        # Global Selling uses ``seller``. ``seller.id`` can return an empty page.
        params = {"seller": seller_id, "limit": limit, "offset": offset, **filters}
        page = self.request("GET", "/marketplace/orders/search", params=params)
        results = list(page.get("results") or [])
        result_count = len(results)
        next_offset = offset + result_count
        total = int((page.get("paging") or {}).get("total", next_offset))
        return {
            "order_ids": list(self._order_ids(results)),
            "result_count": result_count,
            "next_offset": next_offset,
            "total": total,
        }

    def get_order(self, order_id: str) -> dict[str, Any]:
        """读取单个订单的完整详情。"""
        return self.request("GET", f"/marketplace/orders/{order_id}")

    def get_shipment_costs(self, shipment_id: str) -> dict[str, Any]:
        """Read the official final sender/receiver costs for one shipment."""
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            raise ValueError("shipment_id 不能为空")
        return self.request(
            "GET",
            f"/marketplace/shipments/{shipment_id}/costs",
            headers={"x-format-new": "true"},
        )

    def get_shipment(self, shipment_id: str) -> dict[str, Any]:
        """Read the live shipment used to validate split eligibility."""
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            raise ValueError("shipment_id 不能为空")
        return self.request(
            "GET",
            f"/marketplace/shipments/{shipment_id}",
            headers={"x-format-new": "true"},
        )

    def get_shipment_items(self, shipment_id: str) -> list[dict[str, Any]]:
        """Return the authoritative order quantities attached to a shipment."""
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            raise ValueError("shipment_id 不能为空")
        result = self.request(
            "GET",
            f"/marketplace/shipments/{shipment_id}/items",
            headers={"x-format-new": "true"},
        )
        return list(result or [])

    def split_shipment(
        self,
        shipment_id: str,
        *,
        reason: str,
        packs: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create exactly two additional packages through the official API."""
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            raise ValueError("shipment_id 不能为空")
        reason = str(reason or "").strip().upper()
        normalized_packs = [dict(pack or {}) for pack in packs or ()]
        if len(normalized_packs) != 2:
            raise ValueError("拆分发货必须且只能生成 2 个包裹")
        result = self.request(
            "POST",
            f"/marketplace/shipments/{shipment_id}/split",
            headers={"x-format-new": "true"},
            json_body={"reason": reason, "packs": normalized_packs},
            # A timed-out mutation is ambiguous.  Do not automatically repeat
            # an irreversible split; let the caller re-read shipment state.
            max_attempts=1,
        )
        return dict(result or {})

    def get_marketplace_item(
        self,
        item_id: str,
        *,
        attributes: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """读取 Global Selling 本地站点 Listing（含图片）。"""
        params = None
        if attributes:
            params = {"attributes": ",".join(str(value) for value in attributes if value)}
        return self.request("GET", f"/marketplace/items/{item_id}", params=params)

    def get_product_ads_advertisers(self) -> list[dict[str, Any]]:
        """Return the Product Ads advertisers available to this seller token."""
        result = self.request(
            "GET",
            "/advertising/advertisers",
            params={"product_id": "PADS"},
            headers={"Api-Version": "1", "Content-Type": "application/json"},
        )
        return list((result or {}).get("advertisers") or [])

    def search_product_ads_campaigns(
        self, site_id: str, advertiser_id: int, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return dict(self.request(
            "GET",
            f"/marketplace/advertising/{site_id}/advertisers/{advertiser_id}/product_ads/campaigns/search",
            params={"limit": int(limit), "offset": int(offset)},
            headers={"api-version": "2"},
        ) or {})

    def create_product_ads_campaign(
        self, site_id: str, advertiser_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return dict(self.request(
            "POST",
            f"/marketplace/advertising/{site_id}/advertisers/{advertiser_id}/product_ads/campaigns",
            headers={"api-version": "2", "Content-Type": "application/json"},
            json_body=payload,
            max_attempts=1,
        ) or {})

    def update_product_ads_campaign(
        self, site_id: str, campaign_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return dict(self.request(
            "PUT",
            f"/marketplace/advertising/{site_id}/product_ads/campaigns/{campaign_id}",
            headers={"api-version": "2", "Content-Type": "application/json"},
            json_body=payload,
            max_attempts=1,
        ) or {})

    def search_product_ads_ad_groups(
        self, site_id: str, advertiser_id: int, item_id: str
    ) -> dict[str, Any]:
        return dict(self.request(
            "GET",
            f"/marketplace/advertising/{site_id}/advertisers/{advertiser_id}/product_ads/ad_groups/search",
            params={"filters[item_ids]": item_id},
            headers={"api-version": "2"},
        ) or {})

    def activate_product_ads_ad_group(
        self, site_id: str, ad_group_id: int, campaign_id: int
    ) -> dict[str, Any]:
        return dict(self.request(
            "PUT",
            f"/marketplace/advertising/{site_id}/product_ads/ad_groups/{ad_group_id}",
            headers={"api-version": "2", "Content-Type": "application/json"},
            json_body={"status": "active", "campaign_id": int(campaign_id)},
            max_attempts=1,
        ) or {})

    def get_shipment_label(self, shipment_id: str, *, max_attempts: int = 4) -> bytes:
        """调用 Mercado 官方接口下载 shipment 发货面单 PDF。"""
        return self.request_bytes(
            "GET",
            f"/marketplace/shipments/{shipment_id}/labels",
            max_attempts=max_attempts,
        )

    def iter_listing_ids(self, user_id: str, **filters: Any) -> Iterator[str]:
        """使用 scan/scroll 模式遍历账号下的全部 Listing ID。

        普通搜索只适合较小结果集；scan 模式可以继续读取超过 1000 条的数据。
        ``seen`` 用于防御接口分页边界偶尔出现的重复 ID。
        """
        base_params: dict[str, Any] = {
            "search_type": "scan",
            "limit": 100,
            **filters,
        }
        params = dict(base_params)
        seen: set[str] = set()
        while True:
            page = self.request("GET", f"/marketplace/users/{user_id}/items/search", params=params)
            results = page.get("results", [])
            for item_id in results:
                if str(item_id) not in seen:
                    seen.add(str(item_id))
                    yield str(item_id)
            scroll_id = page.get("scroll_id") or page.get("paging", {}).get("scroll_id")
            if not results or not scroll_id:
                break
            params = {**base_params, "scroll_id": scroll_id}

    def get_listings(self, item_ids: Iterable[str], batch_size: int = 20) -> Iterator[dict[str, Any]]:
        """分批调用 multiget 接口并逐条返回 Listing 完整数据。"""
        batch: list[str] = []
        for item_id in item_ids:
            batch.append(item_id)
            if len(batch) >= batch_size:
                yield from self._get_listing_batch(batch)
                batch = []
        if batch:
            yield from self._get_listing_batch(batch)

    def _get_listing_batch(self, item_ids: list[str]) -> Iterator[dict[str, Any]]:
        """读取一批 Listing，仅产出 API 明确返回成功的数据。"""
        response = self.request("GET", "/items", params={"ids": ",".join(item_ids)})
        for entry in response:
            if entry.get("code") == 200 and isinstance(entry.get("body"), dict):
                yield entry["body"]
            else:
                LOGGER.warning("listing 读取失败: %s", entry)
