from __future__ import annotations

import asyncio
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from yandex.app.config import settings
from yandex.app.database import database
from yandex.app.schemas import ProductRecord
from yandex.app.scraper import CaptchaRequired, ScraperError, scraper
from yandex.app.secret_store import protect_secret, secret_fingerprint, unprotect_secret
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


class TaskService:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def _track(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def start_search(self, run_id: int, keyword: str, count: int) -> None:
        self._track(self._run_search(run_id, keyword, count))

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
        return database.save_store(
            alias=alias,
            encrypted_token=protect_secret(normalized_token),
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
        authorization = database.get_zeshun_authorization(authorization_id)
        if not authorization:
            raise LookupError("授权店铺不存在")
        normalized_token = (token or self.token_from_authorized_url(authorized_url) or "").strip()
        if not normalized_token:
            raise ValueError("授权链接中未找到 token，请在 token 输入框中手动填写")
        store, created = await self.add_store(authorization["alias"], normalized_token)
        encrypted_url = protect_secret(authorized_url) if authorized_url else None
        updated = database.complete_zeshun_authorization(
            authorization_id,
            store_id=int(store["id"]),
            encrypted_authorized_url=encrypted_url,
        )
        if not updated:
            raise LookupError("授权店铺不存在")
        return updated, store, created

    async def resolve_store(self, store_id: int) -> tuple[str, StoreContext, dict[str, Any]]:
        stored = database.get_store(store_id, include_secret=True)
        if not stored:
            raise LookupError("店铺不存在")
        encrypted = stored.get("encrypted_token")
        token = unprotect_secret(bytes(encrypted))
        context = await self.validate_token(token)
        refreshed = database.update_store_connection(store_id, context.public_dict()) or stored
        return token, context, refreshed

    async def resolve_stock_target(
        self,
        token: str,
        store: StoreContext,
    ) -> StockTarget:
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
