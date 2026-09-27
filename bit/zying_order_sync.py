"""Read Zying orders through its authenticated UI, then update local purchases."""
import copy
import html
import re
import threading
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

ORDER_URL = "https://meli.zying.net/#/order"
SEARCH_PLACEHOLDER = "订单、采购单、运单,多个编号可以逗号、空格分隔"


def clean(value):
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()


def identifiers(value):
    return set(re.findall(r"[A-Za-z0-9_-]+", clean(value)))


def order_keys(row):
    return {clean(row.get(key)) for key in ("id", "order_number", "pack_id")} - {""}


def parse_purchase(detail, row):
    roots = detail.get("root") or []
    if len(roots) != 1:
        raise ValueError("智赢订单详情不唯一")
    root = roots[0]
    remote_keys = identifiers(root.get("order_no")) | identifiers(root.get("order_key"))
    if not remote_keys.intersection(order_keys(row)):
        raise ValueError("智赢详情订单号与泽顺订单不一致")
    # A pack match alone must not assign its entire cost to one child order.
    if clean(row.get("id")) not in remote_keys:
        raise ValueError("仅匹配到打包单，无法确认子订单成本归属")
    buys = detail.get("buys")
    if not isinstance(buys, list):
        raise ValueError("智赢采购数据格式异常")
    if not buys:
        return None
    changes = {}
    for target, source in (("purchase_order", "no"), ("purchase_tracking", "trace")):
        values = list(dict.fromkeys(clean(buy.get(source)) for buy in buys if clean(buy.get(source))))
        if values:
            value = ",".join(values)
            if len(value) > 255:
                raise ValueError("采购单号或物流号超过字段长度")
            changes[target] = value
    try:
        cost = Decimal(str(root.get("order_cost")))
    except InvalidOperation as exc:
        raise ValueError("智赢成本缺失或不可读取") from exc
    if not cost.is_finite() or cost < 0:
        raise ValueError("智赢成本异常")
    changes["purchase_cost"] = str(cost.quantize(Decimal("0.01")))
    return changes


def snapshot_orders(reader, filters):
    params = dict(filters, page=1, page_size=200)
    result, seen = [], set()
    while True:
        data = reader(**params)
        rows = data.get("rows") or []
        for row in rows:
            key = clean(row.get("id"))
            if not key:
                raise ValueError("订单缺少内部编号")
            if key in seen:
                raise ValueError("订单列表在读取期间发生变化，请重新同步")
            seen.add(key)
            result.append(dict(row))
        if len(result) >= int(data.get("total") or 0):
            return result
        if not rows:
            raise ValueError("订单列表分页不完整，请重新同步")
        params["page"] += 1


def response_data(response):
    if not response.ok:
        raise RuntimeError("智赢请求失败，请检查登录状态")
    payload = response.json()
    if payload.get("code") != 200:
        raise RuntimeError("智赢请求未成功，请检查登录状态与订单查看权限")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("智赢响应异常或登录已失效，请重新登录")
    return data


class ZyingPage:
    def __init__(self, page):
        self.page = page

    def lookup(self, row):
        page = self.page
        # A new document resets the ERP's in-memory date/status filters.
        page.goto(ORDER_URL, wait_until="domcontentloaded")
        page.reload(wait_until="domcontentloaded")
        field = page.get_by_placeholder(SEARCH_PLACEHOLDER, exact=True)
        try:
            field.wait_for(timeout=30000)
        except Exception as exc:
            raise RuntimeError("无法打开智赢订单页，请先在服务器 Edge 登录并确认订单查看权限") from exc
        key = ",".join(sorted(order_keys(row)))
        field.fill(key)
        def searched(response):
            if "cmd=orders.load" not in response.url:
                return False
            body = response.request.post_data_json or {}
            return body.get("key") == key
        with page.expect_response(searched, timeout=30000) as pending:
            field.press("Enter")
        listing = response_data(pending.value).get("list")
        if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
            raise RuntimeError("智赢订单列表格式异常")
        rows = listing["data"]
        if int(listing.get("maxcount") or 0) > len(rows):
            raise ValueError("智赢搜索结果超过一页，无法确认唯一订单")
        matches = [item for item in rows if order_keys(row).intersection(
            identifiers(item.get("no")) | identifiers(item.get("key")))]
        exact = [item for item in matches if clean(row.get("id")) in
                 identifiers(item.get("no")) | identifiers(item.get("key"))]
        matches = exact or matches
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("智赢找到多个对应订单，请人工核对")
        internal_id = clean(matches[0].get("id"))
        if not internal_id.isdigit():
            raise ValueError("智赢内部订单号异常")
        def detailed(response):
            return ("cmd=order.view" in response.url and
                    str((response.request.post_data_json or {}).get("id")) == internal_id)
        with page.expect_response(detailed, timeout=30000) as pending:
            page.locator(f'tr[data-row-key="{internal_id}"]').click()
        detail = response_data(pending.value)
        if clean((detail.get("root") or [{}])[0].get("order_id")) != internal_id:
            raise ValueError("智赢返回了其他订单详情")
        return detail


@contextmanager
def open_reader(options):
    from playwright.sync_api import sync_playwright
    from bit import bit_zying_caiji as config
    endpoint = config.DEFAULT_ZYING_EDGE_DEBUGGER_ADDRESS
    config.ensure_visible_zying_edge_login_window(endpoint, start_url=ORDER_URL)
    if not endpoint.startswith(("http://", "https://", "ws://", "wss://")):
        endpoint = "http://" + endpoint
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=30000)
        page = browser.contexts[0].new_page()
        try:
            page.set_default_timeout(30000)
            page.goto(ORDER_URL, wait_until="domcontentloaded")
            page.bring_to_front()
            yield ZyingPage(page)
        finally:
            page.close()
            # Exiting Playwright disconnects without closing Edge.


class SyncManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}
        self.running = False
        self.login_events = {}

    def status(self, owner):
        with self.lock:
            return copy.deepcopy(self.jobs.get(str(owner), {"running": False, "phase": "idle"}))

    def confirm_login(self, owner):
        with self.lock:
            state = self.jobs.get(str(owner), {})
            if not state.get("running") or state.get("phase") != "waiting_login":
                raise ValueError("当前没有等待登录的同步任务，请重新启动同步")
            state.update(phase="syncing", message="正在验证登录并同步订单")
            self.login_events[str(owner)].set()
        return self.status(owner)

    def start(self, owner, filters, options, reader, writer, browser_factory=open_reader):
        with self.lock:
            if self.running:
                raise ValueError("智赢订单同步正在运行，请稍后再试")
            self.running = True
            state = {"running": True, "phase": "loading", "total": 0, "completed": 0,
                     "updated": 0, "skipped": 0, "failed": 0, "results": [], "message": "正在获取筛选范围内的全部订单"}
            self.jobs[str(owner)] = state
            login_event = threading.Event()
            self.login_events[str(owner)] = login_event
        def run():
            try:
                rows = snapshot_orders(reader, filters)
                with self.lock:
                    state.update(total=len(rows), phase="syncing", message="正在查询智赢采购信息")
                if rows:
                    with browser_factory(options) as browser:
                        if options.get("require_login"):
                            with self.lock:
                                state.update(phase="waiting_login", message="已打开服务器 Edge，请登录智赢后点击确定登录")
                            if not login_event.wait(900):
                                raise RuntimeError("等待登录超时，请重新启动同步")
                        for row in rows:
                            result = {"order_id": clean(row["id"]), "order_number": clean(row.get("order_number"))}
                            try:
                                try:
                                    detail = browser.lookup(row)
                                except ValueError:
                                    raise
                                except Exception as exc:
                                    raise ConnectionError(str(exc)) from exc
                                changes = parse_purchase(detail, row) if detail else None
                                if changes is None:
                                    result.update(status="skipped", message="未添加采购" if detail else "未找到对应订单")
                                else:
                                    saved = writer(row["id"], changes)
                                    if not saved or int(saved.get("matched") or 0) != 1:
                                        raise ValueError("泽顺订单未成功匹配，未确认回填")
                                    result.update(status="updated", message="已同步", **changes)
                            except ConnectionError:
                                raise
                            except Exception as exc:
                                result.update(status="failed", message=str(exc)[:300])
                            with self.lock:
                                state[result["status"]] += 1
                                state["completed"] += 1
                                state["results"].append(result)
                with self.lock:
                    state.update(phase="completed", message="同步完成" if rows else "当前筛选没有订单")
            except Exception as exc:
                with self.lock:
                    state.update(phase="failed", message=str(exc)[:300])
            finally:
                with self.lock:
                    state["running"] = False
                    self.running = False
        thread = threading.Thread(target=run, name="zying-order-sync", daemon=True)
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.running = False
                state.update(running=False, phase="failed", message="同步线程启动失败")
            raise
        return self.status(owner)


manager = SyncManager()
