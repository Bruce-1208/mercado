import argparse
import base64
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import websocket


BIT_API = "http://127.0.0.1:54345"
DEFAULT_WINDOW = "\u8dc3\u9a6c\u626c\u97ad\uff08fti\uff09"
DEFAULT_CDP = "http://127.0.0.1:60012"
HELP_URL = "https://global-selling.mercadolibre.com/help"
OUT = Path("E:\\360MoveData\\Users\\Admin\\Documents\\\u7f8e\u5ba2\u591a\u81ea\u52a8\u5316")
LOG = OUT / "yuema_ai_stable_loop_log.md"
PRODUCT_SEPARATOR = "\u3001"
APPEAL_SUFFIX = (
    "\u8fd9\u51e0\u4e2a\u4ea7\u54c1\u662f\u901a\u7528\u54c1\u724c\uff0c"
    "\u5e76\u975e\u4fb5\u6743\u4ea7\u54c1\uff0c\u8fd9\u662f\u7cfb\u7edf\u8bef\u5224\uff0c"
    "\u9ebb\u70e6\u5e2e\u6211\u5220\u9664\u4fb5\u6743\u8bb0\u5f55\uff0c\u8c22\u8c22"
)

GROUPS = [
    ["5432591422", "2736563219", "5433043278"],
    ["5431591036", "5431668770", "5432253796"],
    ["5431616672", "2826355451", "5090038906"],
    ["2691725929", "4811448042", "4788933476"],
    ["2826400983", "2787485991", "2782013381"],
    ["5170326532", "2872391137", "5204725252"],
    ["2872391079", "2872391011", "2872380655"],
    ["2870098759", "5199395742", "2870050781"],
    ["2870045999", "2870045909", "2870045655"],
    ["5199257236", "2870042347", "2870042321"],
    ["2870042223", "5199197546", "5199196500"],
    ["5199215060", "2747383539", "2747331653"],
    ["2747370331", "2826453319", "2826429917"],
    ["2826388241", "2826388199", "2826388131"],
    ["4586612416", "4863203426", "4811149116"],
    ["2654806805", "4601507614", "2654815957"],
    ["4653335686", "4605014862", "4587564832"],
    ["4862605974", "2665571549", "2641211853"],
    ["2736575857", "5004877686", "4571260806"],
    ["4990453076", "4990566930", "2771676271"],
    ["2641211821", "4948187908", "2678401897"],
    ["4808930954", "2661960953", "2716631901"],
    ["2711733759", "4605190062", "2641274057"],
]


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
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.i = 1

    def call(self, method, params=None, timeout=30):
        msg_id = self.i
        self.i += 1
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
        raise TimeoutError(method)

    def js(self, expression, context_id=None, timeout=30):
        params = {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": True,
        }
        if context_id is not None:
            params["contextId"] = context_id
        res = self.call("Runtime.evaluate", params, timeout=timeout)
        if "exceptionDetails" in res:
            raise RuntimeError(res["exceptionDetails"].get("text", "Runtime.evaluate failed"))
        return res.get("result", {}).get("value")

    def screenshot(self, path):
        data = self.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})["data"]
        path.write_bytes(base64.b64decode(data))

    def close(self):
        self.ws.close()


def log(message):
    OUT.mkdir(parents=True, exist_ok=True)
    if not LOG.exists():
        LOG.write_text("# \u8dc3\u9a6c\u626c\u97ad MX AI \u7a33\u5b9a\u5faa\u73af\u8bb0\u5f55\n", "utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    with LOG.open("a", encoding="utf-8") as f:
        f.write("\n" + line + "\n")
    print(line, flush=True)


def post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def put_json(url):
    req = urllib.request.Request(url, data=b"", method="PUT")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def open_bitbrowser(window_name):
    listing = post_json(f"{BIT_API}/browser/list", {"page": 0, "pageSize": 200})
    matches = [b for b in listing["data"]["list"] if window_name in b.get("name", "")]
    if not matches:
        raise RuntimeError(f"BitBrowser window not found: {window_name}")
    opened = post_json(f"{BIT_API}/browser/open", {"id": matches[0]["id"]})
    if not opened.get("success"):
        raise RuntimeError(f"Failed to open BitBrowser window: {opened}")
    return f"http://{opened['data']['http']}"


def get(path, cdp_http):
    return get_json(cdp_http + path)


def put(url, cdp_http):
    return put_json(cdp_http + "/json/new?" + urllib.parse.quote(url, safe=""))


def help_tab(cdp_http):
    tabs = get("/json/list", cdp_http)
    help_tabs = [t for t in tabs if t.get("type") == "page" and t.get("url") == HELP_URL]
    tab = help_tabs[0] if help_tabs else None
    if not tab:
        tab = put(HELP_URL, cdp_http)

    c = Cdp(tab["webSocketDebuggerUrl"])
    c.call("Page.enable")
    c.call("Runtime.enable")
    c.call("Page.bringToFront")
    if tab.get("url") != HELP_URL:
        c.js(f"location.href={json.dumps(HELP_URL)}")
        time.sleep(4)
    return c


def click_visible(c, label):
    return c.js(
        f"""
        (() => {{
          const label = {json.dumps(label.lower())};
          const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const el = [...document.querySelectorAll('button,[role="button"],a,div')].find(el => {{
            const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim().toLowerCase();
            return visible(el) && text.includes(label);
          }});
          if (el) {{
            el.scrollIntoView({{block:'center'}});
            el.click();
          }}
          return !!el;
        }})()
        """
    )


def click_by_aria(c, label):
    return c.js(
        f"""
        (() => {{
          const label = {json.dumps(label.lower())};
          const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const el = [...document.querySelectorAll('button,[role="button"],a')].find(el => {{
            const aria = (el.getAttribute('aria-label') || '').trim().toLowerCase();
            return visible(el) && aria.includes(label);
          }});
          if (el) {{
            el.scrollIntoView({{block:'center'}});
            el.click();
          }}
          return !!el;
        }})()
        """
    )


def frame_tree(c):
    result = c.call("Page.getFrameTree")
    frames = []

    def walk(node):
        frames.append(node["frame"])
        for child in node.get("childFrames", []) or []:
            walk(child)

    walk(result["frameTree"])
    return frames


def ai_frame_id(c):
    frames = frame_tree(c)
    frame = next((f for f in frames if "meli-ai-chat" in f.get("url", "")), None)
    return frame["id"] if frame else None


def ensure_assistant_open(c):
    for _ in range(3):
        if ai_frame_id(c):
            return
        click_by_aria(c, "Ask the assistant")
        time.sleep(3)
        if ai_frame_id(c):
            return
        click_visible(c, "Assistant")
        time.sleep(2)
        if ai_frame_id(c):
            return
        click_visible(c, "Contact us")
        time.sleep(4)
    if not ai_frame_id(c):
        raise RuntimeError("AI assistant iframe was not found")


def assistant_context(c):
    frame_id = ai_frame_id(c)
    if not frame_id:
        raise RuntimeError("AI assistant iframe was not found")
    return c.call(
        "Page.createIsolatedWorld",
        {"frameId": frame_id, "worldName": "codexAssistant", "grantUniveralAccess": True},
    )["executionContextId"]


def wait_for_input(c, context_id, timeout=20):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = c.js(
            f"""
            (() => {{
              {DEEP_JS}
              const input = chatInput();
              const btn = sendButton();
              if (!input) return null;
              const r = input.getBoundingClientRect();
              return {{
                ok: true,
                input: {{id: input.id, placeholder: input.getAttribute('placeholder') || '', x: r.x, y: r.y, w: r.width, h: r.height}},
                button: btn ? {{disabled: !!btn.disabled || btn.getAttribute('aria-disabled') === 'true'}} : null
              }};
            }})()
            """,
            context_id=context_id,
        )
        if last and last.get("ok"):
            return last
        time.sleep(1)
    raise TimeoutError(f"assistant input not found: {last}")


def recent_chat(c, context_id):
    return c.js(
        f"""
        (() => {{
          {DEEP_JS}
          const rows = deepElements()
            .filter(el => /message-container|message-item/.test(String(el.className || '')) && (el.innerText || '').trim())
            .map(el => (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 500));
          return [...new Set(rows)].slice(-6);
        }})()
        """,
        context_id=context_id,
    )


def send_group(c, group_no, group, round_no):
    context_id = assistant_context(c)
    wait_for_input(c, context_id)
    message = f"{PRODUCT_SEPARATOR.join(group)}{APPEAL_SUFFIX}"

    focus = c.js(
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
    if not focus or not focus.get("ok"):
        raise RuntimeError(f"Could not focus assistant input: {focus}")

    c.call("Input.insertText", {"text": message})
    time.sleep(0.6)

    typed = c.js(
        f"""
        (() => {{
          {DEEP_JS}
          const input = chatInput();
          const btn = sendButton();
          if (!input) return {{ok:false, reason:'no input after typing'}};
          return {{
            ok: true,
            value: input.value || input.innerText || '',
            buttonDisabled: btn ? (!!btn.disabled || btn.getAttribute('aria-disabled') === 'true') : null
          }};
        }})()
        """,
        context_id=context_id,
    )
    if not typed or not typed.get("value"):
        typed = c.js(
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

    sent = c.js(
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
        c.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
        c.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
        sent = {"sent": True, "method": "enter-fallback"}

    time.sleep(8)
    shot = OUT / f"yuema_ai_{datetime.now().strftime('%Y%m%d_%H%M%S')}_r{round_no}_g{group_no}.png"
    c.screenshot(shot)
    chat = recent_chat(c, context_id)
    log(
        "SENT "
        + f"round={round_no} group={group_no} ids={','.join(group)} method={sent.get('method')} "
        + f"typed={bool(typed and typed.get('value'))} screenshot={shot}"
    )
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"\nMessage: {message}\nRecent chat: {json.dumps(chat, ensure_ascii=False)}\n")


def run_once(cdp_http, start, rounds, continuous, delay, round_delay, max_groups):
    c = help_tab(cdp_http)
    try:
        ensure_assistant_open(c)
        first_start = max(1, start)
        round_no = 1
        sent_count = 0
        while continuous or round_no <= rounds:
            current_start = first_start if round_no == 1 else 1
            for idx, group in enumerate(GROUPS, start=1):
                if idx < current_start:
                    continue
                send_group(c, idx, group, round_no)
                sent_count += 1
                if max_groups and sent_count >= max_groups:
                    log(f"MAX GROUPS REACHED max_groups={max_groups}")
                    return
                time.sleep(delay)
            round_no += 1
            if continuous:
                log(f"ROUND COMPLETE round={round_no - 1}; sleeping {round_delay}s before next round")
                time.sleep(round_delay)
    finally:
        c.close()


def main():
    parser = argparse.ArgumentParser(description="Stable MX AI appeal loop for 跃马扬鞭（fti）.")
    parser.add_argument("--window", default=DEFAULT_WINDOW)
    parser.add_argument("--cdp", default="", help=f"DevTools HTTP endpoint. Defaults to BitBrowser lookup, fallback {DEFAULT_CDP}.")
    parser.add_argument("--start", type=int, default=1, help="First group to send in the first round.")
    parser.add_argument("--rounds", type=int, default=1, help="Finite rounds when --continuous is not set.")
    parser.add_argument("--continuous", action="store_true", help="Keep cycling through all MX groups until stopped.")
    parser.add_argument("--delay", type=float, default=15, help="Seconds between groups.")
    parser.add_argument("--round-delay", type=float, default=60, help="Seconds between full rounds in continuous mode.")
    parser.add_argument("--retry-delay", type=float, default=30, help="Seconds before reconnecting after an error.")
    parser.add_argument("--max-groups", type=int, default=0, help="Optional safety cap for test runs.")
    args = parser.parse_args()

    log(
        f"START window={args.window} start={args.start} rounds={args.rounds} "
        f"continuous={args.continuous} delay={args.delay}"
    )

    while True:
        try:
            cdp_http = args.cdp or open_bitbrowser(args.window)
            if not cdp_http:
                cdp_http = DEFAULT_CDP
            run_once(cdp_http, args.start, args.rounds, args.continuous, args.delay, args.round_delay, args.max_groups)
            break
        except Exception as exc:
            log(f"ERROR {type(exc).__name__}: {exc}")
            if not args.continuous:
                raise
            time.sleep(args.retry_delay)
            args.start = 1

    log("STOP")


if __name__ == "__main__":
    main()
