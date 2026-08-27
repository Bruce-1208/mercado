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
from urllib.parse import urljoin, urlsplit, urlunsplit

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
    marketplace_url_has_cross_border_filter,
    merge_listing_candidates,
    normalize_collection_scope,
    normalize_collection_workers,
    ocr_plugin_image,
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
    const originalPrice = root.querySelector && root.querySelector(
      '.andes-money-amount--previous, .ui-search-price__original-value .andes-money-amount, ' +
      's.andes-money-amount'
    );
    const currentPrice = root.querySelector && root.querySelector(
      '.poly-price__current .andes-money-amount, .ui-search-price__second-line .andes-money-amount'
    );
    const collectedPrice = originalPrice || currentPrice;
    const fraction = collectedPrice ? collectedPrice.querySelector(
      '.andes-money-amount__fraction'
    ) : (root.querySelector && root.querySelector('.andes-money-amount__fraction'));
    const cents = collectedPrice ? collectedPrice.querySelector(
      '.andes-money-amount__cents'
    ) : (root.querySelector && root.querySelector('.andes-money-amount__cents'));
    let price = fraction ? clean(fraction.textContent).replace(/\D/g, '') : '';
    if (price && cents) price += '.' + clean(cents.textContent).replace(/\D/g, '');
    const cardText = clean(root.innerText || root.textContent || '');
    rows.push({
      href: link.href,
      title: clean((titleNode && titleNode.textContent) || link.textContent),
      main_image_url: imageUrl(img),
      price,
      currency_id: 'MXN',
      is_cross_border: /(^|\s)internacional(\s|$)/i.test(cardText)
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
  const originalPrice = first([
    '.ui-pdp-price__original-value .andes-money-amount',
    '.ui-pdp-price__second-line .andes-money-amount--previous',
    '.andes-money-amount--previous',
    's.andes-money-amount'
  ]);
  const visibleFraction = originalPrice
    ? originalPrice.querySelector('.andes-money-amount__fraction')
    : first(['.ui-pdp-price__second-line .andes-money-amount__fraction']);
  const visibleCents = originalPrice
    ? originalPrice.querySelector('.andes-money-amount__cents')
    : first(['.ui-pdp-price__second-line .andes-money-amount__cents']);
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
    price: originalPrice ? visiblePrice : ((metaPrice && metaPrice.content) || offer.price || visiblePrice || ''),
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
    bitbrowser_window_id: str = ""


@dataclass
class _DetailPageSlot:
    page: Any
    react_reader: Any


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
  const reactNodes = [];
  const visitedNodes = new Set();
  for (const root of roots) {
    let nodes = [];
    try { nodes = root.querySelectorAll('*'); } catch (_) {}
    for (const node of nodes) {
      if (visitedNodes.has(node)) continue;
      const keys = Object.keys(node);
      if (keys.some(key => (
        key.startsWith('__reactFiber$') || key.startsWith('__reactProps$')
      ))) {
        visitedNodes.add(node);
        reactNodes.push(node);
      }
    }
  }
  const result = {found: reactNodes.length > 0, metrics: {}, data: {}};
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
  for (const node of reactNodes) {
    const keys = Object.keys(node);
    const fiberKey = keys.find(key => key.startsWith('__reactFiber$'));
    const propsKey = keys.find(key => key.startsWith('__reactProps$'));
    const fiber = fiberKey ? node[fiberKey] : null;
    const fiberProps = fiber && fiber.memoizedProps ? fiber.memoizedProps : null;
    const directProps = propsKey ? node[propsKey] : null;
    captureValue(fiberProps, new Set());
    if (directProps !== fiberProps) captureValue(directProps, new Set());
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
                # BitBrowser can install the same ZYing extension under a
                # different generated ID.  Probe every extension isolated
                # world; the evaluator itself only accepts ZYing metric nodes.
                if not str(context.get("origin") or "").startswith(
                    "chrome-extension://"
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
                if (
                    isinstance(value, dict)
                    and value.get("found")
                    and (value.get("data") or value.get("metrics"))
                ):
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


async def _open_runtime(bitbrowser_window_id: str = "") -> _PlaywrightRuntime:
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    bitbrowser_window_id = str(bitbrowser_window_id or "").strip()
    if bitbrowser_window_id:
        from bit.bit_api import openBrowser, releaseBrowserLease

        browser_info = openBrowser(bitbrowser_window_id)
        data = browser_info.get("data") if isinstance(browser_info, dict) else None
        if not data or not data.get("http"):
            await playwright.stop()
            message = (
                browser_info.get("msg")
                if isinstance(browser_info, dict)
                else str(browser_info or "")
            )
            raise RuntimeError(f"打开比特采集窗口失败：{message or browser_info}")
        cdp_url = str(data["http"]).strip()
        if not cdp_url.startswith(("http://", "https://")):
            cdp_url = f"http://{cdp_url}"
        try:
            browser = await playwright.chromium.connect_over_cdp(
                cdp_url, timeout=30000
            )
            if not browser.contexts:
                raise RuntimeError("比特采集窗口没有可用的浏览器上下文")
        except Exception:
            releaseBrowserLease(bitbrowser_window_id)
            await playwright.stop()
            raise
        return _PlaywrightRuntime(
            playwright=playwright,
            browser=browser,
            context=browser.contexts[0],
            managed=False,
            connection_mode=f"bitbrowser:{bitbrowser_window_id}",
            bitbrowser_window_id=bitbrowser_window_id,
        )
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
    if runtime.bitbrowser_window_id:
        try:
            from bit.bit_api import releaseBrowserLease

            releaseBrowserLease(runtime.bitbrowser_window_id)
        except Exception:
            pass


def _should_block_resource(
    resource_type: str,
    request_url: str,
    frame_url: str,
    *,
    allow_images: bool,
) -> bool:
    """Keep listing images because Mercado now waits for them before rendering cards."""
    combined_url = f"{request_url} {frame_url}".lower()
    if any(
        marker in combined_url
        for marker in ("account-verification", "/captcha/", "buyer-login")
    ):
        return False
    if resource_type in {"media", "font"}:
        return True
    return resource_type == "image" and not allow_images


async def _new_page(
    runtime: _PlaywrightRuntime,
    *,
    optimize_resources: bool = True,
) -> Any:
    page = await runtime.context.new_page()
    runtime.pages.append(page)
    page.set_default_timeout(float(os.environ.get("MERCADO_PLAYWRIGHT_TIMEOUT_MS", "30000")))

    # Installing any Playwright route handler on current Mercado search pages
    # can hold BitBrowser on its temporary loader document, even if every
    # request is continued.  Listing pages therefore bypass interception
    # entirely; detail tabs retain the bandwidth-saving policy below.
    if not optimize_resources:
        return page

    async def skip_heavy_assets(route: Any) -> None:
        request_url = str(route.request.url or "").lower()
        frame_url = str(getattr(route.request.frame, "url", "") or "").lower()
        if _should_block_resource(
            str(route.request.resource_type or "").lower(),
            request_url,
            frame_url,
            allow_images=False,
        ):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", skip_heavy_assets)
    return page


async def _open_detail_page_pool(
    runtime: _PlaywrightRuntime, size: int
) -> asyncio.Queue[_DetailPageSlot] | None:
    """Prewarm reusable pages/CDP readers; fake or legacy runtimes use the old path."""
    if not callable(getattr(getattr(runtime, "context", None), "new_page", None)):
        return None
    queue: asyncio.Queue[_DetailPageSlot] = asyncio.Queue()
    try:
        for _ in range(max(1, int(size))):
            page = await _new_page(runtime)
            reader = await _PluginMetricReader.open(page)
            await queue.put(_DetailPageSlot(page=page, react_reader=reader))
    except Exception:
        await _close_detail_page_pool(runtime, queue)
        raise
    return queue


async def _close_detail_page_pool(
    runtime: _PlaywrightRuntime,
    queue: asyncio.Queue[_DetailPageSlot] | None,
) -> None:
    if queue is None:
        return
    while not queue.empty():
        slot = await queue.get()
        try:
            await slot.react_reader.close()
        except Exception:
            pass
        try:
            runtime.pages.remove(slot.page)
        except (AttributeError, ValueError):
            pass
        try:
            if not slot.page.is_closed():
                await slot.page.close()
        except Exception:
            pass


async def _goto(
    page: Any,
    url: str,
    *,
    wait_until: str = "commit",
) -> None:
    timeout = float(os.environ.get("MERCADO_PLAYWRIGHT_NAVIGATION_TIMEOUT_MS", "20000"))
    try:
        # Detail pages use ``commit`` to avoid waiting on tracking requests.
        # Listing pages opt into ``domcontentloaded`` so Mercado can leave its
        # temporary ``?loader=true`` page before card extraction starts.
        await page.goto(url, wait_until=wait_until, timeout=timeout)
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


async def _wait_for_product_detail(page: Any, timeout: float = 10000) -> bool:
    """Return true only for a Mercado product title, not an error-page ``h1``."""
    try:
        await page.wait_for_selector(
            'h1.ui-pdp-title, [data-testid="product-title"]',
            timeout=timeout,
        )
        return True
    except Exception:
        return False


async def _wait_for_listing_dom(page: Any, timeout: float = 4.0) -> None:
    """Wait until lazy-loaded search cards stop growing before extracting."""
    deadline = asyncio.get_running_loop().time() + max(1.0, timeout)
    previous_count = -1
    stable_rounds = 0
    scrolled = False
    while asyncio.get_running_loop().time() < deadline:
        try:
            count = await page.locator(
                'li.ui-search-layout__item, .ui-search-result, .poly-card, [data-testid="result"]'
            ).count()
            if count > 0 and count == previous_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous_count = count
            if count > 0 and not scrolled:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                scrolled = True
            if stable_rounds >= 2:
                return
        except Exception as exc:
            if not _is_navigation_context_error(exc):
                return
        await asyncio.sleep(0.35)


def _synthesized_listing_page_url(source_url: str, page_number: int) -> str:
    """Build Mercado's 48-item offset URL when NoIndex hides pagination links."""
    offset = max(1, (int(page_number) - 1) * 48 + 1)
    parts = urlsplit(str(source_url or ""))
    path = re.sub(r"_Desde_\d+", f"_Desde_{offset}", parts.path, flags=re.I)
    if path == parts.path:
        marker = re.search(r"_NoIndex_", path, flags=re.I)
        if marker:
            path = f"{path[:marker.start()]}_Desde_{offset}{path[marker.start():]}"
        else:
            path = f"{path}_Desde_{offset}"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


async def _listing_candidates(
    runtime: _PlaywrightRuntime,
    source_url: str,
    requested_count: int,
    *,
    collection_scope: str = "all",
    on_page: Callable[[dict[str, Any]], None] | None,
    stop_event: threading.Event | None,
) -> list[dict[str, Any]]:
    # Current Mercado search pages can leave the result grid empty when their
    # lazy picture requests are aborted.  Detail pages still use the lighter
    # default route policy, so this does not multiply detail-tab bandwidth.
    page = await _new_page(runtime, optimize_resources=False)
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
        await _goto(page, page_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(
                'li.ui-search-layout__item, .poly-card, h1.ui-pdp-title, h1',
                timeout=8000,
            )
        except Exception:
            pass
        await _wait_for_listing_dom(page)
        snapshot = await _evaluate_after_navigation(page, LISTING_DOM_SCRIPT)
        actual_url = page.url
        blocked = _blocked_page_message(actual_url, str(snapshot.get("body") or ""))
        if blocked:
            if on_page:
                on_page({
                    "stage": "waiting_verification",
                    "page": page_number,
                    "page_url": actual_url,
                    "page_items": 0,
                    "candidate_count": len(candidates),
                    "browser": runtime.connection_mode,
                    "message": (
                        "Mercado 要求安全验证：采集浏览器已保留，请在窗口中完成一次验证，"
                        "完成后任务会自动继续"
                    ),
                })
            try:
                await page.bring_to_front()
            except Exception:
                pass
            try:
                verification_timeout = max(
                    30.0,
                    min(
                        float(
                            os.environ.get(
                                "MERCADO_PLAYWRIGHT_VERIFICATION_TIMEOUT_SECONDS",
                                "300",
                            )
                        ),
                        900.0,
                    ),
                )
            except ValueError:
                verification_timeout = 300.0
            deadline = asyncio.get_running_loop().time() + verification_timeout
            while asyncio.get_running_loop().time() < deadline:
                _check_stop(stop_event)
                await asyncio.sleep(1.0)
                try:
                    await _wait_for_listing_dom(page, timeout=1.5)
                    snapshot = await _evaluate_after_navigation(page, LISTING_DOM_SCRIPT)
                except Exception as exc:
                    if _is_navigation_context_error(exc):
                        continue
                    raise
                actual_url = page.url
                blocked = _blocked_page_message(
                    actual_url, str(snapshot.get("body") or "")
                )
                try:
                    resolved_item_id = extract_listing_item_id(actual_url)
                except ValueError:
                    resolved_item_id = ""
                if not blocked and (snapshot.get("rows") or resolved_item_id):
                    if on_page:
                        on_page({
                            "stage": "verification_resolved",
                            "page": page_number,
                            "page_url": actual_url,
                            "page_items": 0,
                            "candidate_count": len(candidates),
                            "browser": runtime.connection_mode,
                            "message": "安全验证已完成，正在继续扫描商品列表",
                        })
                    break
            if blocked:
                raise RuntimeError(
                    f"{blocked}；等待 {int(verification_timeout)} 秒仍未完成验证"
                )
        before = len(candidates)
        page_rows = [] if page_number == 1 and direct_source_item_id else snapshot.get("rows") or []
        if (
            collection_scope == "cross_border"
            and marketplace_url_has_cross_border_filter(source_url)
        ):
            # The filtered Mercado result page does not consistently render
            # the "Internacional" badge on every card layout.  Its explicit
            # server-side shipping-origin filter is the authoritative signal.
            page_rows = [{**row, "is_cross_border": True} for row in page_rows]
        merge_listing_candidates(
            candidates,
            page_rows,
            requested_count,
            collection_scope=collection_scope,
        )
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
        raw_next_url = str(snapshot.get("next_url") or "").strip()
        next_url = urljoin(actual_url, raw_next_url) if raw_next_url else ""
        if next_url:
            current_parts = urlsplit(actual_url)
            next_parts = urlsplit(next_url)
            if (
                current_parts.scheme,
                current_parts.netloc,
                current_parts.path,
                current_parts.query,
            ) == (
                next_parts.scheme,
                next_parts.netloc,
                next_parts.path,
                next_parts.query,
            ):
                next_url = ""
        if (
            not next_url
            and len(snapshot.get("rows") or []) >= 40
            and len(candidates) < requested_count
        ):
            # NoIndex result pages often omit the next-page anchor even though
            # more results exist.  Stop if a synthesized page produced no new
            # IDs; otherwise advance using Mercado's standard 48-item offset.
            if page_number > 1 and len(candidates) == before:
                break
            next_url = _synthesized_listing_page_url(
                source_url, page_number + 1
            )
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


def _plugin_text_is_visually_protected(lines: Iterable[Any]) -> bool:
    """Detect the dense placeholder text emitted by visual-text protection."""
    values = [re.sub(r"\s+", "", str(value or "")).lower() for value in lines]
    return any(
        len(value) >= 20 and re.fullmatch(r"[a-z0-9|]+", value)
        for value in values
    )


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
    poll_number = 0
    while asyncio.get_running_loop().time() < deadline:
        poll_number += 1
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
        # React props are the fast path.  A full shadow-DOM/frame scan is much
        # heavier, so use it only initially and then every third poll.
        if not last_lines or poll_number % 3 == 0:
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
        if (
            react_reader is None
            and last_lines
            and _plugin_text_is_visually_protected(last_lines)
        ):
            # The values are already visible but intentionally represented as
            # blob-backed glyphs.  Without an isolated-context reader, waiting
            # longer cannot make DOM text readable, so use OCR.  When the
            # reader is present, however, ZYing's own API can still replace
            # this initial empty snapshot with structured weight/size data.
            return last_metrics, last_lines
        await asyncio.sleep(0.25)
    return last_metrics, last_lines


async def _ocr_visible_plugin_metrics(page: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """OCR the visible ZYing metric strip when another extension hides DOM text."""
    columns = page.locator(".zying-meli-detail-metric-column")
    boxes = []
    for index in range(await columns.count()):
        try:
            box = await columns.nth(index).bounding_box()
        except Exception:
            box = None
        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
            boxes.append(box)
    if not boxes:
        return parse_plugin_metrics(""), {}
    left = min(box["x"] for box in boxes)
    top = min(box["y"] for box in boxes)
    right = max(box["x"] + box["width"] for box in boxes)
    bottom = max(box["y"] + box["height"] for box in boxes)
    image = await page.screenshot(
        type="png",
        clip={
            "x": max(0.0, left - 12.0),
            "y": max(0.0, top - 10.0),
            "width": right - left + 24.0,
            "height": bottom - top + 20.0,
        },
    )
    ocr = await asyncio.to_thread(ocr_plugin_image, image)
    return parse_plugin_metrics(str(ocr.get("text") or "")), ocr


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
    page: Any | None = None,
    react_reader: _PluginMetricReader | None = None,
) -> dict[str, Any]:
    _check_stop(stop_event)
    owns_page = page is None
    if page is None:
        page = await _new_page(runtime)
    # Attach CDP before navigation.  Runtime.enable only reports extension
    # isolated worlds created after the session starts in current BitBrowser;
    # attaching after Mercado has loaded misses ZYing's React context and
    # leaves the visible weight/size panel unreadable.
    owns_reader = react_reader is None
    if react_reader is None:
        react_reader = await _PluginMetricReader.open(page)
    try:
        primary_url = str(candidate["source_url"])
        fallback_url = str(candidate.get("listing_url") or "").strip()
        try:
            await _goto(page, primary_url)
        except Exception:
            if not fallback_url or fallback_url == primary_url:
                raise
            await _goto(page, fallback_url)
        detail_ready = await _wait_for_product_detail(page, timeout=10000)
        if (
            not detail_ready
            and fallback_url
            and fallback_url != primary_url
        ):
            await _goto(page, fallback_url)
            await _wait_for_product_detail(page, timeout=10000)
        details = await _evaluate_after_navigation(page, DETAIL_DOM_SCRIPT)
        blocked = _blocked_page_message(page.url, str(details.get("body") or ""))
        if blocked:
            raise RuntimeError(blocked)

        metrics, plugin_lines = await _wait_for_plugin_metrics(
            page,
            plugin_timeout,
            stop_event,
            react_reader=react_reader,
        )
        ocr_snapshot: dict[str, Any] = {}
        metric_keys = (
            "weight_g",
            "volumetric_weight_kg",
            "package_length_cm",
            "package_width_cm",
            "package_height_cm",
            "dimensions_display",
            "weight_display",
            "plugin_volumetric_display",
        )
        if not all(
            metrics.get(key) is not None
            for key in (
                "weight_g",
                "package_length_cm",
                "package_width_cm",
                "package_height_cm",
            )
        ):
            try:
                ocr_metrics, ocr_snapshot = await _ocr_visible_plugin_metrics(page)
                for key in metric_keys:
                    if metrics.get(key) is None and ocr_metrics.get(key) is not None:
                        metrics[key] = ocr_metrics[key]
            except Exception as exc:
                ocr_snapshot = {"error": str(exc)}

        weight_basis = "plugin_actual"
        if (
            metrics.get("weight_g") is None
            and metrics.get("volumetric_weight_kg") is not None
            and all(
                metrics.get(key) is not None
                for key in (
                    "package_length_cm",
                    "package_width_cm",
                    "package_height_cm",
                )
            )
        ):
            # Some ZYing layouts expose dimensions and chargeable volumetric
            # weight but omit actual weight.  Using the larger chargeable value
            # is conservative for shipping and is explicitly labelled.
            metrics["weight_g"] = float(metrics["volumetric_weight_kg"]) * 1000.0
            weight_basis = "plugin_volumetric_fallback"
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
        ocr_text = str(ocr_snapshot.get("text") or "").strip()
        readable_plugin_text = " ".join(
            value for value in (plugin_text, ocr_text) if value
        )
        if not plugin_lines and not ocr_text:
            errors.append(
                "详情页未检测到智赢插件重量尺寸，请确认 Playwright 采集浏览器已登录智赢"
            )
        elif re.search(r"(?:^|\s)登录(?:\s|$)", readable_plugin_text):
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
            "weight_basis": weight_basis,
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
                "read_method": (
                    "playwright_shadow_dom_ocr_fallback"
                    if ocr_snapshot
                    else "playwright_shadow_dom"
                ),
                "dom_lines": plugin_lines,
                "dom_text": plugin_text,
                "ocr_text": ocr_text,
                "ocr_confidence": ocr_snapshot.get("confidence"),
                "ocr_error": ocr_snapshot.get("error"),
                "weight_basis": weight_basis,
                "dimensions_display": metrics.get("dimensions_display"),
                "weight_display": metrics.get("weight_display"),
                "plugin_volumetric_display": metrics.get("plugin_volumetric_display"),
                "volumetric_formula": "length_cm * width_cm * height_cm / 6000",
                "volumetric_weight_kg": metrics.get("volumetric_weight_kg"),
            },
            "collected_at": datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        }
    finally:
        if owns_reader and react_reader is not None:
            await react_reader.close()
        if owns_page:
            try:
                runtime.pages.remove(page)
            except ValueError:
                pass
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass


async def _repair_items_async(
    rows: Iterable[Mapping[str, Any]],
    *,
    window_id: str,
    plugin_timeout: float,
    attempts: int,
    on_item: Callable[[dict[str, Any]], None] | None,
    on_progress: Callable[[dict[str, Any]], None] | None,
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    """Re-read incomplete details sequentially in one reusable browser context."""
    candidates = [
        {
            "source_item_id": str(row.get("source_item_id") or ""),
            "source_url": str(row.get("source_url") or row.get("final_url") or ""),
            "listing_url": str(row.get("final_url") or ""),
            "main_image_url": str(row.get("main_image_url") or ""),
            "title": str(row.get("title") or ""),
            "price": row.get("price"),
            "currency_id": str(row.get("currency_id") or "MXN"),
        }
        for row in rows
        if str(row.get("scrape_status") or "").lower() != "ok"
        and str(row.get("source_item_id") or "").strip()
        and str(row.get("source_url") or row.get("final_url") or "").strip()
    ]
    if not candidates:
        return {"requested_count": 0, "completed_count": 0, "failed_count": 0, "rows": []}

    attempts = max(1, min(int(attempts), 3))
    runtime = await (_open_runtime(window_id) if window_id else _open_runtime())
    detail_page_pool = await _open_detail_page_pool(runtime, 1)
    detail_slot = (
        await detail_page_pool.get() if detail_page_pool is not None else None
    )
    repaired: list[dict[str, Any]] = []
    completed = failed = 0
    consecutive_failures = 0
    try:
        failure_limit = max(
            1,
            min(
                int(os.environ.get("MERCADO_PLAYWRIGHT_REPAIR_FAILURE_LIMIT", "6")),
                20,
            ),
        )
    except ValueError:
        failure_limit = 6
    try:
        for index, candidate in enumerate(candidates, start=1):
            _check_stop(stop_event)
            value: Any = None
            for attempt in range(attempts):
                try:
                    value = await _collect_detail(
                        runtime,
                        candidate,
                        plugin_timeout=plugin_timeout,
                        stop_event=stop_event,
                        **({
                            "page": detail_slot.page,
                            "react_reader": detail_slot.react_reader,
                        } if detail_slot is not None else {}),
                    )
                except CollectionStopped:
                    raise
                except Exception as exc:
                    value = exc
                if isinstance(value, Mapping) and value.get("scrape_status") == "ok":
                    break
                if attempt + 1 < attempts:
                    await asyncio.sleep(1.0)
            row = _failure_row(candidate, value) if isinstance(value, Exception) else dict(value)
            repaired.append(row)
            if row.get("scrape_status") == "ok":
                completed += 1
                consecutive_failures = 0
            else:
                failed += 1
                consecutive_failures += 1
            if on_item:
                await asyncio.to_thread(on_item, row)
            if on_progress:
                on_progress({
                    "stage": "detail_repair",
                    "current": index,
                    "total": len(candidates),
                    "item_id": candidate["source_item_id"],
                    "message": (
                        f"正在低并发补采重量尺寸（{index}/{len(candidates)}），"
                        f"已修复 {completed} 件"
                    ),
                })
            if consecutive_failures >= failure_limit:
                if on_progress:
                    on_progress({
                        "stage": "detail_repair_stopped",
                        "current": index,
                        "total": len(candidates),
                        "item_id": candidate["source_item_id"],
                        "message": (
                            f"低并发补采连续 {consecutive_failures} 件没有恢复重量尺寸，"
                            "已停止无效重试"
                        ),
                    })
                break
            # ZYing injects metrics asynchronously and becomes unreliable when
            # incomplete items immediately trigger another navigation.
            if index < len(candidates):
                await asyncio.sleep(0.4)
        return {
            "requested_count": len(candidates),
            "attempted_count": len(repaired),
            "skipped_count": len(candidates) - len(repaired),
            "completed_count": completed,
            "failed_count": failed,
            "browser_connection": runtime.connection_mode,
            "rows": repaired,
        }
    finally:
        if detail_slot is not None and detail_page_pool is not None:
            await detail_page_pool.put(detail_slot)
        await _close_detail_page_pool(runtime, detail_page_pool)
        await _close_runtime(runtime)


async def _collect_async(
    source_url: str,
    requested_count: int,
    *,
    collection_scope: str = "all",
    window_id: str = "",
    max_workers: int,
    plugin_timeout: float,
    on_page: Callable[[dict[str, Any]], None] | None,
    on_item: Callable[[dict[str, Any]], None] | None,
    on_progress: Callable[[dict[str, Any]], None] | None,
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    async def open_runtime() -> _PlaywrightRuntime:
        return await (_open_runtime(window_id) if window_id else _open_runtime())

    runtime = await open_runtime()
    results: list[dict[str, Any]] = []
    completed = failed = 0
    detail_page_pool: asyncio.Queue[_DetailPageSlot] | None = None
    try:
        try:
            candidates = await _listing_candidates(
                runtime,
                source_url,
                requested_count,
                collection_scope=collection_scope,
                on_page=on_page,
                stop_event=stop_event,
            )
        except Exception as exc:
            if not _is_browser_closed_error(exc):
                raise
            await _close_runtime(runtime)
            runtime = await open_runtime()
            candidates = await _listing_candidates(
                runtime,
                source_url,
                requested_count,
                collection_scope=collection_scope,
                on_page=on_page,
                stop_event=stop_event,
            )
        if not candidates:
            raise RuntimeError(
                (
                    "列表页没有识别到带 Internacional 标识的跨境卖家商品"
                    if collection_scope == "cross_border"
                    else "Playwright 在列表页没有识别到商品，请确认链接是 Mercado 商品列表或详情页"
                )
            )
        workers = normalize_collection_workers(max_workers)
        semaphore = asyncio.Semaphore(workers)
        detail_page_pool = await _open_detail_page_pool(
            runtime, min(workers, len(candidates))
        )
        navigation_lock = asyncio.Lock()
        verification_probe_lock = asyncio.Lock()
        next_navigation_at = 0.0
        verification_blocked = False
        verification_cooldown_until = 0.0
        verification_incidents = 0
        verification_recovery_successes = 0
        try:
            navigation_stagger = max(
                0.0,
                min(
                    float(
                        os.environ.get(
                            "MERCADO_PLAYWRIGHT_NAVIGATION_STAGGER_SECONDS", "0.30"
                        )
                    ),
                    3.0,
                ),
            )
        except ValueError:
            navigation_stagger = 0.30
        try:
            fast_plugin_timeout = max(
                1.0,
                min(
                    float(plugin_timeout),
                    float(os.environ.get("MERCADO_PLAYWRIGHT_FAST_PLUGIN_TIMEOUT", "4")),
                ),
            )
        except ValueError:
            fast_plugin_timeout = min(float(plugin_timeout), 4.0)

        try:
            verification_cooldown = max(
                0.0,
                min(
                    float(
                        os.environ.get(
                            "MERCADO_PLAYWRIGHT_VERIFICATION_COOLDOWN_SECONDS", "20"
                        )
                    ),
                    180.0,
                ),
            )
        except ValueError:
            verification_cooldown = 20.0
        try:
            verification_max_cooldown = max(
                verification_cooldown,
                min(
                    float(
                        os.environ.get(
                            "MERCADO_PLAYWRIGHT_VERIFICATION_MAX_COOLDOWN_SECONDS",
                            "90",
                        )
                    ),
                    300.0,
                ),
            )
        except ValueError:
            verification_max_cooldown = max(verification_cooldown, 90.0)
        try:
            verification_attempts = max(
                2,
                min(
                    int(
                        os.environ.get(
                            "MERCADO_PLAYWRIGHT_VERIFICATION_ATTEMPTS", "5"
                        )
                    ),
                    8,
                ),
            )
        except ValueError:
            verification_attempts = 5
        try:
            recovery_target = max(
                1,
                min(
                    int(
                        os.environ.get(
                            "MERCADO_PLAYWRIGHT_VERIFICATION_RECOVERY_SUCCESSES", "3"
                        )
                    ),
                    10,
                ),
            )
        except ValueError:
            recovery_target = 3

        def is_verification_failure(value: Any) -> bool:
            message = str(
                value.get("error_message") if isinstance(value, Mapping) else value
            ).lower()
            return any(
                marker in message
                for marker in (
                    "验证页",
                    "买家验证",
                    "安全验证",
                    "captcha",
                    "account-verification",
                    "buyer-login",
                )
            )

        def mark_verification_block(*, probe_failed: bool) -> None:
            nonlocal verification_blocked
            nonlocal verification_cooldown_until
            nonlocal verification_incidents
            nonlocal verification_recovery_successes
            if not verification_blocked or probe_failed:
                verification_incidents += 1
            verification_blocked = True
            verification_recovery_successes = 0
            delay = min(
                verification_cooldown * (2 ** max(verification_incidents - 1, 0)),
                verification_max_cooldown,
            )
            verification_cooldown_until = max(
                verification_cooldown_until,
                asyncio.get_running_loop().time() + delay,
            )
            if on_progress:
                on_progress({
                    "stage": "waiting_verification",
                    "current": len(results),
                    "total": len(candidates),
                    "item_id": "",
                    "message": (
                        "Mercado 触发买家验证，已暂停并发请求，"
                        f"冷却 {delay:g} 秒后自动单线程探测恢复"
                    ),
                })

        async def collect_detail_with_verification_gate(
            active_runtime: _PlaywrightRuntime,
            candidate: Mapping[str, Any],
            slot: _DetailPageSlot | None,
        ) -> Any:
            nonlocal verification_blocked
            nonlocal verification_recovery_successes
            if verification_blocked:
                async with verification_probe_lock:
                    if verification_blocked:
                        remaining = (
                            verification_cooldown_until
                            - asyncio.get_running_loop().time()
                        )
                        if remaining > 0:
                            await asyncio.sleep(remaining)
                        _check_stop(stop_event)
                        try:
                            value: Any = await _collect_detail(
                                active_runtime,
                                candidate,
                                plugin_timeout=fast_plugin_timeout,
                                stop_event=stop_event,
                                **({
                                    "page": slot.page,
                                    "react_reader": slot.react_reader,
                                } if slot is not None else {}),
                            )
                        except CollectionStopped:
                            raise
                        except Exception as exc:
                            value = exc
                        if is_verification_failure(value):
                            mark_verification_block(probe_failed=True)
                        else:
                            verification_recovery_successes += 1
                            if verification_recovery_successes >= recovery_target:
                                verification_blocked = False
                                if on_progress:
                                    on_progress({
                                        "stage": "verification_resolved",
                                        "current": len(results),
                                        "total": len(candidates),
                                        "item_id": str(
                                            candidate.get("source_item_id") or ""
                                        ),
                                        "message": (
                                            "买家验证限制已恢复，正在逐步恢复并发采集"
                                        ),
                                    })
                        return value
            try:
                value = await _collect_detail(
                    active_runtime,
                    candidate,
                    plugin_timeout=fast_plugin_timeout,
                    stop_event=stop_event,
                    **({
                        "page": slot.page,
                        "react_reader": slot.react_reader,
                    } if slot is not None else {}),
                )
            except CollectionStopped:
                raise
            except Exception as exc:
                value = exc
            if is_verification_failure(value):
                mark_verification_block(probe_failed=False)
            return value

        async def wait_for_navigation_slot() -> None:
            """Keep parallel tabs, but avoid a burst of simultaneous navigations."""
            nonlocal next_navigation_at
            async with navigation_lock:
                loop = asyncio.get_running_loop()
                remaining = next_navigation_at - loop.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                next_navigation_at = loop.time() + navigation_stagger

        async def collect_one(
            active_runtime: _PlaywrightRuntime,
            index: int,
            candidate: Mapping[str, Any],
        ) -> tuple[int, Mapping[str, Any], Any]:
            async with semaphore:
                slot = await detail_page_pool.get() if detail_page_pool is not None else None
                value: Any = None
                blocked_seen = False
                try:
                    for attempt in range(verification_attempts):
                        _check_stop(stop_event)
                        await wait_for_navigation_slot()
                        value = await collect_detail_with_verification_gate(
                            active_runtime, candidate, slot
                        )
                        incomplete = (
                            isinstance(value, dict)
                            and (
                                (not value.get("title") and not value.get("main_image_url"))
                                or value.get("scrape_status") in {"partial", "failed"}
                            )
                        )
                        if not isinstance(value, Exception) and not incomplete:
                            break
                        if isinstance(value, Exception) and _is_browser_closed_error(value):
                            break
                        blocked = is_verification_failure(value)
                        blocked_seen = blocked_seen or blocked
                        allowed_attempts = verification_attempts if blocked_seen else 2
                        if attempt + 1 >= allowed_attempts:
                            break
                        if not blocked:
                            delay_key = "MERCADO_PLAYWRIGHT_RETRY_SECONDS"
                            default_delay = 0.35
                            try:
                                retry_delay = max(
                                    0.0,
                                    min(float(os.environ.get(delay_key, str(default_delay))), 10.0),
                                )
                            except ValueError:
                                retry_delay = default_delay
                            await asyncio.sleep(
                                retry_delay + (index % workers) * 0.05
                            )
                    return index, candidate, value
                finally:
                    if slot is not None and detail_page_pool is not None:
                        await detail_page_pool.put(slot)

        async def save_result(
            candidate: Mapping[str, Any], value: Any, current: int
        ) -> None:
            nonlocal completed, failed
            if isinstance(value, CollectionStopped):
                raise value
            row = _failure_row(candidate, value) if isinstance(value, Exception) else value
            if row["scrape_status"] == "ok":
                completed += 1
            else:
                failed += 1
            results.append(row)
            if on_item:
                await asyncio.to_thread(on_item, row)
            if on_progress:
                on_progress({
                    "stage": "detail",
                    "current": current,
                    "total": len(candidates),
                    "item_id": candidate["source_item_id"],
                    "message": (
                        f"已读取 {candidate['source_item_id']} 的页面和智赢 DOM 数据"
                        f"（{current}/{len(candidates)}）"
                    ),
                })

        if on_progress:
            on_progress({
                "stage": "detail_pool",
                "current": 0,
                "total": len(candidates),
                "item_id": "",
                "message": (
                    f"正在用 {workers} 个常驻详情页采集 {len(candidates)} 个商品，"
                    f"插件快速等待 {fast_plugin_timeout:g} 秒"
                ),
            })

        tasks = [
            asyncio.create_task(collect_one(runtime, index, candidate))
            for index, candidate in enumerate(candidates)
        ]
        closed_context_rows: list[tuple[int, Mapping[str, Any]]] = []
        saved_count = 0
        for future in asyncio.as_completed(tasks):
            index, candidate, value = await future
            if isinstance(value, Exception) and _is_browser_closed_error(value):
                closed_context_rows.append((index, candidate))
                continue
            saved_count += 1
            await save_result(candidate, value, saved_count)

        if closed_context_rows:
            await _close_detail_page_pool(runtime, detail_page_pool)
            detail_page_pool = None
            await _close_runtime(runtime)
            runtime = await open_runtime()
            detail_page_pool = await _open_detail_page_pool(
                runtime, min(workers, len(closed_context_rows))
            )
            if on_progress:
                on_progress({
                    "stage": "detail_retry",
                    "current": saved_count,
                    "total": len(candidates),
                    "item_id": " / ".join(
                        candidate["source_item_id"]
                        for _, candidate in closed_context_rows
                    ),
                    "message": f"浏览器上下文已恢复，重试 {len(closed_context_rows)} 个商品",
                })
            retry_tasks = [
                asyncio.create_task(collect_one(runtime, index, candidate))
                for index, candidate in closed_context_rows
            ]
            for future in asyncio.as_completed(retry_tasks):
                _, candidate, value = await future
                saved_count += 1
                await save_result(candidate, value, saved_count)
        return {
            "requested_count": requested_count,
            "candidate_count": len(candidates),
            "completed_count": completed,
            "failed_count": failed,
            "browser_mode": "playwright",
            "browser_connection": runtime.connection_mode,
            "collection_scope": collection_scope,
            "rows": results,
        }
    finally:
        await _close_detail_page_pool(runtime, detail_page_pool)
        await _close_runtime(runtime)


def collect_marketplace_listing_playwright(
    source_url: str,
    requested_count: int,
    *,
    max_workers: int,
    window_id: str = "",
    collection_scope: str = "all",
    plugin_timeout: float,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    on_item: Callable[[dict[str, Any]], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Collect pages concurrently using Playwright without any RPA actions."""
    source_url, requested_count = validate_collection_request(source_url, requested_count)
    max_workers = normalize_collection_workers(max_workers)
    collection_scope = normalize_collection_scope(collection_scope)
    return asyncio.run(
        _collect_async(
            source_url,
            requested_count,
            window_id=window_id,
            collection_scope=collection_scope,
            max_workers=max_workers,
            plugin_timeout=plugin_timeout,
            on_page=on_page,
            on_item=on_item,
            on_progress=on_progress,
            stop_event=stop_event,
        )
    )


def repair_marketplace_items_playwright(
    rows: Iterable[Mapping[str, Any]],
    *,
    window_id: str,
    plugin_timeout: float = 30.0,
    attempts: int = 1,
    on_item: Callable[[dict[str, Any]], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Sequential quality pass for rows that missed ZYing weight/dimensions."""
    return asyncio.run(
        _repair_items_async(
            rows,
            window_id=str(window_id or "").strip(),
            plugin_timeout=max(1.0, float(plugin_timeout)),
            attempts=attempts,
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
    "repair_marketplace_items_playwright",
]
