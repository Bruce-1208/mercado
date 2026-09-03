import importlib
import sys
import time
from pathlib import Path

from bit.bit_api import closeBrowser, openBrowser
from bit.bit_mercado_limit import (
    MERCADO_RATE_LIMIT_TEXT,
    get_mercado_backend_status,
    get_playwright_mercado_page_state,
    process_mercado_rate_limit,
)


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
    """Load the official Playwright package, not this project's helper modules."""
    global _SYNC_API
    if _SYNC_API is not None:
        return _SYNC_API

    original_path = list(sys.path)
    local_modules = {}
    current_module = sys.modules.get(__name__)
    for name, module in list(sys.modules.items()):
        if name == "bit_playwright" or (name.startswith("bit_playwright.") and name != __name__):
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
        self.open_result = {}
        self.shop_name = ""

    def __enter__(self):
        sync_playwright, _ = load_sync_playwright()
        self.playwright_cm = sync_playwright()
        self.playwright = self.playwright_cm.__enter__()
        result = openBrowser(self.window_id)
        print(result)
        self.open_result = dict(result or {})
        data = result.get("data") or {}
        self.shop_name = str(data.get("name") or self.window_id)
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

    def auto_login_mercado(self, wait_seconds=60):
        """使用同一 BitBrowser 窗口自动恢复 Mercado 登录态。"""
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        from bit.bit_mercado_login import (
            load_shop_login_config,
            login_mercado_with_saved_password,
        )

        data = self.open_result.get("data") or {}
        driver_path = data.get("driver")
        debugger_address = data.get("http")
        if not driver_path or not debugger_address:
            return {
                "ok": False,
                "status": "登录失败",
                "message": "BitBrowser 结果缺少 driver 或 http 地址",
            }

        options = webdriver.ChromeOptions()
        options.add_experimental_option("debuggerAddress", debugger_address)
        service = Service(driver_path)
        driver = None
        try:
            login_config = load_shop_login_config(
                self.shop_name,
                window_id=self.window_id,
            )
            driver = webdriver.Chrome(service=service, options=options)
            driver.implicitly_wait(2)
            return login_mercado_with_saved_password(
                driver,
                self.shop_name,
                window_id=self.window_id,
                email=str(login_config.get("email") or "").strip(),
                wait_seconds=max(1, int(wait_seconds)),
            )
        except Exception as exc:
            return {"ok": False, "status": "登录失败", "message": str(exc)}
        finally:
            # 只停止临时 ChromeDriver 服务，不 quit 共用的 BitBrowser。
            try:
                service.stop()
            except Exception:
                pass


def open_mercado_backend_page(
    session,
    url,
    *,
    settle_seconds=5,
    max_rate_limit_retries=2,
    rate_limit_retry_wait_seconds=30,
    max_login_retries=1,
):
    """Playwright 业务页入口：限频换节点，退出登录后自动重登。"""
    page = session.page
    rate_retry_count = 0
    login_retry_count = 0
    while True:
        navigation_error = ""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            navigation_error = str(exc)
        if settle_seconds:
            time.sleep(max(0, float(settle_seconds)))

        state = get_playwright_mercado_page_state(page)
        status = get_mercado_backend_status(state=state)
        if status == "ready":
            if navigation_error:
                return {
                    "ok": False,
                    "status": "navigation_failed",
                    "message": f"{session.shop_name} 打开 Mercado 业务页失败：{navigation_error}",
                    "state": state,
                }
            return {
                "ok": True,
                "status": "ready",
                "message": f"{session.shop_name} Mercado 业务页已就绪",
                "state": state,
                "rate_limit_retry_count": rate_retry_count,
                "login_retry_count": login_retry_count,
            }

        if status == "rate_limited":
            result = process_mercado_rate_limit(
                state=state,
                name=session.shop_name,
                retry_count=rate_retry_count,
                max_retries=max_rate_limit_retries,
                retry_wait_seconds=rate_limit_retry_wait_seconds,
            )
            if result["exhausted"]:
                return {
                    "ok": False,
                    "status": "rate_limited",
                    "message": (
                        f"{session.shop_name} Mercado 限频（{MERCADO_RATE_LIMIT_TEXT}），"
                        f"重试 {rate_retry_count} 次仍未恢复"
                    ),
                    "state": state,
                }
            rate_retry_count = result["retry_count"]
            continue

        if login_retry_count >= max(0, int(max_login_retries)):
            return {
                "ok": False,
                "status": "logged_out",
                "message": f"{session.shop_name} Mercado 登录态失效",
                "state": state,
            }
        login_retry_count += 1
        login_result = session.auto_login_mercado()
        if not login_result.get("ok"):
            return {
                "ok": False,
                "status": "logged_out",
                "message": (
                    f"{session.shop_name} Mercado 登录态失效，自动登录失败："
                    f"{login_result.get('message') or login_result.get('status')}"
                ),
                "state": state,
                "login_result": login_result,
            }


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
