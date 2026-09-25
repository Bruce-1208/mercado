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
  if(roots.length!==1)return {id:'',ambiguous:roots.length>1,root_count:roots.length};
  const root=roots[0],headers=Array.from(root.querySelectorAll('.crud-detail-header .h1')).filter(visible);
  if(headers.length!==1)return {id:'',ambiguous:headers.length>1,root_count:1,header_count:headers.length};
  const normalize=value=>String(value||'').replace(/\\s+/g,' ').trim();
  // Zying renders the title as a textarea for some products and as an input
  // for others. Image components also change class names between variants.
  const titles=Array.from(root.querySelectorAll('textarea,input[type="text"],input:not([type])'))
    .filter(visible).map(e=>normalize(e.value));
  const images=Array.from(root.querySelectorAll('img')).map(e=>e.currentSrc||e.src);
  return {id:normalize(headers[0].textContent),root_count:1,header_count:1,
    titles:titles.filter(Boolean).slice(0,4).map(value=>value.slice(0,160)),image_count:images.length,
    images:images.filter(Boolean).slice(0,4).map(value=>value.slice(0,300)),
    title_match:!!expected.title&&titles.includes(normalize(expected.title)),
    image_match:!!expected.main_image_url&&images.includes(expected.main_image_url)};
}"""

# The current 1688 detail page exposes the complete SKU list in the React data
# backing its official SKU module.  Read only the fields needed by this task and
# cross-check them against the independently rendered packaging data.  This is
# deliberately narrower than serialising the whole page (which also contains a
# large recommendation feed and used to trip the DOM snapshot size guard).
CURRENT_1688_DETAIL = r"""candidateImage => {
  const clean=value=>String(value||'').replace(/\s+/g,' ').trim();
  const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
  const titleNodes=[...document.querySelectorAll('#productTitle .title-content h1')].filter(visible);
  const merchantNodes=[...document.querySelectorAll('#shopNavigation a.shop-company-name[href]')].filter(visible);
  const selection=document.querySelector('#skuSelection[data-module="od_sku_selection"]');
  const recognized=!!(document.querySelector('#productTitle[data-module="od_title"]')||selection);
  if(!recognized)return {recognized:false};
  if(titleNodes.length!==1||merchantNodes.length!==1||!selection)
    return {recognized:true,ready:false,error:'1688新版详情的标题、商家或SKU模块尚未完整加载'};

  const arrays=(element,predicate)=>{
    const key=Object.keys(element).find(name=>name.startsWith('__reactFiber$'));
    let fiber=key?element[key]:null;const found=[],seen=new WeakSet();
    const scan=(value,depth)=>{
      if(!value||typeof value!=='object'||depth>9||seen.has(value))return;
      seen.add(value);
      if(Array.isArray(value)&&predicate(value)){found.push(value);return;}
      for(const [name,child] of Object.entries(value)){
        if(name==='_owner'||name==='stateNode'||name==='return'||name==='child'||name==='sibling')continue;
        if(child&&typeof child==='object'&&!child.$$typeof)scan(child,depth+1);
      }
    };
    for(let depth=0;fiber&&depth<18;depth++,fiber=fiber.return)scan(fiber.memoizedProps,0);
    return found;
  };
  const priceOf=item=>{
    for(const key of ['discountPrice','currentPrice','priceNum','price']){
      const value=item[key];
      if(value!==null&&value!==undefined&&/^\d+(?:\.\d{1,4})?$/.test(String(value))&&Number(value)>0)
        return String(value);
    }
    return '';
  };
  const decode=value=>{const box=document.createElement('textarea');box.innerHTML=String(value||'');return clean(box.value.replace(/>/g,' / '));};
  const weightClaims=text=>{
    const claims=[];
    const pattern=/(^|[^0-9.])([0-9]+(?:\.[0-9]+)?)\s*(kg|公斤|千克|g|克|斤)(?![A-Za-z0-9])/gi;
    for(const match of String(text||'').matchAll(pattern))claims.push(match[2]+match[3]);
    return claims.join('；');
  };
  const normalizeSkus=list=>list.map(item=>{
    const label=decode(item.specAttrs);
    return {id:String(item.skuId||''),label,price:priceOf(item),labelWeight:weightClaims(label)};
  })
    .filter(item=>/^\d+$/.test(item.id)&&item.label&&item.price).sort((a,b)=>a.id.localeCompare(b.id));
  const skuSets=arrays(selection,list=>list.length>0&&list.length<=500&&list.every(item=>item&&typeof item==='object'&&item.skuId&&item.specAttrs))
    .map(normalizeSkus).filter(list=>list.length);
  if(!skuSets.length)return {recognized:true,ready:false,error:'1688新版详情的完整SKU价格数据尚未加载'};
  const largest=Math.max(...skuSets.map(list=>list.length));
  const distinct=new Map(skuSets.filter(list=>list.length===largest).map(list=>[JSON.stringify(list),list]));
  if(distinct.size!==1)return {recognized:true,ready:false,error:'1688新版详情返回了不一致的SKU价格数据'};
  const skus=[...distinct.values()][0];

  const pack=document.querySelector('#productPackInfo[data-module="od_product_pack_info"]');
  const normalizeWeights=list=>list.map(item=>({id:String(item.skuId||''),weight:item.weight}))
    .filter(item=>/^\d+$/.test(item.id)&&Number.isFinite(Number(item.weight))&&Number(item.weight)>0)
    .sort((a,b)=>a.id.localeCompare(b.id));
  const weightSets=pack?arrays(pack,list=>list.length>0&&list.length<=500&&list.every(item=>item&&typeof item==='object'&&item.skuId&&'weight' in item))
    .map(normalizeWeights).filter(list=>list.length):[];
  // Some current 1688 offers expose one verified package weight for the whole
  // offer instead of repeating it with every SKU id, for example
  // packInfoData.skuInfo: [{weight: 300}].  That is still explicit page
  // evidence: apply it to every priced SKU only when the page exposes exactly
  // one unambiguous common weight value.  Never merge conflicting values.
  const commonWeightSets=pack?arrays(pack,list=>list.length===1&&list.every(item=>item&&typeof item==='object'&&!('skuId' in item)&&'weight' in item&&Number.isFinite(Number(item.weight))&&Number(item.weight)>0))
    .map(list=>String(list[0].weight)).filter(Boolean):[];
  let weights=new Map();
  if(weightSets.length){
    const most=Math.max(...weightSets.map(list=>list.length));
    const choices=new Map(weightSets.filter(list=>list.length===most).map(list=>[JSON.stringify(list),list]));
    if(choices.size!==1)return {recognized:true,ready:false,error:'1688新版详情返回了不一致的SKU包装重量数据'};
    weights=new Map([...choices.values()][0].map(item=>[item.id,String(item.weight)]));
  }else{
    const commonValues=[...new Set(commonWeightSets)];
    if(commonValues.length===1)
      weights=new Map(skus.map(sku=>[sku.id,commonValues[0]]));
  }

  let merchantId='';
  try{
    const host=new URL(merchantNodes[0].href).hostname.toLowerCase();
    if(/^[a-z0-9-]+\.1688\.com$/.test(host))merchantId=host.slice(0,-'.1688.com'.length);
  }catch(_){}
  if(!merchantId)return {recognized:true,ready:false,error:'1688新版详情缺少可核验的商家标识'};
  const pictures=[...document.querySelectorAll('#gallery img.preview-img')]
    .map(image=>image.currentSrc||image.src).filter(src=>/^https?:\/\//i.test(src));
  const asset=url=>String(url||'').match(/\/([^/]+?)_!!/)?.[1]||'';
  const wanted=asset(candidateImage);
  const mainImage=(wanted&&pictures.find(src=>asset(src)===wanted))||pictures[0]||'';
  if(!mainImage)return {recognized:true,ready:false,error:'1688新版详情主图尚未加载'};
  const skuIds=new Set(skus.map(sku=>sku.id));
  if([...weights.keys()].some(id=>!skuIds.has(id)))
    return {recognized:true,ready:false,error:'1688新版详情的价格与包装SKU标识不一致'};
  const rows=skus.map(sku=>{
    // Some offers render package weight only in the variant label, e.g.
    // "冲浪板84*56cm(0.45kg)", without a productPackInfo entry. Prefer the
    // structured packaging value when present, then retain the label claim as
    // explicit evidence for the local unit parser.
    const packaged=weights.get(sku.id)||'';
    const weight=packaged?packaged+'g':sku.labelWeight;
    const {labelWeight,...cleanSku}=sku;
    return {...cleanSku,raw_price:'¥'+sku.price,raw_surcharge:'',raw_weight:weight,
      raw_dimensions:sku.label+(skus.length===1&&pack?'；'+clean(pack.innerText).slice(0,2000):''),
      raw_text:sku.label+'；页面单价 ¥'+sku.price+(weight?'；包装重量 '+weight:'')};
  });
  // Keep the detail-page read deliberately narrow: pricing, variant labels and
  // package weights only. Title/shop/image are retained solely for ownership
  // and product-identity checks elsewhere in the pipeline.
  const description='';
  return {recognized:true,ready:true,offer:{title:clean(titleNodes[0].innerText),main_image_url:mainImage,
    description,raw_weight:'',merchant_id:merchantId,skus:rows}};
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


PENDING_REVIEW_STATUS = "待审核"
WRITEBACK_REVIEW_STATUSES = {"通过", "价格异常"}


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
        try:
            for attempt in range(1, 4):
                try:
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
                            if frame is not page.main_frame and frame.is_detached():
                                continue
                            raise
                    return
                except (CircuitOpen, Stopped):
                    raise
                except Exception as exc:
                    message = str(exc).lower()
                    navigation_transition = (
                        "execution context was destroyed" in message
                        or "most likely because of a navigation" in message
                        or "cannot find context with specified id" in message
                    )
                    if not navigation_transition or attempt == 3:
                        raise
                    if self.stop.wait(.15):
                        raise Stopped("操作已停止")
        except CircuitOpen:
            # A real login/captcha page is the user's recovery surface. Detach it
            # from automatic cleanup so it remains visible after this worker exits.
            self.retain(page)
            raise

    def check_frame(self, frame):
        host = (urlsplit(frame.url).hostname or "").lower()
        if frame is frame.page.main_frame and host in ("login.taobao.com", "login.1688.com", "passport.1688.com"):
            raise CircuitOpen("1688需要登录；请在可见Edge中完成登录后返回控制台继续执行")
        risk = self.s["risk"]
        if risk and frame.locator(risk).count() and frame.locator(risk).first.is_visible():
            raise CircuitOpen("1688出现验证码或人机审核；请在可见Edge中处理后返回控制台继续执行")
        # Check visible text nodes in-place and stop at the first match. Pulling
        # the complete body text across CDP made every upload poll serialize the
        # large 1688 home page and also increased false positives.
        text_risk = frame.locator("body").evaluate(r"""body => {
          const pattern=/操作[太过]?于?频繁|发言受限|发送[太过]?于?频繁|滑动验证|人机(?:审核|验证)|请(?:按住|拖动).*滑块|请完成.*验证|安全验证|访问受限/;
          const visible=element=>{
            if(!element||!element.getClientRects().length)return false;
            const style=getComputedStyle(element);
            return style.display!=='none'&&style.visibility!=='hidden'&&style.opacity!=='0';
          };
          const walker=document.createTreeWalker(body,NodeFilter.SHOW_TEXT);
          let inspected=0;
          for(let node=walker.nextNode();node&&inspected<4000;node=walker.nextNode(),inspected++){
            const value=String(node.nodeValue||'').replace(/\s+/g,' ').trim();
            if(value&&value.length<=240&&pattern.test(value)&&visible(node.parentElement))return true;
          }
          const labelled=body.querySelectorAll('[aria-label],[title],input[value]');
          for(let index=0;index<labelled.length&&index<1000;index++){
            const element=labelled[index];
            if(!visible(element))continue;
            const value=[element.getAttribute('aria-label'),element.title,element.value].filter(Boolean).join(' ');
            if(value.length<=240&&pattern.test(value))return true;
          }
          return false;
        }""") if frame.locator("body").count() else False
        if text_risk:
            raise CircuitOpen("1688出现登录、验证码或人机审核提示；请在可见Edge中处理后返回控制台继续执行")

    def delay(self, low=1, high=3):
        seconds = random.uniform(low, high)
        self.log(f"操作间隔 {seconds:.2f} 秒")
        if self.stop.wait(seconds):
            raise Stopped("操作已停止")

    @staticmethod
    def show_for_manual_action(page):
        """Expose only pages that require the operator's immediate attention."""
        bring_to_front = getattr(page, "bring_to_front", None)
        if bring_to_front:
            bring_to_front()

    def page(self, url, host=None):
        safe_url(url, host=host)
        page = self.context.new_page()
        self.owned.append(page)
        # Playwright can operate a non-selected tab. Keep automatic work in the
        # background so a long batch does not interrupt the operator's browser.
        page.goto(url, wait_until="domcontentloaded")
        try:
            safe_url(page.url, host=host)
        except ValueError:
            actual = (urlsplit(page.url).hostname or "未知域名").lower()
            if host == "1688.com":
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

    def retain(self, page):
        """Leave a login/review page open for the user to handle manually."""
        if page in self.owned:
            self.owned.remove(page)
        try:
            self.show_for_manual_action(page)
        except Exception:
            pass

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
        self.show_for_manual_action(page)

    def open_supplier_login(self):
        pages = [page for page in self.context.pages if urlsplit(page.url).hostname in ("www.1688.com", "login.1688.com")]
        if pages:
            page = pages[-1]
        else:
            page = self.context.new_page()
            page.goto(self.config["supplier_home_url"], wait_until="domcontentloaded")
        # Retain this user login tab when the temporary driver disconnects.
        self.show_for_manual_action(page)

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
            # Newer Zying builds keep the authenticated session in a
            # namespaced/local encrypted store instead of the old `token`
            # cookie.  A visible product shell at the protected product route
            # is the authoritative signal in that case; an unauthenticated
            # visit remains on #/login and is rejected above.
            authenticated_shell = ("#/product" in page.url.lower() and shell.count() > 0)
            # Some deployed builds render the shell with generated class names
            # that do not match the compatibility selectors above.  The
            # protected product route itself is still an authoritative signal:
            # unauthenticated sessions are redirected to #/login before this
            # loop reaches the check.
            product_route = "#/product" in page.url.lower()
            if not (has_token or has_cookie or authenticated_shell or product_route):
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

    def next_page(self, page, current, timeout=15):
        """Advance one page with one retry for a dropped SPA click."""
        target = current + 1
        observed = current
        for attempt in range(1, 3):
            button = page.locator(self.s["erp_next"])
            if not button.count() or not button.first.is_visible() or not button.first.is_enabled():
                if attempt == 1:
                    return False
                break
            self.check(page)
            button.first.click()
            deadline = time.monotonic() + timeout
            while True:
                self.check(page)
                try:
                    observed = self.current_page(page)
                except ValueError:
                    # Ant Design briefly replaces the active pagination node.
                    observed = 0
                if observed == target:
                    return True
                if observed not in (0, current):
                    raise CircuitOpen(
                        f"智赢翻页异常：期望第 {target} 页，实际第 {observed} 页；"
                        "已暂停任务并保留进度"
                    )
                if time.monotonic() >= deadline:
                    break
                if self.stop.wait(.25):
                    raise Stopped("操作已停止")
            if observed == current and attempt == 1:
                self.log(
                    f"智赢第 {current} 页的下一页点击未生效，正在重试",
                    level="WARNING",
                )
                continue
            break
        raise CircuitOpen(
            f"智赢翻页等待超时：期望进入第 {target} 页，"
            f"当前页码 {observed or '暂时无法读取'}；已暂停任务并保留进度"
        )

    @staticmethod
    def _rows_fingerprint(signature):
        return hashlib.sha256(
            json.dumps(signature, ensure_ascii=False).encode()
        ).hexdigest()

    def wait_for_product_rows(self, page, page_number, seen=(), timeout=20):
        """Wait for the active page's asynchronous product-list refresh."""
        excluded = set(seen or ())
        deadline = time.monotonic() + timeout
        last_fingerprint = ""
        last_count = 0
        last_error = ""
        attempts = 0
        while True:
            self.check(page)
            attempts += 1
            try:
                rows = page.locator(self.s["erp_rows"])
                rows.first.wait_for(state="visible", timeout=1000)
                signature = rows.all_inner_texts()
                last_count = len(signature)
                if signature:
                    last_fingerprint = self._rows_fingerprint(signature)
                    if last_fingerprint not in excluded:
                        if attempts > 1:
                            self.log(
                                f"智赢第 {page_number} 页商品列表延迟刷新，"
                                f"等待 {attempts} 次后恢复",
                                level="WARNING",
                            )
                        return rows, last_fingerprint
                last_error = ""
            except Exception as exc:
                # React can detach the current card nodes while replacing them.
                last_error = type(exc).__name__
            if time.monotonic() >= deadline:
                detail = (
                    "列表仍与已读页面相同"
                    if last_fingerprint in excluded
                    else f"可见商品 {last_count} 件"
                )
                if last_error:
                    detail += f"，最近读取错误 {last_error}"
                raise CircuitOpen(
                    f"智赢第 {page_number} 页页码已更新，但商品列表在 "
                    f"{timeout} 秒内未完成刷新（{detail}）；"
                    "已暂停任务并保留进度"
                )
            if self.stop.wait(.25):
                raise Stopped("操作已停止")

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
        self.log("商品卡片未展示ID，正在打开智赢详情读取产品编号：" + record["title"])
        # A selected card may already be open (including on a resumed run).
        # Close the old panel before clicking so the same legitimate ID can be
        # read afresh. Merely dropping the previous-ID check accepts stale data.
        for attempt in range(1, 3):
            self.check(page)
            if "login" in page.url.lower():
                raise ValueError("智赢登录已失效，请重新登录并确认")
            before = body.evaluate(ERP_DETAIL_ID_READ, record)
            if before.get("ambiguous"):
                raise ValueError("智赢存在多个商品详情，无法确认产品编号，已停止采集")
            close = page.locator(".curd-detail-wrap:visible .crud-detail-close:visible")
            if before.get("root_count") == 1 and close.count() == 1:
                close.click()
                close_deadline = time.monotonic() + min(timeout, 3)
                while True:
                    self.check(page)
                    before = body.evaluate(ERP_DETAIL_ID_READ, record)
                    if before.get("root_count") == 0:
                        break
                    if time.monotonic() >= close_deadline:
                        raise ValueError("智赢旧商品详情未能关闭，已停止采集以避免读取旧编号")
                    if self.stop.wait(.1):
                        raise Stopped("操作已停止")
            previous_id = self.normalize_erp_id(before.get("id", ""))
            # Playwright locators are resolved again after a rerender. Never
            # retry a positional locator that now points at a different card.
            title = self.value(row, "erp_title", required=True)
            image = urljoin(page.url, self.value(row, "erp_image", "src"))
            if title != record["title"] or (record.get("main_image_url") and image != record["main_image_url"]):
                raise ValueError("智赢列表商品在读取详情前发生变化，已停止采集以避免编号错配")
            self.unique(row, "erp_title").click()
            deadline = time.monotonic() + timeout
            stable_id = ""
            while True:
                self.check(page)
                if "login" in page.url.lower():
                    raise ValueError("智赢登录已失效，请重新登录并确认")
                current = body.evaluate(ERP_DETAIL_ID_READ, record)
                if current.get("ambiguous"):
                    raise ValueError("智赢详情产品编号不唯一，已停止采集")
                key = self.normalize_erp_id(current.get("id", ""))
                matched = key and key != previous_id and (current.get("title_match") or current.get("image_match"))
                if matched:
                    if not re.fullmatch(r"[1-9]\d*", key):
                        raise ValueError("智赢详情未返回有效的ERP产品编号，已停止采集")
                    if key == stable_id:
                        self.log("已从智赢详情读取产品编号：" + key)
                        return key
                stable_id = key if matched else ""
                if time.monotonic() >= deadline:
                    diagnostic = {"attempt": attempt, "source_page": record.get("source_page"),
                                  "source_index": record.get("source_index"), "title": record["title"],
                                  "expected_image": record.get("main_image_url", ""),
                                  "previous_id": previous_id, "observed": current}
                    self.log("智赢详情编号读取超时：" + json.dumps(diagnostic, ensure_ascii=False), level="WARNING")
                    break
                if self.stop.wait(.25):
                    raise Stopped("操作已停止")
            if attempt == 1:
                self.log("智赢详情尚未完成商品核对，重新打开当前商品后重试一次", level="WARNING")
        raise ValueError("商品卡片没有ID，重新打开详情重试后仍无法核对目标商品编号；"
                         f"原编号={previous_id or '空'}，当前编号={key or '空'}，"
                         f"标题匹配={bool(current.get('title_match'))}，主图匹配={bool(current.get('image_match'))}；"
                         "请查看智赢详情编号读取超时日志")

    def collect(self, store, on_task=None):
        selection = selection_params(self.config.get("run_selection"), self.config)
        cursor_mode = "start_product_id" in selection
        start_product_id = self.normalize_erp_id(selection.get("start_product_id", ""))
        end_page = int(self.config["max_pages"] if cursor_mode else selection["end_page"])
        scope = selection_key(selection, self.config)
        page = self.page(self.config["erp_list_url"])
        seen = set()
        # A terminated batch stores this checkpoint as JSON null. Older task
        # databases therefore still need the local fallback before using .get.
        checkpoint = store.state("collection", {}) or {}
        resume_after = checkpoint.get("page", 0) if not checkpoint.get("complete") and checkpoint.get("scope") == scope else 0
        cursor_found = not bool(start_product_id) or bool(resume_after)
        selected_items = 0
        limit_reached = False
        if not resume_after:
            store.reset_scope(scope)
        if resume_after:
            store.log(f"从已保存的第 {resume_after} 页后续采集，前序页面只翻页、不重复提取")
        try:
            store.log(
                "正在应用分类筛选，准备按起始产品编号连续读取"
                if cursor_mode else
                f"正在应用分类筛选，准备采集第 {selection['start_page']}–{selection['end_page']} 页"
            )
            category_label = self.apply_category(page, selection["category"])
            self.first_page(page)
            first = max(1 if cursor_mode else selection["start_page"], resume_after + 1)
            start_item = int(selection.get("start_item") or 1)
            if cursor_mode:
                store.log(
                    f"本次采集：{category_label}，从产品 {start_product_id or '分类首件'} 开始，"
                    f"最多 {int(self.config.get('max_items') or 10)} 件"
                )
            else:
                store.log(f"本次采集：{category_label}，第 {selection['start_page']}–{selection['end_page']} 页；"
                          f"第 {start_item} 件开始（仅首个选中页生效）")
            for page_number in range(1, end_page + 1):
                self.check(page)
                # The active page number changes before React replaces the
                # product cards. Wait for a new card fingerprint so a slow
                # response cannot be mistaken for a duplicate page.
                rows, fingerprint = self.wait_for_product_rows(
                    page, page_number, seen
                )
                if page_number < first:
                    seen.add(fingerprint)
                    if not self.next_page(page, page_number):
                        raise ValueError(f"列表仅有 {page_number} 页，无法到达起始/续跑页 {first}")
                    continue
                seen.add(fingerprint)
                count = 0
                row_list = rows.all()
                item_offset = start_item if not cursor_mode and page_number == selection["start_page"] else 1
                if not cursor_mode and page_number == selection["start_page"] and item_offset > len(row_list):
                    raise ValueError(f"智赢第 {page_number} 页只有 {len(row_list)} 件商品，"
                                     f"无法从第 {item_offset} 件开始")
                for item_index, row in enumerate(row_list, 1):
                    if item_index < item_offset:
                        continue
                    self.check(page)
                    raw = row.inner_text()
                    image = self.value(row, "erp_image", "src", required=True)
                    record = {"title": self.value(row, "erp_title", required=True),
                              "main_image_url": urljoin(page.url, image),
                              "description": self.value(row, "erp_description") or raw,
                              "erp_sku": self.value(row, "erp_sku"), "raw_erp": raw,
                              "source_page": page_number, "source_index": item_index,
                              "source_category": selection["category"],
                              "source_category_label": category_label,
                              "zying_category_name": category_label}
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
                    if not cursor_found:
                        if key != start_product_id:
                            continue
                        cursor_found = True
                        store.log(f"已在智赢第 {page_number} 页定位起始产品 {start_product_id}", key)
                    record["erp_goods_id"] = key
                    added = store.add(record)
                    if not added:
                        # Existing tasks retain their decision/progress, but
                        # refresh the source position so a later run cannot be
                        # reordered by the original database creation time.
                        store.update(key, source_page=page_number, source_index=item_index,
                                     source_category=selection["category"],
                                     source_category_label=category_label,
                                     zying_category_name=category_label)
                    count += added
                    selected_items += 1
                    store.include_in_scope(scope, key, page_number)
                    store.log(f"已采集商品 {key}：{record['title']}", key)
                    if on_task:
                        # Do not read the next card or advance the page checkpoint
                        # until this item's save has been verified.
                        self.visual(store.get(key), "erp_item",
                                    f"正在逐件核对智赢商品 {key}：{record['title']}", page)
                        on_task(key)
                        self.check(page)
                    if cursor_mode and not on_task and selected_items >= int(self.config.get("max_items") or 10):
                        limit_reached = True
                        break
                store.log(f"ERP第 {page_number} 页采集完成，新增 {count} 条；已存在记录保留原进度")
                store.set_state("collection", {"page": page_number, "signature": fingerprint, "at": time.time(),
                                               "url": self.config["erp_list_url"], "scope": scope,
                                               "selection": selection, "complete": page_number == end_page or limit_reached})
                store.export()
                if limit_reached or page_number == end_page:
                    break
                if not self.next_page(page, page_number):
                    store.set_state("collection", {**store.state("collection"), "complete": True})
                    store.log(f"列表已到末页 {page_number}，本次采集结束")
                    break
            if start_product_id and not cursor_found:
                raise ValueError(
                    f"所选分类中未找到起始产品编号 {start_product_id}，请确认编号和分类"
                )
            return len(seen)
        finally:
            self.release(page)

    def image_payload(self, task):
        url = safe_url(task.get("main_image_url", ""))
        # Upload the exact main image of this task, never a hardcoded sample or
        # title search. Keep the image in memory and bound downloads.
        maximum = 10 * 1024 * 1024
        data = None
        retryable_statuses = {408, 420, 429}
        headers = {
            # Alibaba's image CDN can temporarily reject a plain requests
            # client. Keep the request close to the visible 1688 browser
            # request while still downloading the exact ERP image.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": "https://www.1688.com/",
        }
        for attempt in range(1, 4):
            try:
                with requests.get(url, headers=headers, stream=True, timeout=(10, 30)) as response:
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
                break
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == 3:
                    raise ValueError("商品主图连续3次下载失败") from exc
                delay = min(0.5 * (2 ** (attempt - 1)), 8.0)
                self.log(f"商品主图下载第 {attempt} 次失败，{delay:g}秒后重试", task["erp_goods_id"], "WARNING")
                if self.stop.wait(delay):
                    raise Stopped()
            except requests.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if (status not in retryable_statuses and not (500 <= (status or 0) < 600)) or attempt == 3:
                    raise
                response = exc.response
                retry_after = response.headers.get("Retry-After") if response is not None else None
                try:
                    delay = float(retry_after) if retry_after else 0
                except (TypeError, ValueError):
                    delay = 0
                delay = max(0.5 * (2 ** (attempt - 1)), delay)
                delay = min(delay, 30.0)
                self.log(
                    f"商品主图HTTP {status}，第 {attempt} 次下载失败，{delay:g}秒后重试",
                    task["erp_goods_id"], "WARNING",
                )
                if self.stop.wait(delay):
                    raise Stopped()
        with Image.open(io.BytesIO(data)) as picture:
            kind = picture.format
            picture.verify()
        formats = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
                   "WEBP": ("webp", "image/webp"), "GIF": ("gif", "image/gif")}
        if kind not in formats:
            raise ValueError("商品主图不是支持的图片格式")
        extension, mime = formats[kind]
        return {"name": "product-main." + extension, "mimeType": mime, "buffer": data}

    @staticmethod
    def image_submit_button(page):
        """Find the one upload-dialog action without relying on one site class.

        1688 has used several labels for the same action.  Strong image-search
        labels are safe anywhere; generic submit/confirm labels are accepted
        only inside a local component that also owns an image file input.
        """
        matches = []
        for frame in page.frames:
            if frame.is_detached():
                continue
            try:
                buttons = frame.locator("button, [role='button']")
                candidates = buttons.evaluate_all(r"""elements => elements.map((element,index) => {
                      const style=getComputedStyle(element),box=element.getBoundingClientRect();
                      if(!element.getClientRects().length||style.visibility==='hidden'||box.width<=0||box.height<=0||
                         element.disabled||element.getAttribute('aria-disabled')==='true')return null;
                      const clean=value=>String(value||'').replace(/\s+/g,'').trim();
                      const label=clean(element.innerText||element.getAttribute('aria-label')||element.title);
                      const className=String(element.className||'');
                      const strong=/^(搜索图片|图片搜索|开始搜索|立即搜索|以图搜货|开始搜图|立即搜图|确认上传)$/;
                      const generic=/^(提交|确定|确认|搜索)$/;
                      const dialog=element.closest('[role="dialog"],.ant-modal,.next-dialog,.dialog,.modal');
                      let owner=element.parentElement,distance=0,upload=null;
                      while(owner&&owner!==document.body&&owner!==document.documentElement&&distance<8){
                        upload=owner.querySelector(':scope input[type="file"]');
                        if(upload)break;
                        owner=owner.parentElement;distance++;
                      }
                      const ownsUpload=!!upload;
                      const ownsImageUpload=ownsUpload&&(!upload.accept||/(image|jpg|jpeg|png|bmp|webp)/i.test(upload.accept));
                      const hasSelectedFile=ownsImageUpload&&!!upload.files&&upload.files.length>0;
                      // The ordinary keyword-search button lives in the same
                      // header as the image file input on some 1688 builds.
                      // A real image-search action also has the uploaded
                      // preview (blob/data image or canvas) in its component.
                      const hasImagePreview=!!(owner&&owner.querySelector('img[src^="blob:"],img[src^="data:image"],canvas'));
                      // After a file is uploaded, current 1688 renders both the
                      // ordinary header search and an image-preview primary
                      // action.  Only the latter submits the uploaded picture.
                      const imagePreviewPrimary=/(^|\s)action--[^\s]+/.test(className)&&/(^|\s)actionPrimary--[^\s]+/.test(className);
                      if(strong.test(label))return {index,label,priority:imagePreviewPrimary?6:dialog?5:ownsImageUpload?2:1};
                      if(!generic.test(label))return null;
                      // Never click a plain header keyword-search button merely
                      // because it is near a file input.  It is safe only after
                      // the uploaded file is present or the page has mounted an
                      // image-preview primary action.  The live 1688 homepage
                      // replaces the file input after upload, so files.length
                      // can be zero even though the preview action is ready.
                      if(!ownsImageUpload||(!hasSelectedFile&&!hasImagePreview&&!dialog&&!imagePreviewPrimary))return null;
                      return {index,label,priority:imagePreviewPrimary?6:dialog?5:3};
                    }).filter(Boolean)""")
                for detail in candidates:
                    button = buttons.nth(detail["index"])
                    if detail:
                        matches.append((button, detail))
            except Exception as exc:
                if frame.is_detached() or "Frame was detached" in str(exc):
                    continue
                raise
        if matches:
            priority = max(detail["priority"] for _, detail in matches)
            matches = [match for match in matches if match[1]["priority"] == priority]
        if len(matches) > 1:
            raise ValueError("1688图片上传后出现多个提交按钮，无法确认当前主图的提交入口")
        return matches[0] if matches else (None, None)

    def current_supplier_offer(self, page, candidate, timeout=45):
        """Read only current 1688 SKU prices and per-SKU package weights.

        The new 1688 detail shell can render its title and SKU container before
        the React SKU data arrives.  A 25-second bound was long enough for
        cached pages but turned slower first loads into false technical skips.
        Keep polling the visible page for a bounded 45 seconds so a transient
        data race is retried in-place instead of being recorded as a matching
        failure.
        """
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            self.check(page)
            last = page.evaluate(CURRENT_1688_DETAIL, candidate.get("main_image_url", ""))
            if not last.get("recognized"):
                return None
            if last.get("ready"):
                offer = last["offer"]
                if not offer.get("skus"):
                    raise SupplierAdaptationError("1688新版详情没有可读取的SKU价格")
                return {"url": page.url, **offer}
            if self.stop.wait(.35):
                raise Stopped("操作已停止")
        raise SupplierAdaptationError((last or {}).get("error") or "1688新版详情的SKU价格和重量尚未加载")

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
        else:
            # Freeze the verified element. 1688 may append another file input
            # while the preview dialog is mounting; retaining the broad locator
            # would then make set_input_files fail strict-mode validation.
            upload = upload.first
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
                button, detail = self.image_submit_button(page)
                if button is not None:
                    self.check(page)
                    button.click()
                    submitted = True
                    deadline = time.monotonic() + timeout
                    self.visual(task, "search_submitted", f"已点击1688“{detail['label']}”，开始以图搜货", page)
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
                    # A browser-internal/transient/foreign URL is not evidence of
                    # login or human review. check() above has already tested the
                    # explicit login hosts and visible captcha/risk signals. Give
                    # redirects time to settle; otherwise this becomes a normal
                    # search timeout and the batch can continue.
                    pass
                else:
                    links = result.locator(self.s["result_links"]).evaluate_all("els => els.map(e => e.href)")
                    image_result_page = "/1688-search/pc-image-search/" in urlsplit(result.url).path
                    if image_result_page:
                        # The image-search shell renders a handful of unrelated
                        # recommendation/detail links before the real "找到以下货源"
                        # cards arrive. Never treat those early links as results.
                        cards = self.result_image_cards(result)
                        if cards:
                            self.visual(task, "search_results", f"1688已返回以图搜货结果，读取到 {len(cards)} 个商品卡片", result)
                            return result
                    elif links and (result is not page or result.url != previous_url or links != previous_links):
                        # A same-page 1688 homepage can expose unrelated
                        # recommendation links while the upload is still
                        # pending.  On the same page require the links to live
                        # in an explicit result/search container; retain the
                        # one-link replacement compatibility used by older
                        # image-search pages.  A batch of plain homepage links
                        # is not result evidence (and caused false candidates
                        # such as the six unrelated links for 854631485).
                        same_page_result_container = False
                        if result is page:
                            same_page_result_container = bool(result.locator(self.s["result_links"]).evaluate_all(r"""links => links.some(link => {
                              for(let node=link, depth=0; node && depth<6; depth++, node=node.parentElement){
                                const cls=typeof node.className==='string'?node.className:'';
                                const key=((node.id||'')+' '+cls).toLowerCase();
                                if(/(^|[-_ ])(?:result|results|image-search|search-result|goods-list|offer-list|product-list)([-_ ]|$)/.test(key)) return true;
                              }
                              return false;
                            })"""))
                        one_link_replacement = (
                            result is page and len(links) == len(previous_links) == 1 and links != previous_links
                        )
                        if result is page and not same_page_result_container and not one_link_replacement:
                            links = []
                        else:
                            self.visual(task, "search_results", f"1688已返回以图搜货结果，页面含 {len(links)} 个候选链接", result)
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
        if self.record_visual:
            self.record_visual(task["erp_goods_id"], step, message, page_url=page.url if page else "")
        else:
            self.log(message, task["erp_goods_id"])

    def search_images(self, task):
        self.visual(task, "main_image", "已取得当前商品主图，准备在1688以图搜货")
        before_owned = list(self.owned)
        try:
            page = self.page(self.config["supplier_home_url"], "1688.com")
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
            if page in self.owned:
                self.release(page)
            else:
                # Keep cleanup deterministic even when a caller injects an
                # existing result page into the navigation registry (as the
                # live browser can do after a redirect).
                try:
                    page.close()
                except Exception:
                    pass

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
            # Do not equate every unrelated/transient destination with a captcha.
            # check() above pauses only on an explicit login host or visible risk
            # evidence; other destinations time out as ordinary candidate errors.
            if self.stop.wait(.3):
                raise Stopped("操作已停止")
        raise ValueError("点击匹配候选后未进入可核验的1688商品详情")

    def read_offer(self, task, candidate):
        detail = self.open_offer(task, candidate)
        try:
            # The detail page has already passed its navigation/readiness
            # checks; an additional random 1–3 second sleep only serialized
            # every product without adding evidence.
            self.delay(.1, .3)
            self.visual(task, "candidate", "图片匹配后，正在读取1688商品详情和变体售价", detail)
            current = self.current_supplier_offer(detail, candidate)
            if current is not None:
                weighted = sum(bool(sku.get("raw_weight")) for sku in current["skus"])
                self.log(f"已从1688官方SKU模块读取 {len(current['skus'])} 个变体价格，"
                         f"其中 {weighted} 个读取到包装重量；未扫描广告和推荐商品", task["erp_goods_id"])
                if self.record_supplier_adaptation:
                    self.record_supplier_adaptation(task["erp_goods_id"], {
                        "url": detail.url, "at": time.time(), "ok": True,
                        "source": "1688_official_sku_modules",
                        "evidence": {"sku_count": len(current["skus"]), "weighted_sku_count": weighted},
                    })
                return current
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
                             "raw_surcharge": surcharge, "raw_weight": self.value(row, "sku_weight"),
                             "raw_dimensions": row.inner_text(), **price})
            return {"url": detail.url, "title": self.value(detail, "supplier_title", required=True),
                    "main_image_url": urljoin(detail.url, self.value(detail, "supplier_image", "src", True)),
                    "description": self.value(detail, "supplier_description") or detail.locator("body").inner_text()[:20000],
                    "raw_weight": self.value(detail, "supplier_weight"), "skus": skus,
                    "merchant_id": self.value(detail, "supplier_merchant", self.s["supplier_merchant_attribute"])}
        finally:
            self.release(detail)
            # The result/search page is only a temporary navigation surface
            # for opening this offer. Close it with the detail tab so a long
            # batch does not leave one 1688 view tab per product behind.
            self.release_search(task)

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
        # The list shell becomes visible before its virtualized product data is
        # populated after a category search.  A single empty read used to turn
        # a recoverable render race into an ERP writeback skip.
        deadline = time.monotonic() + 10
        matches = []
        row_list = []
        while time.monotonic() < deadline:
            self.check(page)
            row_list = rows.all()
            matches = [row for row in row_list if self.value(row, "erp_title") == task["title"]]
            if matches:
                break
            if self.stop.wait(.35):
                raise Stopped("操作已停止")
        if len(matches) > 1:
            # Titles are not unique in the product list (the same listing can
            # be imported more than once). Prefer the stable ERP product ID
            # captured during collection before falling back to the image.
            # The previous code skipped this discriminator and could leave two
            # same-title/same-image cards, causing an avoidable writeback skip.
            target_id = self.normalize_erp_id(task["erp_goods_id"])
            id_matches = []
            if self.s.get("erp_id"):
                for row in matches:
                    try:
                        row_id = self.normalize_erp_id(self.value(row, "erp_id", required=True))
                    except (ValueError, TypeError):
                        continue
                    if row_id == target_id:
                        id_matches.append(row)
            if len(id_matches) == 1:
                matches = id_matches
                self.log(f"列表中发现同标题商品，已按ERP商品ID {target_id} 唯一定位回填卡片", task["erp_goods_id"])
            elif len(id_matches) > 1:
                matches = id_matches
            if len(matches) > 1:
                # Some Zying list builds do not render an ID in the card at all.
                # Collection already persisted the one-based card position, so
                # use it before falling back to the image (which is commonly
                # duplicated too).  The selected card is still opened below and
                # its detail ID is checked against the task ID before any write.
                try:
                    source_index = int(task.get("source_index") or 0)
                except (TypeError, ValueError):
                    source_index = 0
                if 1 <= source_index <= len(row_list):
                    indexed = row_list[source_index - 1]
                    if self.value(indexed, "erp_title") == task["title"]:
                        matches = [indexed]
                        self.log(
                            f"列表中发现同标题商品，已按采集时页内序号第{source_index}件定位回填卡片",
                            task["erp_goods_id"],
                        )
                if len(matches) > 1:
                    expected_image = task.get("main_image_url", "")
                    expected_parts = urlsplit(expected_image)
                    expected_image_key = ((expected_parts.hostname or "").lower(), expected_parts.path.rstrip("/"))
                    filtered = []
                    for row in matches:
                        image = urljoin(page.url, self.value(row, "erp_image", "src"))
                        parts = urlsplit(image)
                        image_key = ((parts.hostname or "").lower(), parts.path.rstrip("/"))
                        if image == expected_image or image_key == expected_image_key:
                            filtered.append(row)
                    matches = filtered
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

    @staticmethod
    def review_status(root):
        checked = root.locator("input[name='stat']:checked")
        if checked.count() != 1:
            raise ValueError("无法确认智赢当前商品审核状态")
        label = checked.evaluate("e => (e.closest('label')?.innerText||'').replace(/\\s+/g,'').trim()")
        if not label:
            raise ValueError("智赢当前商品状态标签为空")
        return label

    def update_package_by_product_id(self, product_id, weight_g, dimensions_cm):
        """Update one Zying product by exact product number and verify its saved form."""
        product_id = self.normalize_erp_id(str(product_id or ""))
        if not re.fullmatch(r"[1-9]\d*", product_id):
            raise ValueError("智赢产品编号必须是正整数")
        dims = [part.strip() for part in re.split(r"[x×*＊]\s*", str(dimensions_cm or ""))]
        if len(dims) != 3 or any(not re.fullmatch(r"\d+(?:\.\d+)?", part) for part in dims):
            raise ValueError("智赢产品尺寸需要长x宽x高三项数值")
        weight = str(weight_g).strip()
        if not re.fullmatch(r"\d+(?:\.\d+)?", weight):
            raise ValueError("智赢产品重量不是有效数值")
        page = self.page(self.config["erp_list_url"], "meli.zying.net")
        try:
            if "login" in page.url.lower():
                raise ValueError("智赢登录已失效，请重新登录")
            root_selector = ".curd-detail-wrap"
            self._open_zying_product_by_id(page, product_id)
            actual_id = self.normalize_erp_id(self.value(page, "erp_edit_id", required=True))
            if actual_id != product_id:
                raise ValueError(f"智赢详情产品编号不匹配：目标 {product_id}，当前 {actual_id}")
            root = page.locator(f"{root_selector}:visible")
            if root.count() != 1:
                raise ValueError("智赢产品详情未能唯一定位")
            weight_input = self.unique(page, "erp_weight_input")
            weight_input.fill(weight)
            dimensions_controls = self._zying_package_dimension_controls(root)
            if len(dimensions_controls) == 1:
                dimensions_controls[0].fill("x".join(dims))
            elif len(dimensions_controls) == 3:
                for control, value in zip(dimensions_controls, dims):
                    control.fill(value)
            else:
                raise ValueError("智赢详情页未能识别唯一的包装尺寸字段")
            self._set_zying_product_level(page, root)
            if self.unique(page, "erp_weight_input").input_value().strip() != weight:
                raise ValueError("智赢重量输入框回读与目标值不一致")
            expected_dims = ["x".join(dims)] if len(dimensions_controls) == 1 else dims
            if [field.input_value().strip().lower().replace("×", "x") for field in dimensions_controls] != expected_dims:
                raise ValueError("智赢尺寸输入框回读与目标值不一致")
            save = self.erp_save_button(page, root)
            self.check(page)
            save.click()
            try:
                if self.s.get("erp_saved"):
                    page.locator(self.s["erp_saved"]).first.wait_for(state="visible", timeout=8000)
                else:
                    page.get_by_text(re.compile(r"保存成功|操作成功|更新成功|提交成功")).first.wait_for(
                        state="visible", timeout=8000
                    )
            except Exception as exc:
                if "Timeout" not in str(exc):
                    raise
            page.reload(wait_until="domcontentloaded")
            self._open_zying_product_by_id(page, product_id)
            root = page.locator(f"{root_selector}:visible")
            if self.normalize_erp_id(self.value(page, "erp_edit_id", required=True)) != product_id:
                raise ValueError("保存后智赢详情编号发生变化，无法确认写入结果")
            dimensions_controls = self._zying_package_dimension_controls(root)
            if len(dimensions_controls) not in (1, 3):
                raise ValueError("保存后无法重新定位智赢包装尺寸字段")
            if self.unique(page, "erp_weight_input").input_value().strip() != weight:
                raise ValueError("保存后回读的智赢重量与目标值不一致")
            if [field.input_value().strip().lower().replace("×", "x") for field in dimensions_controls] != expected_dims:
                raise ValueError("保存后回读的智赢尺寸与目标值不一致")
            self._verify_zying_product_level(root)
            self.log(f"智赢产品 {product_id} 已保存：重量 {weight}g，尺寸 {'x'.join(dims)}cm，级别重点")
            return {"product_id": product_id, "weight_g": weight, "dimensions_cm": "x".join(dims), "level": "重点"}
        finally:
            self.release(page)

    def _open_zying_product_by_id(self, page, product_id):
        root_selector = ".curd-detail-wrap"
        if root_selector and page.locator(f"{root_selector}:visible").count():
            if self.normalize_erp_id(self.value(page, "erp_edit_id")) == product_id:
                return
        self._search_zying_product(page, product_id)
        rows = page.locator(self.s["erp_rows"])
        rows.first.wait_for(state="visible")
        candidates, last_rows = [], []
        search_started = time.monotonic()
        deadline = search_started + 5
        while time.monotonic() < deadline:
            last_rows = rows.all()
            exact = []
            for row in last_rows:
                row_id = ""
                if self.s.get("erp_id"):
                    try:
                        row_id = self.normalize_erp_id(self.value(row, "erp_id", required=True))
                    except Exception:
                        pass
                if row_id == product_id or (not row_id and re.search(rf"(?<!\d){re.escape(product_id)}(?!\d)", row.inner_text())):
                    exact.append(row)
            if exact:
                candidates = exact
                break
            if len(last_rows) == 1 and time.monotonic() - search_started >= 1.2:
                break
            if self.stop.wait(.3):
                raise Stopped("智赢产品编号搜索等待被停止")
        if not candidates and len(last_rows) == 1:
            # The detail header is still checked before any write, so a list
            # build that omits card IDs can use a sole search result safely.
            candidates = last_rows
        if len(candidates) != 1:
            raise ValueError(f"按产品编号 {product_id} 搜索后匹配到 {len(candidates)} 个智赢商品")
        card = candidates[0]
        title_selector = self.s.get("erp_title")
        title = card.locator(title_selector).first if title_selector and card.locator(title_selector).count() else card
        title.click()
        page.locator(f"{root_selector}:visible").wait_for(state="visible")
        actual_id = self.normalize_erp_id(self.value(page, "erp_edit_id", required=True))
        if actual_id != product_id:
            raise ValueError(f"打开的智赢商品编号不匹配：目标 {product_id}，当前 {actual_id}")

    def _search_zying_product(self, page, product_id):
        selector = self.s.get("erp_product_id_search") or ""
        if selector:
            fields = page.locator(selector)
            visible_fields = [field for field in fields.all() if field.is_visible()]
        else:
            visible_fields = []
            for field in page.locator("input:visible").all():
                details = field.evaluate("""e => {
                  const box=e.closest('.ant-form-item')||e.parentElement;
                  return [e.placeholder,e.getAttribute('aria-label'),box?.innerText].filter(Boolean).join(' ')
                }""")
                if re.search(r"产品编号|商品编号|产品ID|商品ID", details or ""):
                    visible_fields.append(field)
        if len(visible_fields) != 1:
            raise ValueError(f"无法唯一定位智赢产品编号搜索框，请配置 erp_product_id_search（当前匹配 {len(visible_fields)} 个）")
        visible_fields[0].fill(product_id)
        button_selector = self.s.get("erp_product_search_button") or ""
        if button_selector:
            buttons = [button for button in page.locator(button_selector).all() if button.is_visible()]
        else:
            buttons = [button for button in page.locator("button").all()
                       if button.is_visible() and re.fullmatch(r"\s*搜\s*索\s*", button.inner_text() or "")]
            nearby = visible_fields[0].locator("xpath=ancestor::form[1]")
            if not nearby.count():
                nearby = visible_fields[0].locator("xpath=ancestor::*[contains(@class,'ant-form')][1]")
            if nearby.count():
                local_buttons = [button for button in nearby.locator("button").all()
                                 if button.is_visible() and re.fullmatch(r"\s*搜\s*索\s*", button.inner_text() or "")]
                if len(local_buttons) == 1:
                    buttons = local_buttons
        if len(buttons) == 1:
            buttons[0].click()
        elif len(buttons) == 0:
            visible_fields[0].press("Enter")
        else:
            raise ValueError(f"智赢产品搜索按钮匹配到 {len(buttons)} 个，请配置 erp_product_search_button")
        page.wait_for_timeout(500)

    def _zying_package_dimension_controls(self, root):
        selector = self.s.get("erp_dimensions_input") or ""
        if selector:
            matches = [field for field in root.locator(selector).all() if field.is_visible()]
            if len(matches) not in (1, 3):
                raise ValueError(f"智赢尺寸字段选择器必须匹配一个整体尺寸框或长宽高三个字段，实际 {len(matches)} 个")
            return matches
        dimension_items = []
        dimension_groups = []
        side_items = {"长": [], "宽": [], "高": []}
        for item in root.locator(".ant-form-item").all():
            label = item.locator(".ant-form-item-label").inner_text() if item.locator(".ant-form-item-label").count() else ""
            label = re.sub(r"[：:*\s]", "", label)
            fields = [field for field in item.locator("input,textarea").all() if field.is_visible()]
            if not fields:
                continue
            if re.search(r"包装尺寸|商品尺寸|产品尺寸|尺寸", label):
                dimension_items.extend(fields)
                dimension_groups.append(fields)
            for side, pattern in (("长", r"(?:包装|产品)?(?:长|长度|length)"),
                                  ("宽", r"(?:包装|产品)?(?:宽|宽度|width)"),
                                  ("高", r"(?:包装|产品)?(?:高|高度|height)")):
                if re.fullmatch(pattern, label, flags=re.I):
                    side_items[side].extend(fields)
        if len(dimension_items) == 1:
            return dimension_items
        if len(dimension_groups) == 1 and len(dimension_groups[0]) == 3:
            return dimension_groups[0]
        if all(len(side_items[side]) == 1 for side in ("长", "宽", "高")):
            return [side_items[side][0] for side in ("长", "宽", "高")]
        return []

    def _set_zying_product_level(self, page, root):
        selector = self.s.get("erp_product_level_control") or ""
        if selector:
            controls = [control for control in root.locator(selector).all() if control.is_visible()]
            if len(controls) != 1:
                raise ValueError(f"智赢产品级别选择器必须唯一，实际 {len(controls)} 个")
            item = controls[0].locator("xpath=ancestor::*[contains(@class,'ant-form-item')][1]")
            if item.count() != 1:
                raise ValueError("产品级别选择器未定位到表单项")
        else:
            items = []
            for item in root.locator(".ant-form-item").all():
                label = item.locator(".ant-form-item-label").inner_text() if item.locator(".ant-form-item-label").count() else ""
                if re.search(r"产品级别|产品等级|级别", label):
                    items.append(item)
            if len(items) != 1:
                raise ValueError(f"无法唯一定位智赢“产品级别”字段（当前匹配 {len(items)} 个）")
            item = items[0]
        radio_labels = [label for label in item.locator("label").all()
                        if label.is_visible() and re.sub(r"\s+", "", label.inner_text()) == "重点"
                        and label.locator("input[type='radio']").count()]
        if radio_labels:
            if len(radio_labels) != 1:
                raise ValueError("智赢产品级别单选项中没有唯一的“重点”选项")
            radio_labels[0].click()
            self._verify_zying_product_level(root)
            return
        native = [control for control in item.locator("select").all() if control.is_visible()]
        if len(native) == 1:
            native[0].select_option(label="重点")
            self._verify_zying_product_level(root)
            return
        selects = [control for control in item.locator(".ant-select-selector,[role='combobox']").all() if control.is_visible()]
        if len(selects) == 1:
            selects[0].click()
            options = [option for option in page.locator(".ant-select-dropdown:visible .ant-select-item-option").all()
                       if option.is_visible() and re.sub(r"\s+", "", option.inner_text()) == "重点"]
            if len(options) != 1:
                raise ValueError("智赢产品级别下拉项中没有唯一的“重点”选项")
            options[0].click()
            self._verify_zying_product_level(root)
            return
        raise ValueError("智赢产品级别字段不是可识别的单选框或下拉框")

    def _verify_zying_product_level(self, root):
        items = []
        for item in root.locator(".ant-form-item").all():
            label = item.locator(".ant-form-item-label").inner_text() if item.locator(".ant-form-item-label").count() else ""
            if re.search(r"产品级别|产品等级|级别", label):
                items.append(item)
        if len(items) != 1:
            raise ValueError("保存前后无法唯一核对智赢产品级别")
        item = items[0]
        checked = [radio for radio in item.locator("input[type='radio']:checked").all()]
        if checked:
            selected = [radio for radio in checked if re.sub(r"\s+", "", radio.evaluate("e => e.closest('label')?.innerText||''")) == "重点"]
            if len(selected) == 1:
                return True
            raise ValueError("智赢产品级别未回读为“重点”")
        native = [control for control in item.locator("select").all() if control.is_visible()]
        if len(native) == 1:
            selected = native[0].locator("option:checked").inner_text()
            if re.sub(r"\s+", "", selected) == "重点":
                return True
        selected = item.locator(".ant-select-selection-item").all_inner_texts()
        if not selected:
            selected = item.locator("[role='combobox']").all_inner_texts()
        if len(selected) == 1 and re.sub(r"\s+", "", selected[0]) == "重点":
            return True
        raise ValueError("智赢产品级别未回读为“重点”")

    def read_review_status(self, task):
        """Read the live Zying review status before any supplier/AI work."""
        host = urlsplit(self.config["erp_list_url"]).hostname
        page = self.page(task.get("erp_edit_url") or self.config["erp_list_url"], host)
        try:
            self.locate_erp_detail(page, task)
            return self.review_status(page.locator(".curd-detail-wrap"))
        finally:
            self.release(page)

    def write_patch(self, task, changes, before_save):
        from .models import erp_value_equal
        if not changes or set(changes) - {"weight_g", "net_income_usd", "review_status", "dimensions_cm"}:
            raise ValueError("回填字段无效")
        if "dimensions_cm" in changes:
            dims = re.split(r"\s*[x×*＊]\s*", str(changes["dimensions_cm"]))
            if len(dims) != 3 or any(number(side) > 60 for side in dims):
                raise ValueError("包装尺寸必须为长x宽x高，且单边不能超过60 cm")
        if "review_status" in changes and changes["review_status"] not in WRITEBACK_REVIEW_STATUSES:
            raise ValueError("审核状态只能回填为通过或价格异常")
        host = urlsplit(self.config["erp_list_url"]).hostname
        page = self.page(task.get("erp_edit_url") or self.config["erp_list_url"], host)
        def snapshot():
            if self.normalize_erp_id(self.value(page, "erp_edit_id", required=True)) != task["erp_goods_id"]:
                raise ValueError("智赢详情产品编号变化，已停止保存")
            root = page.locator(".curd-detail-wrap")
            dimension_controls = self._zying_package_dimension_controls(root)
            dimensions = "x".join(control.input_value().strip() for control in dimension_controls)
            if len(dimension_controls) == 1:
                dimensions = dimension_controls[0].input_value().strip()
            result = {"weight_g": self.unique(page, "erp_weight_input").input_value(),
                      "net_income_usd": self.unique(page, "erp_net_income_input").input_value(),
                      "review_status": self.review_status(root)}
            if dimension_controls:
                result["dimensions_cm"] = dimensions
            return result
        try:
            self.locate_erp_detail(page, task)
            self.visual(task, "erp_before", "已定位智赢商品，记录修改前重量、净收益和状态", page)
            old = snapshot()
            if old["review_status"] != PENDING_REVIEW_STATUS:
                raise ValueError(f"智赢商品状态为{old['review_status']}，仅待审核商品允许核重核价")
            root = page.locator(".curd-detail-wrap")
            save = self.erp_save_button(page, root)
            before_save(old)
            if not changes:
                # A lower calculated net income may be intentionally retained
                # at the original value. In that case no ERP field needs a
                # write; return the verified snapshot for the audit trail.
                self.visual(task, "erp_saved", "计算净收益低于原值，净收益保持不变；无需重复保存，仅保留重量变更策略", page)
                return old
            for field, selector in (("weight_g", "erp_weight_input"), ("net_income_usd", "erp_net_income_input")):
                if field in changes:
                    value = number(changes[field])
                    if field == "net_income_usd" and value != value.to_integral_value():
                        raise ValueError("净收益必须是整数美元")
                    self.unique(page, selector).fill(str(value))
            if "dimensions_cm" in changes:
                controls = self._zying_package_dimension_controls(root)
                if len(controls) == 1:
                    controls[0].fill("x".join(dims))
                elif len(controls) == 3:
                    for control, side in zip(controls, dims):
                        control.fill(str(number(side)))
                else:
                    raise ValueError("智赢详情未能唯一定位包装尺寸输入框，已停止保存")
            if "review_status" in changes:
                target = changes["review_status"]
                radios = root.locator("input[name='stat']")
                matches = []
                for radio in radios.all():
                    label = radio.evaluate("e => (e.closest('label')?.innerText||'').replace(/\\s+/g,'').trim()")
                    if label == target:
                        matches.append(radio)
                if len(matches) != 1:
                    raise ValueError(f"无法唯一定位智赢审核状态：{target}")
                matches[0].check()
                if self.review_status(root) != target:
                    raise ValueError(f"智赢审核状态未切换为{target}")
            self.visual(task, "erp_saving", "重量、净收益及审核状态已按核重核价结论填写，正在保存", page)
            self.check(page)
            save.click()
            # Toast copy varies between Zying builds and can be absent even
            # when the save request succeeds. Use it as an early confirmation,
            # but let the post-reload field readback be the authoritative check.
            try:
                if self.s["erp_saved"]:
                    page.locator(self.s["erp_saved"]).first.wait_for(state="visible", timeout=5000)
                else:
                    page.get_by_text(re.compile(r"保存成功|操作成功|更新成功|提交成功")).first.wait_for(
                        state="visible", timeout=5000)
            except Exception as exc:
                if "Timeout" not in str(exc):
                    raise
                self.log("智赢未显示保存成功提示，继续通过保存后回读确认", task["erp_goods_id"], "WARNING")
            self.check(page)
            page.reload(wait_until="domcontentloaded")
            safe_url(page.url, host=host)
            self.locate_erp_detail(page, task)
            actual = snapshot()
            for field in old:
                expected = changes.get(field, old[field])
                if not erp_value_equal(field, actual.get(field), expected):
                    raise WritebackMismatch(actual)
            self.visual(task, "erp_saved", "保存后重新打开商品，重量、净收益及审核状态回读确认一致", page)
            return actual
        finally:
            self.release(page)

    def erp_save_button(self, page, root):
        """Resolve the detail form's save action without counting hidden clones.

        Zying has rendered sticky header/footer copies of the same save action in
        different builds.  The old role locator counted hidden copies and then
        stopped before writing anything.  Prefer an explicitly configured
        selector; otherwise inspect only visible, enabled controls inside the
        detail root and choose the strongest save-labelled primary action.
        """
        selector = self.s.get("erp_save", "")
        if selector:
            controls = page.locator(selector)
            usable = [control for control in controls.all()
                      if control.is_visible() and control.is_enabled()]
            if len(usable) != 1:
                raise ValueError(f"智赢详情保存按钮选择器必须唯一且可用，实际 {len(usable)} 个")
            return usable[0]

        def candidates(scope):
            controls = scope.locator("button, [role='button'], a, input[type='submit'], input[type='button']")
            details = controls.evaluate_all(r"""elements => elements.map((element, index) => {
          const style = getComputedStyle(element);
          const box = element.getBoundingClientRect();
          const visible = !!element.getClientRects().length && style.display !== 'none' &&
            style.visibility !== 'hidden' && style.opacity !== '0' && box.width > 0 && box.height > 0;
          const disabled = element.disabled || element.getAttribute('aria-disabled') === 'true' ||
            element.classList.contains('disabled') || element.classList.contains('is-disabled');
          const clean = value => String(value || '').replace(/\s+/g, '').trim();
          const label = clean(element.innerText || element.value || element.getAttribute('aria-label') || element.title ||
            element.getAttribute('data-title'));
          const classes = String(element.className || '');
          const saveLabel = /^(保存(?:并关闭|商品|修改)?|提交(?:保存|修改)?|更新|确认修改|确定)$/;
          const labelled = saveLabel.test(label);
          const submit = (element.tagName.toLowerCase()==='button'||element.tagName.toLowerCase()==='input')&&element.type==='submit';
          if (!visible || disabled || (!labelled && !submit)) return null;
          let score = 0;
          if (label === '保存') score += 10;
          if (labelled) score += 6;
          if (submit) score += 4;
          if (/primary|primary-btn|main/.test(classes)) score += 3;
          if (/save|submit|保存|提交/.test(classes + label)) score += 2;
          if (element.closest('form')) score += 1;
          return {index, label, score};
        }).filter(Boolean)""");
            return controls, details

        controls, details = candidates(root)
        if not details:
            # Some Zying builds render the sticky footer action as a portal
            # beside .curd-detail-wrap rather than as its descendant.
            controls, details = candidates(page)
        if not details:
            raise ValueError("智赢详情页没有可见可用的保存按钮")
        max_score = max(item["score"] for item in details)
        best = [item for item in details if item["score"] == max_score]
        # Header and footer buttons are the same action in the current UI. If
        # both remain tied, use the first DOM occurrence deterministically;
        # unlike the old count check this does not mistake hidden duplicates
        # for an ambiguous save target.
        if len(best) > 1:
            self.log(f"智赢详情发现 {len(best)} 个同级可用保存按钮，已选择详情主操作", level="INFO")
        return controls.nth(best[0]["index"])

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
            self.log("1688详情页自动适配失败，当前商品将自动跳过：" + str(exc), task_id, "WARNING")
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
            root = page.locator(".curd-detail-wrap")
            dimension_controls = self._zying_package_dimension_controls(root)
            if dimension_controls:
                old["dimensions_cm"] = (dimension_controls[0].input_value().strip() if len(dimension_controls) == 1
                                        else "x".join(control.input_value().strip() for control in dimension_controls))
            target_dimensions = task.get("supplier_dimensions_cm") if not task.get("dimensions_notice") else None
            if target_dimensions:
                dims = re.split(r"\s*[x×*＊]\s*", str(target_dimensions))
                if len(dims) != 3 or any(number(side) > 60 for side in dims):
                    raise ValueError("包装尺寸必须为长x宽x高，且单边不能超过60 cm")
                if len(dimension_controls) not in (1, 3):
                    raise ValueError("智赢详情未能唯一定位包装尺寸输入框，已停止保存")
            saved = page.locator(self.s["erp_saved"])
            if saved.count() and saved.first.is_visible():
                raise ValueError("保存成功标识在保存前已可见，无法判断本次保存结果")
            before_save(old)
            # The save policy may keep the original ERP net income after the
            # live form value is read. Re-read the task so a lower calculated
            # value is never written by this legacy workflow.
            net_income = number(task.get("net_income_usd"))
            keep_original_net_income = bool((task.get("pricing") or {}).get("net_income_retained_original"))
            if not keep_original_net_income:
                cost.fill(str(net_income))
            weight.fill(str(number(task["weight_g"])))
            if target_dimensions:
                if len(dimension_controls) == 1:
                    dimension_controls[0].fill("x".join(dims))
                else:
                    for control, side in zip(dimension_controls, dims):
                        control.fill(str(number(side)))
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
            dimension_controls = self._zying_package_dimension_controls(page.locator(".curd-detail-wrap"))
            if dimension_controls:
                actual["dimensions_cm"] = (dimension_controls[0].input_value().strip() if len(dimension_controls) == 1
                                           else "x".join(control.input_value().strip() for control in dimension_controls))
            try:
                if number(actual["net_income_usd"]) != net_income or number(actual["weight_g"]) != number(task["weight_g"]):
                    raise ValueError()
            except ValueError:
                raise WritebackMismatch(actual)
            from .models import erp_value_equal
            if "dimensions_cm" in old and not erp_value_equal(
                    "dimensions_cm", actual.get("dimensions_cm"), target_dimensions or old["dimensions_cm"]):
                raise WritebackMismatch(actual)
            return actual
        finally:
            self.release(page)
