"""Fast Mercado Libre collector backed by Playwright and DOM extraction.

The collector never drives the mouse or keyboard and never screenshots/OCRs
the page.  Product fields come from Mercado Libre's DOM; package weight and
dimensions come from the DOM injected into the detail page by the ZYing
browser extension.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin

from erp.mercadolibre_follow_sell import extract_item_id

from erp.mercadolibre_batch_collector import (
    MAX_LISTING_PAGES,
    CollectionStopped,
    _attribute_rows,
    _blocked_page_message,
    _check_stop,
    _normalize_image_url,
    _number,
    extract_listing_item_id,
    merge_listing_candidates,
    normalize_collection_workers,
    parse_plugin_metrics,
    validate_collection_request,
)


DEFAULT_CDP_URL = os.environ.get(
    "MERCADO_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222"
).strip()
ZYING_EXTENSION_ID = "gmnnicmdgiafgenphemmdcigkpolabhb"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "cache" / "mercado_playwright_profile"
DEFAULT_SETUP_URL = (
    "https://www.mercadolibre.com.mx/"
    "trendy-shingeki-no-kyojin-attack-on-titan-pendant-necklace/p/MLM2030103189"
)

LISTING_DOM_SCRIPT = r"""() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const imageUrl = img => {
    if (!img) return '';
    const srcset = img.getAttribute('srcset') || '';
    return img.currentSrc || img.getAttribute('data-src') ||
      img.getAttribute('data-srcset') ||
      (srcset ? srcset.trim().split(/\s+/)[0] : '') ||
      img.getAttribute('src') || '';
  };
  const itemPattern = /(?:ML[A-Z]|CBT)-?\d+/i;
  const cards = Array.from(document.querySelectorAll(
    'li.ui-search-layout__item, .ui-search-result, .poly-card, [data-testid="result"]'
  ));
  const roots = cards.length ? cards : Array.from(document.querySelectorAll('a[href]'))
    .filter(a => itemPattern.test(a.href));
  const rows = [];
  for (const root of roots) {
    const link = root.matches && root.matches('a[href]') ? root : root.querySelector(
      'a.poly-component__title, a.ui-search-link, a[href*="item_id="], ' +
      'a[href*="/MLM-"], a[href*="/MLB-"]'
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
    'a[title="Siguiente"], li.andes-pagination__button--next a, ' +
    'a[aria-label="Siguiente"], a[aria-label="Próxima"], a[title="Próxima"]'
  );
  return {
    rows,
    next_url: next ? next.href : '',
    title: document.title,
    body: clean(document.body ? document.body.innerText : '').slice(0, 1400)
  };
}"""

DETAIL_DOM_SCRIPT = r"""() => {
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
      const values = Array.isArray(parsed) ? parsed :
        (parsed && parsed['@graph'] ? parsed['@graph'] : [parsed]);
      const match = values.find(row => row && (
        row['@type'] === 'Product' ||
        (Array.isArray(row['@type']) && row['@type'].includes('Product'))
      ));
      if (match) { product = match; break; }
    } catch (_) {}
  }
  const h1 = first(['h1.ui-pdp-title', 'h1']);
  const metaTitle = document.querySelector('meta[property="og:title"]');
  const metaPrice = document.querySelector(
    'meta[itemprop="price"], meta[property="product:price:amount"]'
  );
  const currency = document.querySelector(
    'meta[itemprop="priceCurrency"], meta[property="product:price:currency"]'
  );
  const description = first([
    '.ui-pdp-description__content', '[data-testid="description-content"]',
    '.ui-pdp-description'
  ]);
  const pictures = [];
  const addPicture = value => {
    value = String(value || '').trim();
    if (value && !value.startsWith('data:') && !pictures.includes(value)) {
      pictures.push(value.startsWith('//') ? 'https:' + value : value);
    }
  };
  const structuredImages = Array.isArray(product.image) ? product.image : [product.image];
  structuredImages.forEach(addPicture);
  const ogImage = document.querySelector('meta[property="og:image"]');
  if (ogImage) addPicture(ogImage.content);
  document.querySelectorAll('.ui-pdp-gallery img, figure img, img.ui-pdp-image')
    .forEach(img => addPicture(
      img.currentSrc || img.getAttribute('data-src') || img.getAttribute('src')
    ));
  const specs = [];
  document.querySelectorAll(
    '.andes-table__row, .ui-pdp-specs__table tr, .ui-vpp-striped-specs__row'
  ).forEach(row => {
    const cells = Array.from(row.querySelectorAll(
      'th, td, .andes-table__header, .andes-table__column'
    )).map(node => clean(node.textContent)).filter(Boolean);
    if (cells.length >= 2) specs.push({name: cells[0], value: cells.slice(1).join(' ')});
  });
  const offer = Array.isArray(product.offers) ? product.offers[0] : (product.offers || {});
  const visibleFraction = first(['.ui-pdp-price__second-line .andes-money-amount__fraction']);
  const visibleCents = first(['.ui-pdp-price__second-line .andes-money-amount__cents']);
  let visiblePrice = visibleFraction ? clean(visibleFraction.textContent).replace(/\D/g, '') : '';
  if (visiblePrice && visibleCents) {
    visiblePrice += '.' + clean(visibleCents.textContent).replace(/\D/g, '');
  }
  return {
    final_url: location.href,
    title: clean(
      (h1 && h1.textContent) || product.name || (metaTitle && metaTitle.content)
    ),
    description: clean((description && description.textContent) || product.description),
    price: (metaPrice && metaPrice.content) || offer.price || visiblePrice || '',
    currency_id: (currency && currency.content) || offer.priceCurrency || 'MXN',
    pictures,
    specs,
    body: clean(document.body ? document.body.innerText : '').slice(0, 1800),
    page_title: document.title
  };
}"""

SHADOW_PLUGIN_TEXT_SCRIPT = r"""() => {
  const output = [];
  const seen = new Set();
  const add = value => {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    if (text && !seen.has(text)) { seen.add(text); output.push(text); }
  };
  const walk = root => {
    if (!root || !root.querySelectorAll) return;
    for (const node of root.querySelectorAll('*')) {
      if (node.matches && node.matches(
        '.zying-meli-detail-metric-line, .zying-meli-detail-metric-column'
      )) add(node.innerText || node.textContent);
      if (node.shadowRoot) walk(node.shadowRoot);
    }
  };
  walk(document);
  return output;
}"""


@dataclass
class _PlaywrightRuntime:
    playwright: Any
    context: Any
    browser: Any | None = None
    managed: bool = False
    connection_mode: str = ""
    pages: list[Any] = field(default_factory=list)
    # A persistent Chromium context exits when its last page is closed.  Keep
    # one untracked page alive while detail pages are opened and closed in
    # batches; _close_runtime() closes it together with the context.
    anchor_page: Any | None = None


PLUGIN_REACT_METRICS_SCRIPT = r"""(() => {
  const roots = [];
  const visitedRoots = new Set();
  const walkRoots = root => {
    if (!root || visitedRoots.has(root)) return;
    visitedRoots.add(root);
    roots.push(root);
    let nodes = [];
    try { nodes = root.querySelectorAll('*'); } catch (_) { return; }
    for (const node of nodes) {
      if (node.shadowRoot) walkRoots(node.shadowRoot);
    }
  };
  walkRoots(document);
  const lines = [];
  for (const root of roots) {
    try { lines.push(...root.querySelectorAll('.zying-meli-detail-metric-line')); }
    catch (_) {}
  }
  const result = {found: lines.length > 0, metrics: {}, data: {}};
  const finite = value => {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };
  const captureData = data => {
    if (!data || typeof data !== 'object') return;
    if (data.weight !== undefined && data.weight !== null) {
      const value = finite(data.weight);
      if (value !== null) result.data.weight_g = value;
    }
    if (Array.isArray(data.size) && data.size.length >= 3) {
      const size = data.size.slice(0, 3).map(finite);
      if (size.every(value => value !== null)) result.data.size_cm = size;
    }
    if (data.volumeWeightG !== undefined && data.volumeWeightG !== null) {
      const value = finite(data.volumeWeightG);
      if (value !== null) result.data.volume_weight_g = value;
    }
    const packet = data.renderPacket;
    if (packet && typeof packet === 'object') {
      if (!result.data.weight_display && packet.weightValue) {
        result.data.weight_display = String(packet.weightValue);
      }
      if (!result.data.size_display && packet.sizeValue) {
        result.data.size_display = String(packet.sizeValue);
      }
      if (!result.data.volume_display && packet.volumeWeightValue) {
        result.data.volume_display = String(packet.volumeWeightValue);
      }
    }
  };
  const captureValue = (value, seen) => {
    if (!value || typeof value !== 'object' || seen.has(value)) return;
    seen.add(value);
    if (Array.isArray(value)) {
      value.forEach(child => captureValue(child, seen));
      return;
    }
    const props = value.props && typeof value.props === 'object' ? value.props : value;
    if (props.metric && typeof props.metric === 'object' && props.metric.key) {
      const metric = props.metric;
      result.metrics[String(metric.key)] = {
        value: metric.value == null ? '' : String(metric.value),
        sub_value: metric.subValue == null ? '' : String(metric.subValue),
        trailing_value: metric.trailingValue == null ? '' : String(metric.trailingValue)
      };
    }
    captureData(props.data);
    if (props.children !== undefined) captureValue(props.children, seen);
  };
  for (const line of lines) {
    const fiberKey = Object.keys(line).find(key => key.startsWith('__reactFiber$'));
    const propsKey = Object.keys(line).find(key => key.startsWith('__reactProps$'));
    const fiber = fiberKey ? line[fiberKey] : null;
    const props = fiber && fiber.memoizedProps ? fiber.memoizedProps :
      (propsKey ? line[propsKey] : null);
    captureValue(props, new Set());
  }
  return result;
})()"""


class _PluginMetricReader:
    """Read ZYing React props from the extension's isolated JS context."""

    def __init__(self, session: Any | None = None) -> None:
        self.session = session
        self.context_ids: list[int] = []

    @classmethod
    async def open(cls, page: Any) -> "_PluginMetricReader":
        reader = cls()
        try:
            session = await page.context.new_cdp_session(page)
            reader.session = session

            def remember_context(event: Mapping[str, Any]) -> None:
                context = event.get("context") or {}
                if str(context.get("origin") or "") != (
                    f"chrome-extension://{ZYING_EXTENSION_ID}"
                ):
                    return
                try:
                    context_id = int(context.get("id"))
                except (TypeError, ValueError):
                    return
                if context_id not in reader.context_ids:
                    reader.context_ids.append(context_id)

            session.on("Runtime.executionContextCreated", remember_context)
            await session.send("Runtime.enable")
        except Exception:
            await reader.close()
        return reader

    async def read(self) -> dict[str, Any]:
        if self.session is None:
            return {}
        for context_id in reversed(tuple(self.context_ids)):
            try:
                response = await self.session.send(
                    "Runtime.evaluate",
                    {
                        "expression": PLUGIN_REACT_METRICS_SCRIPT,
                        "contextId": context_id,
                        "returnByValue": True,
                    },
                )
                value = (response.get("result") or {}).get("value")
                if isinstance(value, dict) and value.get("found"):
                    return value
            except Exception:
                # Redirects replace the extension execution context.  The
                # Runtime event handler will append the new context ID.
                continue
        return {}

    async def close(self) -> None:
        session, self.session = self.session, None
        if session is None:
            return
        try:
            await session.detach()
        except Exception:
            pass


def _version_key(path: Path) -> tuple[int, ...]:
    values = re.findall(r"\d+", path.name)
    return tuple(int(value) for value in values)


def discover_zying_extension_dir() -> Path | None:
    """Return the newest installed unpacked ZYing extension directory."""
    explicit = os.environ.get("MERCADO_ZYING_EXTENSION_DIR", "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        return path if (path / "manifest.json").is_file() else None
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return None
    root = (
        Path(local_app_data)
        / "Microsoft"
        / "Edge"
        / "User Data"
        / "Default"
        / "Extensions"
        / ZYING_EXTENSION_ID
    )
    versions = [path for path in root.glob("*") if (path / "manifest.json").is_file()]
    return max(versions, key=_version_key) if versions else None


def _managed_profile_dir() -> Path:
    configured = os.environ.get("MERCADO_PLAYWRIGHT_PROFILE_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_PROFILE_DIR


def _headless_enabled() -> bool:
    return os.environ.get("MERCADO_PLAYWRIGHT_HEADLESS", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }


async def _open_runtime() -> _PlaywrightRuntime:
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    connect_error = ""
    if DEFAULT_CDP_URL:
        try:
            browser = await playwright.chromium.connect_over_cdp(
                DEFAULT_CDP_URL, timeout=2500
            )
            if browser.contexts:
                return _PlaywrightRuntime(
                    playwright=playwright,
                    browser=browser,
                    context=browser.contexts[0],
                    managed=False,
                    connection_mode=f"edge_cdp:{DEFAULT_CDP_URL}",
                )
            connect_error = "CDP 浏览器没有可用的默认上下文"
        except Exception as exc:
            connect_error = str(exc)

    extension_dir = discover_zying_extension_dir()
    if extension_dir is None:
        await playwright.stop()
        suffix = f"；Edge CDP 连接失败：{connect_error}" if connect_error else ""
        raise RuntimeError(
            "未找到智赢 Edge 插件目录，请设置 MERCADO_ZYING_EXTENSION_DIR" + suffix
        )
    profile_dir = _managed_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    extension = str(extension_dir)
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            channel="chromium",
            headless=_headless_enabled(),
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
            args=[
                f"--disable-extensions-except={extension}",
                f"--load-extension={extension}",
            ],
        )
    except Exception:
        await playwright.stop()
        raise
    # launch_persistent_context may start without a page.  When all worker
    # pages in the first batch are closed Chromium otherwise closes the whole
    # context, making every later item fail at BrowserContext.new_page().
    anchor_page = context.pages[0] if context.pages else await context.new_page()
    return _PlaywrightRuntime(
        playwright=playwright,
        context=context,
        managed=True,
        connection_mode=f"managed_profile:{profile_dir}",
        anchor_page=anchor_page,
    )


async def _close_runtime(runtime: _PlaywrightRuntime) -> None:
    for page in list(runtime.pages):
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
    if runtime.managed:
        try:
            await runtime.context.close()
        except Exception:
            pass
    # Do not close a browser reached over CDP: that is the operator's normal
    # Edge instance.  Stopping Playwright only disconnects this client.
    try:
        await runtime.playwright.stop()
    except Exception:
        pass


async def _new_page(runtime: _PlaywrightRuntime) -> Any:
    page = await runtime.context.new_page()
    runtime.pages.append(page)
    page.set_default_timeout(float(os.environ.get("MERCADO_PLAYWRIGHT_TIMEOUT_MS", "30000")))

    async def skip_heavy_assets(route: Any) -> None:
        if route.request.resource_type in {"media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", skip_heavy_assets)
    return page


async def _goto(page: Any, url: str) -> None:
    timeout = float(os.environ.get("MERCADO_PLAYWRIGHT_NAVIGATION_TIMEOUT_MS", "35000"))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except Exception as exc:
        message = str(exc or "").lower()
        transient = "page.goto" in message and (
            "timeout" in message or "err_aborted" in message
        )
        if not transient:
            raise
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        # Mercado can keep redirecting/tracking after useful HTML is already
        # visible.  Continue into the navigation-safe DOM evaluator when the
        # page has at least left about:blank; an actually failed navigation is
        # still rejected below.
        current_url = str(page.url or "")
        if current_url.startswith(("http://", "https://")):
            return
        raise


_NAVIGATION_CONTEXT_MARKERS = (
    "execution context was destroyed",
    "most likely because of a navigation",
    "cannot find context with specified id",
    "inspected target navigated or closed",
)
_BROWSER_CLOSED_MARKERS = (
    "target page, context or browser has been closed",
    "browsercontext.new_page",
    "browser has been closed",
    "context has been closed",
    "connection closed",
)


def _is_navigation_context_error(exc: BaseException) -> bool:
    message = str(exc or "").lower()
    return any(marker in message for marker in _NAVIGATION_CONTEXT_MARKERS)


def _is_browser_closed_error(exc: BaseException) -> bool:
    message = str(exc or "").lower()
    return any(marker in message for marker in _BROWSER_CLOSED_MARKERS)


async def _evaluate_after_navigation(
    page: Any,
    script: str,
    *,
    attempts: int = 4,
) -> Any:
    """Evaluate DOM JavaScript, retrying while Mercado redirects the page."""
    last_error: BaseException | None = None
    for attempt in range(max(1, int(attempts))):
        if page.is_closed():
            raise RuntimeError("Playwright 商品页面已关闭")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            # The next evaluate call gives a more useful error.  A slow
            # resource must not block extraction after DOMContentLoaded.
            pass
        if attempt:
            await asyncio.sleep(min(0.2 * attempt, 0.6))
        try:
            return await page.evaluate(script)
        except Exception as exc:
            last_error = exc
            if not _is_navigation_context_error(exc) or attempt + 1 >= attempts:
                raise
    if last_error is not None:
        raise last_error
    raise RuntimeError("Playwright 页面 DOM 读取失败")


async def _listing_candidates(
    runtime: _PlaywrightRuntime,
    source_url: str,
    requested_count: int,
    *,
    on_page: Callable[[dict[str, Any]], None] | None,
    stop_event: threading.Event | None,
) -> list[dict[str, Any]]:
    page = await _new_page(runtime)
    candidates: list[dict[str, Any]] = []
    visited_pages: set[str] = set()
    page_url = source_url
    page_number = 0
    try:
        direct_source_item_id = extract_listing_item_id(source_url)
    except ValueError:
        direct_source_item_id = ""

    while (
        page_url
        and page_url not in visited_pages
        and len(candidates) < requested_count
        and page_number < MAX_LISTING_PAGES
    ):
        _check_stop(stop_event)
        visited_pages.add(page_url)
        page_number += 1
        await _goto(page, page_url)
        try:
            await page.wait_for_selector(
                'li.ui-search-layout__item, .poly-card, h1.ui-pdp-title, h1',
                timeout=8000,
            )
        except Exception:
            pass
        snapshot = await _evaluate_after_navigation(page, LISTING_DOM_SCRIPT)
        actual_url = page.url
        blocked = _blocked_page_message(actual_url, str(snapshot.get("body") or ""))
        if blocked:
            raise RuntimeError(blocked)
        before = len(candidates)
        page_rows = [] if page_number == 1 and direct_source_item_id else snapshot.get("rows") or []
        merge_listing_candidates(candidates, page_rows, requested_count)
        if len(candidates) == before:
            try:
                item_id = direct_source_item_id or extract_listing_item_id(actual_url)
            except ValueError:
                item_id = ""
            if item_id:
                detail_hint = await _evaluate_after_navigation(
                    page,
                    """() => {
                      const h1 = document.querySelector('h1.ui-pdp-title, h1');
                      const image = document.querySelector(
                        '.ui-pdp-gallery img, figure img, img.ui-pdp-image'
                      );
                      return {
                        title: h1 ? String(h1.textContent || '').trim() : document.title,
                        main_image_url: image ? (
                          image.currentSrc || image.getAttribute('data-src') || image.src || ''
                        ) : ''
                      };
                    }""",
                )
                merge_listing_candidates(
                    candidates,
                    [{
                        "source_item_id": item_id,
                        "source_url": actual_url,
                        **detail_hint,
                    }],
                    requested_count,
                )
        if on_page:
            on_page({
                "page": page_number,
                "page_url": actual_url,
                "page_items": len(candidates) - before,
                "candidate_count": len(candidates),
                "browser": runtime.connection_mode,
            })
        next_url = urljoin(actual_url, str(snapshot.get("next_url") or ""))
        if not next_url:
            break
        page_url = next_url
    try:
        runtime.pages.remove(page)
    except ValueError:
        pass
    try:
        await page.close()
    except Exception:
        pass
    return candidates


async def _plugin_dom_lines(page: Any) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for frame in page.frames:
        frame_lines: Iterable[Any] = []
        try:
            # Playwright locators pierce open Shadow DOM, which is where ZYing
            # mounts the metrics panel.
            frame_lines = await frame.locator(
                ".zying-meli-detail-metric-line, .zying-meli-detail-metric-column"
            ).all_inner_texts()
        except Exception:
            pass
        try:
            frame_lines = [*frame_lines, *(await frame.evaluate(SHADOW_PLUGIN_TEXT_SCRIPT))]
        except Exception:
            pass
        for value in frame_lines:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if text and text not in seen:
                seen.add(text)
                lines.append(text)
    return lines


def _metrics_from_react_payload(
    payload: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Convert the extension's unobfuscated React props into plugin metrics."""
    payload = payload or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    metric_rows = (
        payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    )
    weight_row = (
        metric_rows.get("weight")
        if isinstance(metric_rows.get("weight"), dict)
        else {}
    )
    size_row = (
        metric_rows.get("size")
        if isinstance(metric_rows.get("size"), dict)
        else {}
    )

    weight_display = str(
        data.get("weight_display") or weight_row.get("value") or ""
    ).strip()
    size_display = str(
        data.get("size_display") or size_row.get("value") or ""
    ).strip()
    volume_display = str(
        data.get("volume_display") or size_row.get("sub_value") or ""
    ).strip()

    weight_g = _number(data.get("weight_g"))
    raw_size = data.get("size_cm")
    size_cm = (
        [_number(value) for value in raw_size[:3]]
        if isinstance(raw_size, list) and len(raw_size) >= 3
        else []
    )
    volume_weight_g = _number(data.get("volume_weight_g"))

    parts: list[str] = []
    if weight_g is not None:
        parts.append(f"重量 {weight_g:g}g")
    elif weight_display:
        parts.append(f"重量 {weight_display}")
    if len(size_cm) == 3 and all(value is not None for value in size_cm):
        parts.append("尺寸 " + " x ".join(f"{value:g}" for value in size_cm) + " cm")
    elif size_display:
        parts.append(f"尺寸 {size_display}")
    if volume_weight_g is not None:
        parts.append(f"计抛 {volume_weight_g:g}g")
    elif volume_display:
        parts.append(volume_display)

    decoded_text = " ".join(parts)
    metrics = parse_plugin_metrics(decoded_text)
    lines = []
    if weight_display or weight_g is not None:
        lines.append(f"重量：{weight_display or f'{weight_g:g}g'}")
    if size_display or len(size_cm) == 3:
        rendered_size = size_display or " × ".join(
            f"{value:g}" for value in size_cm
        )
        lines.append(f"尺寸：{rendered_size}")
    if volume_display or volume_weight_g is not None:
        rendered_volume = (
            f"{volume_weight_g:g}g"
            if volume_weight_g is not None
            else volume_display
        )
        lines.append(f"计抛：{rendered_volume}")
    return metrics, lines


async def _wait_for_plugin_metrics(
    page: Any,
    timeout: float,
    stop_event: threading.Event | None,
    *,
    react_reader: _PluginMetricReader | None = None,
) -> tuple[dict[str, Any], list[str]]:
    deadline = asyncio.get_running_loop().time() + max(1.0, float(timeout))
    last_lines: list[str] = []
    last_metrics = parse_plugin_metrics("")
    while asyncio.get_running_loop().time() < deadline:
        _check_stop(stop_event)
        if react_reader is not None:
            react_payload = await react_reader.read()
            react_metrics, react_lines = _metrics_from_react_payload(react_payload)
            if react_lines:
                last_lines = react_lines
                last_metrics = react_metrics
            if (
                react_metrics.get("weight_g") is not None
                and react_metrics.get("package_length_cm") is not None
                and react_metrics.get("package_width_cm") is not None
                and react_metrics.get("package_height_cm") is not None
            ):
                return react_metrics, react_lines
        last_lines = await _plugin_dom_lines(page)
        dom_metrics = parse_plugin_metrics(" ".join(last_lines))
        if any(dom_metrics.get(key) is not None for key in (
            "weight_g", "package_length_cm", "package_width_cm", "package_height_cm"
        )):
            last_metrics = dom_metrics
        if (
            last_metrics.get("weight_g") is not None
            and last_metrics.get("package_length_cm") is not None
            and last_metrics.get("package_width_cm") is not None
            and last_metrics.get("package_height_cm") is not None
        ):
            return last_metrics, last_lines
        await asyncio.sleep(0.3)
    return last_metrics, last_lines


def _failure_row(candidate: Mapping[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        **dict(candidate),
        "scrape_status": "failed",
        "error_message": str(exc),
        "weight_basis": "plugin_actual",
        "source": {
            "id": candidate.get("source_item_id"),
            "title": candidate.get("title"),
        },
        "description": {},
        "page_snapshot": {},
        "plugin_snapshot": {
            "source": "智赢浏览器插件商品详情浮层",
            "read_method": "playwright_shadow_dom",
            "error": str(exc),
        },
        "collected_at": datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
    }


async def _collect_detail(
    runtime: _PlaywrightRuntime,
    candidate: Mapping[str, Any],
    *,
    plugin_timeout: float,
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    _check_stop(stop_event)
    page = await _new_page(runtime)
    react_reader: _PluginMetricReader | None = None
    try:
        await _goto(page, str(candidate["source_url"]))
        try:
            await page.wait_for_selector('h1.ui-pdp-title, h1', timeout=10000)
        except Exception:
            pass
        details = await _evaluate_after_navigation(page, DETAIL_DOM_SCRIPT)
        blocked = _blocked_page_message(page.url, str(details.get("body") or ""))
        if blocked:
            raise RuntimeError(blocked)

        react_reader = await _PluginMetricReader.open(page)
        metrics, plugin_lines = await _wait_for_plugin_metrics(
            page,
            plugin_timeout,
            stop_event,
            react_reader=react_reader,
        )
        pictures = [
            url
            for url in (
                _normalize_image_url(value) for value in details.get("pictures") or []
            )
            if url
        ]
        candidate_image = _normalize_image_url(candidate.get("main_image_url"))
        if not pictures and candidate_image:
            pictures.append(candidate_image)
        main_image = pictures[0] if pictures else candidate_image
        title = str(details.get("title") or candidate.get("title") or "").strip()
        price = _number(details.get("price"))
        if price is None:
            price = _number(candidate.get("price"))
        item_id = extract_item_id(
            str(candidate.get("source_item_id") or page.url)
        )
        plugin_complete = bool(
            metrics.get("weight_g") is not None
            and metrics.get("package_length_cm") is not None
            and metrics.get("package_width_cm") is not None
            and metrics.get("package_height_cm") is not None
        )
        complete = bool(title and main_image and plugin_complete)
        errors: list[str] = []
        if not main_image:
            errors.append("未识别到商品主图")
        plugin_text = " ".join(plugin_lines)
        if not plugin_lines:
            errors.append(
                "详情页未检测到智赢插件重量尺寸，请确认 Playwright 采集浏览器已登录智赢"
            )
        elif re.search(r"(?:^|\s)登录(?:\s|$)", plugin_text):
            errors.append(
                "Playwright 采集浏览器中的智赢插件尚未登录，请先点击“登录智赢采集浏览器”"
            )
        elif not plugin_complete:
            errors.append("智赢插件已显示，但 DOM 中没有完整的重量/尺寸")

        currency_id = str(
            details.get("currency_id") or candidate.get("currency_id") or "MXN"
        )
        source = {
            "id": item_id,
            "site_id": item_id[:3],
            "title": title,
            "price": price,
            "currency_id": currency_id,
            "condition": "new",
            "available_quantity": 1,
            "permalink": str(details.get("final_url") or page.url),
            "pictures": [{"source": url} for url in pictures],
            "attributes": _attribute_rows(details.get("specs") or []),
            "variations": [],
            "sale_terms": [],
        }
        return {
            "source_item_id": item_id,
            "source_url": str(candidate["source_url"]),
            "final_url": str(details.get("final_url") or page.url),
            "main_image_url": main_image,
            "title": title,
            "price": price,
            "currency_id": currency_id,
            "weight_g": metrics.get("weight_g"),
            "volumetric_weight_kg": metrics.get("volumetric_weight_kg"),
            "package_length_cm": metrics.get("package_length_cm"),
            "package_width_cm": metrics.get("package_width_cm"),
            "package_height_cm": metrics.get("package_height_cm"),
            "weight_basis": "plugin_actual",
            "scrape_status": "ok" if complete else "partial",
            "error_message": "；".join(errors),
            "source": source,
            "description": {"plain_text": str(details.get("description") or "")},
            "page_snapshot": {
                "page_title": details.get("page_title"),
                "specs": details.get("specs") or [],
                "pictures": pictures,
                "browser": runtime.connection_mode,
            },
            "plugin_snapshot": {
                "source": "智赢浏览器插件商品详情浮层",
                "read_method": "playwright_shadow_dom",
                "dom_lines": plugin_lines,
                "dom_text": plugin_text,
                "weight_basis": "plugin_actual",
                "dimensions_display": metrics.get("dimensions_display"),
                "weight_display": metrics.get("weight_display"),
                "plugin_volumetric_display": metrics.get("plugin_volumetric_display"),
                "volumetric_formula": "length_cm * width_cm * height_cm / 6000",
                "volumetric_weight_kg": metrics.get("volumetric_weight_kg"),
            },
            "collected_at": datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        }
    finally:
        if react_reader is not None:
            await react_reader.close()
        try:
            runtime.pages.remove(page)
        except ValueError:
            pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def _collect_async(
    source_url: str,
    requested_count: int,
    *,
    max_workers: int,
    plugin_timeout: float,
    on_page: Callable[[dict[str, Any]], None] | None,
    on_item: Callable[[dict[str, Any]], None] | None,
    on_progress: Callable[[dict[str, Any]], None] | None,
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    runtime = await _open_runtime()
    results: list[dict[str, Any]] = []
    completed = failed = 0
    try:
        try:
            candidates = await _listing_candidates(
                runtime,
                source_url,
                requested_count,
                on_page=on_page,
                stop_event=stop_event,
            )
        except Exception as exc:
            if not _is_browser_closed_error(exc):
                raise
            await _close_runtime(runtime)
            runtime = await _open_runtime()
            candidates = await _listing_candidates(
                runtime,
                source_url,
                requested_count,
                on_page=on_page,
                stop_event=stop_event,
            )
        if not candidates:
            raise RuntimeError(
                "Playwright 在列表页没有识别到商品，请确认链接是 Mercado 商品列表或详情页"
            )
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
                        f"Playwright 正在并发采集 {len(batch)} 个详情页"
                        f"（并发数 {workers}，DOM 读取智赢数据）"
                    ),
                })
            tasks = [
                asyncio.create_task(
                    _collect_detail(
                        runtime,
                        candidate,
                        plugin_timeout=plugin_timeout,
                        stop_event=stop_event,
                    )
                )
                for candidate in batch
            ]
            settled = await asyncio.gather(*tasks, return_exceptions=True)
            retry_indexes = [
                index
                for index, value in enumerate(settled)
                if (
                    isinstance(value, Exception)
                    and not isinstance(value, CollectionStopped)
                )
                or (
                    isinstance(value, dict)
                    and not value.get("title")
                    and not value.get("main_image_url")
                )
            ]
            if retry_indexes:
                if any(
                    isinstance(settled[index], Exception)
                    and _is_browser_closed_error(settled[index])
                    for index in retry_indexes
                ):
                    await _close_runtime(runtime)
                    runtime = await _open_runtime()
                if on_progress:
                    on_progress({
                        "stage": "detail_retry",
                        "current": batch_start + 1,
                        "total": len(candidates),
                        "item_id": " / ".join(
                            batch[index]["source_item_id"] for index in retry_indexes
                        ),
                        "message": f"正在重试 {len(retry_indexes)} 个瞬时失败的详情页",
                    })
                retry_values = await asyncio.gather(
                    *[
                        _collect_detail(
                            runtime,
                            batch[index],
                            plugin_timeout=plugin_timeout,
                            stop_event=stop_event,
                        )
                        for index in retry_indexes
                    ],
                    return_exceptions=True,
                )
                for index, retry_value in zip(retry_indexes, retry_values):
                    settled[index] = retry_value
            for offset, (candidate, value) in enumerate(zip(batch, settled), start=1):
                if isinstance(value, CollectionStopped):
                    raise value
                row = _failure_row(candidate, value) if isinstance(value, Exception) else value
                if row["scrape_status"] == "ok":
                    completed += 1
                else:
                    failed += 1
                results.append(row)
                if on_item:
                    on_item(row)
                if on_progress:
                    on_progress({
                        "stage": "detail",
                        "current": batch_start + offset,
                        "total": len(candidates),
                        "item_id": candidate["source_item_id"],
                        "message": f"已读取 {candidate['source_item_id']} 的页面和智赢 DOM 数据",
                    })
        return {
            "requested_count": requested_count,
            "candidate_count": len(candidates),
            "completed_count": completed,
            "failed_count": failed,
            "browser_mode": "playwright",
            "browser_connection": runtime.connection_mode,
            "rows": results,
        }
    finally:
        await _close_runtime(runtime)


def collect_marketplace_listing_playwright(
    source_url: str,
    requested_count: int,
    *,
    max_workers: int,
    plugin_timeout: float,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    on_item: Callable[[dict[str, Any]], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Collect pages concurrently using Playwright without any RPA actions."""
    source_url, requested_count = validate_collection_request(source_url, requested_count)
    max_workers = normalize_collection_workers(max_workers)
    return asyncio.run(
        _collect_async(
            source_url,
            requested_count,
            max_workers=max_workers,
            plugin_timeout=plugin_timeout,
            on_page=on_page,
            on_item=on_item,
            on_progress=on_progress,
            stop_event=stop_event,
        )
    )


async def _open_login_setup_async(start_url: str) -> None:
    runtime = await _open_runtime()
    page = await _new_page(runtime)
    try:
        try:
            await _goto(page, start_url)
        except Exception:
            # Keep the browser available even if Mercado is temporarily slow;
            # the operator can navigate or retry in the visible window.
            pass
        while not page.is_closed():
            await asyncio.sleep(0.5)
    finally:
        await _close_runtime(runtime)


def open_playwright_login_setup(
    start_url: str = DEFAULT_SETUP_URL,
) -> None:
    """Open the persistent collector profile for a one-time ZYing login.

    The function returns after the operator closes the setup page.  It is
    intended to run in a background thread owned by the workbench.
    """
    asyncio.run(_open_login_setup_async(start_url))


__all__ = [
    "DEFAULT_CDP_URL",
    "ZYING_EXTENSION_ID",
    "collect_marketplace_listing_playwright",
    "discover_zying_extension_dir",
    "open_playwright_login_setup",
]
