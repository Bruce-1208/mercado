from __future__ import annotations

import atexit
import asyncio
import hashlib
import json
import multiprocessing
import os
import re
from concurrent.futures import ProcessPoolExecutor
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from functools import partial
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from yandex.app.config import settings
from yandex.app.product_media import normalize_product_pictures
from yandex.app.schemas import ProductRecord


ProgressCallback = Callable[[int, int, str], Awaitable[None]]
ProductCallback = Callable[[ProductRecord], Awaitable[None]]
FOREIGN_MARKERS = (
    "из-за рубежа",
    "доставка из-за рубежа",
    "товар из-за рубежа",
    "international delivery",
    "from abroad",
)
PRODUCT_PATH_RE = re.compile(r"/(?:product--|product/|card/)", re.IGNORECASE)
PRICE_RE = re.compile(r"(\d[\d\s\u00a0\u2009]*(?:[,.]\d{1,2})?)\s*(?:₽|руб)", re.IGNORECASE)
MAX_SEARCH_PAGES = 100
MAX_LAZY_SCROLL_STEPS = 30
LAZY_SCROLL_WAIT_MS = 650
LAZY_STABLE_BOTTOM_ROUNDS = 3


class ScraperError(RuntimeError):
    pass


class CaptchaRequired(ScraperError):
    pass


@dataclass(slots=True)
class ListingCandidate:
    url: str
    name: str
    price: float | None
    old_price: float | None
    image: str
    card_text: str
    foreign_evidence: str
    raw: dict[str, Any]


def compact_space(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d,.]", "", str(value)).replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def price_from_text(text: str) -> float | None:
    match = PRICE_RE.search(text or "")
    if not match:
        return None
    return parse_number(match.group(1).replace(" ", "").replace("\u00a0", ""))


def normalize_product_url(url: str) -> str:
    absolute = urljoin(settings.market_base_url, url)
    parsed = urlparse(absolute)
    query = parse_qs(parsed.query)
    keep: dict[str, str] = {}
    # sku 有时代表具体变体，保留它；其余跟踪参数删除。
    if query.get("sku"):
        keep["sku"] = query["sku"][0]
    return urlunparse(("https", "market.yandex.ru", parsed.path.rstrip("/"), "", urlencode(keep), ""))


def search_page_url(search_url: str, page_number: int) -> str:
    """Build an explicit Yandex search page URL without duplicating ``page``."""
    parsed = urlparse(search_url)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "page"]
    if int(page_number) > 1:
        query.append(("page", str(int(page_number))))
    return urlunparse(parsed._replace(query=urlencode(query)))


def market_sku_from_values(url: str, *values: Any) -> int | None:
    parsed = urlparse(url)
    query_sku = parse_qs(parsed.query).get("sku", [None])[0]
    candidates: list[Any] = [query_sku, *values]
    path_match = re.search(r"/(\d{5,})(?:/)?$", parsed.path)
    if path_match:
        candidates.append(path_match.group(1))
    for candidate in candidates:
        if candidate is None:
            continue
        match = re.search(r"\d{5,}", str(candidate))
        if match:
            with suppress(ValueError):
                return int(match.group())
    return None


def category_id_from_html(html: str) -> int | None:
    patterns = (
        r'"marketCategoryId"\s*:\s*"?(\d+)"?',
        r'"categoryId"\s*:\s*"?(\d+)"?',
        r'"hid"\s*:\s*"?(\d+)"?',
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    return None


def category_id_from_breadcrumbs(breadcrumbs: list[dict[str, str]]) -> int | None:
    for item in reversed(breadcrumbs):
        href = item.get("href", "")
        parsed = urlparse(href)
        hid = parse_qs(parsed.query).get("hid", [None])[0]
        if hid and str(hid).isdigit() and int(hid) > 0:
            return int(hid)
    return None


def make_offer_id(market_sku: int | None, url: str) -> str:
    suffix = str(market_sku) if market_sku else hashlib.sha256(url.encode("utf-8")).hexdigest()[:18]
    return f"YM-CB-{suffix}"


def foreign_evidence(text: str) -> str:
    lowered = compact_space(text).lower()
    for marker in FOREIGN_MARKERS:
        if marker in lowered:
            return marker
    return ""


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if isinstance(value, dict):
            value = value.get("name") or value.get("value")
        if isinstance(value, list):
            value = value[0] if value else ""
        normalized = compact_space(str(value)) if value is not None else ""
        if normalized:
            return normalized
    return ""


def _jsonld_product(items: list[Any]) -> dict[str, Any]:
    queue = list(items)
    while queue:
        item = queue.pop(0)
        if isinstance(item, list):
            queue.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        graph = item.get("@graph")
        if isinstance(graph, list):
            queue.extend(graph)
        item_type = item.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if any(str(value).lower() == "product" for value in types):
            return item
    return {}


class YandexMarketScraper:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._context: BrowserContext | None = None

    async def close(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _get_context(self) -> BrowserContext:
        if self._context:
            return self._context
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        profile_dir = settings.data_dir / "browser-profile"
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=settings.headless,
                locale=settings.locale,
                timezone_id=settings.timezone,
                viewport={"width": 1440, "height": 980},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            await self._playwright.stop()
            self._playwright = None
            raise ScraperError(
                "Chromium 无法启动。请先运行：python -m playwright install chromium"
            ) from exc
        return self._context

    async def _handle_captcha(self, page: Page, progress: ProgressCallback) -> None:
        if "showcaptcha" not in page.url.lower():
            return
        if settings.headless:
            raise CaptchaRequired("Yandex 要求验证码；请将 YANDEX_HEADLESS=false 后重试")
        await progress(0, 0, "Yandex 要求验证，请在已打开的浏览器中手动完成验证码（最多等待 5 分钟）")
        for _ in range(150):
            await asyncio.sleep(2)
            if "showcaptcha" not in page.url.lower():
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                return
        raise CaptchaRequired("等待验证码超时，请完成验证后重新搜索")

    async def _extract_listing_candidates(self, page: Page) -> list[ListingCandidate]:
        raw_items: list[dict[str, Any]] = await page.evaluate(
            r"""
            () => {
              const productRe = /\/(?:product--|product\/|card\/)/i;
              const anchors = Array.from(document.querySelectorAll('a[href]'))
                .filter(a => productRe.test(a.getAttribute('href') || ''));
              const seen = new Set();
              const result = [];
              for (const anchor of anchors) {
                const href = anchor.href;
                if (!href || seen.has(href)) continue;
                const card = anchor.closest(
                  '[data-zone-name="productSnippet"], [data-auto="snippet"], article, [data-baobab-name="productSnippet"]'
                ) || anchor.parentElement?.parentElement?.parentElement || anchor;
                const text = (card.innerText || '').replace(/\s+/g, ' ').trim();
                const titleNode = card.querySelector(
                  '[data-auto="snippet-title"], [data-zone-name="title"], h3, h2'
                );
                const image = card.querySelector('img');
                const title = (
                  titleNode?.textContent || anchor.title || anchor.getAttribute('aria-label') || image?.alt || ''
                ).replace(/\s+/g, ' ').trim();
                if (!title || title.length < 3) continue;
                const priceNode = card.querySelector(
                  '[data-auto="snippet-price-current"], [data-auto="price-value"], [data-zone-name="price"]'
                );
                const oldPriceNode = card.querySelector(
                  '[data-auto="snippet-price-old"], [data-auto="price-old"]'
                );
                seen.add(href);
                result.push({
                  url: href,
                  name: title,
                  cardText: text,
                  priceText: priceNode?.textContent || text,
                  oldPriceText: oldPriceNode?.textContent || '',
                  image: image?.currentSrc || image?.src || '',
                  dataAuto: card.getAttribute('data-auto') || '',
                  zoneName: card.getAttribute('data-zone-name') || ''
                });
              }
              return result;
            }
            """
        )
        candidates: list[ListingCandidate] = []
        for item in raw_items:
            evidence = foreign_evidence(item.get("cardText", ""))
            candidates.append(
                ListingCandidate(
                    url=normalize_product_url(item["url"]),
                    name=compact_space(item.get("name")),
                    price=price_from_text(item.get("priceText", "")),
                    old_price=price_from_text(item.get("oldPriceText", "")),
                    image=item.get("image", ""),
                    card_text=item.get("cardText", ""),
                    foreign_evidence=evidence,
                    raw=item,
                )
            )
        return candidates

    async def _collect_lazy_listing_candidates(
        self,
        page: Page,
    ) -> list[ListingCandidate]:
        """Progressively scroll one result page and retain virtualized cards.

        Yandex can remove cards that have left the viewport, so inspecting only
        the final DOM loses earlier results.  This method merges every snapshot
        by canonical product URL and waits for both DOM/height growth at the
        bottom before declaring the page stable.
        """
        merged: dict[str, ListingCandidate] = {}
        stable_bottom_rounds = 0

        for _ in range(MAX_LAZY_SCROLL_STEPS):
            snapshot = await self._extract_listing_candidates(page)
            for candidate in snapshot:
                previous = merged.get(candidate.url)
                if (
                    previous is None
                    or (candidate.foreign_evidence and not previous.foreign_evidence)
                    or len(candidate.card_text) >= len(previous.card_text)
                ):
                    merged[candidate.url] = candidate

            metrics: dict[str, Any] = await page.evaluate(
                r"""
                () => {
                  const root = document.scrollingElement || document.documentElement;
                  const viewport = Math.max(window.innerHeight || 0, 600);
                  return {
                    top: root.scrollTop || window.scrollY || 0,
                    height: root.scrollHeight || document.body.scrollHeight || 0,
                    viewport,
                    productLinks: document.querySelectorAll(
                      'a[href*="/product--"], a[href*="/product/"], a[href*="/card/"]'
                    ).length
                  };
                }
                """
            )
            before_count = len(merged)
            before_height = int(metrics.get("height") or 0)
            before_links = int(metrics.get("productLinks") or 0)
            viewport = int(metrics.get("viewport") or 600)
            top = int(metrics.get("top") or 0)
            at_bottom = top + viewport >= before_height - 120

            if at_bottom:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                # Give the intersection observer/network request a chance to
                # append another lazy chunk. A timeout is expected at the real
                # end of the page.
                with suppress(PlaywrightTimeoutError):
                    await page.wait_for_function(
                        r"""
                        ({height, links}) => {
                          const root = document.scrollingElement || document.documentElement;
                          const currentLinks = document.querySelectorAll(
                            'a[href*="/product--"], a[href*="/product/"], a[href*="/card/"]'
                          ).length;
                          return root.scrollHeight > height || currentLinks > links;
                        }
                        """,
                        arg={"height": before_height, "links": before_links},
                        timeout=2_500,
                    )
                await page.wait_for_timeout(LAZY_SCROLL_WAIT_MS)
            else:
                await page.evaluate(
                    "distance => window.scrollBy(0, distance)",
                    max(600, int(viewport * 0.82)),
                )
                await page.wait_for_timeout(LAZY_SCROLL_WAIT_MS)

            after_snapshot = await self._extract_listing_candidates(page)
            for candidate in after_snapshot:
                previous = merged.get(candidate.url)
                if (
                    previous is None
                    or (candidate.foreign_evidence and not previous.foreign_evidence)
                    or len(candidate.card_text) >= len(previous.card_text)
                ):
                    merged[candidate.url] = candidate
            after_metrics: dict[str, Any] = await page.evaluate(
                r"""
                () => {
                  const root = document.scrollingElement || document.documentElement;
                  return {
                    top: root.scrollTop || window.scrollY || 0,
                    height: root.scrollHeight || document.body.scrollHeight || 0,
                    viewport: Math.max(window.innerHeight || 0, 600)
                  };
                }
                """
            )
            after_bottom = (
                int(after_metrics.get("top") or 0)
                + int(after_metrics.get("viewport") or 600)
                >= int(after_metrics.get("height") or 0) - 120
            )
            grew = len(merged) > before_count or int(after_metrics.get("height") or 0) > before_height
            if after_bottom and not grew:
                stable_bottom_rounds += 1
                if stable_bottom_rounds == 1:
                    # A small upward nudge retriggers some sticky sentinels.
                    await page.evaluate("window.scrollBy(0, -320)")
                    await page.wait_for_timeout(250)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            else:
                stable_bottom_rounds = 0

            if stable_bottom_rounds >= LAZY_STABLE_BOTTOM_ROUNDS:
                break

        return list(merged.values())

    async def _extract_detail(self, page: Page, listing: ListingCandidate) -> ProductRecord:
        # Yandex 会在首屏加载后再补入描述、相册和完整规格。旧实现只等了搜索页
        # 的固定延时，拿到的经常只是 JSON-LD 中的一张图片和 SEO 摘要。
        for selector in (
            '[data-zone-name="pictureGallery"]',
            '[data-zone-name="description"]',
            '#fullSpecsAnchorId',
        ):
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    await locator.scroll_into_view_if_needed(timeout=3_000)
                    await page.wait_for_timeout(250)
            except Exception:
                # 页面版本或类目模板不一致时继续使用后面的降级数据源。
                pass
        try:
            await page.wait_for_function(
                """() => {
                    const galleries = Array.from(document.querySelectorAll(
                        '[data-auto="media-viewer-thumbnails"]'
                    )).filter(node => node.querySelector('li[role="tab"]'));
                    return Math.max(0, ...galleries.map(
                        node => node.querySelectorAll('img').length
                    )) >= 2;
                }""",
                timeout=8_000,
            )
        except Exception:
            # 有些商品确实只有一张图片，后面的质量门槛会将其标记为待补全。
            pass
        await page.wait_for_timeout(750)

        detail: dict[str, Any] = await page.evaluate(
            r"""
            () => {
              const text = (node) => (node?.textContent || '').replace(/\s+/g, ' ').trim();
              const productRoot = document.querySelector('[data-auto="product-page"]') || document;
              const jsonLd = [];
              for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
                try { jsonLd.push(JSON.parse(script.textContent || '{}')); } catch (_) {}
              }
              const specs = {};
              const specRows = productRoot.querySelectorAll(
                '[data-zone-name="spec"] label, [data-auto="specs-list-minimal"] label, ' +
                'dl, table tr, [data-auto="specification-item"]'
              );
              for (const row of specRows) {
                let key = '';
                let value = '';
                if (row.matches('label')) {
                  const parts = Array.from(row.children).map(text).filter(Boolean);
                  key = parts[0] || '';
                  value = parts.length > 1 ? parts[parts.length - 1] : '';
                } else {
                  const dt = row.querySelector('dt, th, [data-auto="specification-title"]');
                  const dd = row.querySelector('dd, td, [data-auto="specification-value"]');
                  key = text(dt);
                  value = text(dd);
                }
                if (key && value && key.length < 160 && value.length < 1000) specs[key] = value;
              }
              // 只读取商品主相册；全页 img 会混入推荐商品、评论和广告图片。
              const thumbnailGallery = Array.from(document.querySelectorAll(
                '[data-auto="media-viewer-thumbnails"]'
              )).filter(node => node.querySelector('li[role="tab"]')).sort(
                (left, right) => right.querySelectorAll('img').length - left.querySelectorAll('img').length
              )[0];
              const mainGallery = productRoot.querySelector('[data-auto="media-viewer-gallery"]');
              const imageNodes = thumbnailGallery
                ? thumbnailGallery.querySelectorAll('img')
                : (mainGallery ? mainGallery.querySelectorAll('img') : []);
              const images = Array.from(imageNodes)
                .map(img => img.currentSrc || img.src || '')
                .filter(src => /^https?:\/\//.test(src) && !/avatar|logo|icon|sprite/i.test(src));
              const breadcrumbs = Array.from(document.querySelectorAll(
                '[data-zone-name="categoryPath"] a, [data-auto="category-link"], ' +
                '[data-auto="breadcrumb"] a, nav[aria-label*="breadcrumb" i] a'
              )).map(a => ({name: text(a), href: a.href || ''})).filter(item => item.name);
              const descriptionNode = productRoot.querySelector(
                '[data-zone-name="description"], [data-baobab-name="description"]'
              );
              return {
                title: text(document.querySelector('h1')),
                description: text(descriptionNode),
                metaDescription: document.querySelector('meta[name="description"]')?.content || '',
                ogImage: document.querySelector('meta[property="og:image"]')?.content || '',
                bodyText: text(document.body).slice(0, 120000),
                jsonLd,
                specifications: specs,
                images: [...new Set(images)].slice(0, 30),
                breadcrumbs
              };
            }
            """
        )
        # React 有时会在第一次详情快照之后才把其余相册缩略图和完整规格补进 DOM。
        # 二次快照只读取商品主相册（li[role=tab]），避免混入评论晒图。
        html = await page.content()
        await page.wait_for_timeout(500)
        late_detail: dict[str, Any] = await page.evaluate(
            r"""
            () => {
              const text = (node) => (node?.textContent || '').replace(/\s+/g, ' ').trim();
              const galleries = Array.from(document.querySelectorAll(
                '[data-auto="media-viewer-thumbnails"]'
              )).filter(node => node.querySelector('li[role="tab"]')).sort(
                (left, right) => right.querySelectorAll('img').length - left.querySelectorAll('img').length
              );
              const images = Array.from(galleries[0]?.querySelectorAll('img') || [])
                .map(img => img.currentSrc || img.src || '')
                .filter(src => /^https?:\/\//.test(src));
              const specifications = {};
              for (const row of document.querySelectorAll(
                '[data-zone-name="spec"] label, [data-auto="specs-list-minimal"] label'
              )) {
                const parts = Array.from(row.children).map(text).filter(Boolean);
                const key = parts[0] || '';
                const value = parts.length > 1 ? parts[parts.length - 1] : '';
                if (key && value && key.length < 160 && value.length < 1000) {
                  specifications[key] = value;
                }
              }
              return {
                description: text(document.querySelector(
                  '[data-zone-name="description"], [data-baobab-name="description"]'
                )),
                images: [...new Set(images)].slice(0, 30),
                specifications
              };
            }
            """
        )
        detail["images"] = list(
            dict.fromkeys([*(detail.get("images") or []), *(late_detail.get("images") or [])])
        )[:30]
        if len(late_detail.get("description") or "") > len(detail.get("description") or ""):
            detail["description"] = late_detail["description"]
        detail.setdefault("specifications", {}).update(late_detail.get("specifications") or {})
        json_product = _jsonld_product(detail.get("jsonLd", []))
        offers = json_product.get("offers") if isinstance(json_product.get("offers"), dict) else {}
        aggregate = (
            json_product.get("aggregateRating")
            if isinstance(json_product.get("aggregateRating"), dict)
            else {}
        )
        brand = json_product.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")

        detail_text = detail.get("bodyText", "")
        evidence = listing.foreign_evidence or foreign_evidence(detail_text)
        sku = market_sku_from_values(
            page.url,
            json_product.get("sku"),
            json_product.get("productID"),
            re.search(r'"marketSku"\s*:\s*"?(\d+)"?', html).group(1)
            if re.search(r'"marketSku"\s*:\s*"?(\d+)"?', html)
            else None,
        )
        pictures: list[str] = []
        for value in (
            detail.get("images", []),
            json_product.get("image") if isinstance(json_product.get("image"), list) else [json_product.get("image")],
            [detail.get("ogImage")],
            [listing.image],
        ):
            for image in value:
                if isinstance(image, str) and image.startswith(("http://", "https://")) and image not in pictures:
                    pictures.append(image)
                if len(pictures) >= 30:
                    break

        pictures = normalize_product_pictures(pictures)
        price = parse_number(offers.get("price")) or listing.price
        name = _first_nonempty(json_product.get("name"), detail.get("title"), listing.name)
        description = _first_nonempty(
            detail.get("description"),
            json_product.get("description"),
            detail.get("metaDescription"),
            name,
        )
        breadcrumbs = detail.get("breadcrumbs", [])
        last_breadcrumb = breadcrumbs[-1].get("name", "") if breadcrumbs else ""
        category_name = _first_nonempty(json_product.get("category"), last_breadcrumb)
        specifications = detail.get("specifications", {})
        vendor = _first_nonempty(
            brand,
            json_product.get("manufacturer"),
            specifications.get("Бренд"),
            specifications.get("Производитель"),
        )
        raw_data = {
            "listing": listing.raw,
            "jsonLd": json_product,
            "breadcrumbs": breadcrumbs,
            "capturedFrom": page.url,
            "collectionQuality": {
                "descriptionLength": len(description),
                "pictureCount": len(pictures),
                "specificationCount": len(specifications),
            },
        }
        return ProductRecord(
            source_url=normalize_product_url(page.url),
            market_sku=sku,
            offer_id=make_offer_id(sku, page.url),
            name=name,
            description=description[:6000],
            vendor=vendor,
            vendor_code=_first_nonempty(
                json_product.get("mpn"),
                specifications.get("Артикул производителя"),
                specifications.get("Артикул"),
            ),
            category_name=category_name,
            market_category_id=category_id_from_breadcrumbs(breadcrumbs)
            or category_id_from_html(html),
            price=price,
            old_price=listing.old_price,
            currency=_first_nonempty(offers.get("priceCurrency"), "RUR").replace("RUB", "RUR"),
            pictures=pictures,
            specifications=specifications,
            seller_name=_first_nonempty(offers.get("seller")),
            rating=parse_number(aggregate.get("ratingValue")),
            reviews_count=int(parse_number(aggregate.get("reviewCount")) or 0) or None,
            is_foreign=bool(evidence),
            foreign_evidence=evidence,
            raw_data=raw_data,
        )

    async def scrape(
        self,
        keyword: str,
        count: int,
        *,
        progress: ProgressCallback,
        on_product: ProductCallback | None = None,
    ) -> list[ProductRecord]:
        async with self._lock:
            context = await self._get_context()
            search_page = await context.new_page()
            detail_page = await context.new_page()
            executor: ProcessPoolExecutor | None = None
            results: list[ProductRecord] = []
            seen_urls: set[str] = set()
            inspected_urls: set[str] = set()
            try:
                search_url = f"{settings.market_base_url}/search?text={quote(keyword)}"
                await progress(0, 0, "正在打开 Yandex Market 搜索页")
                await search_page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
                await self._handle_captcha(search_page, progress)
                worker_count = min(settings.scraper_processes, max(count, 1))
                storage_state = await context.storage_state()
                executor = ProcessPoolExecutor(
                    max_workers=worker_count,
                    mp_context=multiprocessing.get_context("spawn"),
                    initializer=_detail_worker_initialize,
                    initargs=(
                        storage_state,
                        settings.worker_headless,
                        settings.locale,
                        settings.timezone,
                        settings.request_delay_ms,
                    ),
                )
                candidate_pool: dict[str, ListingCandidate] = {}
                page_number = 1
                page_collected = False
                pages_without_new_urls = 0

                while (
                    len(results) < count
                    and page_number <= MAX_SEARCH_PAGES
                    and pages_without_new_urls < 2
                ):
                    if not page_collected:
                        await search_page.bring_to_front()
                        before_pool_size = len(candidate_pool)
                        page_candidates = await self._collect_lazy_listing_candidates(search_page)
                        for candidate in page_candidates:
                            previous = candidate_pool.get(candidate.url)
                            if (
                                previous is None
                                or (candidate.foreign_evidence and not previous.foreign_evidence)
                                or len(candidate.card_text) >= len(previous.card_text)
                            ):
                                candidate_pool[candidate.url] = candidate
                        new_url_count = len(candidate_pool) - before_pool_size
                        if new_url_count:
                            pages_without_new_urls = 0
                        else:
                            pages_without_new_urls += 1
                        page_collected = True
                        await progress(
                            len(results),
                            len(inspected_urls),
                            f"第 {page_number} 页懒加载完成，识别 {len(page_candidates)} 个商品卡片，新增 {new_url_count} 个",
                        )

                    candidates = list(candidate_pool.values())
                    unseen = [item for item in candidates if item.url not in inspected_urls]
                    hinted = [item for item in unseen if item.foreign_evidence]
                    unhinted = [item for item in unseen if not item.foreign_evidence]
                    # 国外标记通常就在搜索卡片上。每轮只抽查少量无标记卡片，
                    # 兼容标记只出现在详情页的情况，同时避免无界访问。
                    new_candidates = (hinted + unhinted[:8])[
                        : max(count - len(results), worker_count)
                    ]
                    if not new_candidates:
                        if pages_without_new_urls >= 2 or page_number >= MAX_SEARCH_PAGES:
                            break
                        page_number += 1
                        page_collected = False
                        await progress(
                            len(results),
                            len(inspected_urls),
                            f"正在打开 Yandex 搜索结果第 {page_number} 页",
                        )
                        await search_page.goto(
                            search_page_url(search_url, page_number),
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                        await self._handle_captcha(search_page, progress)
                        continue

                    batch_start_scanned = len(inspected_urls)
                    for listing in new_candidates:
                        inspected_urls.add(listing.url)
                    if new_candidates:
                        await progress(
                            len(results),
                            batch_start_scanned,
                            f"正在用 {worker_count} 个进程并行核对 {len(new_candidates)} 个商品",
                        )

                    async def collect_in_worker(
                        listing: ListingCandidate,
                    ) -> tuple[ListingCandidate, dict[str, Any]]:
                        assert executor is not None
                        loop = asyncio.get_running_loop()
                        try:
                            outcome = await loop.run_in_executor(
                                executor,
                                _detail_worker_extract,
                                asdict(listing),
                            )
                        except Exception as exc:
                            outcome = {
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:500],
                            }
                        return listing, outcome

                    tasks = [
                        asyncio.create_task(collect_in_worker(listing))
                        for listing in new_candidates
                    ]
                    completed_in_batch = 0
                    for completed_task in asyncio.as_completed(tasks):
                        listing, outcome = await completed_task
                        completed_in_batch += 1
                        product: ProductRecord | None = None
                        if outcome.get("product"):
                            with suppress(Exception):
                                worker_product = ProductRecord.model_validate(outcome["product"])
                                if not worker_product.missing_publish_fields:
                                    product = worker_product

                        # 子进程遇到验证码、浏览器崩溃、页面超时或缺少真正必需的上传字段时，
                        # 用主浏览器重试一次。图片数量和描述字数不再触发重复采集。
                        if product is None and len(results) < count:
                            await progress(
                                len(results),
                                batch_start_scanned + completed_in_batch,
                                f"子进程采集失败，正在主进程重试：{listing.name[:40]}",
                            )
                            try:
                                await detail_page.goto(
                                    listing.url,
                                    wait_until="domcontentloaded",
                                    timeout=60_000,
                                )
                                await self._handle_captcha(detail_page, progress)
                                await detail_page.wait_for_timeout(500)
                                product = await self._extract_detail(detail_page, listing)
                            except PlaywrightTimeoutError:
                                product = None

                        await progress(
                            len(results),
                            batch_start_scanned + completed_in_batch,
                            f"多进程已核对 {completed_in_batch}/{len(new_candidates)} 个商品",
                        )
                        if product is None:
                            continue
                        if not product.is_foreign or product.source_url in seen_urls:
                            continue
                        if len(results) >= count:
                            continue
                        results.append(product)
                        seen_urls.add(product.source_url)
                        if on_product:
                            await on_product(product)
                        await progress(len(results), len(inspected_urls), f"已找到 {len(results)}/{count} 个国外商品")

                    if len(results) >= count:
                        break

                if not results:
                    raise ScraperError(
                        "没有识别到带“Из-за рубежа”标记的商品。可能是关键词无结果、地区设置不同，或页面结构已变化。"
                    )
                return results
            finally:
                if executor:
                    await asyncio.to_thread(
                        partial(executor.shutdown, wait=True, cancel_futures=True)
                    )
                await detail_page.close()
                await search_page.close()


_WORKER_LOOP: asyncio.AbstractEventLoop | None = None
_WORKER_PLAYWRIGHT: Any = None
_WORKER_BROWSER: Any = None
_WORKER_CONTEXT: BrowserContext | None = None
_WORKER_SCRAPER: YandexMarketScraper | None = None
_WORKER_REQUEST_DELAY_MS = 0


def _detail_worker_initialize(
    storage_state: dict[str, Any],
    headless: bool,
    locale: str,
    timezone_id: str,
    request_delay_ms: int,
) -> None:
    """Initialize one reusable Chromium instance inside each spawned process."""
    global _WORKER_LOOP, _WORKER_PLAYWRIGHT, _WORKER_BROWSER
    global _WORKER_CONTEXT, _WORKER_SCRAPER, _WORKER_REQUEST_DELAY_MS

    _WORKER_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_WORKER_LOOP)
    _WORKER_PLAYWRIGHT = _WORKER_LOOP.run_until_complete(async_playwright().start())
    _WORKER_BROWSER = _WORKER_LOOP.run_until_complete(
        _WORKER_PLAYWRIGHT.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-minimized",
            ],
        )
    )
    _WORKER_CONTEXT = _WORKER_LOOP.run_until_complete(
        _WORKER_BROWSER.new_context(
            storage_state=storage_state,
            locale=locale,
            timezone_id=timezone_id,
            viewport={"width": 1440, "height": 980},
        )
    )
    _WORKER_SCRAPER = YandexMarketScraper()
    _WORKER_REQUEST_DELAY_MS = max(int(request_delay_ms), 200)
    atexit.register(_detail_worker_shutdown)


async def _detail_worker_close_async() -> None:
    global _WORKER_PLAYWRIGHT, _WORKER_BROWSER, _WORKER_CONTEXT
    if _WORKER_CONTEXT:
        with suppress(Exception):
            await _WORKER_CONTEXT.close()
        _WORKER_CONTEXT = None
    if _WORKER_BROWSER:
        with suppress(Exception):
            await _WORKER_BROWSER.close()
        _WORKER_BROWSER = None
    if _WORKER_PLAYWRIGHT:
        with suppress(Exception):
            await _WORKER_PLAYWRIGHT.stop()
        _WORKER_PLAYWRIGHT = None


def _detail_worker_shutdown() -> None:
    global _WORKER_LOOP
    if _WORKER_LOOP and not _WORKER_LOOP.is_closed():
        with suppress(Exception):
            _WORKER_LOOP.run_until_complete(_detail_worker_close_async())
        _WORKER_LOOP.close()
    _WORKER_LOOP = None


async def _detail_worker_extract_async(payload: dict[str, Any]) -> dict[str, Any]:
    if _WORKER_CONTEXT is None or _WORKER_SCRAPER is None:
        return {"error_type": "WorkerNotReady", "error": "采集子进程未完成初始化"}
    listing = ListingCandidate(**payload)
    page = await _WORKER_CONTEXT.new_page()
    try:
        await page.goto(listing.url, wait_until="domcontentloaded", timeout=60_000)
        if "showcaptcha" in page.url.lower():
            return {
                "worker_pid": os.getpid(),
                "error_type": "CaptchaRequired",
                "error": "子进程遇到 Yandex 验证码",
            }
        await page.wait_for_timeout(500)
        product = await _WORKER_SCRAPER._extract_detail(page, listing)
        return {"worker_pid": os.getpid(), "product": product.model_dump(mode="json")}
    except PlaywrightTimeoutError as exc:
        return {
            "worker_pid": os.getpid(),
            "error_type": "PlaywrightTimeoutError",
            "error": str(exc)[:500],
        }
    except Exception as exc:
        return {
            "worker_pid": os.getpid(),
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
    finally:
        with suppress(Exception):
            await page.close()
        await asyncio.sleep(_WORKER_REQUEST_DELAY_MS / 1000)


def _detail_worker_extract(payload: dict[str, Any]) -> dict[str, Any]:
    if _WORKER_LOOP is None or _WORKER_LOOP.is_closed():
        return {"error_type": "WorkerNotReady", "error": "采集子进程事件循环不可用"}
    return _WORKER_LOOP.run_until_complete(_detail_worker_extract_async(payload))


scraper = YandexMarketScraper()
