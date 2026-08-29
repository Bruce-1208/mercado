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
        self.session = session or requests.Session()
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

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """发送已认证请求，并处理 token 失效、限流及临时服务端错误。

        401 每次请求最多触发一次 token 刷新；网络中断、429 和 5xx 使用退避
        等待，避免瞬时故障导致整个定时同步任务直接退出。
        """
        url = path if path.startswith("http") else f"{self.BASE_URL}{path}"
        refreshed = False
        for attempt in range(4):
            try:
                response = self.session.request(method, url, params=params,
                    headers={"Authorization": f"Bearer {self.access_token}"}, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt < 3:
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
                if attempt < 3:
                    delay = min(float(response.headers.get("Retry-After", 2**attempt)), 30)
                    LOGGER.warning("API 暂时不可用 (%s)，%.1f 秒后重试", response.status_code, delay)
                    time.sleep(delay)
                    continue
            if not response.ok:
                raise MercadoAPIError(f"{method} {path} 失败 ({response.status_code}): {response.text[:1000]}")
            return response.json()
        raise MercadoAPIError(f"{method} {path} 多次重试后仍失败")

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
            # Global Selling 的生产接口使用 ``seller``；传 ``seller.id`` 会
            # 返回 200 但 paging.total=0，容易被误判为店铺确实没有订单。
            params = {"seller": seller_id, "limit": limit, "offset": offset, **filters}
            page = self.request("GET", "/marketplace/orders/search", params=params)
            results = page.get("results", [])
            yield from self._order_ids(results)
            offset += len(results)
            total = int(page.get("paging", {}).get("total", offset))
            if not results or offset >= total:
                break

    def get_order(self, order_id: str) -> dict[str, Any]:
        """读取单个订单的完整详情。"""
        return self.request("GET", f"/marketplace/orders/{order_id}")

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
