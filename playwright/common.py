import importlib
import sys
import time
from pathlib import Path

from bit.bit_api import closeBrowser, openBrowser


PROJECT_ROOT = Path(__file__).resolve().parent.parent

SITE_COUNTRY_MAP = {
    "墨西哥": "Mexico",
    "MX": "Mexico",
    "MLM": "Mexico",
    "巴西": "Brazil",
    "BR": "Brazil",
    "MLB": "Brazil",
    "哥伦比亚": "Colombia",
    "CO": "Colombia",
    "MCO": "Colombia",
    "智利": "Chile",
    "CL": "Chile",
    "MLC": "Chile",
    "阿根廷": "Argentina",
    "AR": "Argentina",
    "MLA": "Argentina",
    "乌拉圭": "Uruguay",
    "UY": "Uruguay",
    "MLU": "Uruguay",
}

_SYNC_API = None


def load_sync_playwright():
    """Load official Playwright even though this project has a local playwright package."""
    global _SYNC_API
    if _SYNC_API is not None:
        return _SYNC_API

    original_path = list(sys.path)
    local_modules = {}
    current_module = sys.modules.get(__name__)
    for name, module in list(sys.modules.items()):
        if name == "playwright" or (name.startswith("playwright.") and name != __name__):
            local_modules[name] = module
            sys.modules.pop(name, None)

    sys.path = [
        path
        for path in sys.path
        if Path(path or ".").resolve() != PROJECT_ROOT.resolve()
    ]
    try:
        sync_api = importlib.import_module("playwright.sync_api")
        _SYNC_API = (sync_api.sync_playwright, sync_api.TimeoutError)
        return _SYNC_API
    finally:
        sys.path = original_path
        sys.modules.update(local_modules)
        if current_module is not None:
            sys.modules[__name__] = current_module


class BitPlaywrightSession:
    def __init__(self, window_id, close_on_exit=False):
        self.window_id = window_id
        self.close_on_exit = close_on_exit
        self.playwright_cm = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self):
        sync_playwright, _ = load_sync_playwright()
        self.playwright_cm = sync_playwright()
        self.playwright = self.playwright_cm.__enter__()
        result = openBrowser(self.window_id)
        print(result)
        data = result.get("data") or {}
        endpoint = data.get("ws") or (f"http://{data['http']}" if data.get("http") else "")
        if not endpoint:
            raise RuntimeError(f"BitBrowser open result missing ws/http: {result}")
        self.browser = self.playwright.chromium.connect_over_cdp(endpoint)
        self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        self.page = self.context.new_page()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.page:
                self.page.close()
        except Exception:
            pass
        try:
            if self.playwright_cm:
                self.playwright_cm.__exit__(exc_type, exc, tb)
        finally:
            if self.close_on_exit:
                try:
                    closeBrowser(self.window_id)
                except Exception:
                    pass


def deep_click(page, selector):
    return page.evaluate(
        """
        selector => {
          function findAndClick(root) {
            const node = root.querySelector(selector);
            if (node) {
              node.click();
              return true;
            }
            for (const el of root.querySelectorAll('*')) {
              if (el.shadowRoot && findAndClick(el.shadowRoot)) return true;
            }
            return false;
          }
          return findAndClick(document);
        }
        """,
        selector,
    )


def deep_texts(page, selector):
    return page.evaluate(
        """
        selector => {
          const out = [];
          function collect(root) {
            for (const el of root.querySelectorAll(selector)) {
              const text = (el.textContent || '').trim();
              if (text) out.push(text);
            }
            for (const el of root.querySelectorAll('*')) {
              if (el.shadowRoot) collect(el.shadowRoot);
            }
          }
          collect(document);
          return out;
        }
        """,
        selector,
    )


def select_country(page, site, retries=3):
    country = SITE_COUNTRY_MAP.get(str(site or "").strip(), str(site or "").strip())
    if not country:
        return False

    for attempt in range(1, retries + 1):
        try:
            deep_click(page, 'button[aria-label="Select country"]')
            time.sleep(1)
            option = page.get_by_text(country, exact=True)
            option.click(timeout=10000)
            time.sleep(2)
            return True
        except Exception as exc:
            print(f"select_country failed {site}/{country}, attempt {attempt}: {exc}")
            time.sleep(2)
    return False


def first_text(page, selector, timeout=10000):
    try:
        return page.locator(selector).first.inner_text(timeout=timeout).strip()
    except Exception:
        values = deep_texts(page, selector)
        return values[0] if values else ""


def click_by_text(page, text, exact=False, timeout=10000):
    page.get_by_text(text, exact=exact).click(timeout=timeout)
