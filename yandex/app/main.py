from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from yandex.app.central_authorization import (
    AuthorizationStoreError,
    authorization_store,
)
from yandex.app.config import settings
from yandex.app.database import database
from yandex.app.exchange_rate import ExchangeRateError, exchange_rate_service
from yandex.app.schemas import (
    FeedbackListRequest,
    FeedbackReplyRequest,
    FeedbackSkipRequest,
    InventoryListRequest,
    InventoryStockUpdateRequest,
    ListingDeleteRequest,
    ListingListRequest,
    ListingPriceUpdateRequest,
    OrderActionRequest,
    OrderListRequest,
    PublishRequest,
    QuestionListRequest,
    QuestionReplyRequest,
    ReturnListRequest,
    SearchRequest,
    StoreCreateRequest,
    StoreUpdateRequest,
    TokenRequest,
    ZeshunStoreAuthorizeRequest,
    ZeshunStoreCreateRequest,
    ZeshunStoreUpdateRequest,
)
from yandex.app.scraper import scraper
from yandex.app.secret_store import SecretStoreError
from yandex.app.service import task_service
from yandex.app.yandex_api import YandexApiError


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database.initialize()
    authorization_store.initialize()
    yield
    await scraper.close()


app = FastAPI(
    title="Yandex Market 跟卖助手",
    version="0.5.0",
    lifespan=lifespan,
)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def _forwarded_prefix(request: Request) -> str:
    prefix = request.headers.get("x-forwarded-prefix", "").strip().rstrip("/")
    if not prefix or not prefix.startswith("/"):
        return ""
    if any(part in {"", ".", ".."} for part in prefix.split("/")[1:]):
        return ""
    return prefix


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "max_products": settings.max_products,
            "embedded": request.query_params.get("embedded", "").lower()
            in {"1", "true", "yes"},
            "base_path": _forwarded_prefix(request),
        },
    )


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "yandex-console", "version": app.version}


async def _store_operation(operation) -> tuple[dict, dict]:
    try:
        return await operation
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SecretStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except YandexApiError as exc:
        raise HTTPException(
            status_code=exc.status_code or status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@app.post("/api/token/validate")
async def validate_token(payload: TokenRequest) -> dict:
    try:
        store = await task_service.validate_token(payload.token.get_secret_value())
    except YandexApiError as exc:
        raise HTTPException(
            status_code=exc.status_code or status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return {"ok": True, "store": store.public_dict()}


@app.get("/api/stores")
async def list_stores() -> dict:
    try:
        return {"stores": authorization_store.list_stores()}
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/zeshun-stores")
async def list_zeshun_stores() -> dict:
    try:
        return {"stores": authorization_store.list_zeshun_authorizations()}
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/zeshun-stores", status_code=status.HTTP_201_CREATED)
async def create_zeshun_store(payload: ZeshunStoreCreateRequest) -> dict:
    authorization_url = task_service.build_zeshun_authorization_url(
        payload.tg_code,
        payload.authorization_url,
    )
    try:
        store = authorization_store.create_zeshun_authorization(
            alias=payload.alias,
            tg_code=payload.tg_code,
            authorization_url=authorization_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "store": store}


@app.patch("/api/zeshun-stores/{authorization_id}")
async def update_zeshun_store(
    authorization_id: int,
    payload: ZeshunStoreUpdateRequest,
) -> dict:
    try:
        existing = authorization_store.get_zeshun_authorization(authorization_id)
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not existing:
        raise HTTPException(status_code=404, detail="授权店铺不存在")
    authorization_url = task_service.build_zeshun_authorization_url(
        existing["tg_code"],
        payload.authorization_url,
    )
    try:
        store = authorization_store.update_zeshun_authorization(
            authorization_id,
            alias=payload.alias,
            authorization_url=authorization_url,
        )
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "store": store}


@app.post("/api/zeshun-stores/{authorization_id}/authorize")
async def authorize_zeshun_store(
    authorization_id: int,
    payload: ZeshunStoreAuthorizeRequest,
) -> dict:
    try:
        authorization, store, created = await task_service.authorize_zeshun_store(
            authorization_id,
            authorized_url=payload.authorized_url,
            token=payload.token.get_secret_value() if payload.token is not None else None,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except YandexApiError as exc:
        raise HTTPException(
            status_code=exc.status_code or status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except SecretStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "ok": True,
        "created": created,
        "authorization": authorization,
        "store": store,
    }


@app.delete("/api/zeshun-stores/{authorization_id}")
async def delete_zeshun_store(authorization_id: int) -> dict:
    try:
        if not authorization_store.delete_zeshun_authorization(authorization_id):
            raise HTTPException(status_code=404, detail="授权店铺不存在")
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/exchange-rate")
async def exchange_rate(refresh: bool = False) -> dict:
    try:
        quote = await exchange_rate_service.get_rub_to_cny(force_refresh=refresh)
    except ExchangeRateError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"exchange_rate": quote.public_dict()}


@app.post("/api/stores", status_code=status.HTTP_201_CREATED)
async def create_store(payload: StoreCreateRequest) -> dict:
    try:
        store, created = await task_service.add_store(
            payload.alias,
            payload.token.get_secret_value(),
        )
    except YandexApiError as exc:
        raise HTTPException(
            status_code=exc.status_code or status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except SecretStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "created": created, "store": store}


@app.patch("/api/stores/{store_id}")
async def update_store(store_id: int, payload: StoreUpdateRequest) -> dict:
    try:
        store = authorization_store.update_store_alias(store_id, payload.alias)
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not store:
        raise HTTPException(status_code=404, detail="店铺不存在")
    return {"ok": True, "store": store}


@app.post("/api/stores/{store_id}/refresh")
async def refresh_store(store_id: int) -> dict:
    try:
        _, _, store = await task_service.resolve_store(store_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except YandexApiError as exc:
        raise HTTPException(
            status_code=exc.status_code or status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except SecretStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "store": store}


@app.delete("/api/stores/{store_id}")
async def delete_store(store_id: int) -> dict:
    try:
        if not authorization_store.delete_store(store_id):
            raise HTTPException(status_code=404, detail="店铺不存在")
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/search", status_code=status.HTTP_202_ACCEPTED)
async def create_search(payload: SearchRequest) -> dict[str, int | str]:
    run_id = database.create_search_run(payload.keyword, payload.count)
    task_service.start_search(run_id, payload.keyword, payload.count)
    return {"run_id": run_id, "status": "queued"}


@app.get("/api/search/{run_id}")
async def get_search(run_id: int) -> dict:
    run = database.get_search_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="搜索任务不存在")
    products = database.list_products_for_run(run_id)
    return {"run": run, "products": products}


@app.post("/api/orders")
async def list_orders(payload: OrderListRequest) -> dict:
    try:
        result, store = await task_service.get_orders(
            payload.store_id,
            statuses=payload.statuses,
            date_from=payload.date_from.isoformat() if payload.date_from else None,
            date_to=payload.date_to.isoformat() if payload.date_to else None,
            page_token=payload.page_token,
            limit=payload.limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SecretStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except YandexApiError as exc:
        raise HTTPException(
            status_code=exc.status_code or status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return {"store": store, **result}


@app.post("/api/orders/action")
async def update_order(payload: OrderActionRequest) -> dict:
    result, store = await _store_operation(
        task_service.update_order(payload.store_id, payload.order_id, payload.action)
    )
    return {"ok": True, "store": store, "result": result}


@app.post("/api/inventory")
async def list_inventory(payload: InventoryListRequest) -> dict:
    result, store = await _store_operation(
        task_service.get_inventory(
            payload.store_id,
            offer_ids=payload.offer_ids,
            archived=payload.archived,
            page_token=payload.page_token,
            limit=payload.limit,
        )
    )
    return {"store": store, **result}


@app.put("/api/inventory/stock")
async def update_inventory_stock(payload: InventoryStockUpdateRequest) -> dict:
    result, store = await _store_operation(
        task_service.update_inventory_stock(
            payload.store_id, payload.offer_id, payload.count
        )
    )
    return {"ok": True, "store": store, **result}


@app.post("/api/listings")
async def list_links(payload: ListingListRequest) -> dict:
    result, store = await _store_operation(
        task_service.get_listings(
            payload.store_id,
            offer_ids=payload.offer_ids,
            statuses=payload.statuses,
            page_token=payload.page_token,
            limit=payload.limit,
        )
    )
    return {"store": store, **result}


@app.put("/api/listings/price")
async def update_link_price(payload: ListingPriceUpdateRequest) -> dict:
    result, store = await _store_operation(
        task_service.update_listing_price(
            payload.store_id,
            payload.offer_id,
            value=payload.value,
            currency_id=payload.currency_id,
            discount_base=payload.discount_base,
        )
    )
    return {"ok": True, "store": store, **result}


@app.post("/api/listings/delete")
async def delete_links(payload: ListingDeleteRequest) -> dict:
    result, store = await _store_operation(
        task_service.delete_listings(payload.store_id, payload.offer_ids)
    )
    return {"ok": not result["notDeletedOfferIds"], "store": store, **result}


@app.post("/api/returns")
async def list_returns(payload: ReturnListRequest) -> dict:
    result, store = await _store_operation(
        task_service.get_returns(
            payload.store_id,
            return_type=payload.return_type,
            statuses=payload.statuses,
            shipment_statuses=payload.shipment_statuses,
            date_from=payload.date_from.isoformat() if payload.date_from else None,
            date_to=payload.date_to.isoformat() if payload.date_to else None,
            page_token=payload.page_token,
            limit=payload.limit,
        )
    )
    return {"store": store, **result}


@app.post("/api/feedback")
async def list_feedback(payload: FeedbackListRequest) -> dict:
    result, store = await _store_operation(
        task_service.get_feedbacks(
            payload.store_id,
            reaction_status=payload.reaction_status,
            rating_values=payload.rating_values,
            offer_ids=payload.offer_ids,
            page_token=payload.page_token,
            limit=payload.limit,
        )
    )
    return {"store": store, **result}


@app.post("/api/feedback/reply")
async def reply_to_feedback(payload: FeedbackReplyRequest) -> dict:
    result, store = await _store_operation(
        task_service.reply_to_feedback(
            payload.store_id, payload.feedback_id, payload.text
        )
    )
    return {"ok": True, "store": store, "comment": result}


@app.post("/api/feedback/skip")
async def skip_feedback(payload: FeedbackSkipRequest) -> dict:
    _, store = await _store_operation(
        task_service.skip_feedbacks(payload.store_id, payload.feedback_ids)
    )
    return {"ok": True, "store": store}


@app.post("/api/questions")
async def list_questions(payload: QuestionListRequest) -> dict:
    result, store = await _store_operation(
        task_service.get_questions(
            payload.store_id,
            need_answer=payload.need_answer,
            date_from=payload.date_from.isoformat() if payload.date_from else None,
            date_to=payload.date_to.isoformat() if payload.date_to else None,
            page_token=payload.page_token,
            limit=payload.limit,
        )
    )
    return {"store": store, **result}


@app.post("/api/questions/reply")
async def reply_to_question(payload: QuestionReplyRequest) -> dict:
    result, store = await _store_operation(
        task_service.reply_to_question(
            payload.store_id, payload.question_id, payload.text
        )
    )
    return {"ok": True, "store": store, "result": result}


@app.post("/api/publish", status_code=status.HTTP_202_ACCEPTED)
async def publish(payload: PublishRequest) -> dict:
    product_ids = list(dict.fromkeys(payload.product_ids))
    products = database.get_products(product_ids)
    if len(products) != len(product_ids):
        raise HTTPException(status_code=404, detail="部分商品不存在，请刷新列表")
    try:
        token, store, stored = await task_service.resolve_store(payload.store_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except YandexApiError as exc:
        raise HTTPException(
            status_code=exc.status_code or status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except SecretStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AuthorizationStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        quote = await exchange_rate_service.get_rub_to_cny()
    except ExchangeRateError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        stock_target = await task_service.resolve_stock_target(token, store)
    except YandexApiError as exc:
        raise HTTPException(
            status_code=exc.status_code or status.HTTP_400_BAD_REQUEST,
            detail=f"无法准备库存：{str(exc)}",
        ) from exc
    job_id = database.create_publish_job(
        len(products),
        store.business_id,
        store.campaign_id,
        payload.store_id,
        payload.price_percent,
        quote.rate,
        quote.effective_date,
        "CNY",
        payload.package.model_dump(),
        payload.initial_stock,
        stock_target,
    )
    task_service.start_publish(
        job_id,
        token,
        store,
        products,
        payload.price_percent,
        quote.rate,
        payload.package.model_dump(),
        payload.initial_stock,
        stock_target,
    )
    return {
        "job_id": job_id,
        "status": "running",
        "store": stored,
        "job": database.get_publish_job(job_id),
    }


@app.get("/api/publish/{job_id}")
async def get_publish(job_id: int) -> dict:
    job = database.get_publish_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="发布任务不存在")
    return {"job": job}
