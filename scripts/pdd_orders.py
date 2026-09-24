"""读取拼多多买家端指定日期范围内可访问的订单，导出 Excel/JSON。

脚本使用一个独立的、可见的 Edge 持久化浏览器目录：

    python scripts/pdd_orders.py --login-first --start 2026-01-01

默认在可见 Edge 登录后回到终端按回车确认。可选 --login-first 先启动普通 Edge；手动完成扫码、
短信或验证码登录，确认能看到订单后关闭该 Edge 窗口。随后脚本会用同一资料目录
重新启动 Edge 并读取订单。登录 Cookie 会保存在 .data/pdd_orders_edge_profile。

说明：
* 目标页面是拼多多买家端 mobile.yangkeduo.com/orders.html；
* 脚本读取页面及浏览器自然收到的订单响应，不填写或保存账号密码；
* 不自动处理 CAPTCHA/滑块；
* 拼多多页面是动态渲染的，因此订单原文 raw_text 也会一并保存，方便核对。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ORDERS_URL = "https://mobile.yangkeduo.com/orders.html"
DEFAULT_PROFILE_DIR = Path(__file__).resolve().parents[1] / ".data" / "pdd_orders_edge_profile"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "pdd_orders"
EDGE_CANDIDATES = (
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft" / "Edge" / "Application" / "msedge.exe",
)


@dataclass
class Order:
    order_id: str
    order_time: str = ""
    status: str = ""
    amount: str = ""
    product: str = ""
    tracking_number: str = ""
    logistics_company: str = ""
    raw_text: str = ""
    detail_note: str = ""


def parse_args() -> argparse.Namespace:
    today = datetime.now(timezone(timedelta(hours=8))).date()
    parser = argparse.ArgumentParser(description="手动登录拼多多买家端并导出 Excel/JSON")
    parser.add_argument('--interval', type=float, default=6, help='加载间隔秒数，至少 5 秒')
    parser.add_argument('--list-only', action='store_true', help='仅读取列表，跳过 V2 逐单详情及物流补全')
    parser.add_argument('--save-interval', type=float, default=60, help='Excel 自动保存间隔秒数；JSON 每轮保存（默认 60）')
    parser.add_argument(
        "--start",
        default="2026-01-01",
        help="起始日期，包含该日，格式 YYYY-MM-DD（默认：2026-01-01）",
    )
    parser.add_argument(
        "--end",
        default=today.isoformat(),
        help=f"结束日期，包含该日，格式 YYYY-MM-DD（默认：{today.isoformat()}）",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help=f"浏览器登录态目录（默认：{DEFAULT_PROFILE_DIR}）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"导出目录（默认：{DEFAULT_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=0,
        help="最多滚动/加载轮数；0 不限轮数（默认），加载停滞或风控仍会暂停",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=0,
        help="等待普通 Edge 手动登录窗口关闭的秒数；0 表示不限时（默认：0）",
    )
    parser.add_argument(
        "--login-first",
        action="store_true",
        help="先在完全普通的 Edge 中手动登录，关闭窗口后再开始采集",
    )
    parser.add_argument(
        "--login-only",
        action="store_true",
        help="只启动普通 Edge 保存登录态，关闭窗口后退出，不读取订单",
    )
    parser.add_argument(
        "--edge-path",
        type=Path,
        help="msedge.exe 路径；通常无需指定",
    )
    return parser.parse_args()


def parse_day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"日期格式错误：{value!r}，应为 YYYY-MM-DD") from exc


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


@lru_cache(maxsize=16384)
def extract_date(value: str) -> date | None:
    """解析页面中常见的中文、斜杠和 ISO 日期。"""
    text = normalize_text(value)
    patterns = (
        r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?",
        r"(20\d{2})\s+(\d{1,2})\s+(\d{1,2})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
    return None


def in_range(order: Order, start: date, end: date) -> bool:
    parsed = extract_date(order.order_time)
    return parsed is not None and start <= parsed <= end


def clean_field(value: str, *labels: str) -> str:
    text = normalize_text(value)
    for label in labels:
        text = re.sub(rf"^\s*{re.escape(label)}\s*[:：]?\s*", "", text, count=1)
    return text.strip(" |,，")


def parse_order_card(card: dict[str, Any]) -> Order | None:
    """将页面 JS 提取的一个订单卡片转换为稳定字段。"""
    text = normalize_text(card.get("text"))
    if not text:
        return None

    order_match = re.search(
        r"(?:订单编号|订单号|订单 ID|订单ID)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_-]{7,})",
        text,
        re.I,
    )
    if not order_match:
        return None

    order_id = order_match.group(1)
    time_match = re.search(
        r"(?:下单时间|创建时间|订单时间)\s*[:：]?\s*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"
        r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
        text,
    )
    amount = paid_amount_from_text(text)
    status_words = (
        "已退款",
        "退款成功",
        "交易成功",
        "待付款",
        "待发货",
        "已发货",
        "运输中",
        "待收货",
        "已收货",
        "已完成",
        "退款中",
        "退款/售后",
        "交易关闭",
        "已取消",
    )
    lines = [normalize_text(line) for line in (card.get("lines") or []) if normalize_text(line)]
    status_match = re.search(r'(?:订单状态|交易状态)\s*[:：]?\s*([^\s，；]+)', text)
    status = status_match.group(1) if status_match else next((line for line in lines if line in status_words), '')
    company_names = ("顺丰", "中通", "圆通", "申通", "韵达", "京东", "邮政", "极兔")
    company = next((name for name in company_names if name in text), "")

    product = ""
    for line in lines:
        product_match = re.match(r"(?:商品名称|商品标题|标题|商品)\s*[:：]\s*(.{2,500})$", line)
        if product_match:
            product = '；'.join(filter(None, [product, product_match.group(1)]))
    if not product and lines:
        candidates = [
            line
            for line in lines
            if not re.search(
                r"订单|付款|收货|发货|物流|快递|运单|合计|实付|¥|￥|20\d{2}|"
                r"待付款|待发货|待收货|已完成|交易关闭|已取消",
                line,
            )
        ]
        if candidates:
            product = max(candidates, key=len)[:200]

    return Order(
        order_id=order_id,
        order_time=time_match.group(1) if time_match else "",
        status=status,
        amount=amount,
        product=clean_field(product, "商品"),
        tracking_number='；'.join(dict.fromkeys(m.upper() for m in re.findall(
            r'(?:物流单号|快递单号|运单号|物流编号|快递编号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9-]{5,31})', text))),
        logistics_company=company,
        raw_text=text,
    )


EXTRACT_CARDS_JS = r"""
() => {
  const visible = el => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      rect.width > 0 && rect.height > 0;
  };
  const orderHint = /订单编号|订单号|订单\s*ID/i;
  // 从订单号标签文本定位卡片，避免对每一个 DOM 元素反复读取整棵子树。
  const nodes = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    if (orderHint.test(walker.currentNode.nodeValue || '')) nodes.add(walker.currentNode.parentElement);
  }
  const result = [];
  const seen = new Set();
  for (const node of nodes) {
    if (!visible(node)) continue;
    let candidate = node;
    // 取最小的、同时包含订单号和日期的可见祖先，避免返回整个 body。
    for (let i = 0; i < 7 && candidate.parentElement; i++) {
      const parent = candidate.parentElement;
      const text = String(parent.innerText || '').replace(/\s+/g, ' ').trim();
      if ((text.match(/订单编号|订单号|订单\s*ID/gi) || []).length === 1 && text.length <= 3500 &&
          (orderHint.test(text) || /\d{12,24}/.test(text))) {
        candidate = parent;
      } else {
        break;
      }
    }
    const text = String(candidate.innerText || '').replace(/\s+/g, ' ').trim();
    const match = text.match(/(?:订单编号|订单号|订单\s*ID)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_-]{7,})/i)
      || text.match(/(?<!\d)(\d{12,24})(?!\d)/);
    if (!match || (text.match(/订单编号|订单号|订单\s*ID/gi) || []).length !== 1) continue;
    const id = match[1];
    if (seen.has(id)) continue;
    seen.add(id);
    result.push({
      text,
      lines: String(candidate.innerText || '').split(/\n+/).map(x => x.trim()).filter(Boolean),
    });
  }
  return result;
}
"""


LOAD_MORE_JS = r"""
() => {
  const visible = el => {
    if (!el) return false;
    const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const labels = /^(加载更多|查看更多|下一页|更多订单|查看全部)$/;
  const el = [...document.querySelectorAll('button,a,[role="button"]')]
    .find(node => visible(node) && labels.test(String(node.innerText || '').trim()));
  if (!el) return false;
  el.click();
  return true;
}
"""


def find_edge_executable(explicit: Path | None = None) -> Path:
    if explicit:
        candidate = explicit.expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise RuntimeError(f"找不到 Edge：{candidate}")
    for candidate in EDGE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise RuntimeError("找不到 Microsoft Edge，请用 --edge-path 指定 msedge.exe")


def edge_base_args(profile_dir: Path) -> list[str]:
    """仅使用浏览器运行参数，不注入反检测脚本或伪造设备信息。"""
    return [
        f"--user-data-dir={profile_dir.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]


def run_plain_edge_login(
    edge_path: Path,
    profile_dir: Path,
    timeout_seconds: int,
) -> None:
    """在没有 Playwright/CDP 的普通 Edge 中让用户手动完成登录。"""
    print("正在启动普通 Edge；此阶段没有自动化连接。")
    print("请手动登录拼多多，确认能看到订单列表，然后关闭整个 Edge 窗口。")
    process = subprocess.Popen(
        [str(edge_path), *edge_base_args(profile_dir), "--start-maximized", ORDERS_URL],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        process.wait(timeout=timeout_seconds if timeout_seconds > 0 else None)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "等待手动登录超时。请关闭刚才的 Edge 窗口后重新运行，"
            "或使用 --login-timeout 0 不限制时间。"
        ) from exc
    if process.returncode not in (0, None):
        raise RuntimeError(f"Edge 登录窗口异常退出，退出码：{process.returncode}")
    # 给 Edge 一点时间将 Cookie/Local Storage 刷入资料目录。
    time.sleep(1)


def probe_cdp(port: int, browser_path: str | None = None) -> str | None:
    if not 0 < port < 65536:
        return None
    endpoint = f"http://127.0.0.1:{port}"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'{endpoint}/json/version', timeout=1) as response:
            payload = json.loads(response.read(65536).decode('utf-8'))
        websocket = urlsplit(payload.get('webSocketDebuggerUrl', ''))
        if (websocket.hostname in ('127.0.0.1', 'localhost', '::1') and websocket.port == port
                and websocket.path.startswith('/devtools/browser/')
                and (browser_path is None or websocket.path == browser_path)):
            return endpoint
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return None


def active_profile_endpoint(profile_dir: Path) -> str | None:
    """Chrome/Edge 的资料目录记录动态调试端口及浏览器实例 ID。"""
    try:
        lines = (profile_dir / 'DevToolsActivePort').read_text(encoding='utf-8').splitlines()
        if len(lines) >= 2 and lines[1].startswith('/devtools/browser/'):
            return probe_cdp(int(lines[0]), lines[1])
    except (OSError, ValueError):
        pass
    return None


def windows_command_args(command: str) -> list[str]:
    import ctypes
    from ctypes import wintypes
    count = ctypes.c_int()
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    pointer = parse(command, ctypes.byref(count))
    if not pointer:
        return []
    try:
        return [pointer[i] for i in range(count.value)]
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(pointer)


def existing_profile_ports(profile_dir: Path) -> tuple[bool, list[int]]:
    """兼容旧版固定随机端口：只查询同一专用资料目录的 Edge 主进程。"""
    if os.name != 'nt':
        return False, []
    command = ('Get-CimInstance Win32_Process -Filter "Name = \'msedge.exe\'" | '
               'Select-Object CommandLine | ConvertTo-Json -Compress')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                 '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; ' + command],
                                capture_output=True, encoding='utf-8-sig', timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode or not result.stdout.strip():
            return False, []
        processes = json.loads(result.stdout)
        if isinstance(processes, dict):
            processes = [processes]
        found, ports = False, []
        for process in processes:
            args = windows_command_args(process.get('CommandLine') or '')
            values = dict(arg.split('=', 1) for arg in args if arg.startswith('--') and '=' in arg)
            profile = values.get('--user-data-dir')
            if not profile or Path(profile).resolve() != profile_dir.resolve() or '--type' in values:
                continue
            found = True
            port = values.get('--remote-debugging-port', '')
            if port.isdigit() and int(port) > 0:
                ports.append(int(port))
        return found, ports
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False, []


def wait_for_cdp(process: subprocess.Popen[Any], port: int, timeout_seconds: int = 20,
                 profile_dir: Path | None = None) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        endpoint = active_profile_endpoint(profile_dir) if profile_dir is not None else None
        endpoint = endpoint or (probe_cdp(port) if port else None)
        if endpoint:
            return endpoint
        code = process.poll()
        if code not in (None, 0):
            raise RuntimeError(f'Edge 启动进程异常退出，退出码：{code}')
        # 退出码 0 可能只是把请求交给已有 Edge，不代表浏览器失败。
        time.sleep(0.2)
    raise RuntimeError('Edge 窗口可能已打开，但采集连接不可用。请正常关闭使用本脚本专用资料目录的所有 Edge 窗口后重试；无需关闭其他浏览器或删除登录资料。')


def launch_edge_for_collection(edge_path: Path, profile_dir: Path) -> tuple[subprocess.Popen[Any] | None, str]:
    """启动真实 Edge，再通过仅绑定 127.0.0.1 的 CDP 读取已登录页面。"""
    endpoint = active_profile_endpoint(profile_dir)
    if endpoint:
        print('复用当前专用 Edge 窗口的采集连接。')
        return None, endpoint
    found, ports = existing_profile_ports(profile_dir)
    for port in ports:
        endpoint = probe_cdp(port)
        if endpoint:
            print('复用旧版脚本打开的专用 Edge 窗口。')
            return None, endpoint
    if found:
        raise RuntimeError('专用 Edge 已打开，但没有可用采集连接（可能是普通登录窗口）。请正常关闭该专用窗口后重新运行；登录资料保留。')
    process = subprocess.Popen(
        [
            str(edge_path),
            *edge_base_args(profile_dir),
            '--remote-debugging-port=0',
            "--remote-debugging-address=127.0.0.1",
            # 保留真实 Windows Edge UA，仅用窄窗口触发 H5 的响应式布局。
            "--window-size=520,900",
            ORDERS_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process, wait_for_cdp(process, 0, profile_dir=profile_dir)


def import_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise SystemExit(
            "缺少 Playwright，请先执行：python -m pip install playwright。"
            "新流程直接使用系统 Edge，不需要下载 Playwright Chromium。"
        ) from exc
    return sync_playwright, PlaywrightTimeoutError


def page_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception as exc:
        if page.is_closed() or 'has been closed' in str(exc).lower() or 'page crashed' in str(exc).lower():
            raise RuntimeError('订单页面或浏览器已关闭/崩溃，停止读取并保存已有数据') from exc
        return ""


def looks_logged_in(page: Any, body: str | None = None) -> bool:
    url = str(page.url or "").lower()
    body = page_text(page) if body is None else body
    if "/login" in url or "login.html" in url:
        return False
    if re.search(r"请登录|立即登录|扫码登录|短信验证|账号登录", body[:5000]):
        return False
    order_page_markers = re.search(
        r"我的订单|全部订单|待付款|待发货|待收货|已完成|暂无订单|订单编号|订单号",
        body[:10000],
    )
    return bool(order_page_markers and ("/orders" in url or "订单" in body))


def click_all_tab(page: Any) -> None:
    """尽量切换到‘全部’订单，不依赖混淆 CSS 类名。"""
    try:
        locator = page.get_by_text("全部", exact=True).first
        if locator.count() and locator.is_visible(timeout=500):
            locator.click()
            page.wait_for_timeout(900)
    except Exception:
        pass


def paid_amount_from_text(text: str) -> str:
    match = re.search(r'(?:实际支付|实付金额|实付款|实付|已付金额)\s*(?:[（(][^）)]{0,30}[）)])?\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)(?![\d.])', text)
    return format(Decimal(match.group(1)), '.2f') if match else ''


def paid_amount_from_json(value: dict) -> str:
    # 只有明确带单位的字段或格式化金额文本才允许换算。
    for key, divisor in [('paid_amount_yuan', 1), ('paid_amount_fen', 100), ('paid_amount_cent', 100)]:
        if value.get(key) is not None:
            try:
                number = Decimal(str(value[key])) / divisor
                if number.is_finite() and number >= 0:
                    return format(number, '.2f')
            except InvalidOperation:
                pass
    for key in ('paid_amount_text', 'payment_desc', 'pay_amount_desc'):
        result = paid_amount_from_text(normalize_text(value.get(key)))
        if result:
            return result
    return ''


def shipping_values(value, owner, keys):
    """提取当前订单包裹，不跨入另一个订单或推荐商品。"""
    result = []
    if isinstance(value, dict):
        if value.get('order_sn') and str(value['order_sn']) != owner:
            return []
        for key, child in value.items():
            if key in keys and isinstance(child, (str, int)):
                result.append(str(child))
            elif key in ('shipping', 'logistics', 'logistics_info', 'shipping_info', 'packages', 'package_list', 'express', 'express_list'):
                result.extend(shipping_values(child, owner, keys))
    elif isinstance(value, list):
        for child in value:
            result.extend(shipping_values(child, owner, keys))
    return list(dict.fromkeys(result))


def response_orders(value: Any):
    """只解析浏览器实际收到的响应；不构造、重放或并发请求接口。

    字段随网站版本变化，未知结构保留为空，不能据此保证完整性。
    """
    if isinstance(value, list):
        for item in value:
            yield from response_orders(item)
    elif isinstance(value, dict):
        oid = value.get('order_sn')
        if oid:
            created = value.get('order_time') or value.get('created_time')
            when = ''
            if isinstance(created, (float, int)) or (isinstance(created, str) and created.isdigit()):
                try:
                    ts = float(created)
                    if ts > 10**12:
                        ts /= 1000
                    when = datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
                except (ValueError, OSError, OverflowError):
                    pass
            elif isinstance(created, str) and extract_date(created):
                when = created
            goods = value.get('goods_list', [])
            if not isinstance(goods, list):
                goods = []
            product = '；'.join(normalize_text(g.get('goods_name')) for g in goods if isinstance(g, dict))
            yield Order(str(oid), when, normalize_text(value.get('order_status_desc') or value.get('status_desc')),
                        amount=paid_amount_from_json(value),
                        product=product or normalize_text(value.get('goods_name')),
                        tracking_number='；'.join(shipping_values(value, str(oid), {'tracking_number', 'tracking_no', 'waybill_no'})),
                        logistics_company='；'.join(shipping_values(value, str(oid), {'shipping_name', 'express_name', 'logistics_company'})),
                        raw_text=json.dumps(value, ensure_ascii=False))
        for child in value.values():
            if isinstance(child, (list, dict)):
                yield from response_orders(child)


class Capture:
    def __init__(self):
        self.orders: dict[str, Order] = {}
        self.blocked = False
        self.reason = '尚未完成读取'
        self.json_responses = 0
        self.matched_responses = 0
        self.parse_errors = 0

    def merge(self, order: Order):
        previous = self.orders.get(order.order_id)
        if previous:
            for field in ('tracking_number', 'logistics_company'):
                parts = (getattr(previous, field) + '；' + getattr(order, field)).split('；')
                setattr(order, field, '；'.join(dict.fromkeys(part for part in parts if part)))
            for field in Order.__dataclass_fields__:
                if not getattr(order, field):
                    setattr(order, field, getattr(previous, field))
        self.orders[order.order_id] = order

    def response(self, response):
        from urllib.parse import urlparse
        host = urlparse(response.url).hostname or ''
        if not any(host == d or host.endswith('.' + d) for d in ('yangkeduo.com', 'pinduoduo.com')):
            return
        if response.status in (403, 429):
            self.blocked = True
        if response.request.resource_type not in ('xhr', 'fetch'):
            return
        if 'json' not in response.headers.get('content-type', ''):
            return
        self.json_responses += 1
        try:
            matched = False
            for order in response_orders(response.json()):
                self.merge(order)
                matched = True
            self.matched_responses += int(matched)
        except Exception:
            # HTML 验证页、空响应或结构不适配不等于没有订单。
            self.parse_errors += 1


SCROLL_ORDERS_JS = r"""
() => {
  const root = document.scrollingElement;
  const candidates = [...document.querySelectorAll('body *')].filter(el => {
    const r = el.getBoundingClientRect();
    return r.width > 150 && r.height > 150 && r.bottom > 0 &&
      r.top < innerHeight && r.right > 0 && r.left < innerWidth &&
      /auto|scroll/.test(getComputedStyle(el).overflowY) &&
      el.scrollHeight > el.clientHeight + 4;
  });
  // 优先订单区域的内部滚动容器，避免鼠标不在列表上导致滚动失效。
  candidates.sort((a, b) => {
    const score = el => (/订单|待付款|待收货/.test(el.innerText) ? 1e9 : 0) +
      el.clientHeight * el.clientWidth;
    return score(b) - score(a);
  });
  const target = candidates[0] || root;
  if (!target) return {before: 0, after: 0, height: 0, viewport: 0, target: 'none'};
  const before = target.scrollTop;
  target.scrollTop = Math.min(target.scrollHeight - target.clientHeight,
    before + Math.max(300, Math.floor(target.clientHeight * 0.8)));
  return {before, after: target.scrollTop, height: target.scrollHeight,
    viewport: target.clientHeight, target: target === root ? 'document' : 'inner'};
}
"""


def stalled_rounds(previous_idle, added, scroll, clicked):
    """没有新订单但仍在向下翻阅长页面时，不计为加载停滞。"""
    moved = abs(scroll['after'] - scroll['before']) > 1
    # “加载更多”可能失效；仅点击成功不能证明页面有进展。
    return 0 if added or moved else previous_idle + 1


def scroll_wait_ms(scroll, clicked, interval):
    """已加载长列表内逐屏快移；靠近加载边界或不动时维持慢速等待。"""
    remaining = scroll['height'] - scroll['viewport'] - scroll['after']
    if not clicked and scroll['after'] > scroll['before'] and remaining > max(1200, scroll['viewport'] * 2):
        return min(interval * 1000, 500)
    return interval * 1000


def collection_summary(orders, start, end):
    dates = [extract_date(o.order_time) for o in orders]
    known = [d for d in dates if d is not None]
    return {
        'total': len(orders),
        'in_range': sum(start <= d <= end for d in known),
        'unknown': len(dates) - len(known),
        'outside': sum(not start <= d <= end for d in known),
        'oldest': min(known).isoformat() if known else '未知',
        'newest': max(known).isoformat() if known else '未知',
    }


ORDER_ID_RE = re.compile(r'(?:订单编号|订单号|订单\s*ID)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_-]{7,})', re.I)


def detail_template(url, text, known_ids):
    """从用户实际打开并核对的详情学习路由，不硬编码平台私有接口。"""
    parts = urlsplit(url)
    host = parts.hostname or ''
    if parts.scheme != 'https' or not any(host == d or host.endswith('.' + d) for d in ('yangkeduo.com', 'pinduoduo.com')):
        return None
    ids = set(ORDER_ID_RE.findall(text))
    for key, value in parse_qsl(parts.query):
        if key in ('order_sn', 'orderSn', 'order_id') and value in known_ids and ids == {value}:
            return url, key
    return None


def detail_url(template, order_id):
    url, key = template
    parts = urlsplit(url)
    query = [(k, order_id if k == key else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def detail_guard(page, capture, checkpoint, interval):
    while True:
        text = page_text(page)
        if not (capture.blocked or re.search(r'访问过于频繁|操作过于频繁|安全验证|请完成验证|滑块|验证码|请登录|立即登录', text)
                or 'login' in str(page.url).lower()):
            return text
        capture.reason = '详情读取遇到验证或访问限制，等待手动处理'
        checkpoint_now(checkpoint)
        answer = input('请手动完成当前页面验证，输入 c 继续，其他输入保存退出：').strip().lower()
        if answer != 'c':
            raise RuntimeError('用户停止详情读取，已保存当前进度')
        capture.blocked = False
        page.wait_for_timeout(interval * 1000)


def merge_detail_text(capture, oid, text, bound_logistics=False):
    ids = set(ORDER_ID_RE.findall(text))
    if ids != {oid} and not (bound_logistics and not ids):
        return False
    if not ids:
        text = f'订单号：{oid}\n' + text
    order = parse_order_card({'text': text, 'lines': text.splitlines()})
    if not order or order.order_id != oid:
        return False
    if capture.orders.get(oid) and capture.orders[oid].product and not re.search(r'(?:商品名称|商品标题|标题|商品)\s*[:：]', text):
        # 列表响应中的真实商品名称优先于“页面最长一行”的回退结果。
        order.product = ''
    if bound_logistics:
        # 物流页面的标题、运费、运输状态不能覆盖订单字段。
        order = Order(oid, tracking_number=order.tracking_number,
                      logistics_company=order.logistics_company, raw_text=text)
    capture.merge(order)
    return True


def missing_fields(order):
    labels = [('status', '状态'), ('amount', '实付金额'), ('product', '标题'), ('tracking_number', '物流号')]
    return '、'.join(label for field, label in labels if not getattr(order, field))


def enrich_orders(page, start, end, capture, checkpoint, interval):
    targets = [o.order_id for o in capture.orders.values() if in_range(o, start, end) or not extract_date(o.order_time)]
    if not targets:
        return
    print(f'V2：准备逐单补全 {len(targets)} 单。请在当前浏览器手动打开任意一笔已读取订单的“订单详情”。')
    checkpoint_now(checkpoint)
    while True:
        answer = input('打开详情后按回车，用于识别真实详情地址；输入 q 跳过详情并导出：').strip().lower()
        if answer == 'q':
            for oid in targets:
                capture.orders[oid].detail_note = '未读取详情'
            return
        page.wait_for_timeout(500)
        text = detail_guard(page, capture, checkpoint, interval)
        template = detail_template(page.url, text, capture.orders)
        if template:
            break
        print('无法确认详情路由与订单号，请在同一个标签页打开订单详情，并确保页面显示订单编号。')
    list_reason = capture.reason
    for index, oid in enumerate(targets, 1):
        capture.reason = f'逐单详情读取 {index}/{len(targets)}；列表阶段：{list_reason}'
        print(f'详情 {index}/{len(targets)}：{oid}')
        try:
            target_url = detail_url(template, oid)
            if page.url != target_url or set(ORDER_ID_RE.findall(page_text(page))) != {oid}:
                page.goto(target_url, wait_until='domcontentloaded', timeout=60000)
                page.wait_for_timeout(interval * 1000)
        except Exception as exc:
            capture.orders[oid].detail_note = f'详情加载失败：{type(exc).__name__}'
            checkpoint()
            # 网络失败不密集重试，也不继续批量访问。
            raise RuntimeError('详情加载失败，停止批量访问；已保存此前结果') from exc
        text = detail_guard(page, capture, checkpoint, interval)
        if not merge_detail_text(capture, oid, text):
            capture.orders[oid].detail_note = '页面订单号未匹配，未写入该页面信息'
            checkpoint()
            raise RuntimeError('详情订单号校验失败，已停止，避免串单')
        order_day = extract_date(capture.orders[oid].order_time)
        if order_day and not start <= order_day <= end:
            capture.orders[oid].detail_note = '详情确认日期不在导出范围，跳过物流访问'
            checkpoint()
            continue
        # 只点击明确的只读物流入口，不点击确认收货、付款、取消等按钮。
        logistics_note = '未找到物流入口'
        for label in ('查看物流', '查看物流详情', '物流详情'):
            locator = page.get_by_text(label, exact=True)
            if locator.count() and locator.first.is_visible():
                locator.first.click(timeout=10000)
                page.wait_for_timeout(interval * 1000)
                logistics_text = detail_guard(page, capture, checkpoint, interval)
                bound = merge_detail_text(capture, oid, logistics_text, bound_logistics=True)
                logistics_note = '已查看物流' if bound else '物流页面订单号不匹配'
                # 多包裹标签逐个读取；仅匹配明确的包裹编号标签。
                tabs = page.get_by_text(re.compile(r'^包裹\s*\d+(?:\s*[/／]\s*\d+)?$'))
                for tab_index in range(tabs.count()):
                    tab = tabs.nth(tab_index)
                    if tab.is_visible() and tab.get_attribute('aria-selected') != 'true':
                        tab.click(timeout=10000)
                        page.wait_for_timeout(interval * 1000)
                        merge_detail_text(capture, oid, detail_guard(page, capture, checkpoint, interval), bound_logistics=True)
                break
        order = capture.orders[oid]
        if not order.tracking_number and order.status in ('待付款', '待发货', '已取消', '交易关闭'):
            logistics_note = '当前状态可能尚无物流号'
        missing = missing_fields(order)
        order.detail_note = f'已读取详情；{logistics_note}' + (f'；未获取：{missing}' if missing else '；所需字段齐全')
        checkpoint()
    capture.reason = f'逐单详情读取结束；列表阶段：{list_reason}'


def collect_orders(page, start, end, max_rounds, capture, checkpoint, interval=6):
    idle = 0
    previous = 0
    round_no = 0
    card_cache = {}
    if max_rounds < 0:
        raise ValueError('max_rounds 不能小于 0')
    while True:
        text = page_text(page)
        if capture.blocked or re.search(r'访问过于频繁|操作过于频繁|安全验证|请完成验证|滑块|验证码|请登录|立即登录', text) or not looks_logged_in(page, text):
            capture.reason = '遇到验证、访问限制或登录失效'
            checkpoint_now(checkpoint)
            answer = input('请在浏览器手动处理验证/登录。处理好后输入 c 继续，其他输入保存退出：').strip().lower()
            if answer != 'c':
                return False
            capture.blocked = False
            page.wait_for_timeout(interval * 1000)
            continue
        for card in page.evaluate(EXTRACT_CARDS_JS):
            fingerprint = (card.get('text', ''), tuple(card.get('lines') or ()))
            if fingerprint in card_cache:
                continue
            order = parse_order_card(card)
            card_cache[fingerprint] = True
            if order:
                capture.merge(order)
        if len(card_cache) > 10000:
            card_cache.clear()
        # 最后一轮加载之后先读取 DOM，再判断上限，避免漏掉最后加载的订单。
        summary = collection_summary(list(capture.orders.values()), start, end)
        if max_rounds and round_no >= max_rounds:
            capture.reason = (f'达到用户设置的最大读取轮数 {max_rounds}，列表未完成；'
                              f"已知最早下单日期 {summary['oldest']}，目标起始日期 {start.isoformat()}")
            checkpoint_now(checkpoint)
            print(capture.reason)
            print('已保存部分结果。--max-rounds 0 可取消上限；本次不自动进入详情阶段。')
            return False
        round_no += 1
        capture.reason = '读取中，尚未核实完整性'
        checkpoint()
        count = len(capture.orders)
        print(f"第 {round_no} 轮：读取 {count} 单，范围内 {summary['in_range']} 单，"
              f"日期待核对 {summary['unknown']} 单；已知最早日期 {summary['oldest']}（目标 {start.isoformat()}）")
        idle_before = idle
        previous_count = previous
        previous = count
        if not page.evaluate(LOAD_MORE_JS):
            clicked = False
            scroll = page.evaluate(SCROLL_ORDERS_JS)
        else:
            clicked = True
            scroll = {'before': 0, 'after': 0, 'height': 0, 'viewport': 0, 'target': 'button'}
        # 上面的订单数量差必须在更新 previous 之前计算。
        idle = stalled_rounds(idle_before, count - previous_count, scroll, clicked)
        print(f"  滚动区域={scroll['target']}，位置 {scroll['before']:.0f}→{scroll['after']:.0f}，"
              f"可滚动高度={max(0, scroll['height'] - scroll['viewport']):.0f}；"
              f"JSON响应={capture.json_responses}，含订单响应={capture.matched_responses}，解析失败={capture.parse_errors}")
        page.wait_for_timeout(scroll_wait_ms(scroll, clicked, interval))
        # 等待期间收到新订单则撤销停滞计数。
        if len(capture.orders) > count:
            idle = 0
        if idle >= 5:
            capture.reason = '连续五轮无新增且滚动位置未前进；未确认到底，完整性待核对'
            checkpoint_now(checkpoint)
            print('可能是滚动容器不适配、响应字段未识别、加载受限或确实没有更多；不能仅凭此判断到底。')
            answer = input('加载停滞。手动滚动后输入 c 继续列表，回车进入详情补全，q 保存退出：').strip().lower()
            if answer != 'c':
                return answer != 'q'
            idle = 0
            page.wait_for_timeout(interval * 1000)


class OutputCheckpoint:
    """每轮保存可恢复的 JSON；Excel 定时写入，结束和暂停时强制刷新。"""
    def __init__(self, capture, output_dir, start, end, stem, save_interval=60):
        self.capture, self.output_dir = capture, output_dir
        self.start, self.end, self.stem = start, end, stem
        self.save_interval = save_interval
        self.last_excel = float('-inf')

    def __call__(self):
        orders = list(self.capture.orders.values())
        write_json(orders, self.output_dir, self.stem)
        if time.monotonic() - self.last_excel >= self.save_interval:
            return self.flush(save_json=False)
        return self.output_dir / (self.stem + '.xlsx')

    def flush(self, save_json=True):
        path = write_excel(list(self.capture.orders.values()), self.output_dir, self.start,
                           self.end, self.capture.reason, self.stem, save_json=save_json)
        self.last_excel = time.monotonic()
        return path


def checkpoint_now(checkpoint):
    return checkpoint.flush() if isinstance(checkpoint, OutputCheckpoint) else checkpoint()


def write_json(orders, output_dir, stem):
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = output_dir / (stem + '.json')
    temporary = raw.with_suffix('.tmp.json')
    temporary.write_text(json.dumps([asdict(o) for o in orders], ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(raw)


def save_failure(capture, checkpoint, output_dir, stem, exc):
    """错误日志、JSON、Excel 分别保存，避免 Excel 文件锁覆盖原始异常。"""
    capture.reason = f'运行中断：{type(exc).__name__}: {exc}；结果不完整'
    log = output_dir / (stem + '.error.log')
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        log.write_text(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)), encoding='utf-8')
        print(f'错误日志：{log}')
    except OSError as log_error:
        print(f'错误日志保存失败：{log_error}', file=sys.stderr)
    try:
        write_json(list(capture.orders.values()), output_dir, stem)
        print(f'中途 JSON 已保存：{output_dir / (stem + ".json")}')
    except Exception as json_error:
        print(f'中途 JSON 保存失败，之前的文件仍保留：{json_error}', file=sys.stderr)
    try:
        print(f'中途 Excel 已保存：{checkpoint.flush()}')
    except Exception as excel_error:
        print(f'中途 Excel 保存失败（可能被 Excel 占用）；请保留 JSON：{excel_error}', file=sys.stderr)


def write_excel(orders, output_dir, start, end, reason, stem, save_json=True):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.utils import get_column_letter
    output_dir.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    book.remove(book.active)
    headers = ['订单号', '下单时间', '状态', '实付金额（元）', '标题', '物流号', '物流公司', '读取说明', '缺失字段', '原始内容']
    for name, selected in [('订单', [o for o in orders if in_range(o, start, end)]),
                           ('日期待核对', [o for o in orders if not extract_date(o.order_time)])]:
        sheet = book.create_sheet(name)
        sheet.append(headers)
        for order in selected:
            values = [order.order_id, order.order_time, order.status, order.amount, order.product,
                      order.tracking_number, order.logistics_company, order.detail_note, missing_fields(order), order.raw_text]
            sheet.append([ILLEGAL_CHARACTERS_RE.sub('', str(v))[:32767] for v in values])
            for cell in sheet[sheet.max_row]:
                cell.data_type = 's'  # 商品文本不能变为公式，订单号不能转为科学计数法。
            if order.amount:
                try:
                    sheet.cell(sheet.max_row, 4, float(order.amount)).number_format = '0.00'
                except ValueError:
                    pass
            try:
                sheet.cell(sheet.max_row, 2, datetime.fromisoformat(order.order_time)).number_format = 'yyyy-mm-dd hh:mm:ss'
            except ValueError:
                pass
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        for i, width in enumerate([27, 23, 18, 22, 60, 35, 22, 65, 35, 80], 1):
            sheet.column_dimensions[get_column_letter(i)].width = width
        for cell in sheet[1]:
            cell.font = Font(color='FFFFFF', bold=True)
            cell.fill = PatternFill('solid', fgColor='264653')
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical='top')
    meta = book.create_sheet('导出说明')
    summary = collection_summary(orders, start, end)
    for row in [('项目', '内容'), ('来源', ORDERS_URL), ('起始日期', start.isoformat()),
                ('结束日期（包含当天）', end.isoformat()), ('读取时间（北京时间）', datetime.now(timezone(timedelta(hours=8))).isoformat()),
                ('停止原因', reason), ('完整性', '未核实；请与账号中全部订单核对，网页可能不提供全部历史订单'),
                ('读取订单总数', summary['total']), ('日期范围内订单数', summary['in_range']),
                ('日期待核对订单数', summary['unknown']), ('日期范围外订单数', summary['outside']),
                ('已知最早下单日期', summary['oldest']), ('已知最新下单日期', summary['newest']),
                ('版本', 'V2：逐单详情与物流补全'),
                ('字段说明', '金额为实付金额（元），不使用商品价或待付金额。多个物流号以分号分隔。缺失字段与读取结果单独标注。JSON 保存全部已读取订单。')]:
        meta.append(row)
    meta.column_dimensions['A'].width = 28
    meta.column_dimensions['B'].width = 110
    path = output_dir / (stem + '.xlsx')
    temporary = path.with_suffix('.tmp.xlsx')
    book.save(temporary)
    temporary.replace(path)
    if save_json:
        write_json(orders, output_dir, stem)
    return path


def run(args: argparse.Namespace) -> int:
    start = parse_day(args.start)
    end = parse_day(args.end)
    if start > end:
        raise SystemExit("--start 不能晚于 --end")
    if args.interval < 5 or args.max_rounds < 0:
        raise SystemExit('--interval 至少 5 秒，--max-rounds 不能小于 0（0 表示不限轮数）')
    if args.save_interval < 0:
        raise SystemExit('--save-interval 不能小于 0')
    import openpyxl  # 启动浏览器前检查导出依赖。

    args.profile_dir.mkdir(parents=True, exist_ok=True)
    edge_path = find_edge_executable(args.edge_path)
    print(f"日期范围：{start.isoformat()} 至 {end.isoformat()}（含首尾）")
    print(f"登录态目录：{args.profile_dir}")
    print(f"浏览器：{edge_path}")

    if args.login_first or args.login_only:
        run_plain_edge_login(edge_path, args.profile_dir, args.login_timeout)
        print("登录态已由普通 Edge 保存。")
        if args.login_only:
            return 0

    sync_playwright, _ = import_playwright()
    edge_process: subprocess.Popen[Any] | None = None
    browser = None
    capture = Capture()
    stem = 'pdd_orders_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    checkpoint = OutputCheckpoint(capture, args.output_dir, start, end, stem, args.save_interval)

    with sync_playwright() as playwright:
        try:
            edge_process, cdp_endpoint = launch_edge_for_collection(edge_path, args.profile_dir)
            browser = playwright.chromium.connect_over_cdp(cdp_endpoint)
            if not browser.contexts:
                raise RuntimeError("Edge 没有可读取的浏览器上下文")
            context = browser.contexts[0]
            page = context.pages[-1] if context.pages else context.new_page()
            page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            input('请在浏览器手动登录并进入“全部订单”，确认后按回车开始读取：')
            while not looks_logged_in(page):
                input('尚未识别到订单页面，请完成登录并回到订单页，再按回车：')
            context.on('response', capture.response)
            page.reload(wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(args.interval * 1000)
            click_all_tab(page)
            continue_details = collect_orders(page, start, end, args.max_rounds, capture, checkpoint, args.interval)
            if continue_details and not args.list_only:
                enrich_orders(page, start, end, capture, checkpoint, args.interval)
            path = checkpoint.flush()
            print(f'已导出：{path}；{capture.reason}')
            return 0
        except BaseException as exc:
            save_failure(capture, checkpoint, args.output_dir, stem, exc)
            raise
        finally:
            if edge_process is not None or browser is not None:
                # Playwright 上下文退出只断开连接，用户可继续查看页面。
                # 不强制终止 Edge，否则会破坏页面现场并触发“意外关闭”提示。
                print('脚本不强制关闭 Edge；下次运行会尝试复用该专用窗口的采集连接。')


def main() -> int:
    try:
        return run(parse_args())
    except KeyboardInterrupt:
        print("已取消。")
        return 130
    except Exception as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
