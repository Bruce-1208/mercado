"""Browser collector for Mercado Libre result pages and the ZYing page plugin.

The collector intentionally reads package data from the plugin injected into a
Mercado Libre *detail page*.  It never opens or queries ZYing's product library.
"""

from __future__ import annotations

import json
import io
import os
import re
import threading
import time
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urljoin, urlparse

from erp.mercadolibre_follow_sell import extract_item_id


DEFAULT_ZYING_WINDOW_ID = os.environ.get(
    "BIT_ZYING_WINDOW_ID",
    "9812f185f7ab49d98f3988994d9e8ebf",
)
DEFAULT_BROWSER_MODE = os.environ.get(
    "MERCADO_COLLECTION_BROWSER",
    "playwright",
).strip().lower()
MAX_COLLECTION_COUNT = 500
DEFAULT_COLLECTION_WORKERS = 4
MAX_COLLECTION_WORKERS = 6
MAX_LISTING_PAGES = 100
MERCADO_HOST_PATTERN = re.compile(
    r"(^|\.)(mercadolibre\.[a-z.]+|mercadolivre\.com\.br)$", re.IGNORECASE
)
DIMENSION_PATTERN = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:cm|厘米)?\s*[x×X*]\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:cm|厘米)?\s*[x×X*]\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:cm|厘米)?",
    re.IGNORECASE,
)
VOLUMETRIC_WEIGHT_PATTERN = re.compile(
    r"(?:计\s*抛|抛\s*重|体积\s*重)[^\d]{0,24}(\d+(?:[.,]\d+)?)\s*(kg|公斤|千克|g|克)\b",
    re.IGNORECASE,
)
ACTUAL_WEIGHT_PATTERN = re.compile(
    r"(?:商品\s*)?(?:重量|毛重|净重)[^\d]{0,24}(\d+(?:[.,]\d+)?)\s*(kg|公斤|千克|g|克)\b",
    re.IGNORECASE,
)


class CollectionStopped(RuntimeError):
    """Raised when a running collection task is cancelled."""


def extract_listing_item_id(value: str) -> str:
    """Prefer a seller ``item_id`` over the catalog ID in a product URL."""
    decoded = unquote(str(value or ""))
    match = re.search(
        r"(?:item_id|itemId)\s*[:=]\s*((?:ML[A-Z]|CBT)-?\d+)",
        decoded,
        re.IGNORECASE,
    )
    return extract_item_id(match.group(1) if match else decoded)


def validate_collection_request(source_url: str, requested_count: int) -> tuple[str, int]:
    url = str(source_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("请输入有效的 Mercado Libre 列表链接")
    if not MERCADO_HOST_PATTERN.search(parsed.hostname):
        raise ValueError("链接必须来自 Mercado Libre 商品列表或搜索页面")
    try:
        count = int(requested_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("采集数量必须是整数") from exc
    if count < 1 or count > MAX_COLLECTION_COUNT:
        raise ValueError(f"采集数量必须在 1-{MAX_COLLECTION_COUNT} 之间")
    return url, count


def normalize_collection_workers(value: Any) -> int:
    try:
        workers = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("并发数必须是整数") from exc
    if workers < 1 or workers > MAX_COLLECTION_WORKERS:
        raise ValueError(f"并发数必须在 1-{MAX_COLLECTION_WORKERS} 之间")
    return workers


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    raw = re.sub(r"[^0-9,.-]", "", str(value)).strip()
    if not raw:
        return None
    if raw.count(",") == 1 and "." not in raw:
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        return float(Decimal(raw))
    except (InvalidOperation, ValueError):
        return None


def _normalize_image_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url or url.startswith(("data:", "blob:")):
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    return url


def calculate_volumetric_weight_kg(
    length_cm: Any, width_cm: Any, height_cm: Any
) -> float | None:
    values = [_number(value) for value in (length_cm, width_cm, height_cm)]
    if any(value is None for value in values):
        return None
    length, width, height = values
    return round(float(length * width * height / 6000), 4)


def parse_plugin_metrics(text: str) -> dict[str, Any]:
    """Parse dimensions and volumetric weight from ZYing plugin OCR text."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    dimensions = DIMENSION_PATTERN.search(normalized)
    volumetric_match = VOLUMETRIC_WEIGHT_PATTERN.search(normalized)
    # Remove the volumetric phrase before looking for actual weight so
    # "体积重量" cannot be mistaken for the product's physical weight.
    actual_weight_text = VOLUMETRIC_WEIGHT_PATTERN.sub(" ", normalized)
    weight = ACTUAL_WEIGHT_PATTERN.search(actual_weight_text)
    result: dict[str, Any] = {
        "ocr_text": normalized,
        "package_length_cm": None,
        "package_width_cm": None,
        "package_height_cm": None,
        "weight_g": None,
        "volumetric_weight_kg": None,
        "weight_basis": "plugin_actual",
    }
    if dimensions:
        length = _number(dimensions.group(1))
        width = _number(dimensions.group(2))
        height = _number(dimensions.group(3))
        result.update(
            package_length_cm=length,
            package_width_cm=width,
            package_height_cm=height,
            dimensions_display=" × ".join(dimensions.groups()) + " cm",
            volumetric_weight_kg=calculate_volumetric_weight_kg(length, width, height),
        )
    if weight:
        value = _number(weight.group(1))
        unit = weight.group(2).lower()
        if value is not None:
            result["weight_g"] = value * 1000 if unit in ("kg", "公斤", "千克") else value
            result["weight_display"] = f"{weight.group(1)} {weight.group(2)}"
    if volumetric_match:
        result["plugin_volumetric_display"] = (
            f"{volumetric_match.group(1)} {volumetric_match.group(2)}"
        )
    return result


def ocr_plugin_image(image_bytes: bytes) -> dict[str, Any]:
    """Run OCR over a plugin screenshot and return recognized text/confidence."""
    if not image_bytes:
        return {"text": "", "confidence": None, "lines": []}
    import cv2
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("无法读取智赢插件截图")
    result, _ = RapidOCR()(image)
    lines: list[dict[str, Any]] = []
    for item in result or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        confidence = float(item[2]) if len(item) > 2 else None
        lines.append({"text": str(item[1]), "confidence": confidence})
    text = " ".join(line["text"] for line in lines)
    scores = [line["confidence"] for line in lines if line["confidence"] is not None]
    return {
        "text": text,
        "confidence": round(sum(scores) / len(scores), 4) if scores else None,
        "lines": lines,
    }


def merge_listing_candidates(
    existing: list[dict[str, Any]], rows: Iterable[Mapping[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Append new cards while retaining list order and de-duplicating item IDs."""
    seen = {str(row.get("source_item_id") or "") for row in existing}
    for source_row in rows or []:
        row = dict(source_row)
        href = str(row.get("source_url") or row.get("href") or "").strip()
        try:
            item_id = extract_listing_item_id(str(row.get("source_item_id") or href))
        except ValueError:
            continue
        if item_id in seen:
            continue
        seen.add(item_id)
        existing.append(
            {
                "source_item_id": item_id,
                "source_url": href,
                "title": str(row.get("title") or "").strip(),
                "main_image_url": _normalize_image_url(row.get("main_image_url")),
                "price": _number(row.get("price")),
                "currency_id": str(row.get("currency_id") or "MXN"),
            }
        )
        if len(existing) >= limit:
            break
    return existing


_LISTING_PAGE_SCRIPT = r"""
const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
const imageUrl = img => {
  if (!img) return '';
  const srcset = img.getAttribute('srcset') || '';
  return img.currentSrc || img.getAttribute('data-src') || img.getAttribute('data-srcset') ||
    (srcset ? srcset.trim().split(/\s+/)[0] : '') || img.getAttribute('src') || '';
};
const itemPattern = /(?:ML[A-Z]|CBT)-?\d+/i;
const cards = Array.from(document.querySelectorAll(
  'li.ui-search-layout__item, .ui-search-result, .poly-card, [data-testid="result"]'
));
let roots = cards.length ? cards : Array.from(document.querySelectorAll('a[href]')).filter(a => itemPattern.test(a.href));
const rows = [];
for (const root of roots) {
  const link = root.matches && root.matches('a[href]') ? root : root.querySelector(
    'a.poly-component__title, a.ui-search-link, a[href*="item_id="], a[href*="/MLM-"], a[href*="/MLB-"]'
  );
  if (!link || !itemPattern.test(link.href)) continue;
  const titleNode = root.querySelector && root.querySelector(
    '.poly-component__title, .ui-search-item__title, h2, h3'
  );
  const img = root.querySelector && root.querySelector('img');
  const fraction = root.querySelector && root.querySelector('.andes-money-amount__fraction');
  const cents = root.querySelector && root.querySelector('.andes-money-amount__cents');
  let price = fraction ? clean(fraction.textContent).replace(/\D/g, '') : '';
  if (price && cents) price += '.' + clean(cents.textContent).replace(/\D/g, '');
  rows.push({
    href: link.href,
    title: clean((titleNode && titleNode.textContent) || link.textContent),
    main_image_url: imageUrl(img),
    price,
    currency_id: 'MXN'
  });
}
const next = document.querySelector(
  'a[title="Siguiente"], li.andes-pagination__button--next a, a[aria-label="Siguiente"], a[aria-label="Próxima"], a[title="Próxima"]'
);
return {rows, next_url: next ? next.href : '', title: document.title, body: clean(document.body.innerText).slice(0, 1200)};
"""


_DETAIL_PAGE_SCRIPT = r"""
const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
const first = selectors => {
  for (const selector of selectors) {
    const node = document.querySelector(selector);
    if (node) return node;
  }
  return null;
};
let product = {};
for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
  try {
    const parsed = JSON.parse(script.textContent);
    const values = Array.isArray(parsed) ? parsed : (parsed && parsed['@graph'] ? parsed['@graph'] : [parsed]);
    const match = values.find(row => row && (row['@type'] === 'Product' || (Array.isArray(row['@type']) && row['@type'].includes('Product'))));
    if (match) { product = match; break; }
  } catch (_) {}
}
const h1 = first(['h1.ui-pdp-title', 'h1']);
const metaPrice = document.querySelector('meta[itemprop="price"], meta[property="product:price:amount"]');
const currency = document.querySelector('meta[itemprop="priceCurrency"], meta[property="product:price:currency"]');
const description = first(['.ui-pdp-description__content', '[data-testid="description-content"]', '.ui-pdp-description']);
const pictures = [];
const addPicture = value => {
  value = String(value || '').trim();
  if (value && !value.startsWith('data:') && !pictures.includes(value)) pictures.push(value.startsWith('//') ? 'https:' + value : value);
};
const structuredImages = Array.isArray(product.image) ? product.image : [product.image];
structuredImages.forEach(addPicture);
document.querySelectorAll('.ui-pdp-gallery img, figure img, img.ui-pdp-image').forEach(img => addPicture(img.currentSrc || img.getAttribute('data-src') || img.src));
const specs = [];
document.querySelectorAll('.andes-table__row, .ui-pdp-specs__table tr, .ui-vpp-striped-specs__row').forEach(row => {
  const cells = Array.from(row.querySelectorAll('th, td, .andes-table__header, .andes-table__column')).map(node => clean(node.textContent)).filter(Boolean);
  if (cells.length >= 2) specs.push({name: cells[0], value: cells.slice(1).join(' ')});
});
const offer = Array.isArray(product.offers) ? product.offers[0] : (product.offers || {});
const canonical = document.querySelector('link[rel="canonical"]');
return {
  final_url: (canonical && canonical.href) || location.href,
  title: clean((h1 && h1.textContent) || product.name),
  description: clean((description && description.textContent) || product.description),
  price: (metaPrice && metaPrice.content) || offer.price || '',
  currency_id: (currency && currency.content) || offer.priceCurrency || 'MXN',
  pictures,
  specs,
  body: clean(document.body.innerText).slice(0, 1600),
  page_title: document.title
};
"""


_FIND_ZYING_PLUGIN_SCRIPT = r"""
const selectors = ['.zying-meli-detail-wrap', '[class*="zying-meli-detail"]', '[data-zying-meli-detail]'];
const visited = new Set();
function find(root) {
  if (!root || visited.has(root)) return null;
  visited.add(root);
  for (const selector of selectors) {
    try { const match = root.querySelector(selector); if (match) return match; } catch (_) {}
  }
  let nodes = [];
  try { nodes = root.querySelectorAll('*'); } catch (_) { return null; }
  for (const node of nodes) {
    if (node.shadowRoot) {
      const found = find(node.shadowRoot);
      if (found) return found;
    }
  }
  return null;
}
return find(document);
"""


def _check_stop(stop_event: threading.Event | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise CollectionStopped("采集任务已停止")


def _blocked_page_message(url: str, page_text: str) -> str:
    probe = f"{url} {page_text}".lower()
    markers = (
        "account-verification",
        "buyer-login",
        "/captcha/wall",
        "inicia sesión",
        "iniciar sesión",
        "por seguridad, completa este paso",
        "completa este paso",
        "验证码",
        "verifica que eres tú",
    )
    if any(marker in probe for marker in markers):
        return (
            "Mercado 页面进入买家验证页；请确认 Clash 已按 Mercado 域名直连，"
            "并在 Edge 手工完成当前验证后重试"
        )
    return ""


def _wait_ready(driver: Any, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = driver.execute_script("return document.readyState")
            if state in ("interactive", "complete"):
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("页面加载超时")


def _collect_listing_pages(
    driver: Any,
    source_url: str,
    requested_count: int,
    *,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    stop_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    page_url = source_url
    visited_pages: set[str] = set()
    page_number = 0
    while (
        page_url
        and page_url not in visited_pages
        and len(candidates) < requested_count
        and page_number < MAX_LISTING_PAGES
    ):
        _check_stop(stop_event)
        visited_pages.add(page_url)
        page_number += 1
        driver.get(page_url)
        _wait_ready(driver)
        time.sleep(1.0)
        snapshot = driver.execute_script(_LISTING_PAGE_SCRIPT) or {}
        blocked = _blocked_page_message(driver.current_url, snapshot.get("body", ""))
        if blocked:
            raise RuntimeError(blocked)
        before = len(candidates)
        merge_listing_candidates(candidates, snapshot.get("rows") or [], requested_count)
        # A detail URL is also accepted for one-off collection.  This keeps the
        # same workbench form useful when the operator pastes an individual item.
        if len(candidates) == before:
            try:
                current_item_id = extract_listing_item_id(driver.current_url)
            except ValueError:
                current_item_id = ""
            if current_item_id:
                detail_hint = driver.execute_script(
                    """
                    const h1 = document.querySelector('h1.ui-pdp-title, h1');
                    const image = document.querySelector('.ui-pdp-gallery img, figure img, img.ui-pdp-image');
                    return {
                        title: h1 ? String(h1.textContent || '').trim() : '',
                        main_image_url: image ? (image.currentSrc || image.getAttribute('data-src') || image.src || '') : ''
                    };
                    """
                ) or {}
                if detail_hint.get("title"):
                    merge_listing_candidates(
                        candidates,
                        [{
                            "source_item_id": current_item_id,
                            "source_url": driver.current_url,
                            **detail_hint,
                        }],
                        requested_count,
                    )
        if on_page:
            on_page(
                {
                    "page": page_number,
                    "page_url": driver.current_url,
                    "page_items": len(candidates) - before,
                    "candidate_count": len(candidates),
                }
            )
        next_url = urljoin(driver.current_url, str(snapshot.get("next_url") or ""))
        # Some Mercado list pages lazy-load cards or contain repeated promoted
        # items.  A page with no *new* IDs must not stop pagination while a
        # valid next-page link is still present.
        if not next_url:
            break
        page_url = next_url
    return candidates


def _wait_for_plugin(driver: Any, timeout: float) -> Any | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        element = driver.execute_script(_FIND_ZYING_PLUGIN_SCRIPT)
        if element:
            try:
                if element.is_displayed():
                    return element
            except Exception:
                return element
        time.sleep(0.5)
    return None


def _attribute_rows(specs: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    aliases = {
        "MARCA": "BRAND",
        "MODELO": "MODEL",
        "GENERO": "GENDER",
        "PERSONAJE": "CHARACTER",
        "TALLA": "SIZE",
        "MATERIAL_PRINCIPAL": "MAIN_MATERIAL",
        "COMPOSICION": "COMPOSITION",
        "CANTIDAD_DE_DISFRACES": "COSTUMES_NUMBER",
        "INCLUYE_ACCESORIOS": "INCLUDES_ACCESSORIES",
        "ACCESORIOS_INCLUIDOS": "ACCESSORIES_INCLUDED",
        "ES_KIT": "IS_KIT",
        "TALLA_DEL_DISFRAZ": "COSTUME_SIZE",
        "CONTORNO_DEL_PECHO": "CHEST_CIRCUMFERENCE",
        "CONTORNO_DE_LA_CINTURA": "WAIST_CIRCUMFERENCE",
        "CONTORNO_DE_LA_CADERA": "HIP_CIRCUMFERENCE",
        "ESTILOS": "NECKLACE_STYLES",
        "MATERIAL_DEL_COLLAR": "NECKLACE_MATERIAL",
    }
    attributes: list[dict[str, str]] = []
    for index, spec in enumerate(specs or []):
        name = str(spec.get("name") or "").strip()
        value = str(spec.get("value") or "").strip()
        if not name or not value:
            continue
        decomposed = unicodedata.normalize("NFKD", name)
        ascii_name = "".join(
            character for character in decomposed if not unicodedata.combining(character)
        )
        normalized_id = re.sub(r"[^A-Z0-9]+", "_", ascii_name.upper()).strip("_")
        normalized_id = aliases.get(normalized_id, normalized_id)
        attributes.append({"id": normalized_id or f"SPEC_{index + 1}", "name": name, "value_name": value})
    return attributes


def _collect_detail(
    driver: Any,
    candidate: Mapping[str, Any],
    *,
    plugin_timeout: float,
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    _check_stop(stop_event)
    driver.get(str(candidate["source_url"]))
    _wait_ready(driver)
    time.sleep(1.0)
    page = driver.execute_script(_DETAIL_PAGE_SCRIPT) or {}
    blocked = _blocked_page_message(driver.current_url, page.get("body", ""))
    if blocked:
        raise RuntimeError(blocked)

    item_id = extract_item_id(str(candidate.get("source_item_id") or driver.current_url))
    pictures = [url for url in (_normalize_image_url(value) for value in page.get("pictures") or []) if url]
    main_image = pictures[0] if pictures else str(candidate.get("main_image_url") or "")
    plugin_snapshot: dict[str, Any] = {
        "source": "智赢浏览器插件商品详情浮层",
        "read_method": "plugin_bar_ocr",
        "weight_basis": "plugin_actual",
        "volumetric_formula": "length_cm * width_cm * height_cm / 6000",
    }
    plugin_element = _wait_for_plugin(driver, plugin_timeout)
    if plugin_element is not None:
        dom_text = str(plugin_element.text or "").strip()
        ocr = ocr_plugin_image(plugin_element.screenshot_as_png)
        combined_text = " ".join(value for value in (dom_text, ocr["text"]) if value)
        metrics = parse_plugin_metrics(combined_text)
        plugin_snapshot.update(
            dom_text=dom_text,
            ocr_text=ocr["text"],
            ocr_confidence=ocr["confidence"],
            ocr_lines=ocr["lines"],
            dimensions_display=metrics.get("dimensions_display"),
            weight_display=metrics.get("weight_display"),
            plugin_volumetric_display=metrics.get("plugin_volumetric_display"),
            volumetric_weight_kg=metrics.get("volumetric_weight_kg"),
        )
    else:
        metrics = parse_plugin_metrics("")
        plugin_snapshot["error"] = "详情页未检测到智赢插件浮层，请确认采集浏览器已登录并启用插件"

    title = str(page.get("title") or candidate.get("title") or "").strip()
    price = _number(page.get("price"))
    if price is None:
        price = _number(candidate.get("price"))
    complete = bool(
        title
        and main_image
        and metrics.get("weight_g") is not None
        and metrics.get("package_length_cm") is not None
        and metrics.get("package_width_cm") is not None
        and metrics.get("package_height_cm") is not None
    )
    errors: list[str] = []
    if not main_image:
        errors.append("未识别到商品主图")
    if plugin_element is None:
        errors.append(str(plugin_snapshot["error"]))
    elif metrics.get("weight_g") is None or metrics.get("package_length_cm") is None:
        errors.append("智赢插件已显示，但未能识别完整重量/尺寸")
    source = {
        "id": item_id,
        "site_id": item_id[:3],
        "title": title,
        "price": price,
        "currency_id": str(page.get("currency_id") or candidate.get("currency_id") or "MXN"),
        "condition": "new",
        "available_quantity": 1,
        "permalink": str(page.get("final_url") or driver.current_url),
        "pictures": [{"source": url} for url in pictures],
        "attributes": _attribute_rows(page.get("specs") or []),
        "variations": [],
        "sale_terms": [],
    }
    return {
        "source_item_id": item_id,
        "source_url": str(candidate["source_url"]),
        "final_url": str(page.get("final_url") or driver.current_url),
        "main_image_url": main_image,
        "title": title,
        "price": price,
        "currency_id": source["currency_id"],
        "weight_g": metrics.get("weight_g"),
        "volumetric_weight_kg": metrics.get("volumetric_weight_kg"),
        "package_length_cm": metrics.get("package_length_cm"),
        "package_width_cm": metrics.get("package_width_cm"),
        "package_height_cm": metrics.get("package_height_cm"),
        "weight_basis": "plugin_actual",
        "scrape_status": "ok" if complete else "partial",
        "error_message": "；".join(errors),
        "source": source,
        "description": {"plain_text": str(page.get("description") or "")},
        "page_snapshot": {
            "page_title": page.get("page_title"),
            "specs": page.get("specs") or [],
            "pictures": pictures,
        },
        "plugin_snapshot": plugin_snapshot,
        "collected_at": datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _attach_browser(window_id: str) -> tuple[Any, Any]:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from bit.bit_api import openBrowser

    browser_info = openBrowser(window_id)
    if not browser_info or not browser_info.get("data"):
        message = (browser_info or {}).get("msg") if isinstance(browser_info, dict) else browser_info
        raise RuntimeError(f"打开智赢采集浏览器失败：{message or browser_info}")
    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", browser_info["data"]["http"])
    service = Service(browser_info["data"]["driver"])
    return webdriver.Chrome(service=service, options=options), service


def _collect_marketplace_listing_bitbrowser(
    source_url: str,
    requested_count: int,
    *,
    window_id: str = DEFAULT_ZYING_WINDOW_ID,
    plugin_timeout: float = 15.0,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    on_item: Callable[[dict[str, Any]], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Collect a result URL, following pages until ``requested_count`` items."""
    source_url, requested_count = validate_collection_request(source_url, requested_count)
    driver = service = None
    original_handle = ""
    created_handle = ""
    completed = failed = 0
    results: list[dict[str, Any]] = []
    try:
        driver, service = _attach_browser(str(window_id))
        original_handle = driver.current_window_handle
        driver.switch_to.new_window("tab")
        created_handle = driver.current_window_handle
        candidates = _collect_listing_pages(
            driver,
            source_url,
            requested_count,
            on_page=on_page,
            stop_event=stop_event,
        )
        if not candidates:
            raise RuntimeError("列表页没有识别到商品，请确认链接是 Mercado 商品列表或搜索结果页")
        for index, candidate in enumerate(candidates, start=1):
            _check_stop(stop_event)
            if on_progress:
                on_progress(
                    {
                        "stage": "detail",
                        "current": index,
                        "total": len(candidates),
                        "item_id": candidate["source_item_id"],
                        "message": f"正在采集 {candidate['source_item_id']} 的详情和智赢插件数据",
                    }
                )
            try:
                row = _collect_detail(
                    driver,
                    candidate,
                    plugin_timeout=plugin_timeout,
                    stop_event=stop_event,
                )
                if row["scrape_status"] == "ok":
                    completed += 1
                else:
                    failed += 1
            except CollectionStopped:
                raise
            except Exception as exc:
                failed += 1
                row = {
                    **candidate,
                    "scrape_status": "failed",
                    "error_message": str(exc),
                    "weight_basis": "plugin_actual",
                    "source": {"id": candidate["source_item_id"], "title": candidate.get("title")},
                    "description": {},
                    "page_snapshot": {},
                    "plugin_snapshot": {
                        "source": "智赢浏览器插件商品详情浮层",
                        "read_method": "plugin_bar_ocr",
                        "error": str(exc),
                    },
                    "collected_at": datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
                }
            results.append(row)
            if on_item:
                on_item(row)
        return {
            "requested_count": requested_count,
            "candidate_count": len(candidates),
            "completed_count": completed,
            "failed_count": failed,
            "rows": results,
        }
    finally:
        if driver is not None:
            try:
                if created_handle and created_handle in driver.window_handles:
                    driver.switch_to.window(created_handle)
                    driver.close()
                if original_handle and original_handle in driver.window_handles:
                    driver.switch_to.window(original_handle)
            except Exception:
                pass
        if service is not None:
            try:
                service.stop()
            except Exception:
                pass
        try:
            from bit.bit_api import releaseBrowserLease

            releaseBrowserLease(str(window_id))
        except Exception:
            pass


def _find_json_product(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        for child in value:
            found = _find_json_product(child)
            if found:
                return found
        return {}
    if not isinstance(value, dict):
        return {}
    types = value.get("@type")
    if types == "Product" or (isinstance(types, list) and "Product" in types):
        return value
    for key in ("@graph", "mainEntity", "itemListElement"):
        found = _find_json_product(value.get(key))
        if found:
            return found
    return {}


def parse_listing_html(html_text: str, page_url: str) -> dict[str, Any]:
    """Extract ordered product cards and the next-page URL from Edge page source."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(str(html_text or ""), "html.parser")
    selectors = (
        "li.ui-search-layout__item",
        ".ui-search-result",
        ".poly-card",
        '[data-testid="result"]',
    )
    cards: list[Any] = []
    for selector in selectors:
        cards = soup.select(selector)
        if cards:
            break
    if not cards:
        cards = [link for link in soup.select("a[href]") if re.search(r"(?:ML[A-Z]|CBT)-?\d+", link.get("href", ""), re.I)]
    rows: list[dict[str, Any]] = []
    for card in cards:
        link = card if getattr(card, "name", "") == "a" else card.select_one(
            'a.poly-component__title, a.ui-search-link, a[href*="item_id"], '
            'a[href*="/MLM-"], a[href*="/MLB-"]'
        )
        if link is None:
            continue
        href = urljoin(page_url, str(link.get("href") or ""))
        try:
            extract_listing_item_id(href)
        except ValueError:
            continue
        title_node = card.select_one(".poly-component__title, .ui-search-item__title, h2, h3")
        image_node = card.select_one("img")
        fraction = card.select_one(".andes-money-amount__fraction")
        cents = card.select_one(".andes-money-amount__cents")
        price_text = fraction.get_text(" ", strip=True) if fraction else ""
        if cents and price_text:
            price_text = f"{price_text}.{cents.get_text('', strip=True)}"
        image_url = ""
        if image_node:
            image_url = (
                image_node.get("data-src")
                or image_node.get("data-srcset")
                or image_node.get("src")
                or ""
            )
            if " " in image_url:
                image_url = image_url.split()[0]
        rows.append(
            {
                "href": href,
                "title": (title_node or link).get_text(" ", strip=True),
                "main_image_url": _normalize_image_url(image_url),
                "price": price_text,
                "currency_id": "MXN",
            }
        )
    next_node = soup.select_one(
        'a[title="Siguiente"], li.andes-pagination__button--next a, '
        'a[aria-label="Siguiente"], a[aria-label="Próxima"], a[title="Próxima"]'
    )
    body = soup.get_text(" ", strip=True)
    return {
        "rows": rows,
        "next_url": urljoin(page_url, next_node.get("href")) if next_node and next_node.get("href") else "",
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "body": body[:1600],
    }


def parse_detail_html(html_text: str, page_url: str) -> dict[str, Any]:
    """Extract publication fields from Mercado's server-rendered detail HTML."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(str(html_text or ""), "html.parser")
    product: dict[str, Any] = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            product = _find_json_product(json.loads(script.get_text() or "{}"))
        except (TypeError, ValueError):
            product = {}
        if product:
            break

    def meta(*selectors: str) -> str:
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                return str(node.get("content") or "").strip()
        return ""

    offer = product.get("offers") or {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    title_node = soup.select_one("h1.ui-pdp-title, h1")
    description_node = soup.select_one(
        ".ui-pdp-description__content, [data-testid=description-content], .ui-pdp-description"
    )
    raw_title = (
        title_node.get_text(" ", strip=True) if title_node else ""
    ) or str(product.get("name") or meta('meta[property="og:title"]'))
    title_price = re.search(r"\$\s*([0-9][0-9.,]*)", raw_title)
    title = re.sub(r"\s*[-|]\s*\$\s*[0-9][0-9.,]*.*$", "", raw_title).strip()
    description = (
        description_node.get_text(" ", strip=True) if description_node else ""
    ) or str(product.get("description") or meta('meta[name="description"]'))
    pictures: list[str] = []
    raw_images = product.get("image") or []
    if not isinstance(raw_images, list):
        raw_images = [raw_images]
    raw_images.append(meta('meta[property="og:image"]'))
    for image in raw_images:
        image_url = _normalize_image_url(image)
        if image_url and image_url not in pictures:
            pictures.append(image_url)
    for image_node in soup.select(".ui-pdp-gallery img, figure img, img.ui-pdp-image"):
        image_url = _normalize_image_url(
            image_node.get("data-src") or image_node.get("src") or ""
        )
        if image_url and image_url not in pictures:
            pictures.append(image_url)
    specs: list[dict[str, str]] = []
    for row in soup.select(".andes-table__row, .ui-pdp-specs__table tr, .ui-vpp-striped-specs__row"):
        cells = [node.get_text(" ", strip=True) for node in row.select("th, td, .andes-table__header, .andes-table__column")]
        cells = [value for value in cells if value]
        if len(cells) >= 2:
            specs.append({"name": cells[0], "value": " ".join(cells[1:])})
    canonical = soup.select_one('link[rel="canonical"]')
    return {
        "final_url": urljoin(page_url, canonical.get("href")) if canonical and canonical.get("href") else page_url,
        "title": title.strip(),
        "description": description.strip(),
        "price": meta('meta[itemprop="price"]', 'meta[property="product:price:amount"]') or offer.get("price") or (title_price.group(1) if title_price else None),
        "currency_id": meta('meta[itemprop="priceCurrency"]', 'meta[property="product:price:currency"]') or offer.get("priceCurrency") or "MXN",
        "pictures": pictures,
        "specs": specs,
        "body": soup.get_text(" ", strip=True)[:1600],
        "page_title": soup.title.get_text(" ", strip=True) if soup.title else title,
    }


class EdgeUiSession:
    """Small, visible Edge driver used because ZYing is logged in there.

    It controls one temporary tab using ordinary keyboard shortcuts and copies
    server-rendered page source for parsing.  Plugin values are read only from
    a screenshot of the normal detail page.
    """

    def __init__(self, page_wait: float | None = None) -> None:
        self.page_wait = max(3.0, float(page_wait or os.environ.get("MERCADO_EDGE_PAGE_WAIT", "10")))
        self.handle: int | None = None
        self.created_tabs = 0
        self.current_url = ""

    @staticmethod
    def _find_window() -> tuple[int, tuple[int, int, int, int]]:
        import win32gui

        windows: list[tuple[int, tuple[int, int, int, int]]] = []

        def callback(handle: int, _context: Any) -> None:
            title = win32gui.GetWindowText(handle)
            rect = win32gui.GetWindowRect(handle)
            if (
                win32gui.IsWindowVisible(handle)
                and win32gui.GetClassName(handle) == "Chrome_WidgetWin_1"
                and "Microsoft" in title
                and "Edge" in title
                and rect[0] > -10000
                and rect[2] - rect[0] >= 800
            ):
                windows.append((handle, rect))

        win32gui.EnumWindows(callback, None)
        if not windows:
            raise RuntimeError("未找到可见的 Microsoft Edge 窗口，请先打开 Edge 并登录智赢插件")
        return max(windows, key=lambda row: (row[1][2] - row[1][0]) * (row[1][3] - row[1][1]))

    def _focus(self) -> None:
        import win32con
        import win32gui

        if not self.handle or not win32gui.IsWindow(self.handle):
            self.handle, _ = self._find_window()
        win32gui.ShowWindow(self.handle, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(self.handle)
        except Exception:
            from pywinauto import Application

            Application(backend="uia").connect(handle=self.handle).top_window().set_focus()
        time.sleep(0.4)

    def __enter__(self) -> "EdgeUiSession":
        import pyautogui

        self.handle, _ = self._find_window()
        self._focus()
        pyautogui.hotkey("ctrl", "t")
        self.created_tabs = 1
        time.sleep(0.4)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback_value: Any) -> None:
        if not self.created_tabs:
            return
        try:
            import pyautogui

            self._focus()
            for _ in range(self.created_tabs):
                pyautogui.hotkey("ctrl", "w")
                time.sleep(0.15)
            self.created_tabs = 0
        except Exception:
            pass

    def _navigate_current_tab(self, url: str, wait: float) -> None:
        import pyautogui
        import pyperclip

        self._focus()
        pyautogui.hotkey("ctrl", "l")
        # Pasting avoids keyboard-layout corruption of ':' and '/' on Chinese
        # Windows installations.
        pyperclip.copy(str(url))
        pyautogui.hotkey("ctrl", "v")
        pyautogui.press("enter")
        self.current_url = str(url)
        time.sleep(max(0.2, float(wait)))

    def navigate(self, url: str, wait: float | None = None) -> None:
        self._navigate_current_tab(url, max(1.0, float(wait or self.page_wait)))

    def open_detail_batch(self, urls: Iterable[str]) -> int:
        """Open detail pages in adjacent tabs so page/plugin loading overlaps."""
        import pyautogui

        targets = [str(url or "").strip() for url in urls if str(url or "").strip()]
        if not targets:
            return 0
        self._navigate_current_tab(targets[0], 0.35)
        for target in targets[1:]:
            self._focus()
            pyautogui.hotkey("ctrl", "t")
            self.created_tabs += 1
            time.sleep(0.15)
            self._navigate_current_tab(target, 0.35)
        return len(targets)

    def close_current_work_tab(self) -> None:
        """Close one extra batch tab, returning to the preceding work tab."""
        if self.created_tabs <= 1:
            return
        import pyautogui

        self._focus()
        pyautogui.hotkey("ctrl", "w")
        self.created_tabs -= 1
        time.sleep(0.25)

    def copy_page_source(self, timeout: float = 12.0) -> str:
        import pyautogui
        import pyperclip

        self._focus()
        pyperclip.copy("")
        if not self.current_url:
            raise RuntimeError("Edge 当前没有可读取的商品页面")
        source_open = False
        pyautogui.hotkey("ctrl", "u")
        try:
            time.sleep(4.0)
            # Verify that Ctrl+U actually opened a source tab.  Browser
            # overlays occasionally consume the first shortcut.
            pyautogui.hotkey("ctrl", "l")
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.3)
            address = str(pyperclip.paste() or "")
            pyautogui.press("esc")
            source_open = address.lower().startswith("view-source:")
            if not source_open:
                pyautogui.hotkey("ctrl", "u")
                time.sleep(4.0)
                pyautogui.hotkey("ctrl", "l")
                pyautogui.hotkey("ctrl", "c")
                time.sleep(0.3)
                address = str(pyperclip.paste() or "")
                pyautogui.press("esc")
                source_open = address.lower().startswith("view-source:")
            if not source_open:
                raise RuntimeError("Edge 未能打开当前页面源代码")
            import win32gui

            left, top, right, bottom = win32gui.GetWindowRect(self.handle)
            pyautogui.click(left + max(200, (right - left) // 2), top + min(260, (bottom - top) // 3))
            time.sleep(0.3)
            deadline = time.time() + timeout
            while time.time() < deadline:
                pyautogui.hotkey("ctrl", "a")
                pyautogui.hotkey("ctrl", "c")
                time.sleep(0.6)
                source = str(pyperclip.paste() or "")
                if "<html" in source[:1000].lower() or "<!doctype" in source[:1000].lower():
                    return source
            raise RuntimeError("未能从 Edge 读取页面源代码")
        finally:
            if source_open:
                pyautogui.hotkey("ctrl", "w")
                time.sleep(0.4)

    def plugin_ocr(self) -> dict[str, Any]:
        import cv2
        import numpy as np
        import win32gui
        from PIL import ImageGrab

        self._focus()
        rect = win32gui.GetWindowRect(self.handle)
        image = ImageGrab.grab(bbox=rect, all_screens=True)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return ocr_plugin_image(buffer.getvalue())

    def accessibility_snapshot(self) -> dict[str, Any]:
        """Read visible Edge links through Windows UI Automation."""
        import win32gui
        from pywinauto import Application

        self._focus()
        window = Application(backend="uia").connect(handle=self.handle).window(handle=self.handle)
        current_url = ""
        for edit in window.descendants(control_type="Edit"):
            try:
                value = str(edit.iface_value.CurrentValue or "").strip()
            except Exception:
                continue
            if value.startswith(("http://", "https://")):
                current_url = value
        links: list[dict[str, str]] = []
        for link in window.descendants(control_type="Hyperlink"):
            try:
                href = str(link.iface_value.CurrentValue or "").strip()
                name = str(link.window_text() or "").strip()
            except Exception:
                continue
            if href.startswith(("http://", "https://")):
                links.append({"name": name, "href": href})
        title = win32gui.GetWindowText(self.handle)
        title = re.sub(r"\s+和另外\s*\d+\s*个页面.*$", "", title)
        title = re.sub(r"\s+-\s+[^-]*Microsoft.*Edge.*$", "", title, flags=re.I)
        images: list[str] = []
        fallback_images: list[str] = []
        for link in links:
            href = _normalize_image_url(link["href"])
            if "mlstatic.com" not in href:
                continue
            is_product_image = (
                "D_NQ_" in href
                or link["name"].lower().startswith(("imagen ", "imagem ", "image "))
            )
            target = images if is_product_image else fallback_images
            if href not in target:
                target.append(href)
        if not images:
            images.extend(fallback_images)
        rows: list[dict[str, Any]] = []
        for link in links:
            href = link["href"]
            name = link["name"]
            if not name or any(fragment in href for fragment in ("/payments", "/syi/", "/noindex/")):
                continue
            try:
                item_id = extract_listing_item_id(href)
            except ValueError:
                continue
            rows.append(
                {
                    "source_item_id": item_id,
                    "href": href.split("#", 1)[0],
                    "title": name,
                    "main_image_url": "",
                    "currency_id": "MXN",
                }
            )
        next_url = ""
        for link in links:
            if link["name"].strip().lower() in ("siguiente", "próxima", "proxima", "next"):
                next_url = link["href"]
                break
        return {
            "current_url": current_url or self.current_url,
            "title": title,
            "links": links,
            "rows": rows,
            "images": images,
            "next_url": next_url,
        }


def _edge_detail_row(
    candidate: Mapping[str, Any], page: Mapping[str, Any], ocr: Mapping[str, Any]
) -> dict[str, Any]:
    item_id = extract_item_id(str(candidate.get("source_item_id") or page.get("final_url") or candidate.get("source_url")))
    metrics = parse_plugin_metrics(str(ocr.get("text") or ""))
    pictures = [url for url in (_normalize_image_url(value) for value in page.get("pictures") or []) if url]
    main_image = pictures[0] if pictures else str(candidate.get("main_image_url") or "")
    title = str(page.get("title") or candidate.get("title") or "").strip()
    price = _number(page.get("price"))
    if price is None:
        price = _number(candidate.get("price"))
    plugin_snapshot = {
        "source": "智赢浏览器插件商品详情浮层",
        "read_method": "edge_window_ocr",
        "weight_basis": "plugin_actual",
        "volumetric_formula": "length_cm * width_cm * height_cm / 6000",
        "ocr_text": ocr.get("text") or "",
        "ocr_confidence": ocr.get("confidence"),
        "ocr_lines": ocr.get("lines") or [],
        "dimensions_display": metrics.get("dimensions_display"),
        "weight_display": metrics.get("weight_display"),
        "plugin_volumetric_display": metrics.get("plugin_volumetric_display"),
        "volumetric_weight_kg": metrics.get("volumetric_weight_kg"),
    }
    complete_metrics = all(
        metrics.get(key) is not None
        for key in ("weight_g", "package_length_cm", "package_width_cm", "package_height_cm")
    )
    errors: list[str] = []
    if not main_image:
        errors.append("未识别到商品主图")
    if not complete_metrics:
        errors.append("Edge 中已打开商品页，但未从智赢插件识别到完整重量/尺寸")
    source = {
        "id": item_id,
        "site_id": item_id[:3],
        "title": title,
        "price": price,
        "currency_id": str(page.get("currency_id") or candidate.get("currency_id") or "MXN"),
        "condition": "new",
        "available_quantity": 1,
        "permalink": str(page.get("final_url") or candidate.get("source_url")),
        "pictures": [{"source": url} for url in pictures],
        "attributes": _attribute_rows(page.get("specs") or []),
        "variations": [],
        "sale_terms": [],
    }
    return {
        "source_item_id": item_id,
        "source_url": str(candidate.get("source_url") or ""),
        "final_url": source["permalink"],
        "main_image_url": main_image,
        "title": title,
        "price": price,
        "currency_id": source["currency_id"],
        "weight_g": metrics.get("weight_g"),
        "volumetric_weight_kg": metrics.get("volumetric_weight_kg"),
        "package_length_cm": metrics.get("package_length_cm"),
        "package_width_cm": metrics.get("package_width_cm"),
        "package_height_cm": metrics.get("package_height_cm"),
        "weight_basis": "plugin_actual",
        "scrape_status": "ok" if title and main_image and complete_metrics else "partial",
        "error_message": "；".join(errors),
        "source": source,
        "description": {"plain_text": str(page.get("description") or "")},
        "page_snapshot": {
            "page_title": page.get("page_title"),
            "specs": page.get("specs") or [],
            "pictures": pictures,
        },
        "plugin_snapshot": plugin_snapshot,
        "collected_at": datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _wait_for_edge_detail_and_plugin(
    edge_session: EdgeUiSession,
    candidate: Mapping[str, Any],
    *,
    timeout: float,
    stop_event: threading.Event | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wait for the active batch tab and its ZYing overlay to become ready."""
    expected_item_id = str(candidate.get("source_item_id") or "")
    deadline = time.time() + max(3.0, float(timeout))
    last_page: dict[str, Any] = {}
    last_ocr: dict[str, Any] = {"text": "", "confidence": None, "lines": []}
    while True:
        _check_stop(stop_event)
        last_page = edge_session.accessibility_snapshot()
        actual_url = str(last_page.get("current_url") or candidate.get("source_url") or "")
        try:
            active_item_id = extract_listing_item_id(actual_url)
        except ValueError:
            active_item_id = ""
        if active_item_id == expected_item_id:
            last_ocr = edge_session.plugin_ocr()
            metrics = parse_plugin_metrics(str(last_ocr.get("text") or ""))
            if all(
                metrics.get(key) is not None
                for key in (
                    "weight_g",
                    "package_length_cm",
                    "package_width_cm",
                    "package_height_cm",
                )
            ):
                break
        if time.time() >= deadline:
            break
        time.sleep(0.8)
    return last_page, last_ocr


def _collect_marketplace_listing_edge_ui(
    source_url: str,
    requested_count: int,
    *,
    plugin_timeout: float,
    max_workers: int,
    on_page: Callable[[dict[str, Any]], None] | None,
    on_item: Callable[[dict[str, Any]], None] | None,
    on_progress: Callable[[dict[str, Any]], None] | None,
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    try:
        direct_source_item_id = extract_listing_item_id(source_url)
    except ValueError:
        direct_source_item_id = ""
    visited_pages: set[str] = set()
    page_url = source_url
    page_number = completed = failed = 0
    results: list[dict[str, Any]] = []
    with EdgeUiSession(page_wait=max(8.0, min(plugin_timeout, 15.0))) as edge_session:
        while (
            page_url
            and page_url not in visited_pages
            and len(candidates) < requested_count
            and page_number < MAX_LISTING_PAGES
        ):
            _check_stop(stop_event)
            visited_pages.add(page_url)
            page_number += 1
            edge_session.navigate(page_url)
            page = edge_session.accessibility_snapshot()
            actual_url = str(page.get("current_url") or page_url)
            blocked = _blocked_page_message(actual_url, str(page.get("title") or ""))
            if blocked:
                raise RuntimeError(blocked)
            before = len(candidates)
            page_rows = [] if page_number == 1 and direct_source_item_id else (page.get("rows") or [])
            merge_listing_candidates(candidates, page_rows, requested_count)
            if len(candidates) == before:
                try:
                    item_id = direct_source_item_id or extract_listing_item_id(actual_url)
                except ValueError:
                    item_id = ""
                if item_id:
                    merge_listing_candidates(
                        candidates,
                        [{
                            "source_item_id": item_id,
                            "source_url": actual_url,
                            "title": page.get("title") or item_id,
                            "main_image_url": (page.get("images") or [""])[0],
                            "currency_id": "MXN",
                        }],
                        requested_count,
                    )
            if on_page:
                on_page({
                    "page": page_number,
                    "page_url": page_url,
                    "page_items": len(candidates) - before,
                    "candidate_count": len(candidates),
                })
            next_url = str(page.get("next_url") or "")
            if not next_url:
                break
            page_url = next_url
        if not candidates:
            raise RuntimeError("Edge 列表页没有识别到商品，请确认链接是 Mercado 商品列表或搜索结果页")

        workers = normalize_collection_workers(max_workers)
        for batch_start in range(0, len(candidates), workers):
            _check_stop(stop_event)
            batch = candidates[batch_start:batch_start + workers]
            if on_progress:
                on_progress({
                    "stage": "detail_batch",
                    "current": batch_start + 1,
                    "total": len(candidates),
                    "item_id": " / ".join(row["source_item_id"] for row in batch),
                    "message": (
                        f"正在并发加载 {len(batch)} 个商品详情页"
                        f"（并发数 {workers}）"
                    ),
                })
            edge_session.open_detail_batch(row["source_url"] for row in batch)
            # The newest tab is active.  Read the batch in reverse and close
            # each extra tab, which naturally activates the preceding one.
            for reverse_index, candidate in enumerate(reversed(batch)):
                _check_stop(stop_event)
                index = batch_start + len(batch) - reverse_index
                if on_progress:
                    on_progress({
                        "stage": "detail",
                        "current": index,
                        "total": len(candidates),
                        "item_id": candidate["source_item_id"],
                        "message": (
                            f"正在读取 {candidate['source_item_id']} 的商品数据"
                            "和智赢插件重量尺寸"
                        ),
                    })
                try:
                    accessible, ocr = _wait_for_edge_detail_and_plugin(
                        edge_session,
                        candidate,
                        timeout=plugin_timeout,
                        stop_event=stop_event,
                    )
                    actual_url = str(accessible.get("current_url") or candidate["source_url"])
                    page = {
                        "final_url": actual_url,
                        "title": candidate.get("title") or accessible.get("title"),
                        "description": "",
                        "price": candidate.get("price"),
                        "currency_id": candidate.get("currency_id") or "MXN",
                        "pictures": accessible.get("images") or (
                            [candidate.get("main_image_url")] if candidate.get("main_image_url") else []
                        ),
                        "specs": [],
                        "body": "",
                        "page_title": accessible.get("title") or candidate.get("title"),
                    }
                    blocked = _blocked_page_message(
                        str(page.get("final_url") or candidate["source_url"]),
                        f"{page.get('page_title')} {page.get('body')}",
                    )
                    if blocked:
                        raise RuntimeError(blocked)
                    row = _edge_detail_row(candidate, page, ocr)
                    if row["scrape_status"] == "ok":
                        completed += 1
                    else:
                        failed += 1
                except CollectionStopped:
                    raise
                except Exception as exc:
                    failed += 1
                    row = {
                        **candidate,
                        "scrape_status": "failed",
                        "error_message": str(exc),
                        "weight_basis": "plugin_actual",
                        "source": {"id": candidate["source_item_id"], "title": candidate.get("title")},
                        "description": {},
                        "page_snapshot": {},
                        "plugin_snapshot": {
                            "source": "智赢浏览器插件商品详情浮层",
                            "read_method": "edge_window_ocr",
                            "error": str(exc),
                        },
                        "collected_at": datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                results.append(row)
                if on_item:
                    on_item(row)
                if reverse_index < len(batch) - 1:
                    edge_session.close_current_work_tab()
    return {
        "requested_count": requested_count,
        "candidate_count": len(candidates),
        "completed_count": completed,
        "failed_count": failed,
        "rows": results,
    }


def collect_marketplace_listing(
    source_url: str,
    requested_count: int,
    *,
    window_id: str = DEFAULT_ZYING_WINDOW_ID,
    browser_mode: str = DEFAULT_BROWSER_MODE,
    max_workers: int = DEFAULT_COLLECTION_WORKERS,
    plugin_timeout: float = 15.0,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    on_item: Callable[[dict[str, Any]], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Collect a Mercado list; Playwright is the default and non-RPA path."""
    source_url, requested_count = validate_collection_request(source_url, requested_count)
    max_workers = normalize_collection_workers(max_workers)
    mode = str(browser_mode or DEFAULT_BROWSER_MODE).strip().lower()
    # Treat the former Edge labels as Playwright aliases so an old environment
    # variable cannot silently reactivate the keyboard/screenshot RPA path.
    if mode in ("playwright", "pw", "edge", "edge_ui", "msedge"):
        from erp.mercadolibre_playwright_collector import (
            collect_marketplace_listing_playwright,
        )

        return collect_marketplace_listing_playwright(
            source_url,
            requested_count,
            max_workers=max_workers,
            plugin_timeout=plugin_timeout,
            on_page=on_page,
            on_item=on_item,
            on_progress=on_progress,
            stop_event=stop_event,
        )
    if mode == "legacy_edge_rpa":
        return _collect_marketplace_listing_edge_ui(
            source_url,
            requested_count,
            plugin_timeout=plugin_timeout,
            max_workers=max_workers,
            on_page=on_page,
            on_item=on_item,
            on_progress=on_progress,
            stop_event=stop_event,
        )
    if mode not in ("bitbrowser", "bit", "selenium", "legacy_bitbrowser"):
        raise ValueError(f"不支持的采集浏览器模式: {browser_mode}")
    return _collect_marketplace_listing_bitbrowser(
        source_url,
        requested_count,
        window_id=window_id,
        plugin_timeout=plugin_timeout,
        on_page=on_page,
        on_item=on_item,
        on_progress=on_progress,
        stop_event=stop_event,
    )


__all__ = [
    "CollectionStopped",
    "DEFAULT_BROWSER_MODE",
    "DEFAULT_COLLECTION_WORKERS",
    "DEFAULT_ZYING_WINDOW_ID",
    "MAX_COLLECTION_COUNT",
    "MAX_COLLECTION_WORKERS",
    "calculate_volumetric_weight_kg",
    "collect_marketplace_listing",
    "extract_listing_item_id",
    "merge_listing_candidates",
    "normalize_collection_workers",
    "ocr_plugin_image",
    "parse_plugin_metrics",
    "parse_detail_html",
    "parse_listing_html",
    "validate_collection_request",
]
