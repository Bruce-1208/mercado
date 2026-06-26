import argparse
import base64
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

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
}


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
        return result.get("result", {}).get("value")

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
    listing = post_json(f"{BIT_API}/browser/list", {"page": 0, "pageSize": 200})
    browsers = listing["data"]["list"]
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


def switch_site_if_needed(cdp: Cdp, site: str):
    site = site.upper()
    wait_for(cdp, "!!document.body", timeout=30, label="document body")
    text = cdp.js("(document.body && document.body.innerText) || ''") or ""
    if f"\n{site}\n" in text or SITE_NAMES.get(site, site) in text:
        return

    cdp.js(
        r"""
        (() => {
          const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const els = [...document.querySelectorAll('button,a,[role="button"],div,span')].filter(vis);
          const current = els.find(el => {
            const t = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();
            return /^(MX|BR|AR|CL|CO)$/.test(t);
          });
          if (current) current.click();
          return !!current;
        })()
        """
    )
    time.sleep(1.5)
    picked = cdp.js(
        f"""
        (() => {{
          const site = {json.dumps(site)};
          const name = {json.dumps(SITE_NAMES.get(site, site))};
          const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const els = [...document.querySelectorAll('button,a,[role="button"],li,div,span')].filter(vis);
          const target = els.find(el => {{
            const t = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();
            return t === site || t.includes(name);
          }});
          if (target) target.click();
          return !!target;
        }})()
        """
    )
    time.sleep(5)
    if not picked:
        print(f"Warning: could not find a visible site switch option for {site}; continuing with current site.")


def collect_infractions(cdp_http: str, site: str):
    cdp = tab_for(cdp_http, INFRACTIONS_URL, "/noindex/pppi/infractions")
    try:
        cdp.js(f"location.href={json.dumps(INFRACTIONS_URL)}; true")
        wait_for(cdp, "document.readyState === 'complete' || document.readyState === 'interactive'", label="infractions load")
        time.sleep(5)
        switch_site_if_needed(cdp, site)
        first = cdp.js("({url: location.href, title: document.title, text: (((document.body && document.body.innerText) || '').slice(0, 2500))})")
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
        page_size = 7
        max_pages = max(1, min(60, ((total or 0) + page_size - 1) // page_size if total else 60))
        all_ids = []
        seen = set()
        for page in range(1, max_pages + 1):
            offset = (page - 1) * page_size
            url = f"{INFRACTIONS_URL}?tab=detections&offset={offset}"
            cdp.js(f"if (location.href !== {json.dumps(url)}) location.href={json.dumps(url)}; true")
            wait_for(cdp, "!!document.body", timeout=30, label=f"page {page} body")
            time.sleep(2)
            data = cdp.js(
                r"""
                (() => {
                  const text = (document.body && document.body.innerText) || '';
                  const ids = [...new Set((text.match(/#\s*(\d{8,12})/g) || []).map(x => x.replace(/\D/g, '')))];
                  return { ids, marker: ids.join(',') };
                })()
                """
            )
            if not data["ids"] or data["marker"] in seen:
                break
            seen.add(data["marker"])
            all_ids.extend([x for x in data["ids"] if x not in all_ids])
            print(f"page {page}: {', '.join(data['ids'])}")
            if total and len(all_ids) >= total:
                break
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
    frame = next((f for f in frame_tree(cdp) if "meli-ai-chat" in f.get("url", "")), None)
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


def open_ai_assistant(cdp_http: str) -> Cdp:
    cdp = tab_for(cdp_http, HELP_URL, "/help", exact_url=HELP_URL)
    cdp.js(f"if (location.href !== {json.dumps(HELP_URL)}) location.href={json.dumps(HELP_URL)}; true")
    wait_for(cdp, "document.readyState === 'complete' || document.readyState === 'interactive'", label="help load")
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


def send_ai_groups(cdp: Cdp, groups, prefix: str):
    for idx, group in enumerate(groups, start=1):
        message = f"{PRODUCT_SEPARATOR.join(group)}{AI_APPEAL_SUFFIX}"
        result = send_ai_message(cdp, message)
        time.sleep(7)
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
            hub.js(f"if (!location.href.includes('/help/hub/30928')) location.href={json.dumps(HUB_URL)}; true")
            wait_for(hub, "document.readyState === 'complete' || document.readyState === 'interactive'", label="hub load")
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
    return cdp


def send_human_group(cdp: Cdp, group, prefix: str, group_index=1):
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
    print(f"Human group {group_index} result: {result}; {message}")


def mouse_click(cdp: Cdp, x, y):
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
    cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})


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
            send_ai_groups(ai, ai_groups, prefix)
        finally:
            ai.close()

    if args.mode in ("human", "both") and human_groups:
        idx = max(1, args.human_group_index) - 1
        human = open_human_chat(cdp_http)
        try:
            send_human_group(human, human_groups[idx], prefix, idx + 1)
        finally:
            human.close()


if __name__ == "__main__":
    main()
