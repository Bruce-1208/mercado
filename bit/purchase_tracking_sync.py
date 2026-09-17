"""采购平台物流号同步。

该模块刻意不保存账号密码。密码只在启动线程的调用栈中短暂存在；登录成功后，
Playwright 使用按平台、账号隔离的持久化浏览器目录复用 Cookie。需要短信、扫码或
验证码时，浏览器保持可见，操作员可直接在窗口中完成验证。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


PLATFORMS = {
    "1688": {
        "label": "1688",
        "login_url": "https://login.1688.com/member/signin.htm",
        "orders_url": "https://trade.1688.com/order/buyer_order_list.htm",
    },
    "taobao": {
        "label": "淘宝 / 天猫",
        "login_url": "https://login.taobao.com/member/login.jhtml",
        "orders_url": "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm",
    },
    "pdd": {
        "label": "拼多多",
        "login_url": "https://mobile.yangkeduo.com/login.html",
        "orders_url": "https://mobile.yangkeduo.com/orders.html",
    },
    "xianyu": {
        "label": "闲鱼",
        "login_url": "https://login.taobao.com/member/login.jhtml?redirectURL=https%3A%2F%2Fwww.goofish.com%2F",
        "orders_url": "https://www.goofish.com/personal",
    },
}

_TRACKING_PATTERNS = (
    re.compile(
        r"(?:物流单号|快递单号|运单号|物流编号|快递编号)\s*[：:]?\s*"
        r"([A-Z0-9][A-Z0-9\-]{5,31})",
        re.I,
    ),
    re.compile(r"\b(?:SF|YT|ZTO|STO|YD|JT|JDVA|EMS)[A-Z0-9\-]{6,28}\b", re.I),
)
_COMPANIES = {
    "顺丰": "shunfeng",
    "中通": "zhongtong",
    "圆通": "yuantong",
    "申通": "shentong",
    "韵达": "yunda",
    "京东": "jd",
    "邮政": "ems",
    "EMS": "ems",
    "极兔": "jtexpress",
}


def supported_platforms():
    return [
        {"id": key, "label": value["label"]}
        for key, value in PLATFORMS.items()
    ]


def extract_tracking(text):
    """从平台物流详情文本中提取运单号和快递公司。"""
    normalized = " ".join(str(text or "").split())
    tracking_number = ""
    for pattern in _TRACKING_PATTERNS:
        match = pattern.search(normalized)
        if match:
            tracking_number = match.group(1) if match.lastindex else match.group(0)
            break
    company = next(
        (code for name, code in _COMPANIES.items() if name.lower() in normalized.lower()),
        "",
    )
    return {
        "tracking_number": tracking_number.upper(),
        "logistics_company": company,
    }


def _account_key(account):
    return hashlib.sha256(str(account or "default").encode("utf-8")).hexdigest()[:16]


class BrowserPurchasePlatform:
    """在可见 Edge 中登录采购平台并逐单读取物流信息。"""

    def __init__(self, platform, account, password, *, state_callback=None):
        if platform not in PLATFORMS:
            raise ValueError("不支持的采购平台")
        self.platform = platform
        self.account = str(account or "").strip()
        self.password = str(password or "")
        self.config = PLATFORMS[platform]
        self.state_callback = state_callback or (lambda **_changes: None)
        self._playwright = None
        self._context = None
        self._page = None

    @property
    def profile_dir(self):
        root = Path(__file__).resolve().parents[1] / ".data" / "purchase_tracking_profiles"
        return root / self.platform / _account_key(self.account)

    def _notify(self, message, **changes):
        self.state_callback(message=message, **changes)

    def open(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("当前电脑未安装 Playwright，无法打开采购平台") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        launch_options = {
            "user_data_dir": str(self.profile_dir),
            "headless": False,
            "viewport": {"width": 1360, "height": 900},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                channel="msedge", **launch_options
            )
        except Exception:
            self._context = self._playwright.chromium.launch_persistent_context(
                **launch_options
            )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._ensure_login()
        # 尽量缩短敏感信息在对象中的存活时间。
        self.password = ""
        return self

    def close(self):
        self.password = ""
        try:
            if self._context:
                self._context.close()
        finally:
            if self._playwright:
                self._playwright.stop()
            self._context = self._page = self._playwright = None

    def _visible_locator(self, selectors):
        for frame in self._page.frames:
            for selector in selectors:
                try:
                    locator = frame.locator(selector).first
                    if locator.count() and locator.is_visible(timeout=400):
                        return locator
                except Exception:
                    continue
        return None

    def _try_fill_login(self):
        username = self._visible_locator((
            "input[name='loginId']", "input[name='TPL_username']",
            "input[name='username']", "input[type='tel']", "input[type='text']",
        ))
        password = self._visible_locator((
            "input[name='password']", "input[name='TPL_password']", "input[type='password']",
        ))
        if username and self.account:
            username.fill(self.account)
        if password and self.password:
            password.fill(self.password)
        submit = self._visible_locator((
            "button[type='submit']", "input[type='submit']",
            "button:has-text('登录')", "button:has-text('登 录')",
        ))
        if submit and password:
            submit.click()

    def _looks_logged_in(self):
        url = str(self._page.url or "").lower()
        if any(value in url for value in ("login.", "/login", "signin")):
            return False
        try:
            text = self._page.locator("body").inner_text(timeout=1500)
        except Exception:
            return False
        return not bool(re.search(r"请登录|立即登录|验证码|扫码登录|短信验证", text[:3000]))

    def _ensure_login(self):
        self._page.goto(self.config["orders_url"], wait_until="domcontentloaded", timeout=45000)
        if self._looks_logged_in():
            self._notify("已复用登录状态，正在读取采购订单", phase="syncing")
            return
        self._notify(
            "已打开登录页；如出现扫码、短信或验证码，请在浏览器中完成",
            phase="waiting_login",
        )
        self._page.goto(self.config["login_url"], wait_until="domcontentloaded", timeout=45000)
        self._try_fill_login()
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if self._looks_logged_in():
                self._notify("登录成功，正在读取采购订单", phase="syncing")
                return
            self._page.wait_for_timeout(1200)
        raise RuntimeError("登录等待超时，请重新发起同步并在 5 分钟内完成验证")

    def _search_order(self, purchase_order):
        purchase_order = str(purchase_order or "").strip()
        page = self._page
        page.goto(self.config["orders_url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1200)
        search = self._visible_locator((
            "input[placeholder*='订单']", "input[placeholder*='商品']",
            "input[name*='order']", "input[type='search']",
        ))
        if search:
            search.fill(purchase_order)
            search.press("Enter")
            page.wait_for_timeout(1800)

        # 先将页面滚到匹配订单，再进入物流详情。不同平台的按钮文案不同，
        # 所以按可见文本查找，而不依赖经常变化的 CSS 类名。
        exact = page.get_by_text(purchase_order, exact=False).first
        if exact.count():
            try:
                exact.scroll_into_view_if_needed(timeout=2500)
            except Exception:
                pass
        for label in ("查看物流", "物流详情", "查看快递", "订单详情", "查看详情"):
            button = page.get_by_text(label, exact=True).first
            try:
                if button.count() and button.is_visible(timeout=500):
                    before = set(page.context.pages)
                    button.click()
                    page.wait_for_timeout(1600)
                    opened = [item for item in page.context.pages if item not in before]
                    if opened:
                        self._page = page = opened[-1]
                        page.wait_for_load_state("domcontentloaded", timeout=30000)
                    break
            except Exception:
                continue
        return " ".join(page.locator("body").inner_text(timeout=10000).split())

    def fetch_tracking(self, purchase_order):
        text = self._search_order(purchase_order)
        if str(purchase_order) not in text:
            return {"status": "not_found", "message": "平台未找到该采购订单"}
        result = extract_tracking(text)
        if not result["tracking_number"]:
            return {"status": "pending", "message": "订单尚未发货或平台未显示物流号"}
        return {"status": "synced", **result}


class PurchaseTrackingSyncManager:
    """单进程后台同步管理器，便于 Web 页面轮询进度。"""

    def __init__(self, adapter_factory=BrowserPurchasePlatform):
        self.adapter_factory = adapter_factory
        self._lock = threading.Lock()
        self._thread = None
        self._state = self._idle_state()

    @staticmethod
    def _idle_state():
        return {
            "running": False,
            "phase": "idle",
            "message": "等待开始同步",
            "platform": "",
            "total": 0,
            "processed": 0,
            "synced": 0,
            "pending": 0,
            "failed": 0,
            "results": [],
            "started_at": "",
            "finished_at": "",
        }

    def status(self):
        with self._lock:
            return deepcopy(self._state)

    def _update(self, **changes):
        with self._lock:
            self._state.update(changes)

    @staticmethod
    def _normalize_orders(orders):
        normalized = []
        seen = set()
        for row in orders or ():
            order_id = str((row or {}).get("order_id") or "").strip()
            purchase_order = str((row or {}).get("purchase_order") or "").strip()
            key = (order_id, purchase_order)
            if order_id and purchase_order and key not in seen:
                seen.add(key)
                normalized.append({"order_id": order_id, "purchase_order": purchase_order})
        return normalized

    def start(self, *, platform, account, password, orders, update_order):
        if platform not in PLATFORMS:
            raise ValueError("请选择 1688、淘宝、拼多多或闲鱼")
        account = str(account or "").strip()
        password = str(password or "")
        if not account:
            raise ValueError("请输入采购平台账号")
        normalized_orders = self._normalize_orders(orders)
        if not normalized_orders:
            raise ValueError("所选订单还没有填写采购订单号")
        with self._lock:
            if self._state.get("running"):
                raise RuntimeError("已有物流号同步任务正在运行")
            self._state = {
                **self._idle_state(),
                "running": True,
                "phase": "starting",
                "message": "正在打开采购平台",
                "platform": platform,
                "total": len(normalized_orders),
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        worker = threading.Thread(
            target=self._run,
            kwargs={
                "platform": platform,
                "account": account,
                "password": password,
                "orders": normalized_orders,
                "update_order": update_order,
            },
            name=f"purchase-tracking-{platform}",
            daemon=True,
        )
        self._thread = worker
        worker.start()
        return self.status()

    def _run(self, *, platform, account, password, orders, update_order):
        adapter = None
        results = []
        try:
            adapter = self.adapter_factory(
                platform,
                account,
                password,
                state_callback=self._update,
            )
            password = ""
            adapter.open()
            tracking_cache = {}
            for index, row in enumerate(orders, start=1):
                purchase_order = row["purchase_order"]
                self._update(
                    phase="syncing",
                    message=f"正在查询采购单 {purchase_order}",
                )
                try:
                    if purchase_order not in tracking_cache:
                        tracking_cache[purchase_order] = dict(
                            adapter.fetch_tracking(purchase_order) or {}
                        )
                    item = deepcopy(tracking_cache[purchase_order])
                    item.update(row)
                    if item.get("status") == "synced":
                        update_order(
                            row["order_id"],
                            item["tracking_number"],
                            item.get("logistics_company") or "",
                        )
                except Exception as exc:
                    item = {**row, "status": "failed", "message": str(exc)}
                results.append(item)
                counters = {
                    key: sum(1 for result in results if result.get("status") == key)
                    for key in ("synced", "pending", "failed")
                }
                counters["failed"] += sum(
                    1 for result in results if result.get("status") == "not_found"
                )
                self._update(processed=index, results=deepcopy(results), **counters)
            self._update(
                running=False,
                phase="completed",
                message=f"同步完成：成功 {self._state.get('synced', 0)} 单",
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception as exc:
            self._update(
                running=False,
                phase="error",
                message=str(exc) or exc.__class__.__name__,
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        finally:
            password = ""
            if adapter:
                try:
                    adapter.close()
                except Exception:
                    pass
            self._thread = None


purchase_tracking_sync_manager = PurchaseTrackingSyncManager()
