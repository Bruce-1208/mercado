"""Visible CDP browser adapter. Site-specific selectors are explicit and inspectable."""
import hashlib
import json
import random
import re
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, build_opener

from .config import safe_url, selection_key, selection_params
from .models import clean_title, number, parse_price

# React keeps the DOM fiber pointer across updates. Its alternate may now be the
# committed tree; reading the original memoizedProps can return the initial [].
CATEGORY_PROPS = """element => {
  const key=Object.keys(element).find(k=>k.startsWith('__reactFiber$'));
  let node=key?element[key]:null;
  while(node){let root=node;while(root.return)root=root.return;
    const active=root.stateNode?.current&&root.stateNode.current!==root?(node.alternate||node):node;
    const p=active.memoizedProps||{};
    if(Array.isArray(p.options))return p;
    node=node.return;
  }
  node=element.__vueParentComponent;
  while(node){if(Array.isArray(node.props?.options))return node.props;node=node.parent;}
  return null;
}"""
CATEGORY_READ = """element => {
  const p=(PROPS)(element);if(!p)return {options:[],value:[],ready:false};
  const f={label:'label',value:'value',children:'children',...p.fieldNames};
  const copy=items=>items.map(o=>({value:String(o[f.value]),label:String(o[f.label]??'').trim(),
    disabled:!!o.disabled,children:copy(o[f.children]||[])}));
  return {options:copy(p.options),value:p.value||[],ready:true};
}""".replace("PROPS", CATEGORY_PROPS)
CATEGORY_SET = """(element,wanted) => {
  const p=(PROPS)(element);if(!p)return false;
  const f={value:'value',children:'children',...p.fieldNames};
  let choices=p.options;const selected=[];
  for(const id of wanted){const item=choices.find(o=>String(o[f.value])===id);
    if(!item||item.disabled)return false;selected.push(item);choices=item[f.children]||[];}
  const change=p.onChange||p['onUpdate:value'];if(typeof change!=='function')return false;
  change(selected.map(o=>o[f.value]),selected);return true;
}""".replace("PROPS", CATEGORY_PROPS)


def category_paths(options, parents=()):
    result = []
    for option in options:
        path = (*parents, option)
        result.append({"value": "/".join(p["value"] for p in path),
                       "label": " / ".join(p["label"] for p in path),
                       "name": option["label"], "depth": len(path),
                       "parent_value": "/".join(p["value"] for p in parents),
                       "disabled": any(p.get("disabled", False) for p in path),
                       "path_values": [p["value"] for p in path]})
        result.extend(category_paths(option.get("children", []), path))
    return result


class CircuitOpen(RuntimeError):
    pass


class Stopped(RuntimeError):
    pass


class Browser:
    def __init__(self, config, stop, log):
        self.config, self.stop, self.log = config, stop, log
        self.s = config["selectors"]
        self.pw = self.browser = None
        self.owned = []

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        try:
            # Resolve the local endpoint without system HTTP proxies. In particular,
            # proxy settings must not send the Edge debug discovery request outside this PC.
            endpoint = self.config["cdp_url"].rstrip("/")
            safe_url(endpoint, local=True)
            with build_opener(ProxyHandler({})).open(endpoint + "/json/version", timeout=5) as response:
                websocket = json.load(response).get("webSocketDebuggerUrl", "")
            parsed = urlsplit(websocket)
            if parsed.scheme not in ("ws", "wss") or parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Edge未返回有效的本机连接地址")
            self.browser = self.pw.chromium.connect_over_cdp(websocket, timeout=15000)
            if not self.browser.contexts:
                raise ValueError("未找到可连接的 Edge 会话，请先点击打开登录页面")
            self.context = self.browser.contexts[0]
            self.context.set_default_timeout(12000)
            self.context.set_default_navigation_timeout(30000)
            session = self.browser.new_browser_cdp_session()
            version = session.send("Browser.getVersion")
            session.detach()
            if "Headless" in version.get("userAgent", ""):
                raise ValueError("禁止连接无头浏览器，请使用可见Edge窗口")
            self.log("已连接可见浏览器，复用已有会话；程序不执行登录")
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        # Disconnect only; never Browser.close() the user's browser.
        for page in self.owned:
            try:
                page.close()
            except Exception:
                pass
        if self.pw:
            self.pw.stop()

    def check(self, page):
        if self.stop.is_set():
            raise Stopped("操作已停止")
        for frame in page.frames:
            risk = self.s["risk"]
            if risk and frame.locator(risk).count() and frame.locator(risk).first.is_visible():
                raise CircuitOpen("页面出现验证码或访问验证")
            text = frame.locator("body").inner_text(timeout=3000) if frame.locator("body").count() else ""
            if re.search(r"操作[太过]?于?频繁|发言受限|发送[太过]?于?频繁|滑动验证|请完成.*验证|安全验证|访问受限", text):
                raise CircuitOpen("页面出现操作频繁、发言受限或安全验证提示")

    def delay(self, low=1, high=3):
        seconds = random.uniform(low, high)
        self.log(f"操作间隔 {seconds:.2f} 秒")
        if self.stop.wait(seconds):
            raise Stopped("操作已停止")

    def page(self, url, host=None):
        safe_url(url, host=host)
        page = self.context.new_page()
        self.owned.append(page)
        page.goto(url, wait_until="domcontentloaded")
        safe_url(page.url, host=host)
        self.check(page)
        return page

    def release(self, page):
        if page in self.owned:
            page.close()
            self.owned.remove(page)

    def value(self, root, key, attribute=None, required=False):
        selector = self.s[key]
        if not selector:
            if required:
                raise ValueError(f"请配置DOM字段：{key}")
            return ""
        items = root.locator(selector)
        if items.count() != 1:
            if required:
                raise ValueError(f"DOM字段 {key} 必须唯一，实际 {items.count()} 个")
            return ""
        item = items.first
        if attribute:
            val = item.get_attribute(attribute) or ""
        else:
            tag = item.evaluate("e => e.tagName.toLowerCase()")
            val = item.input_value() if tag in ("input", "textarea", "select") else item.inner_text()
        return val.strip()

    def unique(self, page, key):
        selector = self.s[key]
        if not selector:
            raise ValueError(f"请配置DOM字段：{key}")
        locator = page.locator(selector)
        locator.first.wait_for(state="visible")
        if locator.count() != 1:
            raise ValueError(f"DOM字段 {key} 必须唯一")
        return locator

    def open_login(self):
        from .edge import LOGIN_URL
        pages = [p for p in self.context.pages if urlsplit(p.url).hostname == "seller.zying.net"]
        if pages:
            page = pages[-1]
        else:
            page = self.context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
        # This is a user login tab, intentionally not owned/closed by the worker.
        page.bring_to_front()

    def confirm_login(self):
        for page in reversed(self.context.pages):
            host = urlsplit(page.url).hostname or ""
            if host != "zying.net" and not host.endswith(".zying.net"):
                continue
            if "login" in page.url.lower() or page.locator("input[type='password']:visible").count():
                continue
            self.check(page)
            has_token = page.evaluate("() => !!localStorage.getItem('token')")
            has_cookie = any(c["name"] == "token" and c.get("value") for c in self.context.cookies([page.url]))
            shell = page.locator(".ant-layout-sider, .ant-menu, .product-title")
            if not (has_token or has_cookie) or not shell.count():
                continue
            # No credentials leave the browser adapter or enter our task database.
            return {"page_url": page.url, "confirmed_at": time.time()}
        raise ValueError("尚未检测到已登录的智赢后台。请在刚打开的 Edge 窗口完成登录，再点击“我已成功登录”")

    def categories(self):
        page = self.page(self.config["erp_list_url"])
        try:
            if "login" in page.url.lower():
                raise ValueError("智赢登录已失效，请重新登录并确认")
            control = self.unique(page, "erp_category_control")
            options = category_paths(self.read_categories(page, control)["options"])
            self.log(f"已从智赢商品页刷新分类：{len(options)} 个，最深 {max(o['depth'] for o in options)} 级")
            return options
        finally:
            self.release(page)

    def read_categories(self, page, control, timeout=20):
        deadline = time.monotonic() + timeout
        previous, data = None, {}
        while time.monotonic() < deadline:
            self.check(page)
            if "login" in page.url.lower():
                raise ValueError("智赢登录已失效，请重新登录并确认")
            data = control.evaluate(CATEGORY_READ)
            options = data.get("options", [])
            # Require a populated tree that remains unchanged across two reads.
            # Empty initial React props must never replace a successful snapshot.
            if options and options == previous:
                return data
            previous = options
            if self.stop.wait(.5):
                raise Stopped("操作已停止")
        diagnostic = control.evaluate("""e => ({tag:e.tagName,classes:e.className,
          react:Object.keys(e).some(k=>k.startsWith('__reactFiber$')),vue:!!e.__vueParentComponent,
          placeholder:e.querySelector('input')?.placeholder||'',text:e.innerText?.slice(0,120)||''})""")
        self.log("分类读取诊断：" + json.dumps({"url": page.url, "ready": data.get("ready"), "control": diagnostic}, ensure_ascii=False), level="ERROR")
        raise ValueError("智赢分类尚未加载完成或当前页面无分类，未更新分类列表。请确认商品列表页后重试")

    def apply_category(self, page, category):
        selector = self.s["erp_category_control"]
        controls = page.locator(selector) if selector else None
        if not controls or not controls.count():
            raise ValueError("未找到分类控件，无法确认筛选范围，请检查erp_category_control")
        if controls.count() != 1:
            raise ValueError("存在多个分类控件，请缩小erp_category_control选择器")
        control = controls.first
        current = self.read_categories(page, control)
        paths = category_paths(current["options"])
        target = next((p for p in paths if p["value"] == category), None) if category else None
        if category and not target:
            raise ValueError("所选分类在当前智赢页面已不存在，请刷新分类后重新选择")
        if target and target.get("disabled"):
            raise ValueError("所选分类已被智赢禁用，请重新选择")
        wanted = target["path_values"] if target else []
        if [str(v) for v in current["value"]] != wanted:
            if not control.evaluate(CATEGORY_SET, wanted):
                raise ValueError("分类筛选设置失败，请检查当前页面适配")
            self.delay(.3, .6)
            selected = [str(v) for v in control.evaluate(CATEGORY_READ)["value"]]
            if selected != wanted:
                raise ValueError("页面分类未切换成功，已停止采集")
        search = page.get_by_role("button", name="搜索", exact=True)
        if search.count() != 1:
            raise ValueError("未找到唯一的智赢分类搜索按钮")
        self.check(page)
        search.click()
        self.delay(2, 3)
        return target["label"] if target else "全部分类"

    def current_page(self, page):
        text = self.value(page, "erp_page_active", required=True)
        if not text.isdigit():
            raise ValueError("无法确认当前页码，已停止采集")
        return int(text)

    def first_page(self, page):
        if self.current_page(page) != 1:
            self.unique(page, "erp_page_first").click()
            self.delay(1, 2)
            if self.current_page(page) != 1:
                raise ValueError("返回第一页失败，已停止采集")

    def next_page(self, page, current):
        button = page.locator(self.s["erp_next"])
        if not button.count() or not button.first.is_visible() or not button.first.is_enabled():
            return False
        self.check(page)
        button.first.click()
        self.delay(2, 4)
        if self.current_page(page) != current + 1:
            raise ValueError("翻页后页码不符，已停止采集")
        return True

    def collect(self, store):
        selection = selection_params(self.config.get("run_selection"), self.config)
        scope = selection_key(selection, self.config)
        page = self.page(self.config["erp_list_url"])
        seen = set()
        checkpoint = store.state("collection", {})
        resume_after = checkpoint.get("page", 0) if not checkpoint.get("complete") and checkpoint.get("scope") == scope else 0
        if not resume_after:
            store.reset_scope(scope)
        if resume_after:
            store.log(f"从已保存的第 {resume_after} 页后续采集，前序页面只翻页、不重复提取")
        try:
            category_label = self.apply_category(page, selection["category"])
            self.first_page(page)
            first = max(selection["start_page"], resume_after + 1)
            store.log(f"本次采集：{category_label}，第 {selection['start_page']}–{selection['end_page']} 页")
            for page_number in range(1, selection["end_page"] + 1):
                self.check(page)
                if page_number < first:
                    if not self.next_page(page, page_number):
                        raise ValueError(f"列表仅有 {page_number} 页，无法到达起始/续跑页 {first}")
                    continue
                rows = page.locator(self.s["erp_rows"])
                rows.first.wait_for(state="visible")
                signature = rows.all_inner_texts()
                fingerprint = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
                if fingerprint in seen:
                    raise ValueError("翻页后商品未变化，采集已停止")
                seen.add(fingerprint)
                count = 0
                for row in rows.all():
                    self.check(page)
                    raw = row.inner_text()
                    key = self.value(row, "erp_id", required=True)
                    key = re.sub(r"^(?:产品ID|商品ID|ID)\s*[:：]?\s*", "", key).strip()
                    image = self.value(row, "erp_image", "src", required=True)
                    record = {"erp_goods_id": key, "title": self.value(row, "erp_title", required=True),
                              "main_image_url": urljoin(page.url, image),
                              "description": self.value(row, "erp_description") or raw,
                              "erp_sku": self.value(row, "erp_sku"), "raw_erp": raw,
                              "source_page": page_number, "source_category": selection["category"],
                              "source_category_label": category_label}
                    link = self.value(row, "erp_edit_link", "href")
                    if link:
                        record["erp_edit_url"] = urljoin(page.url, link)
                    ref = self.value(row, "erp_reference")
                    if ref:
                        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(g|克|kg|公斤|千克)", ref, re.I)
                        if match:
                            record["reference_weight_g"] = str(number(match[1]) * (1000 if match[2].lower() in ("kg", "公斤", "千克") else 1))
                        record["raw_reference"] = ref
                    count += store.add(record)
                    store.include_in_scope(scope, key, page_number)
                store.log(f"ERP第 {page_number} 页采集完成，新增 {count} 条；已存在记录保留原进度")
                store.set_state("collection", {"page": page_number, "signature": fingerprint, "at": time.time(),
                                               "url": self.config["erp_list_url"], "scope": scope,
                                               "selection": selection, "complete": page_number == selection["end_page"]})
                store.export()
                if page_number == selection["end_page"]:
                    break
                if not self.next_page(page, page_number):
                    store.set_state("collection", {**store.state("collection"), "complete": True})
                    store.log(f"列表已到末页 {page_number}，本次采集结束")
                    break
            return len(seen)
        finally:
            self.release(page)

    def candidates(self, task):
        page = self.page(self.config["supplier_home_url"], "1688.com")
        try:
            self.delay(2, 6)
            field = self.unique(page, "search_input")
            field.fill("")
            field.press_sequentially(clean_title(task["title"]), delay=random.randint(60, 140))
            self.check(page)
            self.unique(page, "search_button").click()
            self.delay(2, 4)
            self.check(page)
            page.mouse.wheel(0, random.randint(250, 650))
            links = page.locator(self.s["result_links"])
            if not links.count():
                return
            urls = []
            for link in links.all():
                url = urljoin(page.url, link.get_attribute("href") or "")
                if url not in urls and urlsplit(url).hostname == "detail.1688.com":
                    urls.append(url)
                if len(urls) >= self.config["max_candidates"]:
                    break
            for url in urls:
                detail = self.page(url, "1688.com")
                try:
                    self.delay()
                    self.check(detail)
                    sku_rows = detail.locator(self.s["sku_rows"])
                    sku_rows.first.wait_for(state="visible")
                    skus = []
                    for sku in sku_rows.all():
                        sku_id = (sku.get_attribute(self.s["sku_id_attribute"]) or "").strip()
                        if not sku_id:
                            raise ValueError("SKU缺少稳定ID")
                        skus.append({"id": sku_id, "label": self.value(sku, "sku_label", required=True),
                                     "price": parse_price(self.value(sku, "sku_price", required=True))})
                    yield {"url": detail.url, "title": self.value(detail, "supplier_title", required=True),
                           "main_image_url": urljoin(detail.url, self.value(detail, "supplier_image", "src", True)),
                           "description": self.value(detail, "supplier_description"), "skus": skus,
                           "merchant_id": self.value(detail, "supplier_merchant", self.s["supplier_merchant_attribute"], True)}
                except (CircuitOpen, Stopped):
                    raise
                except Exception as exc:
                    self.log(f"候选详情无法读取：{type(exc).__name__}: {exc}", task["erp_goods_id"], "WARNING")
                finally:
                    self.release(detail)
        finally:
            self.release(page)

    def chat_root(self, page):
        self.check(page)
        selector = self.s["chat_identity"]
        roots = [f for f in page.frames if selector and f.locator(selector).count() == 1]
        if len(roots) != 1:
            raise ValueError("无法唯一定位商家会话；请配置chat_identity，支持iframe")
        return roots[0]

    def verify_chat(self, page, merchant):
        root = self.chat_root(page)
        actual = self.value(root, "chat_identity", self.s["chat_identity_attribute"], True)
        if actual != merchant:
            raise ValueError("会话商家ID与匹配供货商不一致")
        return root

    def messages(self, page, merchant):
        root = self.verify_chat(page, merchant)
        if not self.s["chat_messages"]:
            raise ValueError("请配置只匹配商家入站消息的chat_messages")
        result = []
        for item in root.locator(self.s["chat_messages"]).all():
            message_id = item.get_attribute(self.s["chat_message_id_attribute"])
            stamp = item.get_attribute(self.s["chat_message_time_attribute"])
            if not message_id or not stamp:
                raise ValueError("商家消息必须有稳定ID和时间戳，不能把历史消息当新回复")
            at = float(stamp)
            if at > 100000000000:
                at /= 1000
            result.append({"id": message_id, "at": at, "text": item.inner_text()})
        return result

    def prepare_chat(self, task):
        original_owned = list(self.owned)
        try:
            return self._prepare_chat(task)
        except Exception:
            for page in list(self.owned):
                if page not in original_owned:
                    self.release(page)
            raise

    def _prepare_chat(self, task):
        detail = self.page(task["supplier_url"], "1688.com")
        previous = list(self.context.pages)
        self.check(detail)
        self.unique(detail, "chat_open").click()
        self.delay()
        new_pages = [p for p in self.context.pages if p not in previous]
        for p in new_pages:
            self.owned.append(p)
        page = new_pages[-1] if new_pages else detail
        self.verify_chat(page, task["merchant_id"])
        url = safe_url(page.url)
        if urlsplit(url).hostname not in ("air.1688.com", "air.taobao.com", "web.wangwang.taobao.com"):
            host = urlsplit(url).hostname
            if not host or not any(host == d or host.endswith("." + d) for d in ("1688.com", "taobao.com", "alicdn.com")):
                raise ValueError("会话必须属于1688/千牛官方域名")
        if url == task["supplier_url"]:
            raise ValueError("会话没有可恢复地址，请在页面适配中使用可独立打开的网页千牛会话")
        if page is not detail:
            self.release(detail)
        return page, url, [m["id"] for m in self.messages(page, task["merchant_id"])]

    def send(self, page, task, text):
        root = self.verify_chat(page, task["merchant_id"])
        field = self.unique(root, "chat_input")
        field.fill("")
        field.press_sequentially(text, delay=random.randint(45, 90))
        self.delay(1, 3)
        self.check(page)
        self.unique(root, "chat_send").click()
        self.delay(1, 2)
        self.check(page)

    def replies(self, task):
        page = self.page(task["conversation_url"])
        try:
            self.delay()
            messages = self.messages(page, task["merchant_id"])
            baseline = set(task.get("reply_baseline", []))
            return [m for m in messages if m["id"] not in baseline and task["sent_at"] <= m["at"] <= task["deadline"]]
        finally:
            self.release(page)

    def write(self, task, before_save):
        host = urlsplit(self.config["erp_list_url"]).hostname
        page = self.page(task["erp_edit_url"], host)
        try:
            if self.value(page, "erp_edit_id", required=True) != task["erp_goods_id"]:
                raise ValueError("ERP编辑页商品ID不一致")
            if self.value(page, "erp_edit_sku", required=True) != task["erp_sku"]:
                raise ValueError("ERP编辑页SKU不一致")
            cost = self.unique(page, "erp_cost_input")
            weight = self.unique(page, "erp_weight_input")
            old = {"cost_price": cost.input_value(), "weight_g": weight.input_value()}
            saved = page.locator(self.s["erp_saved"])
            if saved.count() and saved.first.is_visible():
                raise ValueError("保存成功标识在保存前已可见，无法判断本次保存结果")
            before_save(old)
            cost.fill(str(number(task["cost_price"])))
            weight.fill(str(number(task["weight_g"])))
            self.check(page)
            self.unique(page, "erp_save").click()
            saved.first.wait_for(state="visible")
            self.check(page)
            page.reload(wait_until="domcontentloaded")
            safe_url(page.url, host=host)
            if self.value(page, "erp_edit_id", required=True) != task["erp_goods_id"] or self.value(page, "erp_edit_sku", required=True) != task["erp_sku"]:
                raise ValueError("保存后商品或SKU变化")
            if number(self.unique(page, "erp_cost_input").input_value()) != number(task["cost_price"]) or number(self.unique(page, "erp_weight_input").input_value()) != number(task["weight_g"]):
                raise ValueError("刷新后回读的成本或重量不一致")
        finally:
            self.release(page)
