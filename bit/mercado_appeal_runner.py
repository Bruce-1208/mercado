import argparse
import base64
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from bit.bit_mercado_limit import (
    get_mercado_backend_status,
    process_mercado_rate_limit,
)
from bit.mercado_click_delay import javascript_contains_click, mercado_click_cooldown

try:
    from bit.chat_log import append_chat_log
except Exception:
    try:
        from chat_log import append_chat_log
    except Exception:
        def append_chat_log(*args, **kwargs):
            return None

try:
    import websocket
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: websocket-client. Install it with:\n"
        "  pip install websocket-client\n"
    ) from exc


BIT_API = "http://127.0.0.1:54345"
INFRACTIONS_URL = "https://global-selling.mercadolibre.com/noindex/pppi/infractions"
HELP_URL = "https://global-selling.mercadolibre.com/help"
HUB_URL = "https://global-selling.mercadolibre.com/help/hub/30928?source"
DIRECT_CHAT_URL = "https://global-selling.mercadolibre.com/help/chat/v2"
OUT_DIR = Path(__file__).resolve().parent
PRODUCT_SEPARATOR = "\u3001"
AI_APPEAL_SUFFIX = (
    "\u8fd9\u51e0\u4e2a\u4ea7\u54c1\u662f\u901a\u7528\u54c1\u724c\uff0c"
    "\u5e76\u975e\u4fb5\u6743\u4ea7\u54c1\uff0c\u8fd9\u662f\u7cfb\u7edf\u8bef\u5224\uff0c"
    "\u9ebb\u70e6\u5e2e\u6211\u5220\u9664\u4fb5\u6743\u8bb0\u5f55\uff0c\u8c22\u8c22"
)
HUMAN_APPEAL_SUFFIX = (
    "\u8fd9\u4e9b\u4ea7\u54c1\u662f\u901a\u7528\u54c1\u724c\uff0c"
    "\u5e76\u975e\u4fb5\u6743\u4ea7\u54c1\uff0c\u8fd9\u662f\u7cfb\u7edf\u8bef\u5224\uff0c"
    "\u9ebb\u70e6\u5e2e\u6211\u5220\u9664\u4fb5\u6743\u8bb0\u5f55\uff0c\u8c22\u8c22"
)


SITE_NAMES = {
    "MX": "Mexico",
    "BR": "Brazil",
    "AR": "Argentina",
    "CL": "Chile",
    "CO": "Colombia",
    "UY": "Uruguay",
}
SITE_REMOTE_VALUES = {
    "MX": "MLM-remote",
    "BR": "MLB-remote",
    "AR": "MLA-remote",
    "CL": "MLC-remote",
    "CO": "MCO-remote",
    "UY": "MLU-remote",
}

SITE_OPTION_REPLIES = {
    "MX": "Mexico (Direct to consumer)",
    "BR": "Brazil",
    "CL": "Chile",
    "CO": "Colombia",
    "AR": "Argentina",
    "UY": "Uruguay",
}
SITE_OPTION_MENU_OPTIONS = (
    "Mexico (Direct to consumer)",
    "Mexico (Fulfillment)",
    "Brazil",
    "Chile",
    "Colombia",
    "Argentina",
    "Uruguay",
)


DEEP_JS = r"""
function deepElements(root = document) {
  const out = [];
  const walk = node => {
    const elements = node.querySelectorAll ? Array.from(node.querySelectorAll('*')) : [];
    for (const el of elements) {
      out.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(root);
  return out;
}
function visible(el) {
  return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
}
function chatInput() {
  return deepElements().find(el => {
    const placeholder = el.getAttribute('placeholder') || '';
    const aria = el.getAttribute('aria-label') || '';
    return visible(el) && (
      el.id === 'chat-input' ||
      placeholder.includes('Ask the assistant') ||
      aria.includes('Chat message input')
    );
  });
}
function sendButton() {
  return deepElements().find(el => {
    const aria = el.getAttribute('aria-label') || '';
    const cls = String(el.className || '');
    return visible(el) && el.tagName === 'BUTTON' && (
      aria.includes('Send message') ||
      cls.includes('new-chat-input__right-button')
    );
  });
}
function nativeSetValue(el, value) {
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set ||
    Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value')?.set;
  if (setter) setter.call(el, value);
  else el.value = value;
  el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
}
"""


class Cdp:
    def __init__(self, ws_url: str):
        # Chrome/BitBrowser may reject DevTools WebSocket connections that carry
        # a 127.0.0.1 Origin header unless the browser was launched with
        # --remote-allow-origins. websocket-client can omit that header.
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.next_id = 1

    def call(self, method, params=None, timeout=30):
        msg_id = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"Timeout waiting for {method}")

    def js(self, expression, timeout=30, context_id=None):
        params = {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": True,
        }
        if context_id is not None:
            params["contextId"] = context_id
        result = self.call(
            "Runtime.evaluate",
            params,
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "Runtime.evaluate failed"))
        value = result.get("result", {}).get("value")
        if javascript_contains_click(expression) and value is not False:
            mercado_click_cooldown()
        return value

    def screenshot(self, path: Path):
        data = self.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})["data"]
        path.write_bytes(base64.b64decode(data))

    def close(self):
        self.ws.close()


def post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def put_json(url):
    req = urllib.request.Request(url, data=b"", method="PUT")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def open_bitbrowser(window_name: str) -> str:
    browsers = []
    for page in range(0, 20):
        listing = post_json(f"{BIT_API}/browser/list", {"page": page, "pageSize": 100})
        page_browsers = listing["data"]["list"]
        if not page_browsers:
            break
        browsers.extend(page_browsers)
        if len(page_browsers) < 100:
            break
    matches = [b for b in browsers if window_name in b.get("name", "")]
    if not matches:
        names = [b.get("name", "") for b in browsers]
        raise RuntimeError(f"Window not found: {window_name}. Existing names include: {names[:30]}")
    browser_id = matches[0]["id"]
    opened = post_json(f"{BIT_API}/browser/open", {"id": browser_id})
    if not opened.get("success"):
        raise RuntimeError(f"Failed to open BitBrowser window: {opened}")
    http = opened["data"]["http"]
    print(f"Opened {opened['data']['name']} on {http}")
    return f"http://{http}"


def tab_for(cdp_http: str, url: str, match: str, exact_url: str | None = None) -> Cdp:
    tabs = get_json(f"{cdp_http}/json/list")
    if exact_url:
        tab = next((t for t in tabs if t.get("type") == "page" and t.get("url", "") == exact_url), None)
    else:
        tab = next((t for t in tabs if t.get("type") == "page" and match in t.get("url", "")), None)
    if not tab:
        tab = put_json(f"{cdp_http}/json/new?{urllib.parse.quote(url, safe='')}")
    cdp = Cdp(tab["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Page.bringToFront")
    return cdp


def wait_for(cdp: Cdp, expression: str, timeout=60, label="condition"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = cdp.js(expression)
            if last:
                return last
        except Exception as exc:
            last = str(exc)
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for {label}: {last}")


def current_site_state(cdp: Cdp):
    return cdp.js(
        r"""
        (() => {
          const text = (document.body && document.body.innerText) || '';
          const lines = text.split(/\n+/).map(x => x.trim()).filter(Boolean);
          const site = lines.find(x => /^(MX|BR|AR|CL|CO|UY)$/.test(x)) || null;
          return {url: location.href, title: document.title, site, text: text.slice(0, 1200)};
        })()
        """
    )


def open_cdp_mercado_backend_page(
    cdp: Cdp,
    url: str,
    *,
    name="",
    max_rate_limit_retries=2,
    retry_wait_seconds=30,
):
    """CDP 入口页限频重试与退出登录检测。"""
    retry_count = 0
    while True:
        cdp.js(f"location.href={json.dumps(url)}; true")
        wait_for(
            cdp,
            "document.readyState === 'complete' || document.readyState === 'interactive'",
            timeout=60,
            label="Mercado backend page",
        )
        state = current_site_state(cdp)
        status = get_mercado_backend_status(
            state={
                "current_url": state.get("url", ""),
                "title": state.get("title", ""),
                "page_text": state.get("text", ""),
            }
        )
        if status == "ready":
            return state
        if status == "logged_out":
            raise RuntimeError(
                f"{name} Mercado 登录态失效，请先完成登录后重试："
                f"{state.get('url', '')}"
            )
        limit_result = process_mercado_rate_limit(
            state={
                "current_url": state.get("url", ""),
                "title": state.get("title", ""),
                "page_text": state.get("text", ""),
            },
            name=name,
            retry_count=retry_count,
            max_retries=max_rate_limit_retries,
            retry_wait_seconds=retry_wait_seconds,
        )
        if limit_result["exhausted"]:
            raise RuntimeError(f"{name} Mercado 限频，重试 {retry_count} 次仍未恢复")
        retry_count = limit_result["retry_count"]


def verify_site(cdp: Cdp, site: str):
    site = site.upper()
    state = current_site_state(cdp)
    country = SITE_NAMES.get(site, site)
    text = state.get("text") or ""
    title = state.get("title") or ""
    current = state.get("site")
    title_matches = country.lower() in title.lower() or country.lower() in text.lower()
    return (current == site) or (f"\n{site}\n" in f"\n{text}\n" and title_matches)


def wait_until_site_switched(cdp: Cdp, site: str, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if verify_site(cdp, site):
            return True
        time.sleep(1)
    return False


def read_infractions_page(cdp: Cdp):
    return cdp.js(
        r"""
        (() => {
          function deepElements(root = document) {
            const out = [];
            const walk = node => {
              const elements = node.querySelectorAll ? Array.from(node.querySelectorAll('*')) : [];
              for (const el of elements) {
                out.push(el);
                if (el.shadowRoot) walk(el.shadowRoot);
              }
            };
            walk(root);
            return out;
          }
          const text = (document.body && document.body.innerText) || '';
          const byClass = deepElements()
            .filter(el => String(el.className || '').includes('infraction-item__id'))
            .map(el => (el.innerText || el.textContent || '').replace(/\D/g, '').trim())
            .filter(x => /^\d{8,12}$/.test(x));
          const byText = (text.match(/#\s*(\d{8,12})/g) || []).map(x => x.replace(/\D/g, ''));
          const ids = [...new Set([...byClass, ...byText])];
          const activePage = (deepElements().find(el => {
            const cls = String(el.className || '').toLowerCase();
            return cls.includes('pagination') && cls.includes('active');
          }) || {}).innerText || '';
          return {
            ids,
            marker: ids.join(','),
            count: ids.length,
            url: location.href,
            activePage: String(activePage).trim(),
            text: text.slice(0, 2500)
          };
        })()
        """
    ) or {"ids": [], "marker": "", "count": 0, "url": "", "activePage": "", "text": ""}


def wait_for_infractions_page(cdp: Cdp, previous_marker: str | None = None, timeout=30):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        wait_for(cdp, "!!document.body", timeout=10, label="infractions body")
        last = read_infractions_page(cdp)
        marker = last.get("marker") or ""
        if marker and marker != (previous_marker or ""):
            return last
        time.sleep(1)
    return last


def click_next_infractions_page(cdp: Cdp):
    return cdp.js(
        r"""
        (() => {
          function deepElements(root = document) {
            const out = [];
            const walk = node => {
              const elements = node.querySelectorAll ? Array.from(node.querySelectorAll('*')) : [];
              for (const el of elements) {
                out.push(el);
                if (el.shadowRoot) walk(el.shadowRoot);
              }
            };
            walk(root);
            return out;
          }
          const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const disabled = el => {
            const cls = String(el.className || '').toLowerCase();
            const parentCls = String(el.closest('li')?.className || '').toLowerCase();
            return el.disabled || el.getAttribute('aria-disabled') === 'true' ||
              cls.includes('disabled') || parentCls.includes('disabled');
          };
          const candidates = deepElements().filter(el => {
            if (!visible(el) || disabled(el)) return false;
            const tag = (el.tagName || '').toLowerCase();
            if (!['a', 'button', 'span', 'li', 'div'].includes(tag)) return false;
            const text = (el.innerText || el.textContent || '').trim();
            const aria = (el.getAttribute('aria-label') || '').trim();
            const title = (el.getAttribute('title') || '').trim();
            const cls = String(el.className || '');
            return /^next$/i.test(text) || /next/i.test(aria) || /next/i.test(title) ||
              (cls.includes('andes-pagination') && /next|arrow/i.test(`${text} ${aria} ${title} ${cls}`));
          });
          const target = candidates.find(el => ['a', 'button'].includes((el.tagName || '').toLowerCase())) ||
            candidates.map(el => el.closest('a,button')).find(Boolean);
          if (!target || disabled(target)) return false;
          target.scrollIntoView({block: 'center', inline: 'center'});
          target.click();
          return true;
        })()
        """
    )


def goto_infractions_offset(cdp: Cdp, offset: int, previous_marker: str | None = None):
    url = f"{INFRACTIONS_URL}?tab=detections&offset={offset}"
    open_cdp_mercado_backend_page(cdp, url)
    return wait_for_infractions_page(cdp, previous_marker=previous_marker, timeout=30)


def switch_site_if_needed(cdp: Cdp, site: str):
    site = site.upper()
    wait_for(cdp, "!!document.body", timeout=30, label="document body")
    if verify_site(cdp, site):
        return

    labels = [site, SITE_NAMES.get(site, site), SITE_REMOTE_VALUES.get(site, "")]
    if site == "MX":
        labels.extend(["Mexico (Direct to consumer)", "México", "MLM"])

    opened = False
    picked = False
    last_pick_info = {}
    for attempt in range(1, 4):
        opened = bool(cdp.js(
            r"""
            (() => {
              function deepElements(root = document) {
                const out = [];
                const walk = node => {
                  const elements = node.querySelectorAll ? Array.from(node.querySelectorAll('*')) : [];
                  for (const el of elements) {
                    out.push(el);
                    if (el.shadowRoot) walk(el.shadowRoot);
                  }
                };
                walk(root);
                return out;
              }
              const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              const textOf = el => [
                el.innerText || '',
                el.textContent || '',
                el.getAttribute('aria-label') || '',
                el.getAttribute('title') || '',
                el.getAttribute('data-value') || ''
              ].join(' ').trim();
              const els = deepElements().filter(vis);
              const current = els.find(el => {
                const t = textOf(el);
                const cls = String(el.className || '');
                return cls.includes('site-switcher') ||
                  cls.includes('nav-header-cbt__site-switcher') ||
                  /^(MX|BR|AR|CL|CO|UY)$/.test(t) ||
                  /select\s+(country|site)|country|site/i.test(t);
              });
              const clickable = current && (current.closest('button,a,[role="button"]') || current);
              if (clickable) {
                clickable.scrollIntoView({block: 'center', inline: 'center'});
                clickable.click();
              }
              return !!clickable;
            })()
            """
        ))
        time.sleep(1 + attempt * 0.5)

        last_pick_info = cdp.js(
            f"""
            (() => {{
              const labels = {json.dumps([x for x in labels if x])}.map(x => String(x).toLowerCase());
              const remote = {json.dumps(SITE_REMOTE_VALUES.get(site, ""))};
              function deepElements(root = document) {{
                const out = [];
                const walk = node => {{
                  const elements = node.querySelectorAll ? Array.from(node.querySelectorAll('*')) : [];
                  for (const el of elements) {{
                    out.push(el);
                    if (el.shadowRoot) walk(el.shadowRoot);
                  }}
                }};
                walk(root);
                return out;
              }}
              const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              const textOf = el => [
                el.innerText || '',
                el.textContent || '',
                el.getAttribute('aria-label') || '',
                el.getAttribute('title') || '',
                el.getAttribute('data-value') || ''
              ].join(' ').trim();
              const all = deepElements();
              const visible = all.filter(vis);
              const exact = visible.find(el => remote && el.getAttribute('data-value') === remote);
              const textTarget = visible.find(el => {{
                const text = textOf(el).toLowerCase();
                return labels.some(label => label && text.includes(label));
              }});
              const rawTarget = exact || textTarget;
              const target = rawTarget && (
                rawTarget.closest('[data-value],button,a,[role="button"],li,div') || rawTarget
              );
              if (target) {{
                target.scrollIntoView({{block: 'center', inline: 'center'}});
                target.click();
                for (const type of ['mousedown', 'mouseup', 'click']) {{
                  target.dispatchEvent(new MouseEvent(type, {{bubbles: true, cancelable: true, view: window}}));
                }}
              }}
              const rect = target ? target.getBoundingClientRect() : null;
              const visibleOptions = visible
                .map(el => textOf(el))
                .filter(Boolean)
                .filter(text => /(Mexico|México|Brazil|Argentina|Chile|Colombia|Uruguay|MLM|MLB|MLA|MLC|MCO|MLU)/i.test(text))
                .slice(0, 20);
              return {{
                picked: !!target,
                targetText: rawTarget ? textOf(rawTarget).slice(0, 180) : '',
                targetValue: rawTarget ? (rawTarget.getAttribute('data-value') || '') : '',
                rect: rect ? {{x: rect.x, y: rect.y, width: rect.width, height: rect.height}} : null,
                visibleOptions
              }};
            }})()
            """
        ) or {}
        picked = bool(last_pick_info.get("picked"))
        rect = last_pick_info.get("rect") or {}
        if picked and rect.get("width") and rect.get("height"):
            mouse_click(cdp, rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
        time.sleep(2)
        if wait_until_site_switched(cdp, site, timeout=8):
            return
        if picked:
            cdp.js("location.reload(); true")
            wait_for(cdp, "document.readyState === 'complete' || document.readyState === 'interactive'", timeout=20, label="site switch reload")
            time.sleep(2)
            if wait_until_site_switched(cdp, site, timeout=5):
                return

    if not opened or not picked:
        state = current_site_state(cdp)
        raise RuntimeError(
            f"Failed to switch site to {site}: opened={opened} picked={picked} "
            f"current={state.get('site')} title={state.get('title')} url={state.get('url')} "
            f"pickInfo={last_pick_info}"
        )
    if not verify_site(cdp, site):
        state = current_site_state(cdp)
        raise RuntimeError(
            f"Site switch verification failed: expected={site} current={state.get('site')} "
            f"title={state.get('title')} url={state.get('url')}"
        )


def collect_infractions(cdp_http: str, site: str):
    cdp = tab_for(cdp_http, INFRACTIONS_URL, "/noindex/pppi/infractions")
    try:
        open_cdp_mercado_backend_page(
            cdp,
            INFRACTIONS_URL + "?tab=detections&offset=0",
        )
        time.sleep(5)
        switch_site_if_needed(cdp, site)
        first_page = goto_infractions_offset(cdp, 0)
        first = cdp.js("({url: location.href, title: document.title, text: (((document.body && document.body.innerText) || '').slice(0, 2500))})")
        if not verify_site(cdp, site):
            state = current_site_state(cdp)
            raise RuntimeError(
                f"Refusing to collect wrong site: expected={site} current={state.get('site')} "
                f"title={state.get('title')} url={state.get('url')}"
            )
        print(first["text"][:500])

        total = cdp.js(
            r"""
            (() => {
              const text = (document.body && document.body.innerText) || '';
              const m = text.match(/(\d+)\s+infringements\s+detected in the platform/i);
              return m ? Number(m[1]) : null;
            })()
            """
        )
        page_size = max(1, first_page.get("count") or 7)
        max_pages = max(1, min(60, ((total or 0) + page_size - 1) // page_size if total else 60))
        all_ids = []
        seen = set()
        data = first_page
        for page in range(1, max_pages + 1):
            if not verify_site(cdp, site):
                state = current_site_state(cdp)
                raise RuntimeError(
                    f"Refusing to collect wrong site on page {page}: expected={site} "
                    f"current={state.get('site')} title={state.get('title')} url={state.get('url')}"
                )

            marker = data.get("marker") or ""
            ids = data.get("ids") or []
            if not ids:
                print(f"page {page}: no ids found; stop collecting")
                break
            if marker in seen:
                print(f"page {page}: repeated page marker {marker}; stop collecting")
                break

            seen.add(marker)
            all_ids.extend([x for x in ids if x not in all_ids])
            print(f"page {page}: {', '.join(ids)}")
            if total and len(all_ids) >= total:
                break

            previous_marker = marker
            next_data = None
            clicked = click_next_infractions_page(cdp)
            if clicked:
                next_data = wait_for_infractions_page(cdp, previous_marker=previous_marker, timeout=30)
                if not next_data or next_data.get("marker") == previous_marker:
                    print(f"page {page}: Next click did not change page; fallback to offset")
                    next_data = None

            if not next_data:
                next_offset = page * page_size
                next_data = goto_infractions_offset(cdp, next_offset, previous_marker=previous_marker)

            if not next_data or not next_data.get("ids") or next_data.get("marker") == previous_marker:
                print(f"page {page}: no next page after click/offset; stop collecting")
                break
            data = next_data
        return first, all_ids
    finally:
        cdp.close()


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def click_visible(cdp: Cdp, label: str):
    return cdp.js(
        f"""
        (() => {{
          const label = {json.dumps(label.lower())};
          const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const el = [...document.querySelectorAll('button,[role="button"],a,div')].find(el => {{
            const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim().toLowerCase();
            return vis(el) && text.includes(label);
          }});
          if (el) {{
            el.scrollIntoView({{block:'center'}});
            el.click();
          }}
          return !!el;
        }})()
        """
    )


def click_by_aria(cdp: Cdp, label: str):
    return cdp.js(
        f"""
        (() => {{
          const label = {json.dumps(label.lower())};
          const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const el = [...document.querySelectorAll('button,[role="button"],a')].find(el => {{
            const aria = (el.getAttribute('aria-label') || '').trim().toLowerCase();
            return vis(el) && aria.includes(label);
          }});
          if (el) {{
            el.scrollIntoView({{block:'center'}});
            el.click();
          }}
          return !!el;
        }})()
        """
    )


def frame_tree(cdp: Cdp):
    result = cdp.call("Page.getFrameTree")
    frames = []

    def walk(node):
        frames.append(node["frame"])
        for child in node.get("childFrames", []) or []:
            walk(child)

    walk(result["frameTree"])
    return frames


def ai_frame_id(cdp: Cdp):
    frame = next(
        (
            f
            for f in frame_tree(cdp)
            if "meli-ai-chat" in f.get("url", "") or "maxwell/new-chat" in f.get("url", "")
        ),
        None,
    )
    return frame["id"] if frame else None


def assistant_context(cdp: Cdp):
    frame_id = ai_frame_id(cdp)
    if not frame_id:
        raise RuntimeError("AI assistant iframe was not found")
    return cdp.call(
        "Page.createIsolatedWorld",
        {"frameId": frame_id, "worldName": "codexAssistant", "grantUniveralAccess": True},
    )["executionContextId"]


def wait_for_ai_input(cdp: Cdp, context_id: int, timeout=20):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = cdp.js(
            f"""
            (() => {{
              {DEEP_JS}
              const input = chatInput();
              if (!input) return null;
              const r = input.getBoundingClientRect();
              return {{ok:true, id:input.id, placeholder:input.getAttribute('placeholder') || '', x:r.x, y:r.y, w:r.width, h:r.height}};
            }})()
            """,
            context_id=context_id,
        )
        if last and last.get("ok"):
            return last
        time.sleep(1)
    raise TimeoutError(f"assistant input not found: {last}")


def ai_recent_chat(cdp: Cdp):
    context_id = assistant_context(cdp)
    return cdp.js(
        f"""
        (() => {{
          {DEEP_JS}
          const rows = deepElements()
            .filter(el => /message-container|message-item|message|bubble/.test(String(el.className || '')) && (el.innerText || '').trim())
            .map(el => (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 600));
          const richRows = [];
          const bodyText = document.body ? (document.body.innerText || '') : '';
          richRows.push(bodyText);
          for (const el of deepElements()) {{
            const text = [
              el.innerText || '',
              el.textContent || '',
              el.getAttribute('aria-label') || '',
              el.getAttribute('title') || ''
            ].join(' ');
            if (/Mexico\\s*\\(Direct\\s+to\\s+consumer\\)/i.test(text) && /Uruguay/i.test(text)) {{
              richRows.push(text);
            }}
          }}
          const menuRows = richRows
            .filter(Boolean)
            .map(text => String(text).replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim())
            .filter(text => /Mexico\\s*\\(Direct\\s+to\\s+consumer\\)/i.test(text) && /Uruguay/i.test(text))
            .map(text => text.slice(0, 1200));
          return [...new Set([...rows, ...menuRows])].slice(-12);
        }})()
        """,
        context_id=context_id,
    ) or []


def contains_site_option_menu(text: str) -> bool:
    lower = re.sub(r"\s+", " ", text or "").lower()
    lower = lower.replace("（", "(").replace("）", ")")
    compact = re.sub(r"[\s。．.、,，:：;；]+", "", lower)
    compact_menu = re.sub(
        r"[\s。．.、,，:：;；]+",
        "",
        "".join(SITE_OPTION_MENU_OPTIONS).lower(),
    )
    if compact_menu in compact:
        return True
    compact_options = [
        re.sub(r"[\s。．.、,，:：;；]+", "", option.lower())
        for option in SITE_OPTION_MENU_OPTIONS
    ]
    return sum(1 for option in compact_options if option in compact) >= 5


def is_site_option_question(text: str) -> bool:
    if contains_site_option_menu(text):
        return True
    lower = (text or "").lower()
    option_markers = [
        "mexico (direct to consumer)",
        "mexico (fulfillment)",
        "brazil",
        "chile",
        "colombia",
        "argentina",
        "uruguay",
    ]
    question_markers = [
        "which country",
        "country",
        "option",
        "对应的是",
        "哪个国家",
        "选项",
        "确认",
    ]
    return any(x in lower for x in option_markers) and any(x in lower for x in question_markers)


def site_option_reply(site: str) -> str:
    return SITE_OPTION_REPLIES.get((site or "MX").upper(), "Mexico (Direct to consumer)")


def maybe_reply_site_option(cdp: Cdp, site: str, window: str = "", timeout=25):
    end = time.time() + timeout
    seen = set()
    while time.time() < end:
        messages = ai_recent_chat(cdp)
        for message in reversed(messages):
            if message in seen:
                continue
            seen.add(message)
            if is_site_option_question(message):
                reply = site_option_reply(site)
                result = send_ai_message(cdp, reply)
                append_chat_log(
                    window,
                    site,
                    "send_site_option",
                    message=reply,
                    response=message,
                    chat=messages,
                    extra={"send_result": result},
                )
                print(f"AI asked site option; replied {reply} ({result})")
                return reply
        time.sleep(3)
    return ""


def open_ai_assistant(cdp_http: str) -> Cdp:
    cdp = tab_for(cdp_http, HELP_URL, "/help", exact_url=HELP_URL)
    open_cdp_mercado_backend_page(cdp, HELP_URL)
    time.sleep(4)
    for _ in range(3):
        if ai_frame_id(cdp):
            return cdp
        click_by_aria(cdp, "Ask the assistant")
        time.sleep(3)
        if ai_frame_id(cdp):
            return cdp
        click_visible(cdp, "Assistant")
        time.sleep(2)
        if ai_frame_id(cdp):
            return cdp
        click_visible(cdp, "Contact us")
        time.sleep(4)
    if not ai_frame_id(cdp):
        raise RuntimeError("AI assistant iframe was not found")
    return cdp


def send_ai_message(cdp: Cdp, message: str):
    context_id = assistant_context(cdp)
    wait_for_ai_input(cdp, context_id)
    focused = cdp.js(
        f"""
        (() => {{
          {DEEP_JS}
          const input = chatInput();
          if (!input) return {{ok:false, reason:'no input'}};
          input.scrollIntoView({{block:'center'}});
          input.focus();
          nativeSetValue(input, '');
          return {{ok:true}};
        }})()
        """,
        context_id=context_id,
    )
    if not focused or not focused.get("ok"):
        raise RuntimeError(f"Could not focus assistant input: {focused}")

    cdp.call("Input.insertText", {"text": message})
    time.sleep(0.6)
    typed = cdp.js(
        f"""
        (() => {{
          {DEEP_JS}
          const input = chatInput();
          const btn = sendButton();
          if (!input) return {{ok:false, reason:'no input after typing'}};
          return {{
            ok:true,
            value: input.value || input.innerText || '',
            buttonDisabled: btn ? (!!btn.disabled || btn.getAttribute('aria-disabled') === 'true') : null
          }};
        }})()
        """,
        context_id=context_id,
    )
    if not typed or not typed.get("value"):
        typed = cdp.js(
            f"""
            (() => {{
              {DEEP_JS}
              const input = chatInput();
              if (!input) return {{ok:false, reason:'fallback no input'}};
              input.focus();
              nativeSetValue(input, {json.dumps(message)});
              return {{ok:true, value: input.value || input.innerText || ''}};
            }})()
            """,
            context_id=context_id,
        )

    sent = cdp.js(
        f"""
        (() => {{
          {DEEP_JS}
          const btn = sendButton();
          if (!btn) return {{sent:false, reason:'no send button'}};
          const disabled = !!btn.disabled || btn.getAttribute('aria-disabled') === 'true';
          if (disabled) return {{sent:false, reason:'send button disabled'}};
          btn.click();
          return {{sent:true, method:'shadow-button'}};
        }})()
        """,
        context_id=context_id,
    )
    if not sent or not sent.get("sent"):
        key(cdp, "keyDown", "Enter", "Enter", 13)
        key(cdp, "keyUp", "Enter", "Enter", 13)
        sent = {"sent": True, "method": "enter-fallback"}
    return {"typed": bool(typed and typed.get("value")), **sent}


def send_ai_groups(cdp: Cdp, groups, prefix: str, site: str = "MX", window: str = ""):
    for idx, group in enumerate(groups, start=1):
        message = f"{PRODUCT_SEPARATOR.join(group)}{AI_APPEAL_SUFFIX}"
        before_chat = ai_recent_chat(cdp)
        result = send_ai_message(cdp, message)
        time.sleep(7)
        chat = ai_recent_chat(cdp)
        append_chat_log(
            window,
            site,
            "send_ai_group",
            message=message,
            chat=chat,
            extra={
                "group_index": idx,
                "group_ids": group,
                "send_result": result,
                "before_chat": before_chat,
            },
        )
        maybe_reply_site_option(cdp, site, window)
        path = OUT_DIR / f"{prefix}_ai_group_{idx}.png"
        cdp.screenshot(path)
        print(f"AI group {idx} sent ({result}): {message}")


def open_human_chat(cdp_http: str) -> Cdp:
    # Prefer already-open direct chat.
    tabs = get_json(f"{cdp_http}/json/list")
    tab = next((t for t in tabs if t.get("type") == "page" and "/help/chat/v2" in t.get("url", "")), None)
    if not tab:
        hub = tab_for(cdp_http, HUB_URL, "/help/hub/30928")
        try:
            open_cdp_mercado_backend_page(hub, HUB_URL)
            time.sleep(4)
            hub.js(
                r"""
                (() => {
                  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                  const btn = [...document.querySelectorAll('button,a,[role="button"]')].find(el => {
                    const t = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim().toLowerCase();
                    return vis(el) && t.startsWith('chat');
                  });
                  if (btn) btn.click();
                  return !!btn;
                })()
                """
            )
            time.sleep(10)
        finally:
            hub.close()
        tabs = get_json(f"{cdp_http}/json/list")
        tab = next((t for t in tabs if t.get("type") == "page" and "/help/chat/v2" in t.get("url", "")), None)
    if not tab:
        tab = put_json(f"{cdp_http}/json/new?{urllib.parse.quote(DIRECT_CHAT_URL, safe='')}")
    cdp = Cdp(tab["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Page.bringToFront")
    open_cdp_mercado_backend_page(cdp, DIRECT_CHAT_URL)
    return cdp


def send_human_group(cdp: Cdp, group, prefix: str, site: str = "", window: str = "", group_index=1):
    message = f"{PRODUCT_SEPARATOR.join(group)}{HUMAN_APPEAL_SUFFIX}"
    result = cdp.js(
        f"""
        (async () => {{
          const msg = {json.dumps(message)};
          const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const input = [...document.querySelectorAll('textarea,input,[contenteditable="true"],div[role="textbox"]')].find(vis);
          if (!input) return {{sent:false, reason:'no input'}};
          input.focus();
          if (input.isContentEditable || input.getAttribute('role') === 'textbox') {{
            input.textContent = msg;
            input.dispatchEvent(new InputEvent('input', {{bubbles:true,inputType:'insertText',data:msg}}));
          }} else {{
            const setter = Object.getOwnPropertyDescriptor(input.constructor.prototype, 'value')?.set;
            if (setter) setter.call(input, msg); else input.value = msg;
            input.dispatchEvent(new Event('input', {{bubbles:true}}));
            input.dispatchEvent(new Event('change', {{bubbles:true}}));
          }}
          await new Promise(r => setTimeout(r, 500));
          input.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter', code:'Enter', bubbles:true}}));
          input.dispatchEvent(new KeyboardEvent('keyup', {{key:'Enter', code:'Enter', bubbles:true}}));
          return {{sent:true}};
        }})()
        """
    )
    time.sleep(5)
    path = OUT_DIR / f"{prefix}_human_group_{group_index}.png"
    cdp.screenshot(path)
    append_chat_log(
        window or prefix,
        site,
        "send_human_group",
        message=message,
        extra={"group_index": group_index, "group_ids": group, "send_result": result},
    )
    print(f"Human group {group_index} result: {result}; {message}")


def mouse_click(cdp: Cdp, x, y):
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
    cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
    mercado_click_cooldown()


def key(cdp: Cdp, event_type, key_name, code, vk, modifiers=0):
    cdp.call(
        "Input.dispatchKeyEvent",
        {
            "type": event_type,
            "key": key_name,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": modifiers,
        },
    )


def main():
    parser = argparse.ArgumentParser(description="Mercado Libre infringement appeal runner.")
    parser.add_argument("--window", required=True, help="BitBrowser window name, e.g. 健步如飞（fti）")
    parser.add_argument("--site", required=True, help="Site code, e.g. MX, BR, AR")
    parser.add_argument("--mode", choices=["collect", "ai", "human", "both"], default="collect")
    parser.add_argument("--human-group-index", type=int, default=1, help="1-based human group to send; only one group is sent each run.")
    args = parser.parse_args()

    site = args.site.upper()
    prefix = f"{args.window}_{site}".replace("（", "_").replace("）", "").replace(" ", "_")
    cdp_http = open_bitbrowser(args.window)
    first, ids = collect_infractions(cdp_http, site)
    ai_groups = list(chunks(ids, 3))
    human_groups = list(chunks(ids, 10))

    payload = {"window": args.window, "site": site, "first": first, "ids": ids, "aiGroups": ai_groups, "humanGroups": human_groups}
    (OUT_DIR / f"{prefix}_ids.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / f"{prefix}_summary.md").write_text(
        f"# {args.window} {site} 侵权编号\n\n"
        f"Title: {first['title']}\n\n"
        f"Sample:\n\n{first['text']}\n\n"
        f"IDs ({len(ids)}): {', '.join(ids)}\n\n"
        f"AI groups: {len(ai_groups)}; human groups: {len(human_groups)}.\n",
        encoding="utf-8",
    )
    print(f"Collected {len(ids)} IDs for {args.window} {site}.")

    if args.mode in ("ai", "both"):
        ai = open_ai_assistant(cdp_http)
        try:
            send_ai_groups(ai, ai_groups, prefix, site, args.window)
        finally:
            ai.close()

    if args.mode in ("human", "both") and human_groups:
        idx = max(1, args.human_group_index) - 1
        human = open_human_chat(cdp_http)
        try:
            send_human_group(human, human_groups[idx], prefix, site, args.window, idx + 1)
        finally:
            human.close()


if __name__ == "__main__":
    main()
