"""Visible CDP browser adapter. Site-specific selectors are explicit and inspectable."""
import hashlib
import io
import json
import random
import re
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, build_opener

from .config import safe_url, selection_key, selection_params
import requests
from PIL import Image

from .models import number
from .pricing import sku_cost
from .supplier_adapter import AUTO_FIELDS, DOM_SNAPSHOT, SupplierAdaptationError, verified_selectors

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

# These selectors are shared with the existing Zying detail collector. Product
# cards do not always render an ID; the detail header shows the ERP product ID.
ERP_DETAIL_ID_READ = """(page, expected) => {
  const visible=e=>e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
  const roots=Array.from(page.querySelectorAll('.curd-detail-wrap')).filter(visible);
  if(roots.length!==1)return {id:'',ambiguous:roots.length>1};
  const root=roots[0],headers=Array.from(root.querySelectorAll('.crud-detail-header .h1')).filter(visible);
  if(headers.length!==1)return {id:'',ambiguous:headers.length>1};
  const normalize=value=>String(value||'').replace(/\\s+/g,' ').trim();
  const titles=Array.from(root.querySelectorAll("textarea[placeholder='请输入内容']")).map(e=>normalize(e.value));
  const images=Array.from(root.querySelectorAll('img.ant-image-img')).map(e=>e.currentSrc||e.src);
  return {id:normalize(headers[0].textContent),
    title_match:!!expected.title&&titles.includes(normalize(expected.title)),
    image_match:!!expected.main_image_url&&images.includes(expected.main_image_url)};
}"""


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


def category_control_score(diagnostic, options):
    """Prefer the product-category cascader over shop/site cascaders."""
    context = " ".join(str(diagnostic.get(k, "")) for k in ("label", "placeholder", "text"))
    score = len(category_paths(options)) * 10
    if re.search(r"商品分类|产品分类|商品类目|产品类目|分类|类目", context):
        score += 100000
    if re.search(r"店铺|站点|国家|平台|公司|武汉泽顺|巴西", context):
        score -= 100000
    return score


class CircuitOpen(RuntimeError):
    pass


class Stopped(RuntimeError):
    pass


class NoExactMatch(ValueError):
    """A completed search cannot establish an exact match; continue the batch."""


class SearchTimeout(NoExactMatch):
    """No search response was available; not evidence for blocking an ERP item."""


class WritebackMismatch(ValueError):
    def __init__(self, actual):
        super().__init__("刷新后回读的美元净收益、重量或状态不一致")
        self.actual = actual


class Browser:
    def __init__(self, config, stop, log):
        self.config, self.stop, self.log = config, stop, log
        self.s = dict(config["selectors"])
        self.adapt_supplier = None
        self.record_supplier_adaptation = None
        self.record_visual = None
        self.pw = self.browser = None
        self.owned = []
        self.search_results = {}

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
            try:
                if frame is not page.main_frame and frame.is_detached():
                    continue
                self.check_frame(frame)
            except (CircuitOpen, Stopped):
                raise
            except Exception:
                # 1688 replaces ad/login frames while the main page loads.
                # Only a removed child frame can be ignored; keep main-frame
                # failures and every live captcha/risk signal blocking.
                if frame is not page.main_frame and frame.is_detached():
                    continue
                raise

    def check_frame(self, frame):
        host = (urlsplit(frame.url).hostname or "").lower()
        if frame is frame.page.main_frame and host in ("login.taobao.com", "login.1688.com", "passport.1688.com"):
            raise CircuitOpen("1688需要登录；请在可见Edge中完成登录后返回控制台继续执行")
        risk = self.s["risk"]
        if risk and frame.locator(risk).count() and frame.locator(risk).first.is_visible():
            raise CircuitOpen("1688出现验证码或人机审核；请在可见Edge中处理后返回控制台继续执行")
        text = frame.locator("body").inner_text(timeout=3000) if frame.locator("body").count() else ""
        if re.search(r"操作[太过]?于?频繁|发言受限|发送[太过]?于?频繁|滑动验证|人机(?:审核|验证)|请(?:按住|拖动).*滑块|请完成.*验证|安全验证|访问受限", text):
            raise CircuitOpen("1688出现登录、验证码或人机审核提示；请在可见Edge中处理后返回控制台继续执行")

    def delay(self, low=1, high=3):
        seconds = random.uniform(low, high)
        self.log(f"操作间隔 {seconds:.2f} 秒")
        if self.stop.wait(seconds):
            raise Stopped("操作已停止")

    @staticmethod
    def focus(page):
        """Select a real Playwright page; tolerate lightweight offline fakes."""
        bring_to_front = getattr(page, "bring_to_front", None)
        if bring_to_front:
            bring_to_front()

    def page(self, url, host=None):
        safe_url(url, host=host)
        page = self.context.new_page()
        self.owned.append(page)
        # Every business step must be observable in the dedicated Edge window.
        # A CDP-created tab is not guaranteed to become the selected tab when
        # Edge is already in use, so focus it both before and after navigation.
        page.bring_to_front()
        page.goto(url, wait_until="domcontentloaded")
        page.bring_to_front()
        try:
            safe_url(page.url, host=host)
        except ValueError:
            actual = (urlsplit(page.url).hostname or "未知域名").lower()
            if host == "1688.com":
                page.bring_to_front()
                self.check(page)
                if actual in ("login.taobao.com", "login.1688.com", "passport.1688.com"):
                    raise CircuitOpen("1688需要登录；请在可见Edge中完成登录后返回控制台继续执行") from None
            raise
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
        pages = [p for p in self.context.pages if urlsplit(p.url).hostname == "meli.zying.net"]
        if pages:
            page = pages[-1]
        else:
            page = self.context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
        # This is a user login tab, intentionally not owned/closed by the worker.
        page.bring_to_front()

    def open_supplier_login(self):
        pages = [page for page in self.context.pages if urlsplit(page.url).hostname in ("www.1688.com", "login.1688.com")]
        if pages:
            page = pages[-1]
        else:
            page = self.context.new_page()
            page.goto(self.config["supplier_home_url"], wait_until="domcontentloaded")
        # Retain this user login tab when the temporary driver disconnects.
        page.bring_to_front()

    def confirm_login(self):
        for page in reversed(self.context.pages):
            host = urlsplit(page.url).hostname or ""
            if host != "meli.zying.net":
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
            control = self.category_control(page)
            options = category_paths(self.read_categories(page, control)["options"])
            self.log(f"已从智赢商品页刷新分类：{len(options)} 个，最深 {max(o['depth'] for o in options)} 级")
            return options
        finally:
            self.release(page)

    def category_control(self, page, timeout=20):
        selector = self.s["erp_category_control"]
        if not selector:
            raise ValueError("请配置DOM字段：erp_category_control")
        controls = page.locator(selector)
        controls.first.wait_for(state="visible")
        candidates = [control for control in controls.all() if control.is_visible()]
        if not candidates:
            raise ValueError("智赢商品页没有可见的分类控件")
        if len(candidates) == 1:
            return candidates[0]
        diagnostics = []
        for control in candidates:
            diagnostics.append(control.evaluate("""e => {
              const item=e.closest('.ant-form-item')||e.parentElement;
              return {label:item?.querySelector('.ant-form-item-label,label')?.innerText?.trim()||'',
                placeholder:e.querySelector('input')?.placeholder||'',text:e.innerText?.trim().slice(0,120)||''};
            }"""))
        deadline = time.monotonic() + timeout
        snapshots = [[] for _ in candidates]
        while time.monotonic() < deadline:
            for index, control in enumerate(candidates):
                snapshots[index] = control.evaluate(CATEGORY_READ).get("options", [])
            scores = [category_control_score(diagnostic, options)
                      for diagnostic, options in zip(diagnostics, snapshots)]
            ranked = sorted(range(len(scores)), key=lambda index: scores[index], reverse=True)
            if scores[ranked[0]] > scores[ranked[1]] and (snapshots[ranked[0]] or scores[ranked[0]] >= 100000):
                chosen = ranked[0]
                self.log(f"智赢页面有 {len(candidates)} 个级联框，已识别商品分类控件：{diagnostics[chosen]}")
                return candidates[chosen]
            if self.stop.wait(.5):
                raise Stopped("操作已停止")
        safe_diagnostics = [{**d, "option_count": len(category_paths(o))}
                            for d, o in zip(diagnostics, snapshots)]
        self.log("分类控件识别失败：" + json.dumps(safe_diagnostics, ensure_ascii=False), level="ERROR")
        raise ValueError("页面存在多个级联框，无法区分商品分类与店铺/站点，请在参数设置中缩小分类控件选择器")

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
        control = self.category_control(page)
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
        search = self.category_search(page, control)
        self.check(page)
        search.click()
        self.delay(2, 3)
        return target["label"] if target else "全部分类"

    def category_search(self, page, control, timeout=12):
        # Ant buttons can insert a space between two Chinese characters, and
        # an icon's accessible name can be included in the button's role name.
        # Match the rendered button text, then resolve duplicates near the filter.
        selector = self.s.get("erp_search", "")
        pattern = re.compile(r"^\s*搜\s*索\s*$")

        def visible_buttons(root):
            buttons = root.locator(selector) if selector else root.locator("button").filter(has_text=pattern)
            return [button for button in buttons.all() if button.is_visible()]

        deadline = time.monotonic() + timeout
        while True:
            self.check(page)
            matches = visible_buttons(page)
            if len(matches) > 1 and not selector:
                for ancestor in reversed(control.locator("xpath=ancestor::*").all()):
                    nearby = visible_buttons(ancestor)
                    if nearby:
                        matches = nearby
                        break
            if len(matches) == 1 and matches[0].is_enabled():
                return matches[0]
            if len(matches) > 1 or time.monotonic() >= deadline:
                break
            if self.stop.wait(.25):
                raise Stopped("操作已停止")
        labels = [button.inner_text().strip()[:60] for button in page.locator("button").all() if button.is_visible()]
        self.log("分类搜索按钮识别失败：" + json.dumps({"visible_matches": len(matches), "buttons": labels[:30]}, ensure_ascii=False), level="ERROR")
        raise ValueError(f"智赢分类搜索按钮无法确定（可见匹配 {len(matches)} 个），请在参数设置中配置 erp_search")

    def current_page(self, page):
        selector = self.s["erp_page_active"]
        if not selector:
            raise ValueError("请配置DOM字段：erp_page_active")
        visible = [item for item in page.locator(selector).all() if item.is_visible()]
        if len(visible) == 1:
            text = visible[0].inner_text().strip()
            if text.isdigit():
                return int(text)
            raise ValueError("无法确认当前页码，已停止采集")
        if len(visible) > 1:
            raise ValueError(f"DOM字段 erp_page_active 必须唯一，实际 {len(visible)} 个")

        # Ant Design omits pagination entirely for a one-page result set in
        # some Zying builds. Accept page 1 only when no enabled previous/next
        # navigation and no multiple numbered pages can contradict it.
        numbered = [item for item in page.locator("li.ant-pagination-item").all() if item.is_visible()]
        previous = [item for item in page.locator(
            "li.ant-pagination-prev:not(.ant-pagination-disabled) button").all() if item.is_visible()]
        following = [item for item in page.locator(self.s["erp_next"]).all() if item.is_visible()]
        if len(numbered) <= 1 and not previous and not following:
            self.log("智赢当前结果仅一页，页面未渲染活动页码；按第 1 页继续逐件核对")
            return 1
        raise ValueError("无法唯一确认智赢当前页码，已停止采集")

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

    @staticmethod
    def normalize_erp_id(value):
        return re.sub(r"^(?:产品编号|商品编号|产品ID|商品ID|ID)\s*[:：]?\s*", "", value, flags=re.I).strip()

    def erp_goods_id(self, page, row, record, timeout=20):
        selector = self.s["erp_id"]
        if selector:
            matches = row.locator(selector).count()
            if matches or selector != ".product-id":
                key = self.normalize_erp_id(self.value(row, "erp_id", required=True))
                if not key:
                    raise ValueError("ERP商品ID字段为空，已停止采集")
                return key
        # Read only the detail opened by this card. Never derive an ID from its
        # title, array index, supplier URL, or another product's stale detail.
        body = page.locator("body")
        before = body.evaluate(ERP_DETAIL_ID_READ, record)
        if before.get("ambiguous"):
            raise ValueError("智赢存在多个商品详情，无法确认产品编号，已停止采集")
        previous_id = self.normalize_erp_id(before.get("id", ""))
        self.log("商品卡片未展示ID，正在打开智赢详情读取产品编号：" + record["title"])
        self.check(page)
        self.unique(row, "erp_title").click()
        deadline = time.monotonic() + timeout
        while True:
            self.check(page)
            current = body.evaluate(ERP_DETAIL_ID_READ, record)
            if current.get("ambiguous"):
                raise ValueError("智赢详情产品编号不唯一，已停止采集")
            raw_id = current.get("id", "")
            key = self.normalize_erp_id(raw_id)
            if key and key != previous_id and (current.get("title_match") or current.get("image_match")):
                if not re.fullmatch(r"[1-9]\d*", key):
                    raise ValueError("智赢详情未返回有效的ERP产品编号，已停止采集")
                self.log("已从智赢详情读取产品编号：" + key)
                return key
            if time.monotonic() >= deadline:
                raise ValueError("商品卡片没有ID，打开详情后仍未读到与目标商品匹配的新产品编号；请检查智赢详情是否正常加载")
            if self.stop.wait(.25):
                raise Stopped("操作已停止")

    def collect(self, store, on_task=None):
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
            store.log(f"正在应用分类筛选，准备采集第 {selection['start_page']}–{selection['end_page']} 页")
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
                    # Supplier matching and writeback open temporary tabs. Bring
                    # the retained Zying list back before reading the next card,
                    # keeping the one-product-at-a-time sequence visible.
                    self.focus(page)
                    raw = row.inner_text()
                    image = self.value(row, "erp_image", "src", required=True)
                    record = {"title": self.value(row, "erp_title", required=True),
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
                    # Snapshot all card fields before a detail click can rerender
                    # the list, so values cannot come from the next card.
                    key = self.erp_goods_id(page, row, record)
                    record["erp_goods_id"] = key
                    count += store.add(record)
                    store.include_in_scope(scope, key, page_number)
                    store.log(f"已采集商品 {key}：{record['title']}", key)
                    if on_task:
                        # Do not read the next card or advance the page checkpoint
                        # until this item's save has been verified.
                        self.visual(store.get(key), "erp_item",
                                    f"正在逐件核对智赢商品 {key}：{record['title']}", page)
                        on_task(key)
                        self.check(page)
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

    def image_payload(self, task):
        url = safe_url(task.get("main_image_url", ""))
        # Upload the exact main image of this task, never a hardcoded sample or
        # title search. Keep the image in memory and bound downloads.
        maximum = 10 * 1024 * 1024
        with requests.get(url, stream=True, timeout=(10, 30)) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                if self.stop.is_set():
                    raise Stopped()
                size += len(chunk)
                if size > maximum:
                    raise ValueError("商品主图超过10MB，无法上传以图搜货")
                chunks.append(chunk)
        data = b"".join(chunks)
        with Image.open(io.BytesIO(data)) as picture:
            kind = picture.format
            picture.verify()
        formats = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
                   "WEBP": ("webp", "image/webp"), "GIF": ("gif", "image/gif")}
        if kind not in formats:
            raise ValueError("商品主图不是支持的图片格式")
        extension, mime = formats[kind]
        return {"name": "product-main." + extension, "mimeType": mime, "buffer": data}

    def image_search(self, page, task, timeout=30):
        self.log("使用当前商品主图在1688以图搜货", task["erp_goods_id"])
        self.visual(task, "supplier_home", "已打开1688首页，准备上传当前商品主图", page)
        if self.s["image_search_open"]:
            self.unique(page, "image_search_open").click()
        upload = page.locator(self.s["image_search_upload"])
        # File inputs commonly remain hidden; visible-only unique() cannot be
        # used here. Ambiguous upload controls must still stop the operation.
        upload.first.wait_for(state="attached")
        if upload.count() != 1:
            count = upload.count()
            # Responsive/sticky search bars can duplicate hidden file inputs.
            # File inputs themselves are normally hidden; judge their parent
            # search region, never pick an arbitrary first upload target.
            active = [item for item in upload.all() if item.evaluate("""e => {
              let box=null;
              for(let p=e.parentElement;p;p=p.parentElement){
                const style=getComputedStyle(p);
                if(style.display==='none'||style.visibility==='hidden')return false;
                const r=p.getBoundingClientRect();
                if(!box&&r.width>0&&r.height>0)box=r;
              }
              return !!box&&box.bottom>0&&box.right>0&&box.top<innerHeight&&box.left<innerWidth;
            }""")]
            if len(active) != 1:
                raise ValueError(f"DOM字段 image_search_upload 必须唯一，实际 {count} 个，当前可见区域 {len(active)} 个")
            upload = active[0]
            self.log(f"页面存在 {count} 个上传控件，已定位当前可见区域的唯一上传控件", task["erp_goods_id"])
        self.check(page)
        previous = list(self.context.pages)
        previous_url = page.url
        previous_links = page.locator(self.s["result_links"]).evaluate_all("els => els.map(e => e.href)")
        empty_pattern = r"(?:没有找到|未找到|暂无)[^\n]{0,20}(?:相关商品|匹配商品|搜索结果)|没有符合条件的商品"
        previous_empty = re.findall(empty_pattern, page.locator("body").inner_text())
        payload = self.image_payload(task)
        self.visual(task, "uploading", "已读取当前商品主图，正在上传到1688", page)
        upload.set_input_files(payload)
        self.visual(task, "uploaded", "当前商品主图已上传，等待1688以图搜货结果", page)
        if self.s["image_search_submit"]:
            self.unique(page, "image_search_submit").click()
            self.visual(task, "search_submitted", "已点击搜索图片，开始1688以图搜货", page)
        submitted = bool(self.s["image_search_submit"])
        deadline = time.monotonic() + timeout
        result = page
        while time.monotonic() < deadline:
            if not submitted:
                buttons = [b for b in page.get_by_text("搜索图片", exact=True).all() if b.is_visible()]
                if len(buttons) > 1:
                    raise ValueError("1688搜索图片按钮不唯一，无法确认当前主图的提交入口")
                if buttons and buttons[0].is_enabled():
                    self.check(page)
                    buttons[0].click()
                    submitted = True
                    deadline = time.monotonic() + timeout
                    self.visual(task, "search_submitted", "已点击搜索图片，开始1688以图搜货", page)
            for opened in self.context.pages:
                if opened not in previous and opened not in self.owned:
                    self.owned.append(opened)
            opened = [p for p in self.context.pages if p not in previous]
            if len(opened) > 1:
                raise ValueError("以图搜货打开多个页面，无法唯一确认结果页")
            result = opened[0] if opened else page
            self.check(result)
            if result.url != "about:blank":
                try:
                    safe_url(result.url, "1688.com")
                except ValueError:
                    host = urlsplit(result.url).hostname or "未知域名"
                    result.bring_to_front()
                    raise CircuitOpen("1688搜图进入登录或人机审核页面（" + host + "）；请处理后返回控制台继续执行") from None
                links = result.locator(self.s["result_links"]).evaluate_all("els => els.map(e => e.href)")
                if links and (result is not page or result.url != previous_url or links != previous_links):
                    self.visual(task, "search_results", f"1688已返回以图搜货结果，页面含 {len(links)} 个候选链接", result)
                    return result
                if "/1688-search/pc-image-search/" in urlsplit(result.url).path:
                    cards = self.result_image_cards(result)
                    if cards:
                        self.visual(task, "search_results", f"1688已返回以图搜货结果，读取到 {len(cards)} 个商品卡片", result)
                        return result
                empty = re.findall(empty_pattern, result.locator("body").inner_text())
                if not links and empty and (result is not page or result.url != previous_url or empty != previous_empty):
                    self.visual(task, "search_empty", "1688显示没有找到相关商品", result)
                    raise NoExactMatch("1688以图搜货返回空结果：" + empty[0])
            # Polling is not a new business action; keep logs focused on the
            # product's progress instead of writing several delays per second.
            if self.stop.wait(.4):
                raise Stopped("操作已停止")
        reason = f"搜索超时：主图上传后{timeout:g}秒内1688未返回可读取的新结果，无法确认完全匹配"
        self.visual(task, "search_timeout", reason, result)
        raise SearchTimeout(reason)

    def visual(self, task, step, message, page=None):
        if page is not None:
            self.check(page)
            page.bring_to_front()
        if self.record_visual:
            self.record_visual(task["erp_goods_id"], step, message, page_url=page.url if page else "")
        else:
            self.log(message, task["erp_goods_id"])

    def search_images(self, task):
        self.visual(task, "main_image", "已取得当前商品主图，准备在1688以图搜货")
        before_owned = list(self.owned)
        try:
            page = self.page(self.config["supplier_home_url"], "1688.com")
            self.delay(2, 4)
            result = self.image_search(page, task)
            self.search_results[task["erp_goods_id"]] = result
            cards = result.locator(self.s["result_links"]).evaluate_all(r"""links => {
              const seen=new Set(), cards=[];
              for(const link of links){
                const url=new URL(link.href);
                if(url.hostname!=='detail.1688.com'||!/\/offer\/\d+\.html/.test(url.pathname))continue;
                const canonical=url.origin+url.pathname;
                if(seen.has(canonical))continue;
                let root=link, img=null;
                for(let depth=0;root&&depth<4;depth++,root=root.parentElement){
                  const offerUrls=new Set([...root.querySelectorAll('a[href*="detail.1688.com/offer/"]')].map(a=>a.href.split('?')[0]));
                  if(offerUrls.size>1)break;
                  img=[...root.querySelectorAll('img')].find(i=>i.getBoundingClientRect().width>30&&i.getBoundingClientRect().height>30);
                  if(img)break;
                }
                if(!img)continue;
                const src=img.currentSrc||img.src;
                if(!/^https?:/.test(src))continue;
                seen.add(canonical);cards.push({url:canonical,main_image_url:src,title:(link.innerText||img.alt||root.innerText||'').trim().slice(0,600)});
              }
              return cards;
            }""")
            if not cards:
                cards = self.result_image_cards(result)
            selected = cards[:self.config["max_candidates"]]
            if not selected:
                raise ValueError("1688已有搜索结果，但无法读取商品卡片主图；保留智赢原数据")
            self.visual(task, "compare_images", f"读取前{len(selected)}张候选图，正在比对主图（上限{self.config['max_candidates']}张）", result)
            if page is not result:
                self.release(page)
            return selected
        except Exception:
            for opened in list(self.owned):
                if opened not in before_owned:
                    self.release(opened)
            self.search_results.pop(task["erp_goods_id"], None)
            raise

    def result_image_cards(self, page):
        """Read rendered result cards that navigate via click instead of href."""
        return page.locator("body").evaluate(r"""body => {
          const visible=e=>e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
          const large=e=>visible(e)&&e.getBoundingClientRect().width>=80&&e.getBoundingClientRect().height>=80;
          const marker=[...body.querySelectorAll('*')].find(e=>visible(e)&&e.children.length===0&&e.textContent.trim()==='找到以下货源');
          if(!marker)return [];
          const top=marker.getBoundingClientRect().bottom;
          const path=e=>{const parts=[];while(e&&e.nodeType===1){let n=1;for(let p=e.previousElementSibling;p;p=p.previousElementSibling)if(p.tagName===e.tagName)n++;parts.unshift(e.tagName.toLowerCase()+':nth-of-type('+n+')');e=e.parentElement;}return parts.join(' > ');};
          const seen=new Set(),cards=[];
          for(const img of body.querySelectorAll('img')){
            if(!large(img)||img.getBoundingClientRect().top<top)continue;
            const src=img.currentSrc||img.src;if(!/^https?:/.test(src)||seen.has(src))continue;
            let card=null;
            for(let p=img.parentElement;p&&p!==body;p=p.parentElement){
              if([...p.querySelectorAll('img')].filter(large).length!==1)break;
              if(/[¥￥]\s*\d/.test(p.innerText)){card=p;break;}
            }
            if(!card)continue;
            const box=img.getBoundingClientRect();seen.add(src);
            cards.push({url:location.href,search_url:location.href,result_image_path:path(img),main_image_url:src,title:card.innerText.trim().slice(0,800),top:box.top,left:box.left});
          }
          return cards.sort((a,b)=>Math.abs(a.top-b.top)>30?a.top-b.top:a.left-b.left).map(({top,left,...card})=>card);
        }""")

    def release_search(self, task):
        page = self.search_results.pop(task["erp_goods_id"], None)
        if page is not None:
            self.release(page)

    def open_offer(self, task, candidate):
        if not candidate.get("result_image_path"):
            return self.page(candidate["url"], "1688.com")
        result = self.search_results.get(task["erp_goods_id"])
        if result is None or result.is_closed():
            result = self.page(candidate["search_url"], "1688.com")
            self.search_results[task["erp_goods_id"]] = result
        image = result.locator(candidate["result_image_path"])
        if image.count() != 1 or image.evaluate("e=>e.currentSrc||e.src") != candidate["main_image_url"]:
            raise ValueError("1688结果卡片已变化，无法确认待打开的候选图片")
        before, original_url = list(self.context.pages), result.url
        self.check(result)
        image.click()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            opened = [p for p in self.context.pages if p not in before]
            for page in opened:
                if page not in self.owned:
                    self.owned.append(page)
            if len(opened) > 1:
                raise ValueError("候选卡片打开多个页面，无法确认商品详情")
            detail = opened[0] if opened else result
            self.check(detail)
            parsed = urlsplit(detail.url)
            if parsed.hostname == "detail.1688.com" and re.fullmatch(r"/offer/\d+\.html", parsed.path):
                if detail is result:
                    self.search_results.pop(task["erp_goods_id"], None)
                return detail
            if detail.url not in ("about:blank", original_url) and parsed.hostname and not parsed.hostname.endswith("1688.com"):
                detail.bring_to_front()
                raise CircuitOpen("1688详情进入登录或人机审核页面（" + parsed.hostname + "）；请处理后返回控制台继续执行")
            if self.stop.wait(.3):
                raise Stopped("操作已停止")
        raise ValueError("点击匹配候选后未进入可核验的1688商品详情")

    def read_offer(self, task, candidate):
        detail = self.open_offer(task, candidate)
        try:
            self.delay()
            self.visual(task, "candidate", "图片匹配后，正在读取1688商品详情和变体售价", detail)
            self.ensure_supplier_detail(detail, task["erp_goods_id"])
            rows = detail.locator(self.s["sku_rows"])
            rows.first.wait_for(state="visible")
            skus = []
            for row in rows.all():
                sku_id = (row.get_attribute(self.s["sku_id_attribute"]) or "").strip()
                if not sku_id:
                    raise ValueError("目标SKU缺少稳定标识")
                raw_price = self.value(row, "sku_price")
                surcharge = self.value(row, "sku_surcharge") if self.config["sku_price_mode"] == "base_plus_surcharge" else None
                try:
                    price = sku_cost(raw_price, surcharge)
                    if self.config["sku_price_mode"] == "base_plus_surcharge" and not surcharge:
                        raise ValueError("缺少目标变体加价")
                except ValueError as exc:
                    price = {"price": None, "price_error": str(exc)}
                skus.append({"id": sku_id, "label": self.value(row, "sku_label", required=True),
                             "raw_text": row.inner_text(), "raw_price": raw_price,
                             "raw_surcharge": surcharge, "raw_weight": self.value(row, "sku_weight"), **price})
            return {"url": detail.url, "title": self.value(detail, "supplier_title", required=True),
                    "main_image_url": urljoin(detail.url, self.value(detail, "supplier_image", "src", True)),
                    "description": self.value(detail, "supplier_description") or detail.locator("body").inner_text()[:20000],
                    "raw_weight": self.value(detail, "supplier_weight"), "skus": skus,
                    "merchant_id": self.value(detail, "supplier_merchant", self.s["supplier_merchant_attribute"])}
        finally:
            self.release(detail)

    def locate_erp_detail(self, page, task):
        """Reopen the exact collected product using its category/page/card."""
        from .config import DEFAULTS
        for key in ("erp_edit_id", "erp_net_income_input", "erp_weight_input"):
            self.s[key] = self.s.get(key) or DEFAULTS["selectors"][key]
        current = self.value(page, "erp_edit_id")
        if self.normalize_erp_id(current) == task["erp_goods_id"]:
            return
        self.apply_category(page, task.get("source_category", ""))
        self.first_page(page)
        target_page = int(task.get("source_page") or 1)
        for current_page in range(1, target_page):
            if not self.next_page(page, current_page):
                raise ValueError("无法返回目标商品所在的智赢页码")
        rows = page.locator(self.s["erp_rows"])
        rows.first.wait_for(state="visible")
        matches = [row for row in rows.all() if self.value(row, "erp_title") == task["title"]]
        if len(matches) > 1:
            matches = [row for row in matches if urljoin(page.url, self.value(row, "erp_image", "src")) == task.get("main_image_url")]
        if len(matches) != 1:
            raise ValueError(f"无法唯一定位待回填商品卡片，匹配到{len(matches)}个")
        row = matches[0]
        key = self.erp_goods_id(page, row, task)
        if key != task["erp_goods_id"]:
            raise ValueError("待回填商品ID与采集记录不一致")
        if self.normalize_erp_id(self.value(page, "erp_edit_id")) != key:
            self.unique(row, "erp_title").click()
            page.locator(self.s["erp_edit_id"]).wait_for(state="visible")
        if self.normalize_erp_id(self.value(page, "erp_edit_id", required=True)) != key:
            raise ValueError("打开的智赢详情ID与目标商品不一致")

    def write_patch(self, task, changes, before_save):
        from .models import erp_value_equal
        if not changes or set(changes) - {"weight_g", "net_income_usd", "review_status"}:
            raise ValueError("回填字段无效")
        status_values = {"屏蔽": "8000", "风险": "9000"}
        if "review_status" in changes and changes["review_status"] not in status_values:
            raise ValueError("不支持的智赢目标状态")
        host = urlsplit(self.config["erp_list_url"]).hostname
        page = self.page(task.get("erp_edit_url") or self.config["erp_list_url"], host)
        def snapshot():
            if self.normalize_erp_id(self.value(page, "erp_edit_id", required=True)) != task["erp_goods_id"]:
                raise ValueError("智赢详情产品编号变化，已停止保存")
            root = page.locator(".curd-detail-wrap")
            checked = root.locator("input[name='stat']:checked")
            if checked.count() != 1:
                raise ValueError("无法确认智赢当前商品审核状态")
            label = checked.evaluate("e => (e.closest('label')?.innerText||'').trim()")
            if not label:
                raise ValueError("智赢当前商品状态标签为空")
            return {"weight_g": self.unique(page, "erp_weight_input").input_value(),
                    "net_income_usd": self.unique(page, "erp_net_income_input").input_value(),
                    "review_status": label}
        try:
            self.locate_erp_detail(page, task)
            self.visual(task, "erp_before", "已定位智赢商品，记录修改前重量、净收益和状态", page)
            old = snapshot()
            root = page.locator(".curd-detail-wrap")
            save = self.unique(page, "erp_save") if self.s["erp_save"] else root.get_by_role("button", name=re.compile(r"^\s*保\s*存\s*$"))
            if save.count() != 1 or not save.is_visible() or not save.is_enabled():
                raise ValueError("未找到唯一可用的智赢详情保存按钮")
            radio = None
            if "review_status" in changes:
                radio = root.locator(f"input[name='stat'][value='{status_values[changes['review_status']]}']")
                if radio.count() != 1 or changes["review_status"] not in radio.evaluate("e => e.closest('label')?.innerText||''"):
                    raise ValueError("智赢状态选项与目标标签不一致")
            before_save(old)
            for field, selector in (("weight_g", "erp_weight_input"), ("net_income_usd", "erp_net_income_input")):
                if field in changes:
                    value = number(changes[field])
                    if field == "net_income_usd" and value != value.to_integral_value():
                        raise ValueError("净收益必须是整数美元")
                    self.unique(page, selector).fill(str(value))
            if radio is not None:
                radio.check()
            self.visual(task, "erp_saving", "回填已完成，正在保存智赢商品；未指定的字段保留原值", page)
            self.check(page)
            save.click()
            if self.s["erp_saved"]:
                page.locator(self.s["erp_saved"]).first.wait_for(state="visible")
            else:
                page.get_by_text(re.compile(r"保存成功|操作成功")).first.wait_for(state="visible", timeout=15000)
            self.check(page)
            page.reload(wait_until="domcontentloaded")
            safe_url(page.url, host=host)
            self.locate_erp_detail(page, task)
            actual = snapshot()
            for field in old:
                expected = changes.get(field, old[field])
                if not erp_value_equal(field, actual[field], expected):
                    raise WritebackMismatch(actual)
            self.visual(task, "erp_saved", "保存后重新打开商品，重量、净收益及状态回读确认一致", page)
            return actual
        finally:
            self.release(page)

    def ensure_supplier_detail(self, page, task_id):
        # Generated nth-of-type paths are valid only for this observed page.
        # Never save them as global CSS or reuse them on the next supplier.
        self.s = dict(self.config["selectors"])
        missing = [field for field in AUTO_FIELDS if not self.s[field]]
        if not missing:
            return
        event = {"url": page.url, "at": time.time(), "missing": missing, "ok": False}
        try:
            if not self.config["supplier_auto_adapt"] or not self.adapt_supplier:
                raise SupplierAdaptationError("1688详情页缺少适配规则：" + "、".join(missing))
            if urlsplit(page.url).hostname != "detail.1688.com" or not re.fullmatch(r"/offer/\d+\.html", urlsplit(page.url).path):
                raise SupplierAdaptationError("1688未进入有效商品详情页，无法自动适配")
            self.check(page)
            self.log("开始1688详情页自动适配：读取标题、主图、商家和SKU；仅校验页面，不执行回填", task_id)
            snapshot = page.evaluate(DOM_SNAPSHOT)
            if snapshot["truncated"] or not snapshot["nodes"]:
                raise SupplierAdaptationError("1688页面为空或结构过大，无法完整校验自动适配")
            answer = self.adapt_supplier(snapshot)
            self.check(page)
            detected, evidence = verified_selectors(page, snapshot, answer)
            for field in missing:
                self.s[field] = detected[field]
            if "supplier_merchant" in missing:
                self.s["supplier_merchant_attribute"] = detected["supplier_merchant_attribute"]
            if "sku_rows" in missing:
                self.s["sku_id_attribute"] = detected["sku_id_attribute"]
            event.update(ok=True, evidence=evidence, selectors={key: self.s[key] for key in detected})
            self.log(f"1688详情页自动适配通过：已核验标题、主图、商家及 {len(evidence['skus'])} 个SKU", task_id)
        except (CircuitOpen, Stopped):
            event["reason"] = "页面验证或人工停止中断适配"
            raise
        except Exception as exc:
            event["reason"] = str(exc)
            self.log("1688详情页自动适配失败，当前商品暂停：" + str(exc), task_id, "ERROR")
            raise SupplierAdaptationError("1688详情页自动适配失败：" + str(exc)) from exc
        finally:
            if self.record_supplier_adaptation:
                self.record_supplier_adaptation(task_id, event)

    def candidates(self, task):
        self.visual(task, "main_image", "已取得当前商品主图，准备在1688以图搜货")
        before_owned = list(self.owned)
        page = self.page(self.config["supplier_home_url"], "1688.com")
        result_page = None
        try:
            self.delay(2, 6)
            result_page = self.image_search(page, task)
            self.check(result_page)
            if not task.get("erp_sku"):
                raise NoExactMatch("主图搜索已执行，但ERP未提供目标SKU规格，无法确认完全匹配")
            links = result_page.locator(self.s["result_links"])
            if not links.count():
                return
            urls = []
            for link in links.all():
                url = urljoin(result_page.url, link.get_attribute("href") or "")
                if url not in urls and urlsplit(url).hostname == "detail.1688.com":
                    urls.append(url)
                if len(urls) >= self.config["max_candidates"]:
                    break
            for index, url in enumerate(urls, 1):
                detail = self.page(url, "1688.com")
                try:
                    self.delay()
                    self.check(detail)
                    self.visual(task, "candidate", f"正在核验候选 {index}/{len(urls)} 的图片与完整规格", detail)
                    self.ensure_supplier_detail(detail, task["erp_goods_id"])
                    sku_rows = detail.locator(self.s["sku_rows"])
                    sku_rows.first.wait_for(state="visible")
                    skus = []
                    for sku in sku_rows.all():
                        sku_id = (sku.get_attribute(self.s["sku_id_attribute"]) or "").strip()
                        if not sku_id:
                            raise ValueError("SKU缺少稳定ID")
                        raw_price = self.value(sku, "sku_price")
                        raw_surcharge = self.value(sku, "sku_surcharge") if self.config["sku_price_mode"] == "base_plus_surcharge" else None
                        try:
                            price = sku_cost(raw_price, raw_surcharge)
                            if self.config["sku_price_mode"] == "base_plus_surcharge" and not raw_surcharge:
                                raise ValueError("页面缺少目标变体的加价信息")
                        except ValueError as exc:
                            # A missing price must not discard an otherwise
                            # matched SKU: ask its supplier for the final price.
                            price = {"price": None, "price_error": str(exc)}
                        skus.append({"id": sku_id, "label": self.value(sku, "sku_label", required=True),
                                     "raw_text": sku.inner_text(),
                                     "raw_price": raw_price, "raw_surcharge": raw_surcharge,
                                     "raw_weight": self.value(sku, "sku_weight"), **price})
                    yield {"url": detail.url, "title": self.value(detail, "supplier_title", required=True),
                           "main_image_url": urljoin(detail.url, self.value(detail, "supplier_image", "src", True)),
                           "description": self.value(detail, "supplier_description") or detail.locator("body").inner_text()[:20000], "skus": skus,
                           "raw_weight": self.value(detail, "supplier_weight"),
                           "merchant_id": self.value(detail, "supplier_merchant", self.s["supplier_merchant_attribute"], True)}
                except (CircuitOpen, Stopped, SupplierAdaptationError):
                    raise
                except Exception as exc:
                    self.log(f"候选详情无法读取：{type(exc).__name__}: {exc}", task["erp_goods_id"], "WARNING")
                finally:
                    self.release(detail)
        finally:
            for opened in list(self.owned):
                if opened not in before_owned:
                    self.release(opened)

    def supplier_page(self, task):
        page = self.page(task["supplier_url"], "1688.com")
        try:
            self.check(page)
            self.ensure_supplier_detail(page, task["erp_goods_id"])
            rows = page.locator(self.s["sku_rows"])
            rows.first.wait_for(state="visible")
            selected = [row for row in rows.all() if row.get_attribute(self.s["sku_id_attribute"]) == task["supplier_sku_id"]]
            if len(selected) != 1:
                raise ValueError("页面无法唯一确认已匹配的目标SKU")
            if self.value(page, "supplier_merchant", self.s["supplier_merchant_attribute"], True) != task["merchant_id"]:
                raise ValueError("供货页面商家与任务不一致")
            return ("目标SKU行：" + selected[0].inner_text() + "\n商品说明："
                    + (self.value(page, "supplier_description") or page.locator("body").inner_text()[:20000]))
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
            cost = self.unique(page, "erp_net_income_input")
            weight = self.unique(page, "erp_weight_input")
            net_income = number(task.get("net_income_usd"))
            if net_income != net_income.to_integral_value():
                raise ValueError("回填净收益必须是向上取整后的整数美元")
            old = {"net_income_usd": cost.input_value(), "weight_g": weight.input_value()}
            saved = page.locator(self.s["erp_saved"])
            if saved.count() and saved.first.is_visible():
                raise ValueError("保存成功标识在保存前已可见，无法判断本次保存结果")
            before_save(old)
            cost.fill(str(net_income))
            weight.fill(str(number(task["weight_g"])))
            self.check(page)
            self.unique(page, "erp_save").click()
            saved.first.wait_for(state="visible")
            self.check(page)
            page.reload(wait_until="domcontentloaded")
            safe_url(page.url, host=host)
            if self.value(page, "erp_edit_id", required=True) != task["erp_goods_id"] or self.value(page, "erp_edit_sku", required=True) != task["erp_sku"]:
                raise ValueError("保存后商品或SKU变化")
            actual = {"net_income_usd": self.unique(page, "erp_net_income_input").input_value(),
                      "weight_g": self.unique(page, "erp_weight_input").input_value()}
            try:
                if number(actual["net_income_usd"]) != net_income or number(actual["weight_g"]) != number(task["weight_g"]):
                    raise ValueError()
            except ValueError:
                raise WritebackMismatch(actual)
            return actual
        finally:
            self.release(page)
