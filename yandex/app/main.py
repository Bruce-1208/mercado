from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from yandex.app.config import settings
from yandex.app.database import database
from yandex.app.exchange_rate import ExchangeRateError, exchange_rate_service
from yandex.app.schemas import (
    PublishRequest,
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
    yield
    await scraper.close()


app = FastAPI(
    title="Yandex Market 跟卖助手",
    version="0.3.0",
    lifespan=lifespan,
)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "max_products": settings.max_products,
            "embedded": request.query_params.get("embedded", "").lower()
            in {"1", "true", "yes"},
        },
    )


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "yandex-console", "version": app.version}


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
    return {"stores": database.list_stores()}


@app.get("/api/zeshun-stores")
async def list_zeshun_stores() -> dict:
    return {"stores": database.list_zeshun_authorizations()}


@app.post("/api/zeshun-stores", status_code=status.HTTP_201_CREATED)
async def create_zeshun_store(payload: ZeshunStoreCreateRequest) -> dict:
    authorization_url = task_service.build_zeshun_authorization_url(
        payload.tg_code,
        payload.authorization_url,
    )
    try:
        store = database.create_zeshun_authorization(
            alias=payload.alias,
            tg_code=payload.tg_code,
            authorization_url=authorization_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "store": store}


@app.patch("/api/zeshun-stores/{authorization_id}")
async def update_zeshun_store(
    authorization_id: int,
    payload: ZeshunStoreUpdateRequest,
) -> dict:
    existing = database.get_zeshun_authorization(authorization_id)
    if not existing:
        raise HTTPException(status_code=404, detail="授权店铺不存在")
    authorization_url = task_service.build_zeshun_authorization_url(
        existing["tg_code"],
        payload.authorization_url,
    )
    store = database.update_zeshun_authorization(
        authorization_id,
        alias=payload.alias,
        authorization_url=authorization_url,
    )
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
    return {
        "ok": True,
        "created": created,
        "authorization": authorization,
        "store": store,
    }


@app.delete("/api/zeshun-stores/{authorization_id}")
async def delete_zeshun_store(authorization_id: int) -> dict:
    if not database.delete_zeshun_authorization(authorization_id):
        raise HTTPException(status_code=404, detail="授权店铺不存在")
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
    return {"ok": True, "created": created, "store": store}


@app.patch("/api/stores/{store_id}")
async def update_store(store_id: int, payload: StoreUpdateRequest) -> dict:
    store = database.update_store_alias(store_id, payload.alias)
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
    return {"ok": True, "store": store}


@app.delete("/api/stores/{store_id}")
async def delete_store(store_id: int) -> dict:
    if not database.delete_store(store_id):
        raise HTTPException(status_code=404, detail="店铺不存在")
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
