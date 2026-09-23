"""读取拼多多买家端指定日期范围内的全部订单。

脚本使用一个独立的、可见的 Edge 持久化浏览器目录：

    python scripts/pdd_orders.py --login-first --start 2026-01-01

首次运行建议加 --login-first。脚本先启动完全普通的 Edge；请手动完成扫码、
短信或验证码登录，确认能看到订单后关闭该 Edge 窗口。随后脚本会用同一资料目录
重新启动 Edge 并读取订单。登录 Cookie 会保存在 .data/pdd_orders_edge_profile。

说明：
* 目标页面是拼多多买家端 mobile.yangkeduo.com/orders.html；
* 脚本只读取页面已展示的订单，不填写或保存账号密码；
* 不自动处理 CAPTCHA/滑块；
* 拼多多页面是动态渲染的，因此订单原文 raw_text 也会一并保存，方便核对。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any


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


def parse_args() -> argparse.Namespace:
    today = datetime.now(timezone(timedelta(hours=8))).date()
    parser = argparse.ArgumentParser(description="手动登录拼多多买家端并导出 Excel/JSON")
    parser.add_argument('--interval', type=float, default=6, help='加载间隔秒数，至少 5 秒')
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
        default=240,
        help="最多滚动/加载轮数，防止页面异常时无限运行（默认：240）",
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
    amount_match = re.search(
        r"(?:实付|合计|付款|总价|应付)\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)"
        r"|[¥￥]\s*(\d+(?:\.\d{1,2})?)",
        text,
    )
    tracking_match = re.search(
        r"(?:物流单号|快递单号|运单号|物流编号|快递编号)\s*[:：]?\s*"
        r"([A-Z0-9][A-Z0-9-]{5,31})",
        text,
        re.I,
    )
    if not tracking_match:
        tracking_match = re.search(
            r"\b(?:SF|YT|ZTO|STO|YD|JT|JDVA|EMS)[A-Z0-9-]{6,28}\b",
            text,
            re.I,
        )

    status_words = (
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
    status = next((word for word in status_words if word in text), "")
    company_names = ("顺丰", "中通", "圆通", "申通", "韵达", "京东", "邮政", "极兔")
    company = next((name for name in company_names if name in text), "")

    lines = [normalize_text(line) for line in (card.get("lines") or []) if normalize_text(line)]
    product = ""
    for line in lines:
        product_match = re.match(r"(?:商品|共\s*\d+\s*件)\s*[:：]?\s*(.{2,200})$", line)
        if product_match:
            product = product_match.group(1)
            break
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

    amount = (amount_match.group(1) or amount_match.group(2)) if amount_match else ""
    return Order(
        order_id=order_id,
        order_time=time_match.group(1) if time_match else "",
        status=status,
        amount=amount,
        product=clean_field(product, "商品"),
        tracking_number=(tracking_match.group(1) if tracking_match.lastindex else tracking_match.group(0)).upper()
        if tracking_match
        else "",
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
  const hasDate = /20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?/;
  const nodes = [...document.querySelectorAll('body *')].filter(el => {
    if (!visible(el)) return false;
    const text = String(el.innerText || '').replace(/\s+/g, ' ').trim();
    return text && (orderHint.test(text) || (hasDate.test(text) && /\d{12,24}/.test(text)));
  });
  const result = [];
  const seen = new Set();
  nodes.sort((a,b) => a.innerText.length - b.innerText.length);
  for (const node of nodes) {
    let candidate = node;
    // 取最小的、同时包含订单号和日期的可见祖先，避免返回整个 body。
    for (let i = 0; i < 7 && candidate.parentElement; i++) {
      const parent = candidate.parentElement;
      const text = String(parent.innerText || '').replace(/\s+/g, ' ').trim();
      if ((text.match(/订单编号|订单号|订单\s*ID/gi) || []).length === 1 && text.length >= 80 && text.length <= 3500 &&
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


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_cdp(process: subprocess.Popen[Any], port: int, timeout_seconds: int = 20) -> str:
    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Edge 启动失败，退出码：{process.returncode}")
        try:
            with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("webSocketDebuggerUrl"):
                return endpoint
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError("Edge 已启动，但无法建立本机采集连接")


def launch_edge_for_collection(edge_path: Path, profile_dir: Path) -> tuple[subprocess.Popen[Any], str]:
    """启动真实 Edge，再通过仅绑定 127.0.0.1 的 CDP 读取已登录页面。"""
    port = free_loopback_port()
    process = subprocess.Popen(
        [
            str(edge_path),
            *edge_base_args(profile_dir),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            # 保留真实 Windows Edge UA，仅用窄窗口触发 H5 的响应式布局。
            "--window-size=520,900",
            ORDERS_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        return process, wait_for_cdp(process, port)
    except Exception:
        if process.poll() is None:
            process.terminate()
        raise


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
        return normalize_text(page.locator("body").inner_text(timeout=3000))
    except Exception:
        return ""


def looks_logged_in(page: Any) -> bool:
    url = str(page.url or "").lower()
    body = page_text(page)
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
            yield Order(str(oid), when, normalize_text(value.get('order_status_desc')),
                        product=product or normalize_text(value.get('goods_name')),
                        raw_text=json.dumps(value, ensure_ascii=False))
        for child in value.values():
            if isinstance(child, (list, dict)):
                yield from response_orders(child)


class Capture:
    def __init__(self):
        self.orders: dict[str, Order] = {}
        self.blocked = False
        self.reason = '尚未完成读取'

    def merge(self, order: Order):
        previous = self.orders.get(order.order_id)
        if previous:
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
        try:
            for order in response_orders(response.json()):
                self.merge(order)
        except Exception:
            # HTML 验证页、空响应或结构不适配不等于没有订单。
            pass


def collect_orders(page, start, end, max_rounds, capture, checkpoint, interval=6):
    idle = 0
    previous = 0
    for round_no in range(max_rounds):
        text = page_text(page)
        if capture.blocked or re.search(r'访问过于频繁|操作过于频繁|安全验证|请完成验证|滑块|验证码|请登录|立即登录', text) or not looks_logged_in(page):
            capture.reason = '遇到验证、访问限制或登录失效'
            checkpoint()
            answer = input('请在浏览器手动处理验证/登录。处理好后输入 c 继续，其他输入保存退出：').strip().lower()
            if answer != 'c':
                return
            capture.blocked = False
            page.wait_for_timeout(interval * 1000)
            continue
        for card in page.evaluate(EXTRACT_CARDS_JS):
            order = parse_order_card(card)
            if order:
                capture.merge(order)
        capture.reason = '读取中，尚未核实完整性'
        checkpoint()
        count = len(capture.orders)
        print(f'第 {round_no + 1} 轮：读取 {count} 单，范围内 {sum(in_range(o, start, end) for o in capture.orders.values())} 单')
        idle = idle + 1 if count == previous else 0
        previous = count
        if idle >= 5:
            capture.reason = '连续五轮无新增订单；可能到底或页面限制，完整性待核对'
            checkpoint()
            answer = input('没有新增订单。可手动滚动或进入订单详情后输入 c 继续，其他输入导出退出：').strip().lower()
            if answer != 'c':
                return
            idle = 0
        if not page.evaluate(LOAD_MORE_JS):
            page.mouse.wheel(0, 650)
        page.wait_for_timeout(interval * 1000)
    capture.reason = '达到最大读取轮数，完整性待核对'


def write_excel(orders, output_dir, start, end, reason, stem):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.utils import get_column_letter
    output_dir.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    book.remove(book.active)
    headers = ['订单号', '下单时间', '状态', '金额（页面展示）', '商品', '运单号', '物流公司', '原始内容']
    for name, selected in [('订单', [o for o in orders if in_range(o, start, end)]),
                           ('日期待核对', [o for o in orders if not extract_date(o.order_time)])]:
        sheet = book.create_sheet(name)
        sheet.append(headers)
        for order in selected:
            sheet.append([ILLEGAL_CHARACTERS_RE.sub('', str(v))[:32767] for v in asdict(order).values()])
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
        for i, width in enumerate([27, 23, 18, 22, 60, 27, 18, 80], 1):
            sheet.column_dimensions[get_column_letter(i)].width = width
        for cell in sheet[1]:
            cell.font = Font(color='FFFFFF', bold=True)
            cell.fill = PatternFill('solid', fgColor='264653')
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical='top')
    meta = book.create_sheet('导出说明')
    for row in [('项目', '内容'), ('来源', ORDERS_URL), ('起始日期', start.isoformat()),
                ('结束日期（包含当天）', end.isoformat()), ('读取时间（北京时间）', datetime.now(timezone(timedelta(hours=8))).isoformat()),
                ('停止原因', reason), ('完整性', '未核实；请与账号中全部订单核对，网页可能不提供全部历史订单'),
                ('字段说明', '缺失字段留空；不推测金额单位。JSON 保存读取到的全部订单，日期未知的另列工作表。')]:
        meta.append(row)
    meta.column_dimensions['A'].width = 28
    meta.column_dimensions['B'].width = 110
    path = output_dir / (stem + '.xlsx')
    temporary = path.with_suffix('.tmp.xlsx')
    book.save(temporary)
    temporary.replace(path)
    raw = output_dir / (stem + '.json')
    temp_json = raw.with_suffix('.tmp.json')
    temp_json.write_text(json.dumps([asdict(o) for o in orders], ensure_ascii=False, indent=2), encoding='utf-8')
    temp_json.replace(raw)
    return path


def run(args: argparse.Namespace) -> int:
    start = parse_day(args.start)
    end = parse_day(args.end)
    if start > end:
        raise SystemExit("--start 不能晚于 --end")
    if args.interval < 5 or args.max_rounds < 1:
        raise SystemExit('--interval 至少 5 秒，--max-rounds 必须大于 0')
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
    def checkpoint():
        return write_excel(list(capture.orders.values()), args.output_dir, start, end, capture.reason, stem)

    with sync_playwright() as playwright:
        try:
            edge_process, cdp_endpoint = launch_edge_for_collection(edge_path, args.profile_dir)
            browser = playwright.chromium.connect_over_cdp(cdp_endpoint)
            if not browser.contexts:
                raise RuntimeError("Edge 没有可读取的浏览器上下文")
            context = browser.contexts[0]
            context.on('response', capture.response)
            page = context.pages[-1] if context.pages else context.new_page()
            page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            input('请在浏览器手动登录并进入“全部订单”，确认后按回车开始读取：')
            while not looks_logged_in(page):
                input('尚未识别到订单页面，请完成登录并回到订单页，再按回车：')

            click_all_tab(page)
            collect_orders(page, start, end, args.max_rounds, capture, checkpoint, args.interval)
            path = checkpoint()
            print(f'已导出：{path}；{capture.reason}')
            return 0
        except BaseException:
            capture.reason = '用户中断或运行异常，已保存读取结果，完整性待核对'
            print(f'中途保存：{checkpoint()}')
            raise
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
                time.sleep(1)
            if edge_process is not None and edge_process.poll() is None:
                edge_process.terminate()
                try:
                    edge_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    edge_process.kill()


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
