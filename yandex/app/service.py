from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from yandex.app.central_authorization import authorization_store
from yandex.app.config import settings
from yandex.app.database import database
from yandex.app.order_finance import enrich_order_finances
from yandex.app.schemas import ProductRecord
from yandex.app.scraper import CaptchaRequired, ScraperError, scraper
from yandex.app.secret_store import secret_fingerprint
from yandex.app.yandex_api import StockTarget, StoreContext, YandexApiError, YandexSellerClient


def _card_quality_summary(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "content_rating": card.get("contentRating"),
        "average_content_rating": card.get("averageContentRating"),
        "status": card.get("status"),
        "card_status": card.get("cardStatus"),
        "parameter_value_count": len(card.get("parameterValues") or []),
        "recommendations": card.get("recommendations") or [],
        "errors": card.get("errors") or [],
        "warnings": card.get("warnings") or [],
    }


def _require_scope(
    store: StoreContext,
    scopes: set[str],
    feature: str,
    *,
    read_only: bool = False,
) -> None:
    normalized = {
        str(scope).strip().lower().replace("_", "-") for scope in store.auth_scopes
    }
    accepted = set(scopes)
    if read_only:
        accepted.update(f"{scope}:read-only" for scope in scopes)
        accepted.add("all-methods:read-only")
    if "all-methods" in normalized or normalized & accepted:
        return
    permission = "读取" if read_only else "管理"
    raise YandexApiError(f"token 缺少“{feature}”{permission}权限")


class TaskService:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._order_sync_runner: asyncio.Task[Any] | None = None
        self._order_sync_locks: dict[int, asyncio.Lock] = {}

    def _track(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def start_search(self, run_id: int, keyword: str, count: int) -> None:
        self._track(self._run_search(run_id, keyword, count))

    def start_order_sync_scheduler(self) -> bool:
        if not settings.order_sync_enabled:
            return False
        if self._order_sync_runner and not self._order_sync_runner.done():
            return True
        self._order_sync_runner = asyncio.create_task(
            self._order_sync_loop(), name="yandex-order-sync"
        )
        return True

    async def stop_order_sync_scheduler(self) -> None:
        runner = self._order_sync_runner
        self._order_sync_runner = None
        if not runner:
            return
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner

    async def _order_sync_loop(self) -> None:
        while True:
            try:
                await self.run_due_order_syncs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Yandex 订单后台调度失败，下个检查周期会重试")
            await asyncio.sleep(settings.order_sync_poll_seconds)

    @staticmethod
    def _elapsed_seconds(value: Any, now: datetime) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (now - parsed.astimezone(UTC)).total_seconds())

    @staticmethod
    def _stored_context(stored: dict[str, Any]) -> StoreContext:
        return StoreContext(
            business_id=int(stored["business_id"]),
            business_name=str(stored.get("business_name") or ""),
            campaign_id=int(stored["campaign_id"]),
            store_name=str(stored.get("store_name") or ""),
            placement_type=str(stored.get("placement_type") or ""),
            api_availability=str(stored.get("api_availability") or ""),
            auth_scopes=list(stored.get("auth_scopes") or []),
        )

    async def run_due_order_syncs(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Run the independent 15-minute discovery and 12-hour status jobs."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        stores = await asyncio.to_thread(authorization_store.list_stores)
        semaphore = asyncio.Semaphore(3)

        async def run(stored: dict[str, Any]) -> dict[str, Any] | None:
            store_id = int(stored["id"])
            state = database.get_order_sync_state(store_id)
            retry_elapsed = self._elapsed_seconds(state.get("updated_at"), current)
            if (
                state.get("last_error")
                and retry_elapsed is not None
                and retry_elapsed < settings.order_sync_retry_seconds
            ):
                return None
            new_elapsed = self._elapsed_seconds(state.get("new_orders_synced_at"), current)
            old_elapsed = self._elapsed_seconds(state.get("old_orders_synced_at"), current)
            new_due = new_elapsed is None or new_elapsed >= settings.order_new_sync_seconds
            old_due = old_elapsed is None or old_elapsed >= settings.order_status_sync_seconds
            if not new_due and not old_due:
                return None
            mode = "full" if new_due and old_due else ("insert" if new_due else "update")
            async with semaphore:
                try:
                    return await self.sync_store_orders(store_id, mode=mode, now=current)
                except Exception as exc:
                    logging.warning(
                        "Yandex 店铺 %s 订单同步失败：%s",
                        stored.get("alias") or store_id,
                        exc,
                    )
                    return {"store_id": store_id, "mode": mode, "error": str(exc)[:500]}

        results = await asyncio.gather(*(run(store) for store in stores))
        return [result for result in results if result is not None]

    async def _fetch_recent_orders(
        self,
        token: str,
        store: StoreContext,
        *,
        today: datetime,
    ) -> list[dict[str, Any]]:
        client = YandexSellerClient(token)
        try:
            market_timezone = ZoneInfo(settings.timezone)
        except ZoneInfoNotFoundError:
            market_timezone = UTC
        date_to = today.astimezone(market_timezone).date()
        date_from = date_to - timedelta(days=29)
        page_token = ""
        seen_tokens: set[str] = set()
        orders: list[dict[str, Any]] = []
        for _ in range(500):
            page = await client.get_orders(
                store.business_id,
                campaign_id=store.campaign_id,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                page_token=page_token,
                limit=50,
            )
            orders.extend(page.get("orders") or [])
            next_token = str((page.get("paging") or {}).get("nextPageToken") or "")
            if not next_token:
                return orders
            if next_token in seen_tokens:
                raise YandexApiError("订单接口返回了重复分页标记，已停止本轮同步")
            seen_tokens.add(next_token)
            page_token = next_token
        raise YandexApiError("订单分页超过 500 页，已停止本轮同步")

    async def sync_store_orders(
        self,
        store_id: int,
        *,
        mode: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        lock = self._order_sync_locks.setdefault(int(store_id), asyncio.Lock())
        async with lock:
            current = (now or datetime.now(UTC)).astimezone(UTC)
            try:
                sync_state = database.get_order_sync_state(store_id)
                new_elapsed = self._elapsed_seconds(
                    sync_state.get("new_orders_synced_at"), current
                )
                old_elapsed = self._elapsed_seconds(
                    sync_state.get("old_orders_synced_at"), current
                )
                new_due = (
                    new_elapsed is None
                    or new_elapsed >= settings.order_new_sync_seconds
                )
                old_due = (
                    old_elapsed is None
                    or old_elapsed >= settings.order_status_sync_seconds
                )
                if database.count_cached_orders(store_id) == 0:
                    mode = "full"
                elif mode == "full":
                    mode = (
                        "full"
                        if new_due and old_due
                        else ("insert" if new_due else ("update" if old_due else ""))
                    )
                elif mode == "insert" and not new_due:
                    mode = ""
                elif mode == "update" and not old_due:
                    mode = ""
                if not mode:
                    return {
                        "store_id": int(store_id),
                        "mode": "skipped",
                        "fetched": 0,
                        "inserted": 0,
                        "updated": 0,
                    }
                stored = await asyncio.to_thread(
                    authorization_store.get_store, store_id, include_secret=True
                )
                if not stored:
                    raise LookupError("店铺不存在")
                token = str(stored.get("access_token") or "")
                if not token:
                    raise LookupError("店铺授权缺少 token，请重新授权")
                context = self._stored_context(stored)
                _require_scope(
                    context,
                    {"inventory-and-order-processing"},
                    "订单",
                    read_only=True,
                )
                orders = await self._fetch_recent_orders(token, context, today=current)
                changes = database.cache_orders(store_id, orders, mode=mode)
                timestamp = current.isoformat(timespec="seconds")
                state_updates: dict[str, Any] = {"last_error": ""}
                if mode in {"insert", "full"}:
                    state_updates["new_orders_synced_at"] = timestamp
                if mode in {"update", "full"}:
                    state_updates["old_orders_synced_at"] = timestamp
                database.update_order_sync_state(store_id, **state_updates)
                return {
                    "store_id": int(store_id),
                    "mode": mode,
                    "fetched": len(orders),
                    **changes,
                }
            except Exception as exc:
                database.update_order_sync_state(store_id, last_error=str(exc)[:1000])
                raise

    async def _run_search(self, run_id: int, keyword: str, count: int) -> None:
        database.update_search_run(run_id, status="running", message="正在启动浏览器")

        async def progress(found: int, scanned: int, message: str) -> None:
            current = database.get_search_run(run_id) or {}
            database.update_search_run(
                run_id,
                found_count=max(found, int(current.get("found_count") or 0)),
                scanned_count=max(scanned, int(current.get("scanned_count") or 0)),
                message=message,
            )

        async def on_product(product: ProductRecord) -> None:
            product.run_id = run_id
            database.upsert_product(product)

        try:
            products = await scraper.scrape(
                keyword,
                count,
                progress=progress,
                on_product=on_product,
            )
            message = f"已抓取并入库 {len(products)} 个国外商品"
            if len(products) < count:
                message += f"；已遍历可用搜索分页，共找到 {len(products)} 个"
            database.update_search_run(
                run_id,
                status="completed",
                found_count=len(products),
                message=message,
            )
        except (CaptchaRequired, ScraperError) as exc:
            database.update_search_run(run_id, status="failed", message=str(exc))
        except Exception as exc:  # 防止后台任务无状态退出；不包含 token。
            database.update_search_run(
                run_id,
                status="failed",
                message=f"抓取失败：{type(exc).__name__}: {str(exc)[:400]}",
            )

    async def validate_token(self, token: str) -> StoreContext:
        return await YandexSellerClient(token).get_store_context()

    async def add_store(self, alias: str, token: str) -> tuple[dict[str, Any], bool]:
        normalized_token = token.strip()
        context = await self.validate_token(normalized_token)
        return authorization_store.save_store(
            alias=alias,
            token=normalized_token,
            token_fingerprint=secret_fingerprint(normalized_token),
            store=context.public_dict(),
        )

    @staticmethod
    def build_zeshun_authorization_url(tg_code: str, explicit_url: str = "") -> str:
        template = explicit_url.strip() or settings.zeshun_authorization_url_template
        if not template:
            return ""
        if "{tg_code}" in template:
            return template.replace("{tg_code}", quote(tg_code, safe=""))
        parsed = urlsplit(template)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query["tg_code"] = [tg_code]
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), parsed.fragment)
        )

    @staticmethod
    def token_from_authorized_url(authorized_url: str) -> str | None:
        if not authorized_url:
            return None
        parsed = urlsplit(authorized_url)
        values = parse_qs(parsed.query, keep_blank_values=True)
        fragment_values = parse_qs(parsed.fragment, keep_blank_values=True)
        values.update(fragment_values)
        normalized = {key.lower().replace("-", "_"): items for key, items in values.items()}
        for key in ("access_token", "token", "api_key", "apikey"):
            candidates = normalized.get(key) or []
            if candidates and candidates[0].strip():
                return candidates[0].strip()
        return None

    async def authorize_zeshun_store(
        self,
        authorization_id: int,
        *,
        authorized_url: str,
        token: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        authorization = authorization_store.get_zeshun_authorization(authorization_id)
        if not authorization:
            raise LookupError("授权店铺不存在")
        normalized_token = (token or self.token_from_authorized_url(authorized_url) or "").strip()
        if not normalized_token:
            raise ValueError("授权链接中未找到 token，请在 token 输入框中手动填写")
        store, created = await self.add_store(authorization["alias"], normalized_token)
        updated = authorization_store.complete_zeshun_authorization(
            authorization_id,
            store_id=int(store["id"]),
        )
        if not updated:
            raise LookupError("授权店铺不存在")
        return updated, store, created

    async def resolve_store(self, store_id: int) -> tuple[str, StoreContext, dict[str, Any]]:
        stored = authorization_store.get_store(store_id, include_secret=True)
        if not stored:
            raise LookupError("店铺不存在")
        token = str(stored.get("access_token") or "")
        if not token:
            raise LookupError("店铺授权缺少 token，请重新授权")
        context = await self.validate_token(token)
        refreshed = (
            authorization_store.update_store_connection(store_id, context.public_dict())
            or stored
        )
        return token, context, refreshed

    async def get_orders(
        self,
        store_id: int,
        *,
        statuses: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page_token: str = "",
        limit: int = 50,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(
            store, {"inventory-and-order-processing"}, "订单", read_only=True
        )
        sync_state = database.get_order_sync_state(store_id)
        current = datetime.now(UTC)
        new_elapsed = self._elapsed_seconds(sync_state.get("new_orders_synced_at"), current)
        old_elapsed = self._elapsed_seconds(sync_state.get("old_orders_synced_at"), current)
        new_due = new_elapsed is None or new_elapsed >= settings.order_new_sync_seconds
        old_due = old_elapsed is None or old_elapsed >= settings.order_status_sync_seconds
        if database.count_cached_orders(store_id) == 0 or (new_due and old_due):
            await self.sync_store_orders(store_id, mode="full", now=current)
        elif new_due:
            await self.sync_store_orders(store_id, mode="insert", now=current)
        elif old_due:
            await self.sync_store_orders(store_id, mode="update", now=current)
        offset = 0
        if str(page_token).startswith("cache:"):
            try:
                offset = max(0, int(str(page_token).split(":", 1)[1]))
            except ValueError:
                offset = 0
        page_size = max(1, min(int(limit), 50))
        cached_orders, has_more = database.list_cached_orders(
            store_id,
            statuses=statuses,
            date_from=date_from,
            date_to=date_to,
            offset=offset,
            limit=page_size,
        )
        client = YandexSellerClient(token)
        cached_orders = await enrich_order_finances(
            client, store.business_id, store.campaign_id, cached_orders,
        )
        orders = {
            "orders": cached_orders,
            "paging": {
                "nextPageToken": f"cache:{offset + page_size}" if has_more else ""
            },
            "sync": database.get_order_sync_state(store_id),
        }
        return orders, stored

    async def update_order(
        self, store_id: int, order_id: int, action: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"inventory-and-order-processing"}, "订单")
        transitions = {
            "READY_TO_SHIP": ("PROCESSING", "READY_TO_SHIP"),
            "CANCEL": ("CANCELLED", "SHOP_FAILED"),
        }
        if action not in transitions:
            raise ValueError("不支持的订单操作")
        order_status, substatus = transitions[action]
        result = await YandexSellerClient(token).update_order_status(
            store.campaign_id,
            order_id,
            status=order_status,
            substatus=substatus,
        )
        database.patch_cached_order_status(
            store_id,
            order_id,
            status=order_status,
            substatus=substatus,
        )
        return result, stored

    async def get_inventory(
        self,
        store_id: int,
        *,
        offer_ids: list[str] | None = None,
        archived: bool = False,
        page_token: str = "",
        limit: int = 100,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(
            store, {"offers-and-cards-management"}, "商品和库存", read_only=True
        )
        client = YandexSellerClient(token)
        if store.placement_type.strip().upper() in {"FBS", "DBS", "EXPRESS"}:
            target = await client.resolve_stock_target(
                store.business_id, store.campaign_id, store.placement_type
            )
        else:
            target = StockTarget(
                method="campaign", warehouse_id=None, warehouse_name="Yandex 仓库"
            )
        result = await client.get_offer_stocks(
            store.business_id,
            store.campaign_id,
            target,
            offer_ids=offer_ids,
            archived=archived,
            page_token=page_token,
            limit=limit,
        )
        stock_offer_ids = list(
            dict.fromkeys(
                str(offer.get("offerId"))
                for warehouse in result["warehouses"]
                for offer in warehouse.get("offers") or []
                if isinstance(offer, dict) and offer.get("offerId")
            )
        )
        details: list[dict[str, Any]] = []
        warning = ""
        try:
            for index in range(0, len(stock_offer_ids), 100):
                details.extend(
                    await client.get_business_offer_prices(
                        store.business_id, stock_offer_ids[index : index + 100]
                    )
                )
        except YandexApiError as exc:
            warning = f"商品名称和价格暂未补全：{exc}"
        details_by_id = {str(item.get("offerId")): item for item in details}
        for warehouse in result["warehouses"]:
            for offer in warehouse.get("offers") or []:
                if isinstance(offer, dict):
                    offer["details"] = details_by_id.get(str(offer.get("offerId")), {})
        result["target"] = target.public_dict()
        result["warning"] = warning
        return result, stored

    async def update_inventory_stock(
        self, store_id: int, offer_id: str, count: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"offers-and-cards-management"}, "商品和库存")
        client = YandexSellerClient(token)
        target = await client.resolve_stock_target(
            store.business_id, store.campaign_id, store.placement_type
        )
        result = await client.update_offer_stock(
            store.business_id, store.campaign_id, offer_id, count, target
        )
        return {"response": result, "target": target.public_dict()}, stored

    async def get_listings(
        self,
        store_id: int,
        *,
        offer_ids: list[str] | None = None,
        statuses: list[str] | None = None,
        page_token: str = "",
        limit: int = 100,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(
            store, {"offers-and-cards-management"}, "链接", read_only=True
        )
        client = YandexSellerClient(token)
        result = await client.get_campaign_offers(
            store.campaign_id,
            offer_ids=offer_ids,
            statuses=statuses,
            page_token=page_token,
            limit=limit,
        )
        ids = list(
            dict.fromkeys(
                str(item.get("offerId"))
                for item in result["offers"]
                if item.get("offerId")
            )
        )
        details: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            for index in range(0, len(ids), 100):
                details.extend(
                    await client.get_business_offer_prices(
                        store.business_id, ids[index : index + 100]
                    )
                )
        except YandexApiError as exc:
            warnings.append(f"商品名称、图片或前台链接暂未补全：{exc}")
        detail_by_id = {str(item.get("offerId")): item for item in details}
        for item in result["offers"]:
            item["details"] = detail_by_id.get(str(item.get("offerId")), {})
        if ids:
            try:
                hidden_ids = set(
                    await client.get_hidden_offer_ids(store.campaign_id, ids)
                )
                for item in result["offers"]:
                    item["paused"] = str(item.get("offerId")) in hidden_ids
            except YandexApiError as exc:
                warnings.append(f"商品暂停状态暂未补全：{exc}")
        result["warning"] = "；".join(warnings)
        result["statusCounts"] = {
            status: sum(1 for item in result["offers"] if item.get("status") == status)
            for status in sorted({str(item.get("status")) for item in result["offers"] if item.get("status")})
        }
        return result, stored

    async def update_listing_price(
        self,
        store_id: int,
        offer_id: str,
        *,
        value: float,
        currency_id: str,
        discount_base: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"pricing"}, "链接价格")
        result = await YandexSellerClient(token).update_listing_price(
            store.business_id,
            store.campaign_id,
            offer_id,
            value=value,
            currency_id=currency_id,
            discount_base=discount_base,
        )
        return result, stored

    async def update_listing_dimensions(
        self,
        store_id: int,
        offer_id: str,
        package: dict[str, float],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"offers-and-cards-management"}, "商品包装和重量")
        result = await YandexSellerClient(token).update_listing_dimensions(
            store.business_id,
            offer_id,
            length=package["length"],
            width=package["width"],
            height=package["height"],
            weight=package["weight"],
        )
        return result, stored

    async def update_listing_visibility(
        self,
        store_id: int,
        offer_ids: list[str],
        *,
        paused: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"offers-and-cards-management"}, "链接销售状态")
        client = YandexSellerClient(token)
        response = (
            await client.pause_offer_displays(store.campaign_id, offer_ids)
            if paused
            else await client.resume_offer_displays(store.campaign_id, offer_ids)
        )
        return {
            "response": response,
            "offerIds": offer_ids,
            "paused": paused,
            "visibilityScope": "campaign",
        }, stored

    async def delete_listings(
        self, store_id: int, offer_ids: list[str]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"offers-and-cards-management"}, "链接")
        result = await YandexSellerClient(token).delete_campaign_offers(
            store.campaign_id, offer_ids
        )
        return result, stored

    async def get_returns(
        self, store_id: int, **filters: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(
            store, {"inventory-and-order-processing"}, "退货", read_only=True
        )
        result = await YandexSellerClient(token).get_returns(store.campaign_id, **filters)
        return result, stored

    async def get_chats(
        self, store_id: int, **filters: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"communication"}, "客户消息", read_only=True)
        result = await YandexSellerClient(token).get_chats(store.business_id, **filters)
        return result, stored

    async def get_chat_history(
        self, store_id: int, chat_id: int, **filters: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"communication"}, "客户消息", read_only=True)
        result = await YandexSellerClient(token).get_chat_history(
            store.business_id, chat_id, **filters
        )
        return result, stored

    async def send_chat_message(
        self, store_id: int, chat_id: int, text: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"communication"}, "客户消息")
        result = await YandexSellerClient(token).send_chat_message(
            store.business_id, chat_id, text
        )
        return result, stored

    async def create_chat(
        self, store_id: int, context_type: str, context_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"communication"}, "客户消息")
        result = await YandexSellerClient(token).create_chat(
            store.business_id, context_type, context_id
        )
        return result, stored

    async def get_feedbacks(
        self, store_id: int, **filters: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"communication"}, "客户沟通", read_only=True)
        result = await YandexSellerClient(token).get_feedbacks(store.business_id, **filters)
        return result, stored

    async def reply_to_feedback(
        self, store_id: int, feedback_id: int, text: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"communication"}, "客户沟通")
        result = await YandexSellerClient(token).reply_to_feedback(
            store.business_id, feedback_id, text
        )
        return result, stored

    async def skip_feedbacks(
        self, store_id: int, feedback_ids: list[int]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"communication"}, "客户沟通")
        result = await YandexSellerClient(token).skip_feedbacks(
            store.business_id, feedback_ids
        )
        return result, stored

    async def get_questions(
        self, store_id: int, **filters: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"communication"}, "客户沟通", read_only=True)
        result = await YandexSellerClient(token).get_questions(store.business_id, **filters)
        return result, stored

    async def reply_to_question(
        self, store_id: int, question_id: int, text: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token, store, stored = await self.resolve_store(store_id)
        _require_scope(store, {"communication"}, "客户沟通")
        result = await YandexSellerClient(token).reply_to_question(
            store.business_id, question_id, text
        )
        return result, stored

    async def resolve_stock_target(
        self,
        token: str,
        store: StoreContext,
    ) -> StockTarget:
        _require_scope(store, {"offers-and-cards-management"}, "商品和库存")
        return await YandexSellerClient(token).resolve_stock_target(
            store.business_id,
            store.campaign_id,
            store.placement_type,
        )

    def start_publish(
        self,
        job_id: int,
        token: str,
        store: StoreContext,
        products: list[dict[str, Any]],
        price_percent: float,
        rub_to_cny_rate: float,
        package_dimensions: dict[str, float],
        initial_stock: int,
        stock_target: StockTarget,
    ) -> None:
        self._track(
            self._run_publish(
                job_id,
                token,
                store,
                products,
                price_percent,
                rub_to_cny_rate,
                package_dimensions,
                initial_stock,
                stock_target,
            )
        )

    async def _run_publish(
        self,
        job_id: int,
        token: str,
        store: StoreContext,
        products: list[dict[str, Any]],
        price_percent: float,
        rub_to_cny_rate: float,
        package_dimensions: dict[str, float],
        initial_stock: int,
        stock_target: StockTarget,
    ) -> None:
        client = YandexSellerClient(token)
        succeeded = 0
        failed = 0
        summaries: list[dict[str, Any]] = []
        prepared: list[dict[str, Any]] = []
        for product in products:
            product_id = int(product["id"])
            if not product.get("is_foreign"):
                message = "安全检查失败：该商品没有国外发货标记"
                database.mark_product_publish(product_id, "failed", message)
                database.add_publish_result(job_id, product_id, "failed", message)
                summaries.append({"product_id": product_id, "status": "failed", "message": message})
                failed += 1
                continue
            database.mark_product_publish(product_id, "publishing", "正在提交 Yandex API")
            mapping_response: dict[str, Any] | None = None
            stage = "提交商品卡"
            try:
                source_price = product.get("price")
                publish_product = dict(product)
                publish_product["weight_dimensions"] = package_dimensions
                if source_price is not None and float(source_price) > 0:
                    source_currency = str(product.get("currency") or "RUR").upper()
                    if source_currency not in {"RUR", "RUB"}:
                        raise YandexApiError(
                            f"暂不支持把 {source_currency} 自动换算成人民币"
                        )
                    listing_price = calculate_listing_price(
                        source_price,
                        price_percent,
                        rub_to_cny_rate,
                    )
                    publish_product["price"] = listing_price
                    publish_product["currency"] = "CNY"
                    price_message = (
                        f"上架价 {listing_price:.2f} CNY"
                        f"（{float(source_price):.2f} RUR × {rub_to_cny_rate:.6f}"
                        f" CNY/RUB × {price_percent:g}%）"
                    )
                else:
                    price_message = "商品没有抓取价格，本次未提交价格"
                mapping_response = await client.publish_product(
                    store.business_id,
                    publish_product,
                )
                response = {
                    "offer_mapping": mapping_response,
                    "stock_target": stock_target.public_dict(),
                    "initial_stock": initial_stock,
                }
                pending_message = (
                    "商品卡已接收，等待本批统一恢复展示并写入初始库存"
                )
                database.mark_product_publish(product_id, "publishing", pending_message)
                database.add_publish_result(
                    job_id,
                    product_id,
                    "stock_pending",
                    pending_message,
                    response,
                )
                prepared.append(
                    {
                        "product_id": product_id,
                        "offer_id": str(product["offer_id"]),
                        "price_message": price_message,
                        "response": response,
                    }
                )
            except YandexApiError as exc:
                prefix = "商品卡已提交，但" if mapping_response is not None else ""
                message = f"{prefix}{stage}失败：{str(exc)}"[:2000]
                error_response = {
                    "failed_stage": stage,
                    "offer_mapping": mapping_response or {},
                    "yandex_error": exc.details if isinstance(exc.details, dict) else {},
                }
                database.mark_product_publish(product_id, "failed", message)
                database.add_publish_result(
                    job_id,
                    product_id,
                    "failed",
                    message,
                    error_response,
                )
                summaries.append({"product_id": product_id, "status": "failed", "message": message})
                failed += 1
            except Exception as exc:
                prefix = "商品卡已提交，但" if mapping_response is not None else ""
                message = f"{prefix}{stage}失败：{type(exc).__name__}: {str(exc)[:1700]}"
                database.mark_product_publish(product_id, "failed", message)
                database.add_publish_result(
                    job_id,
                    product_id,
                    "failed",
                    message,
                    {"offer_mapping": mapping_response or {}},
                )
                summaries.append({"product_id": product_id, "status": "failed", "message": message})
                failed += 1
            await asyncio.sleep(0.2)

        if prepared:
            batch_stage = "恢复商品展示"
            try:
                display_response: dict[str, Any] | None = None
                stock_response: dict[str, Any] | None = None
                for attempt in range(3):
                    try:
                        display_response = await client.resume_offer_displays(
                            store.campaign_id,
                            [item["offer_id"] for item in prepared],
                        )
                        break
                    except YandexApiError as exc:
                        if exc.status_code not in {400, 404} or attempt == 2:
                            raise
                        await asyncio.sleep(2**attempt)

                batch_stage = "写入初始库存"
                for attempt in range(3):
                    try:
                        stock_response = await client.update_offer_stocks(
                            store.business_id,
                            store.campaign_id,
                            [item["offer_id"] for item in prepared],
                            initial_stock,
                            stock_target,
                        )
                        break
                    except YandexApiError as exc:
                        if exc.status_code not in {400, 404} or attempt == 2:
                            raise
                        await asyncio.sleep(2**attempt)

                if display_response is None or stock_response is None:
                    raise YandexApiError(f"{batch_stage}没有返回结果")

                # 上传后回读一次官方卡片状态。内容更新可能需要几分钟，因此这里既保存
                # 当前评分，也保留更新状态和建议，方便确认 Yandex 是否真正接收了参数。
                quality_by_offer: dict[str, dict[str, Any]] = {}
                quality_error = ""
                try:
                    offer_ids = [item["offer_id"] for item in prepared]
                    for start in range(0, len(offer_ids), 200):
                        cards = await client.get_offer_cards(
                            store.business_id,
                            offer_ids[start : start + 200],
                        )
                        for card in cards:
                            offer = card.get("offer") or {}
                            offer_id = str(card.get("offerId") or offer.get("offerId") or "")
                            if offer_id:
                                quality_by_offer[offer_id] = _card_quality_summary(card)
                except YandexApiError as exc:
                    quality_error = str(exc)[:1000]

                for item in prepared:
                    product_id = item["product_id"]
                    response = dict(item["response"])
                    response["resume_display"] = display_response
                    response["stock"] = stock_response
                    quality = quality_by_offer.get(item["offer_id"])
                    if quality:
                        response["card_quality"] = quality
                    elif quality_error:
                        response["card_quality_error"] = quality_error
                    message = (
                        f"Yandex 已接收商品卡并写入库存 {initial_stock} 件"
                        f"（{stock_target.warehouse_name}），等待更新为准备出售；"
                        f"{item['price_message']}；包装 "
                        f"{package_dimensions['length']:g}×{package_dimensions['width']:g}×"
                        f"{package_dimensions['height']:g} cm / {package_dimensions['weight']:g} kg"
                    )
                    if quality and quality.get("content_rating") is not None:
                        message += f"；当前卡片评分 {quality['content_rating']}/100"
                    elif quality and quality.get("status"):
                        message += f"；卡片内容状态 {quality['status']}，评分等待 Yandex 更新"
                    elif quality_error:
                        message += "；评分回读暂不可用，可稍后在卖家后台查看"
                    database.mark_product_publish(product_id, "published", message)
                    database.update_publish_result(
                        job_id,
                        product_id,
                        "published",
                        message,
                        response,
                    )
                    summaries.append({"product_id": product_id, "status": "published"})
                    succeeded += 1
            except Exception as exc:
                yandex_details = (
                    exc.details
                    if isinstance(exc, YandexApiError) and isinstance(exc.details, dict)
                    else {}
                )
                error_text = str(exc)[:1700]
                for item in prepared:
                    product_id = item["product_id"]
                    message = f"商品卡已提交，但{batch_stage}失败：{error_text}"
                    response = dict(item["response"])
                    response.update({"failed_stage": batch_stage, "yandex_error": yandex_details})
                    database.mark_product_publish(product_id, "failed", message)
                    database.update_publish_result(
                        job_id,
                        product_id,
                        "failed",
                        message,
                        response,
                    )
                    summaries.append(
                        {"product_id": product_id, "status": "failed", "message": message}
                    )
                    failed += 1
        database.finish_publish_job(
            job_id,
            succeeded=succeeded,
            failed=failed,
            response={"items": summaries},
        )


def calculate_listing_price(
    price: float | int,
    price_percent: float,
    rub_to_cny_rate: float = 1,
) -> float:
    adjusted = (
        Decimal(str(price))
        * Decimal(str(rub_to_cny_rate))
        * Decimal(str(price_percent))
        / Decimal("100")
    )
    # 当前 Yandex 店铺对 RUR/CNY 的 basicPrice 都要求整数，计算阶段即统一取整，
    # 保证页面展示、任务记录和最终 API 载荷完全一致。
    return float(adjusted.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


task_service = TaskService()
