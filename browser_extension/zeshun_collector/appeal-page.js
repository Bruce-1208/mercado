"use strict";

// This function is injected into Mercado's MAIN world. It has no dependency on
// the extension global and never receives console credentials or AI API keys.
async function mercadoAppealPageAction(command, args = {}) {
  const supportedHost = host => /(^|\.)(mercadolibre\.com(?:\.(?:mx|br|ar|co|uy))?|mercadolivre\.com\.br|mercadolibre\.cl)$/i.test(host);
  if (location.protocol !== "https:" || !supportedHost(location.hostname)) return {unsupported: true};
  const norm = value => String(value || "").replace(/\s+/g, " ").trim();
  const plain = value => norm(value).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const all = (root = document) => {
    const out = [];
    for (const el of root.querySelectorAll("*")) {
      out.push(el);
      if (el.shadowRoot) out.push(...all(el.shadowRoot));
    }
    return out;
  };
  const visible = el => {
    const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const enabled = el => visible(el) && !el.disabled && !el.readOnly && el.getAttribute("aria-disabled") !== "true";
  const label = el => norm([el.innerText, el.getAttribute("aria-label"), el.getAttribute("title"), el.getAttribute("placeholder"), el.getAttribute("aria-placeholder")].join(" "));
  const nodes = all();
  const clickable = el => el.matches("button,a,[role='button'],[role='link']") && enabled(el);
  const siteIds = {MX: "MLM", BR: "MLB", CL: "MLC", CO: "MCO", AR: "MLA", UY: "MLU"};
  const normalizeSite = value => {
    const upper = String(value || "").toUpperCase().replace(/-REMOTE$/, "");
    if (Object.values(siteIds).includes(upper)) return upper;
    if (siteIds[upper]) return siteIds[upper];
    const names = {MLM: /mexico|méxico|墨西哥/i, MLB: /brazil|brasil|巴西/i, MLC: /chile|智利/i, MCO: /colombia|哥伦比亚/i, MLA: /argentina|阿根廷/i, MLU: /uruguay|乌拉圭/i};
    return Object.keys(names).find(id => names[id].test(value || "")) || "";
  };
  function context() {
    let rendering = window._n?.ctx?.r;
    if (!rendering?.appProps) {
      // The rendering script can outlive the Nordic runtime global. Decode its
      // JSON assignment without evaluating scripts or scanning article text.
      const source = document.getElementById("__NORDIC_RENDERING_CTX__")?.textContent || "";
      const assignment = /\b_n\.ctx\.r\s*=\s*/.exec(source);
      if (assignment) {
        const start = assignment.index + assignment[0].length;
        let depth = 0, quoted = false, escaped = false;
        for (let i = start; i < source.length; i++) {
          const char = source[i];
          if (escaped) { escaped = false; continue; }
          if (quoted && char === "\\") { escaped = true; continue; }
          if (char === '"') quoted = !quoted;
          if (!quoted && char === "{") depth++;
          if (!quoted && char === "}" && --depth === 0) {
            try { rendering = JSON.parse(source.slice(start, i + 1)); } catch (_) {}
            break;
          }
        }
      }
    }
    const roots = [rendering?.appProps?.pageProps, window.__NEXT_DATA__?.props?.pageProps,
      window.__PRELOADED_STATE__, window.__INITIAL_STATE__].filter(Boolean);
    // Only inspect explicit logged-in user fields, never arbitrary seller IDs
    // from products displayed in the page.
    const identity = roots.map(root => ({
      id: root.userId ?? root.loggedUserId ?? root.auth?.user?.id ?? root.user?.id ?? root.loggedUser?.id,
      nickname: root.nickname || root.user?.nickname || root.loggedUser?.nickname || "",
      guest: root.isGuest === true && String(root.userId) === "-1" && root.siteId === "CBT",
      operating: root.operating_site_id || root.operatingSiteId || (root.siteId !== "CBT" ? root.siteId : "")
    }));
    const positive = identity.filter(user => /^\d+$/.test(String(user.id || "")) && Number(user.id) > 0);
    const sellerIds = [...new Set(positive.map(user => String(user.id)))];
    const current = nodes.find(el => el.matches(".nav-header-cbt__current-site,.nav-header-cbt__site-switcher-value"));
    const selected = nodes.find(el => el.matches("#nav-header-cbt__switcher [class*='option-selected'],#nav-header-cbt__switcher [data-value][class*='selected'],[data-value$='-remote'][class*='selected'],[data-value$='-full'][class*='selected'],[data-value$='-fulfillment'][class*='selected']"));
    const flag = nodes.find(el => el.matches(".nav-header-cbt__current-flag img, .nav-header-cbt__current img"));
    const selectedRemote = selected?.getAttribute("data-value") || selected?.querySelector("[data-value]")?.getAttribute("data-value") || "";
    const siteSignals = [selectedRemote, current?.innerText,
      flag?.getAttribute("alt"), ...identity.map(user => user.operating)].map(normalizeSite).filter(Boolean);
    const uniqueSites = [...new Set(siteSignals)];
    let cookieSite = "", cookieRemote = "";
    try { cookieRemote = decodeURIComponent(document.cookie.match(/(?:^|;\s*)cbtSiteId=([^;]+)/)?.[1] || ""); cookieSite = normalizeSite(cookieRemote); } catch (_) {}
    const available = nodes.filter(el => el.matches("#nav-header-cbt__switcher [data-value], [class*='site-switcher'] [data-value]"))
      .map(el => el.getAttribute("data-value")).filter(Boolean);
    const loginForm = nodes.some(el => visible(el) && el.matches("input[type='password'],#login_user_id,input[name='login_user_id']"));
    const text = plain(document.body?.innerText || "");
    const loggedOut = identity.some(user => user.guest) || /\/(?:login|lgz|legacy-user)(?:\/|$)/i.test(location.pathname) || loginForm;
    return {seller_id: sellerIds.length === 1 ? sellerIds[0] : "", identity_conflict: sellerIds.length > 1,
      nickname: positive[0]?.nickname || "", site_id: uniqueSites.length === 1 ? uniqueSites[0] : (!uniqueSites.length ? cookieSite : ""),
      site_conflict: uniqueSites.length > 1 || /-(?:full|fulfillment)/i.test(selectedRemote || cookieRemote),
      selected_remote: selectedRemote, cookie_remote: cookieRemote, available, logged_out: loggedOut,
      rate_limited: text.includes("hubo un error accediendo a esta pagina"), url: location.href, title: document.title};
  }
  if (command === "context") return context();
  if (command === "select_site") {
    const target = normalizeSite(args.site_id);
    if (!target) return {error: "无效站点"};
    const before = context();
    if (before.site_id === target && !before.site_conflict) return {already: true};
    const option = nodes.find(el => el.getAttribute("data-value") === `${target}-remote` && enabled(el));
    if (option) { option.click(); return {clicked: true}; }
    const switcher = nodes.find(el => clickable(el) && (el.matches("#nav-header-cbt__site-switcher-toggle,.nav-header-cbt__site-switcher-trigger,.nav-header-cbt__site-switcher") ||
      /select country|select site|选择国家|选择站点/i.test(label(el))));
    if (switcher) { switcher.click(); return {opened: true}; }
    return {error: "未找到当前店铺站点切换器"};
  }
  const assistantShell = nodes.find(el => el.id === "sa-assistant-chat");
  const shell = args.mode !== "人工客服" ? assistantShell : null;
  const aiFrame = /meli-ai-chat|maxwell(?:\/|%2f)new-chat/i.test(location.href);
  const frameChat = (args.mode !== "人工客服" && aiFrame) || /\/help\/chat|\/chat\/v2/i.test(location.href);
  const humanChat = args.mode === "人工客服" && /\/help(?:\/|$)|\/chat(?:\/|$)/i.test(location.pathname);
  const assistantNodes = args.mode === "人工客服" && assistantShell ? new Set([assistantShell, ...all(assistantShell)]) : new Set();
  const chatScope = shell ? all(shell) : (frameChat || humanChat ? nodes.filter(el => !assistantNodes.has(el)) : []);
  const input = chatScope.filter(el => enabled(el) && el.matches("textarea,input,[contenteditable='true'],[role='textbox']"))
    .filter(el => el.id === "chat-input" || /chat message|ask me|ask the assistant|escribe|digite|message|write your|question|输入|消息|问题/i.test(label(el)) ||
      (el.isContentEditable && el.getAttribute("role") === "textbox"))
    .sort((a, b) => {
      const score = el => (el.id === "chat-input" ? 10000 : 0) + (el.isContentEditable ? 3500 : 0) + el.getBoundingClientRect().bottom;
      return score(b) - score(a);
    })[0];
  const assistant = ".message-item--assistant,.message-item--agent,.chat-ui-message-bubble--from-agent,[class*='message-bubble--from-agent'],[data-role='assistant'],[data-role='agent'],[data-author='assistant'],[data-author='agent'],[data-sender='assistant'],[data-sender='agent'],[data-message-author='assistant'],[data-message-author='agent'],.assistant-message,.agent-message";
  const user = ".message-item--user,.message-item--seller,.chat-ui-message-bubble--from-user,[class*='message-bubble--from-user'],[data-role='user'],[data-role='seller'],[data-author='user'],[data-author='seller'],[data-sender='user'],[data-sender='seller'],[data-message-author='user'],[data-message-author='seller'],.user-message,.seller-message";
  function snapshot() {
    if (!window.__zeshunAppealMessageIdentity) window.__zeshunAppealMessageIdentity = {epoch: `${Date.now()}-${Math.random()}`, ids: new WeakMap(), next: 1};
    const identity = window.__zeshunAppealMessageIdentity;
    const messages = chatScope.filter(el => visible(el) && (el.matches(user) || el.matches(assistant)))
      .filter(el => !all(el).some(child => visible(child) && (child.matches(user) || child.matches(assistant))))
      .map(el => {
        if (!identity.ids.has(el)) identity.ids.set(el, String(identity.next++));
        return {id: el.getAttribute("data-message-id") || el.getAttribute("data-id") || el.id || identity.ids.get(el),
          role: el.matches(user) ? "user" : "assistant", text: norm(el.innerText)};
      }).filter(message => message.text);
    return {epoch: identity.epoch, conversation_id: new URL(location.href).searchParams.get("conversation_id") || "", messages,
      busy: chatScope.some(el => visible(el) && el.matches("[aria-busy='true'],.thinking-indicator,[class*='typing-indicator']"))};
  }
  if (command === "probe" || command === "snapshot") return {ready: Boolean(input), chat: Boolean(chatScope.length), url: location.href, snapshot: snapshot()};
  if (command === "open_chat") {
    if (input) return {ready: true};
    const entry = nodes.find(el => clickable(el) && (args.mode === "人工客服"
      ? /we.?ll send you a message|chat with|人工客服/i.test(label(el))
      : el.id === "sa-icon-button-wrapper" || el.getAttribute("aria-controls") === "sa-assistant-chat" || /ai assistant|ask the assistant|asistente|assistente|智能助手|助手/i.test(label(el))));
    if (entry) { entry.click(); return {clicked: true}; }
    const contact = nodes.find(el => clickable(el) && /contact us|contactanos|contactenos|fale conosco|联系我们/i.test(label(el)));
    if (contact && args.allow_contact) { contact.click(); return {clicked: true}; }
    return {ready: false};
  }
  if (command === "new_conversation") {
    const entry = chatScope.find(el => clickable(el) && /new conversation|new chat|iniciar otra consulta|nova conversa|发起新咨询|新的对话/i.test(label(el)));
    const before = snapshot();
    if (!entry && aiFrame && args.mode !== "人工客服") {
      const url = new URL(location.href);
      url.pathname = "/maxwell/new-chat";
      url.searchParams.delete("conversation_id"); url.searchParams.delete("lifecycle"); url.hash = "";
      for (const [key, value] of Object.entries({flavor: "seller-assistant", hideHeader: "true", origin: "sa", andes_ui: "legacy", customLocale: "en-US", _new_conversation_ts: String(Date.now())})) url.searchParams.set(key, value);
      location.assign(url.href);
      return {clicked: true, before};
    }
    if (!entry) return {clicked: false};
    entry.click();
    return {clicked: true, before};
  }
  if (command === "fill") {
    if (!input) return {error: "没有找到客服输入框"};
    const value = String(args.message || "");
    input.scrollIntoView({block: "center", inline: "center"}); input.focus();
    if (input.isContentEditable || input.getAttribute("contenteditable") === "true") input.textContent = value;
    else {
      const proto = input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(input, value); else input.value = value;
    }
    input.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: value}));
    input.dispatchEvent(new Event("change", {bubbles: true}));
    return {filled: norm(input.value ?? input.textContent) === norm(value), snapshot: snapshot()};
  }
  if (command === "send") {
    // The worker records a durable in-flight marker BEFORE invoking this command.
    // Do not fall back to a second click or Enter after a possible submission.
    if (!input || norm(input.value ?? input.textContent) !== norm(args.message)) return {clicked: false, error: "发送前输入内容校验失败"};
    const button = chatScope.filter(el => clickable(el) && (/send|enviar|发送|提交/i.test(label(el)) ||
      String(el.className).includes("new-chat-input__right-button") || el.type === "submit"))
      .sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)[0];
    if (!button) return {clicked: false, error: "没有找到客服发送按钮"};
    const before = snapshot(); button.click();
    return {clicked: true, acknowledged: true, send_method: "button_click", snapshot: before};
  }
  if (command === "metric_link" || command === "request_delay_report") {
    const aliases = {投诉: /complaints?|claims?|投诉|客诉/i, 取消率: /cancell?ations?|cancelled orders|canceled orders|取消/i, 延误: /non.compliant shipments|late shipments|delayed shipments|shipping delays|handling time|延误|延迟发货/i};
    const titles = nodes.filter(el => String(el.className).includes("variable__title"));
    const title = titles.find(el => aliases[args.form]?.test(label(el)));
    if (!title) return {error: `没有找到${args.form}指标卡片`};
    const card = title.closest(".andes-card") || title.parentElement?.closest("[class*='variable']") || title.parentElement;
    const candidates = [card, ...all(card)];
    const link = candidates.find(el => clickable(el) && (args.form === "延误"
      ? /download affected orders|下载受影响|下载.*订单/i.test(label(el))
      : /review in metrics|view in metrics|see metrics|ver en metricas|revisar en metricas|查看.*指标/i.test(plain(label(el))) || /\/metrics(?:[/?#]|$)/i.test(el.getAttribute("href") || "")));
    const zero = /(?:^|\s)0(?:[.,]0+)?\s*%/.test(norm(card.innerText));
    if (!link) return zero ? {empty: true} : {error: `${args.form}指标没有可读取的订单入口`};
    if (command === "request_delay_report") {
      const requestedAt = Date.now();
      link.click();
      return {clicked: true, requested_at: requestedAt};
    }
    const href = link.getAttribute("href");
    if (href && href !== "#" && !href.startsWith("javascript:")) {
      const url = new URL(href, location.href);
      if (url.origin !== location.origin) return {error: "订单报表入口不属于当前店铺同源页面"};
      return {url: url.href};
    }
    if (args.form !== "延误") {
      // Let Mercado's own handler construct the Metrics URL. Links with an
      // explicit target stay in this tab; the worker verifies the resulting URL
      // and login identity before reading any order rows.
      if (link.tagName === "A") link.setAttribute("target", "_self");
      link.click();
      return {clicked: true};
    }
    return {error: "延误报表通过邮件发送，当前店铺没有可读取的最新 CSV 报表"};
  }
  if (command === "orders") {
    const ids = new Set();
    const add = value => { const id = String(value || "").replace(/\D/g, ""); if (/^\d{10,20}$/.test(id)) ids.add(id); };
    for (const el of nodes.filter(visible)) {
      for (const attr of ["data-order-id", "data-order-number", "data-sale-id"]) add(el.getAttribute(attr));
      for (const match of (el.getAttribute("href") || "").matchAll(/(?:orders?|sales?|ventas?|pedidos?)[^0-9]{0,20}(\d{10,20})/gi)) add(match[1]);
      const text = norm(el.innerText);
      if (!text || text.length > 800) continue;
      for (const match of text.matchAll(/(?:orders?|sales?|ventas?|vendas?|pedidos?|订单|销售单)[^0-9]{0,30}(\d{10,20})/gi)) add(match[1]);
      if (el.matches("tr,li,[role='row']") || /row|order|sale|card|item|record/.test(String(el.className))) {
        for (const match of text.matchAll(/\b\d{10,20}\b/g)) add(match[0]);
      }
    }
    return {ids: [...ids], fingerprint: `${[...ids].join("|")}:${document.body?.scrollHeight || 0}`};
  }
  if (command === "next_orders") {
    const buttons = nodes.filter(clickable);
    const next = buttons.find(el => el.matches("a[rel='next'],.andes-pagination__button--next") ||
      [el.innerText, el.getAttribute("aria-label"), el.getAttribute("title")].some(value => /^(next|next page|siguiente|proxima|下一页)$/i.test(plain(value))) ||
      /load more|show more|see more|view more|cargar mas|mostrar mas|ver mas|ver mais|carregar mais|加载更多|显示更多|查看更多/i.test(plain(label(el))));
    if (next) { next.click(); return {advanced: true}; }
    const before = window.scrollY;
    window.scrollTo(0, document.body?.scrollHeight || document.documentElement.scrollHeight);
    return {advanced: window.scrollY !== before};
  }
  if (command === "csv_report") {
    const url = new URL(args.url, location.href);
    if (url.origin !== location.origin || !supportedHost(url.hostname)) return {error: "延误报表必须来自当前店铺同源页面"};
    const response = await fetch(url.href, {credentials: "include", cache: "no-store", signal: AbortSignal.timeout(20000)});
    if (!response.ok) return {error: `延误报表读取失败 HTTP ${response.status}`};
    const contentType = response.headers.get("content-type") || "";
    if (!/csv|text\/plain|octet-stream/i.test(contentType) && !/\.csv(?:[?#]|$)/i.test(url.href)) return {error: "最新延误 CSV 报表尚未就绪"};
    const text = await response.text();
    if (text.length > 5_000_000 || /^\s*</.test(text)) return {error: "延误报表格式不正确"};
    return {csv: text};
  }
  return {error: "不支持的申诉页面动作"};
}

globalThis.MercadoAppealPage = {action: mercadoAppealPageAction};
