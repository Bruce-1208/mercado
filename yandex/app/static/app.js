const state = {
  runId: null,
  requestedCount: 200,
  products: [],
  selectedProducts: new Set(),
  stores: [],
  zeshunStores: [],
  selectedStoreId: null,
  exchangeRate: null,
  searchPollTimer: null,
  publishPollTimer: null,
  currentView: "orders",
  orders: [],
  orderPageTokens: [""],
  orderPageIndex: 0,
  orderNextToken: "",
  orderRequestId: 0,
  listings: [],
  listingSelection: new Set(),
  listingPageTokens: [""],
  listingPageIndex: 0,
  listingNextToken: "",
  listingRequestId: 0,
  inventoryPageTokens: [""],
  inventoryPageIndex: 0,
  inventoryNextToken: "",
  inventoryRequestId: 0,
  returnPageTokens: [""],
  returnPageIndex: 0,
  returnNextToken: "",
  returnRequestId: 0,
  feedbackPageTokens: [""],
  feedbackPageIndex: 0,
  feedbackNextToken: "",
  feedbackRequestId: 0,
  questionPageTokens: [""],
  questionPageIndex: 0,
  questionNextToken: "",
  questionRequestId: 0,
};

const $ = (id) => document.getElementById(id);
const keywordInput = $("keyword");
const countInput = $("count");
const pricePercentInput = $("pricePercent");
const searchButton = $("searchButton");
const publishButton = $("publishButton");
const statusPanel = $("statusPanel");
const resultsSection = $("resultsSection");
const packageInputs = {
  length: $("packageLength"),
  width: $("packageWidth"),
  height: $("packageHeight"),
  weight: $("packageWeight"),
};
const initialStockInput = $("initialStock");
const PACKAGE_STORAGE_KEY = "yandex-reseller-package-v1";
const YANDEX_BASE_PATH = String(window.YANDEX_BASE_PATH || "").replace(/\/+$/, "");

function toast(message, isError = false) {
  const element = $("toast");
  element.textContent = message;
  element.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(element._timer);
  element._timer = setTimeout(() => { element.className = "toast"; }, 4800);
}

async function api(path, options = {}) {
  let response;
  try {
    const normalizedPath = path.startsWith("/") ? path : `/${path}`;
    response = await fetch(`${YANDEX_BASE_PATH}${normalizedPath}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
  } catch (_) {
    throw new Error("无法连接本地服务。请运行 start.cmd，然后刷新页面重试");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function selectedStore() {
  return state.stores.find((store) => store.id === state.selectedStoreId) || null;
}

const VIEW_COPY = {
  orders: ["ORDER OPERATIONS", "订单中心", "查看店铺最近 30 天订单，并快速定位待处理状态。"],
  listings: ["STORE LISTINGS", "链接管理", "查看当前店铺全部商品链接，并执行改价或删除。"],
  inventory: ["INVENTORY CONTROL", "商品库存", "巡检各仓库库存并直接调整可售数量。"],
  returns: ["RETURNS OPERATIONS", "退货管理", "跟踪未取件、退货、退款决定和逆向物流。"],
  feedback: ["CUSTOMER VOICE", "客户声音", "集中处理商品评价与买家问答。"],
  products: ["PRODUCT OPERATIONS", "搜品上架", "搜索国外商品，补充履约数据并批量挂载到当前店铺。"],
  stores: ["STORE CONNECTIONS", "店铺管理", "管理 API-Key、TG 授权记录和当前操作店铺。"],
};

function switchView(view) {
  if (!VIEW_COPY[view]) return;
  state.currentView = view;
  document.querySelectorAll("[data-view]").forEach((element) => {
    element.classList.toggle("active", element.dataset.view === view);
  });
  document.querySelectorAll("[data-view-target]").forEach((button) => {
    const active = button.dataset.viewTarget === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  const [eyebrow, title, subtitle] = VIEW_COPY[view];
  $("pageEyebrow").textContent = eyebrow;
  $("pageTitle").textContent = title;
  $("pageSubtitle").textContent = subtitle;
  if (view === "orders" && selectedStore() && !state.orders.length) loadOrders({ resetPage: true });
  if (view === "listings" && selectedStore()) loadListings({ resetPage: true });
  if (view === "inventory" && selectedStore()) loadInventory({ resetPage: true });
  if (view === "returns" && selectedStore()) loadReturns({ resetPage: true });
  if (view === "feedback" && selectedStore()) loadFeedback({ resetPage: true });
}

document.addEventListener("click", (event) => {
  const navigation = event.target.closest("[data-view-target], [data-view-link]");
  if (!navigation) return;
  switchView(navigation.dataset.viewTarget || navigation.dataset.viewLink);
});

const ORDER_STATUS = {
  PLACING: "下单中",
  RESERVED: "已预留",
  UNPAID: "待付款",
  PENDING: "待处理",
  PROCESSING: "处理中",
  DELIVERY: "配送中",
  PICKUP: "待取货",
  DELIVERED: "已送达",
  CANCELLED: "已取消",
  PARTIALLY_RETURNED: "部分退货",
  RETURNED: "已退货",
  UNKNOWN: "未知状态",
};
const PROGRAM_LABEL = { FBY: "FBY", FBS: "FBS", DBS: "DBS", EXPRESS: "极速达", LAAS: "LaaS" };
const DELIVERY_LABEL = { DELIVERY: "送货上门", PICKUP: "自提", POST: "邮寄", DIGITAL: "数字商品" };
const ORDER_ITEM_STATUS = { CREATED: "已创建", SHIPPED: "已交付配送", CANCELLED: "已取消/移除", DELIVERED_TO_BUYER: "买家已收货", LOST: "丢失", REJECTED: "未签收", RETURNED: "已退回" };
const PAYMENT_TYPE = { PREPAID: "下单时付款", POSTPAID: "收货时付款" };
const PAYMENT_METHOD = {
  YANDEX: "银行卡", APPLE_PAY: "Apple Pay", GOOGLE_PAY: "Google Pay", CREDIT: "信贷付款",
  TINKOFF_CREDIT: "Tinkoff 信贷", TINKOFF_INSTALLMENTS: "Tinkoff 分期", EXTERNAL_CERTIFICATE: "礼品凭证",
  SBP: "快速支付系统", B2B_ACCOUNT_PREPAYMENT: "企业账户预付", MICROCREDIT: "Split 小额信贷",
  BNPL_TBC: "TBC 银行先买后付", DIGITAL_RUBLE: "数字卢布", CARD_ON_DELIVERY: "收货时刷卡",
  BOUND_CARD_ON_DELIVERY: "收货时绑定卡扣款", BNPL_BANK_ON_DELIVERY: "收货时 Super Split", BNPL_ON_DELIVERY: "收货时 Split", CASH_ON_DELIVERY: "货到付现金",
};
const ORDER_SUBSTATUS = {
  STARTED: "已确认，待处理", READY_TO_SHIP: "已备货，待发货", USER_NOT_PAID: "未按时付款",
  USER_CHANGED_MIND: "买家改变主意", USER_REFUSED_DELIVERY: "买家不接受配送条件", USER_REFUSED_PRODUCT: "商品不合适",
  SHOP_FAILED: "卖家无法履约", PICKUP_EXPIRED: "超过取件期限", TOO_LONG_DELIVERY: "配送时间过长", INCORRECT_PERSONAL_DATA: "跨境收件资料错误",
};

function orderEnum(value, labels = {}) {
  if (value === null || value === undefined || value === "") return "未返回";
  return Object.hasOwn(labels, value) ? (labels[value] === value ? String(value) : `${labels[value]} · ${value}`) : String(value);
}

function isoDate(date) {
  const offset = date.getTimezoneOffset();
  return new Date(date.getTime() - offset * 60000).toISOString().slice(0, 10);
}

function initializeOrderDates() {
  const end = new Date();
  const start = new Date();
  start.setDate(end.getDate() - 29);
  $("orderFrom").value = isoDate(start);
  $("orderTo").value = isoDate(end);
}

function formatDateTime(value, dateOnly = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit",
    ...(dateOnly ? {} : { hour: "2-digit", minute: "2-digit" }),
  }).format(date);
}

function formatMoney(value, currency) {
  if (value === null || value === undefined || value === "" || !currency || !Number.isFinite(Number(value))) return "—";
  const code = currency === "RUR" ? "RUB" : String(currency);
  return `${new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(value))} ${code}`;
}

function moneyKnown(amount) {
  return amount && amount.value !== null && amount.value !== undefined && amount.value !== "" && amount.currency && Number.isFinite(Number(amount.value));
}

function financeMoney(amount) {
  return moneyKnown(amount) ? escapeHtml(formatMoney(amount.value, amount.currency)) : "—";
}

function financeLine(label, amount) {
  return `<div class="finance-line"><span>${escapeHtml(label)}</span><strong>${financeMoney(amount)}</strong></div>`;
}

function renderFinanceSummary(orders, field, target, coverage, caption) {
  const totals = new Map();
  let known = 0;
  for (const order of orders) {
    const amount = order.finance?.[field];
    if (!moneyKnown(amount)) continue;
    const currency = amount.currency === "RUR" ? "RUB" : amount.currency;
    totals.set(currency, (totals.get(currency) || 0) + Number(amount.value));
    known += 1;
  }
  $(target).textContent = known ? [...totals].map(([currency, value]) => formatMoney(value, currency)).join(" / ") : "—";
  $(coverage).textContent = `${caption} · ${known}/${orders.length} 单有数据`;
}

function safeOrderUrl(value, productPage = false) {
  if (typeof value !== "string" || !value.trim()) return "";
  value = value.trim();
  if (value.length > 8192 || /[\s\\\x00-\x1f\x7f]/.test(value)) return "";
  try {
    const url = new URL(value);
    if (url.username || url.password || url.port || !["https:", "http:"].includes(url.protocol)) return "";
    if (productPage && (url.protocol !== "https:" || !["market.yandex.ru", "www.market.yandex.ru"].includes(url.hostname))) return "";
    return url.href;
  } catch (_) {
    return "";
  }
}

function renderOrderProduct(item, { detailed = false } = {}) {
  const name = item.offerName || item.name || item.offerId || item.offer_id || "商品";
  const sku = item.offerId || item.offer_id;
  const image = safeOrderUrl(item.image_url);
  const url = safeOrderUrl(item.product_url, true);
  const title = url
    ? `<a class="order-product-title" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(name)} · 在新窗口打开前台页面">${escapeHtml(name)}</a>`
    : `<strong class="order-product-title" title="${escapeHtml(name)}">${escapeHtml(name)}</strong>`;
  const thumbnail = `<span class="order-product-thumb"><span class="order-product-placeholder">${image ? "加载图片" : "暂无图片"}</span>${image ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(name)}" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ""}</span>`;
  const media = url
    ? `<a class="order-product-media" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" aria-label="${escapeHtml(name)}：打开前台页面（新窗口）">${thumbnail}</a>`
    : `<span class="order-product-media">${thumbnail}</span>`;
  const statuses = item.item_statuses || item.itemStatuses || [];
  const statusText = statuses.map((entry) => `${orderEnum(entry.status, ORDER_ITEM_STATUS)} × ${entry.count ?? "—"}`).join(" / ");
  const itemId = item.item_id ?? item.id;
  const vat = item.vat ?? item.prices?.vat;
  return `<div class="order-product${detailed ? " detailed" : ""}">${media}<div class="order-product-copy">${title}<small class="order-product-sku">SKU ${escapeHtml(sku || "未返回")}</small>${!url ? `<small class="order-product-link-hint">前台链接未返回</small>` : ""}${detailed && itemId != null ? `<small>订单行 ID ${escapeHtml(itemId)}</small>` : ""}${detailed && statusText ? `<small>${escapeHtml(statusText)}</small>` : ""}${detailed && vat != null ? `<small>增值税 ${escapeHtml(vat)}</small>` : ""}</div></div>`;
}

$("orderTableBody").addEventListener("load", (event) => {
  if (event.target.matches(".order-product-thumb img")) event.target.parentElement.classList.add("has-image");
}, true);
$("orderTableBody").addEventListener("error", (event) => {
  const image = event.target;
  if (!image.matches(".order-product-thumb img")) return;
  image.parentElement.querySelector(".order-product-placeholder").textContent = "图片不可用";
  image.remove();
}, true);

function renderOrderFinanceDetails(order) {
  const finance = order.finance || {};
  const items = finance.items || [];
  const rows = items.map((item) => `<tr>
    <td>${renderOrderProduct(item, { detailed: true })}</td>
    <td>${escapeHtml(item.count ?? "—")}</td>
    <td>${financeMoney(item.listing_unit)}</td>
    <td>${financeMoney(item.listing_total)}</td>
    <td>${financeMoney(item.buyer_payment)}</td>
    <td>${financeMoney(item.cashback)}</td>
    <td>${financeMoney(item.seller_subsidy)}</td>
  </tr>`).join("");
  const notes = (finance.notes || []).map((note) => `<li>${escapeHtml(note)}</li>`).join("");
  return `<details class="order-finance-details">
    <summary>查看价格与结算明细</summary>
    <div class="finance-detail-body">
      <p class="finance-explanation">链接价格为当前店铺设置价，可能与下单时展示价不同；买家付款、积分及补贴分别列出。卖家结余按已返回的款项和费用估算，最终以结算账单为准。</p>
      <div class="finance-breakdown">
        <div><h3>买家与补贴</h3>${financeLine("商品付款", finance.buyer_payment)}${financeLine("积分抵扣", finance.cashback)}${financeLine("卖家补贴", finance.seller_subsidy)}</div>
        <div><h3>卖家结余</h3>${financeLine("已回传资金净额", finance.seller_gross)}${financeLine("平台费用（含物流）", finance.platform_fees)}${financeLine("结余（估算）", finance.seller_net)}<p>${escapeHtml(finance.settlement_label || "结算数据待返回")}</p></div>
        <div><h3>运费明细</h3>${financeLine("买家支付", finance.buyer_shipping)}${financeLine("配送补贴", finance.delivery_subsidy)}${financeLine("配送金额合计", finance.delivery_total)}${financeLine("卖家物流费用", finance.seller_shipping)}</div>
      </div>
      ${rows ? `<div class="finance-items-wrap"><table class="finance-items-table"><thead><tr><th>商品 / SKU</th><th>数量</th><th>链接单价 · 当前</th><th>链接合计</th><th>商品付款合计</th><th>积分合计</th><th>补贴合计</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      ${notes ? `<ul class="finance-notes">${notes}</ul>` : ""}
    </div>
  </details>`;
}

function orderField(label, value) {
  const text = value === null || value === undefined || value === "" ? "未返回" : String(value);
  return `<div class="order-info-field"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(text)}</dd></div>`;
}

function orderInfoCard(title, fields) {
  return `<section class="order-info-card"><h3>${escapeHtml(title)}</h3><dl>${fields.join("")}</dl></section>`;
}

function fullOrderDate(value) {
  if (!value) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
}

function orderRange(from, to) {
  if (!from && !to) return null;
  if (from && from === to) return from;
  return `${from || "未返回"} — ${to || "未返回"}`;
}

function orderRegion(region) {
  const names = [];
  for (let current = region, depth = 0; current && depth < 12; current = current.parent, depth += 1) {
    if (current.name) names.unshift(current.name);
  }
  return names.join(" / ");
}

function renderOrderInformation(order) {
  const items = Array.isArray(order.items) ? order.items : [];
  const delivery = order.delivery || {};
  const dates = delivery.dates || {};
  const shipment = delivery.shipment || {};
  const pickup = delivery.pickup || {};
  const destination = delivery.type === "PICKUP" ? pickup : (delivery.courier || {});
  const address = destination.address || {};
  const quantities = items.map((item) => item.count);
  const completeCount = quantities.length && quantities.every((count) => Number.isInteger(count) && count >= 0);
  const quantity = completeCount ? quantities.reduce((sum, count) => sum + count, 0) : "未返回";
  const yesNo = (value) => value === true ? "是" : value === false ? "否" : null;
  const cards = [
    orderInfoCard("订单信息", [
      orderField("订单号", order.orderId ?? order.id), orderField("外部订单号", order.externalOrderId),
      orderField("店铺 API 编号", order.campaignId), orderField("履约模式", orderEnum(order.programType, PROGRAM_LABEL)),
      orderField("订单状态", orderEnum(order.status, ORDER_STATUS)), orderField("处理阶段 / 原因", orderEnum(order.substatus, ORDER_SUBSTATUS)),
      orderField("商品数量", `${items.length} 个商品行 / ${quantity} 件`),
      orderField("申请取消", yesNo(order.cancelRequested)), orderField("测试订单", yesNo(order.fake)),
    ]),
    orderInfoCard("付款与来源", [
      orderField("付款类型", orderEnum(order.paymentType, PAYMENT_TYPE)),
      orderField("支付方式", orderEnum(order.paymentMethod, PAYMENT_METHOD)),
      orderField("买家类型", orderEnum(order.buyerType, { PERSON: "个人买家", BUSINESS: "企业买家" })),
      orderField("来源平台", orderEnum(order.sourcePlatform, { MARKET: "Yandex Market" })),
    ]),
    orderInfoCard("时间安排", [
      orderField("下单时间", fullOrderDate(order.creationDate)), orderField("更新时间", fullOrderDate(order.updateDate)),
      orderField("发货日期", shipment.shipmentDate), orderField("发货时间", shipment.shipmentTime),
      orderField("预计配送日期", orderRange(dates.fromDate, dates.toDate)),
      orderField("配送时间段", orderRange(dates.fromTime, dates.toTime)),
      orderField(delivery.type === "PICKUP" ? "实际送达提货点" : "实际送达日期", dates.realDeliveryDate),
    ]),
    orderInfoCard("配送与仓库", [
      orderField("配送方式", orderEnum(delivery.type, DELIVERY_LABEL)), orderField("承运商", delivery.serviceName),
      orderField("物流服务 ID", delivery.deliveryServiceId),
      orderField("配送主体", orderEnum(delivery.deliveryPartnerType, { SHOP: "卖家配送", YANDEX_MARKET: "平台配送" })),
      orderField("交付方式", orderEnum(delivery.dispatchType, { BUYER: "送至买家", MARKET_BRANDED_OUTLET: "平台自提点", SHOP_OUTLET: "店铺自提点" })),
      orderField("仓库 ID", delivery.warehouseId), orderField("发货批次 ID", shipment.id),
    ]),
  ];
  const destinationFields = [orderField("收货地区", orderRegion(destination.region))];
  const addressLabels = { country: "国家", postcode: "邮编", city: "城市", district: "行政区", subway: "附近地铁", street: "街道", house: "门牌", block: "楼栋", entrance: "入口", entryphone: "门禁", floor: "楼层", apartment: "房间" };
  for (const [key, label] of Object.entries(addressLabels)) {
    if (address[key] !== null && address[key] !== undefined && address[key] !== "") destinationFields.push(orderField(label, address[key]));
  }
  if (delivery.type === "PICKUP") {
    destinationFields.push(orderField("平台提货点 ID", pickup.logisticPointId), orderField("店铺提货点编号", pickup.outletCode), orderField("自提保管期限", pickup.outletStorageLimitDate));
  }
  cards.push(orderInfoCard(delivery.type === "PICKUP" ? "提货点信息" : "收货地区与地址", destinationFields));
  const extraFields = [orderField("订单备注", order.notes)];
  if (order.services?.liftType) extraFields.push(orderField("搬运上楼服务", orderEnum(order.services.liftType, { NOT_NEEDED: "无需上楼", MANUAL: "人工搬运", ELEVATOR: "电梯", CARGO_ELEVATOR: "货梯", FREE: "含免费上楼" })));
  cards.push(orderInfoCard("备注与服务", extraFields));
  const tracks = (delivery.tracks || []).map((track) => `<li><span>运单号</span><strong>${escapeHtml(track.trackCode || "未返回")}</strong><small>物流服务 ID ${escapeHtml(track.deliveryServiceId ?? "未返回")}</small></li>`).join("");
  const boxes = (delivery.boxesLayout || []).map((box) => {
    const contents = (box.items || []).map((entry) => {
      const item = items.find((candidate) => String(candidate.id) === String(entry.id));
      const parts = entry.partialCount;
      const count = parts ? `拆分件 ${parts.current ?? "—"} / ${parts.total ?? "—"}` : `× ${entry.fullCount ?? "—"}`;
      return `<li><span>${escapeHtml(item?.offerName || item?.offerId || "商品")}</span><small>订单行 ID ${escapeHtml(entry.id ?? "未返回")}${item?.offerId ? ` · SKU ${escapeHtml(item.offerId)}` : ""}</small><strong>${escapeHtml(count)}</strong></li>`;
    }).join("");
    return `<section class="order-box"><div class="order-box-heading"><strong>包裹 ${escapeHtml(box.boxId ?? "未返回")}</strong><span>条码 ${escapeHtml(box.barcode || "未返回")}</span></div><ul>${contents || "<li>包内商品信息未返回</li>"}</ul></section>`;
  }).join("");
  const markings = items.filter((item) => item.tags?.length || item.requiredInstanceTypes?.length || item.instances?.length).map((item) => {
    const fields = [orderField("商品 / SKU", `${item.offerName || "商品"} / ${item.offerId || "未返回"}`)];
    if (item.tags?.length) fields.push(orderField("商品标签", item.tags.map((tag) => orderEnum(tag, { ULTIMA: "高端商品", SAFE_TAG: "安全标识" })).join(" / ")));
    if (item.requiredInstanceTypes?.length) fields.push(orderField("所需标记类型", item.requiredInstanceTypes.join(" / ")));
    const instances = (item.instances || []).map((instance, index) => {
      const labels = { cis: "CIS 标记码", cisFull: "完整 CIS", uin: "UIN 编号", rnpt: "批次追踪号", gtd: "海关申报号", countryCode: "原产国" };
      const values = Object.entries(labels).filter(([key]) => instance[key] != null && instance[key] !== "").map(([key, label]) => orderField(label, instance[key]));
      return values.length ? `<h4>商品实例 ${index + 1}</h4><dl>${values.join("")}</dl>` : "";
    }).join("");
    return `<section class="order-marking-card"><dl>${fields.join("")}</dl>${instances}</section>`;
  }).join("");
  return `<details class="order-info-details"><summary>查看订单与履约详情</summary><div class="order-info-body"><p class="order-info-caption">时间戳按浏览器时区显示；配送日期与时段保留平台安排。地址、运单及包裹仅展示平台已返回的数据。</p><div class="order-info-grid">${cards.join("")}</div><section class="order-logistics"><h3>物流单号</h3>${tracks ? `<ul class="order-track-list">${tracks}</ul>` : `<p class="order-info-empty">物流单号尚未返回</p>`}<h3>包裹与装箱明细 · ${(delivery.boxesLayout || []).length} 个已返回包裹</h3><div class="order-boxes">${boxes || `<p class="order-info-empty">装箱信息尚未返回</p>`}</div></section>${markings ? `<section class="order-markings"><h3>商品标签与合规标记</h3>${markings}</section>` : ""}</div></details>`;
}

function deliverySummary(order) {
  const delivery = order.delivery || {};
  const dates = delivery.dates || {};
  const shipment = delivery.shipment || {};
  const type = DELIVERY_LABEL[delivery.type] || delivery.type || "配送信息待定";
  const targetDate = shipment.shipmentDate || dates.fromDate || dates.toDate || dates.realDeliveryDate;
  return { type, detail: targetDate ? `${shipment.shipmentDate ? "发货" : "预计"} ${formatDateTime(targetDate, true)}` : (delivery.serviceName || "日期待定") };
}

function renderOrderItems(items) {
  const visible = (items || []).slice(0, 2);
  if (!visible.length) return `<span class="order-more">暂无商品明细</span>`;
  const rows = visible.map((item) => `<div class="order-item">${renderOrderProduct(item)}<span class="order-item-count">× ${escapeHtml(item.count ?? "—")}</span></div>`).join("");
  const remaining = (items || []).length - visible.length;
  return rows + (remaining > 0 ? `<span class="order-more">另有 ${remaining} 种商品</span>` : "");
}

function renderOrderActions(order) {
  const status = String(order.status || "").toUpperCase();
  const substatus = String(order.substatus || "").toUpperCase();
  if (status !== "PROCESSING" || substatus !== "STARTED") return "";
  const orderId = order.orderId ?? order.id;
  if (!orderId) return "";
  return `<div class="order-quick-actions"><button type="button" data-order-action="READY_TO_SHIP" data-order-id="${escapeHtml(orderId)}">标记备货完成</button><button type="button" class="danger-link" data-order-action="CANCEL" data-order-id="${escapeHtml(orderId)}">无法履约并取消</button></div>`;
}

function renderOrders(orders) {
  state.orders = orders;
  $("orderTotal").textContent = String(orders.length);
  $("orderPending").textContent = String(orders.filter((order) => ["PENDING", "PROCESSING", "UNPAID"].includes(String(order.status).toUpperCase())).length);
  renderFinanceSummary(orders, "listing_total", "orderListingTotal", "orderListingCoverage", "当前设置价 × 数量");
  renderFinanceSummary(orders, "buyer_payment", "orderRevenue", "orderPaymentCoverage", "商品付款，不含运费与积分");
  renderFinanceSummary(orders, "seller_net", "orderSellerNet", "orderNetCoverage", "扣除已返回的平台费用");
  renderFinanceSummary(orders, "seller_shipping", "orderShippingTotal", "orderShippingCoverage", "平台物流费用");
  $("orderUpdatedAt").textContent = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date());
  $("orderTableBody").innerHTML = orders.map((order) => {
    const status = String(order.status || "UNKNOWN").toUpperCase();
    const delivery = deliverySummary(order);
    const finance = order.finance || {};
    const orderId = order.orderId ?? order.id ?? "—";
    return `<tr class="order-main-row">
      <td><div class="order-id"><strong>#${escapeHtml(orderId)}</strong><span>${escapeHtml(PROGRAM_LABEL[order.programType] || order.programType || "模式未知")} · ${formatDateTime(order.creationDate)}</span>${order.fake ? `<span>测试订单</span>` : ""}</div><div class="order-items">${renderOrderItems(order.items)}</div></td>
      <td><span class="status-pill ${escapeHtml(status.toLowerCase())}">${escapeHtml(ORDER_STATUS[status] || status)}</span><div class="delivery-cell"><strong>${escapeHtml(delivery.type)}</strong><span class="delivery-detail">${escapeHtml(delivery.detail)}</span>${order.delivery?.serviceName ? `<span class="delivery-detail">${escapeHtml(order.delivery.serviceName)}</span>` : ""}</div>${order.substatus ? `<span class="order-substatus">${escapeHtml(orderEnum(order.substatus, ORDER_SUBSTATUS))}</span>` : ""}${order.cancelRequested ? `<span class="cancel-alert">买家申请取消</span>` : ""}${renderOrderActions(order)}<span class="order-date">更新 ${formatDateTime(order.updateDate || order.creationDate)}</span></td>
      <td class="finance-cell" data-label="链接价格"><div class="money-cell">${financeMoney(finance.listing_total)}</div><small>当前设置价 × 数量</small></td>
      <td class="finance-cell" data-label="买家付款"><div class="money-cell">${financeMoney(finance.buyer_payment)}</div><small>商品付款</small>${financeLine("积分", finance.cashback)}${order.paymentType === "POSTPAID" ? `<small>货到付款</small>` : ""}</td>
      <td class="finance-cell" data-label="卖家结余"><div class="money-cell seller-net">${financeMoney(finance.seller_net)}</div><small>${escapeHtml(finance.settlement_label || "结算数据待返回")}</small>${financeLine("平台费用", finance.platform_fees)}${financeLine("卖家补贴", finance.seller_subsidy)}</td>
      <td class="finance-cell" data-label="运费">${financeLine("买家运费", finance.buyer_shipping)}${financeLine("卖家运费", finance.seller_shipping)}</td>
    </tr><tr class="order-detail-row"><td colspan="6">${renderOrderInformation(order)}${renderOrderFinanceDetails(order)}</td></tr>`;
  }).join("");
  $("orderPageLabel").textContent = `第 ${state.orderPageIndex + 1} 页 · 本页 ${orders.length} 条`;
  $("orderPrevButton").disabled = state.orderPageIndex === 0;
  $("orderNextButton").disabled = !state.orderNextToken;
}

function showOrderState(name, message = "") {
  ["ordersLoading", "ordersNoStore", "ordersError", "ordersEmpty", "ordersContent"].forEach((id) => $(id).classList.toggle("hidden", id !== name));
  if (name === "ordersError") $("ordersError").innerHTML = `<span class="state-icon">!</span><strong>订单读取失败</strong><p>${escapeHtml(message)}</p><button class="button secondary" type="button" data-order-retry>重试</button>`;
}

function resetOrderMetrics() {
  ["orderTotal", "orderPending", "orderListingTotal", "orderRevenue", "orderSellerNet", "orderShippingTotal", "orderUpdatedAt"].forEach((id) => { $(id).textContent = "—"; });
  ["orderListingCoverage", "orderPaymentCoverage", "orderNetCoverage", "orderShippingCoverage"].forEach((id) => { $(id).textContent = "等待订单金额数据"; });
}

async function loadOrders({ resetPage = false, pageToken = null } = {}) {
  const store = selectedStore();
  if (!store) {
    state.orderRequestId += 1;
    state.orders = [];
    resetOrderMetrics();
    showOrderState("ordersNoStore");
    $("orderRefreshButton").disabled = false;
    $("orderRefreshButton").textContent = "刷新订单";
    return;
  }
  const from = $("orderFrom").value;
  const to = $("orderTo").value;
  if (from && to) {
    const start = new Date(`${from}T00:00:00`);
    const end = new Date(`${to}T00:00:00`);
    const days = Math.round((end - start) / 86400000);
    if (days < 0) return toast("订单开始日期不能晚于结束日期", true);
    if (days > 29) return toast("订单查询范围最多 30 天", true);
  }
  if (resetPage) {
    state.orderPageTokens = [""];
    state.orderPageIndex = 0;
    state.orderNextToken = "";
  }
  const token = pageToken === null ? (state.orderPageTokens[state.orderPageIndex] || "") : pageToken;
  const requestId = ++state.orderRequestId;
  state.orders = [];
  resetOrderMetrics();
  showOrderState("ordersLoading");
  $("orderRefreshButton").disabled = true;
  $("orderRefreshButton").textContent = "读取中…";
  try {
    const status = $("orderStatus").value;
    const data = await api("/api/orders", {
      method: "POST",
      body: JSON.stringify({ store_id: store.id, statuses: status ? [status] : [], date_from: from || null, date_to: to || null, page_token: token, limit: 50 }),
    });
    if (requestId !== state.orderRequestId) return;
    state.orderNextToken = data.paging?.nextPageToken || "";
    const orders = data.orders || [];
    renderOrders(orders);
    showOrderState(orders.length ? "ordersContent" : "ordersEmpty");
  } catch (error) {
    if (requestId !== state.orderRequestId) return;
    resetOrderMetrics();
    showOrderState("ordersError", error.message);
  } finally {
    if (requestId === state.orderRequestId) {
      $("orderRefreshButton").disabled = false;
      $("orderRefreshButton").textContent = "刷新订单";
    }
  }
}

$("orderFilterForm").addEventListener("submit", (event) => { event.preventDefault(); loadOrders({ resetPage: true }); });
$("orderPrevButton").addEventListener("click", () => {
  if (state.orderPageIndex === 0) return;
  state.orderPageIndex -= 1;
  loadOrders({ pageToken: state.orderPageTokens[state.orderPageIndex] || "" });
});
$("orderNextButton").addEventListener("click", () => {
  if (!state.orderNextToken) return;
  state.orderPageIndex += 1;
  state.orderPageTokens[state.orderPageIndex] = state.orderNextToken;
  loadOrders({ pageToken: state.orderNextToken });
});
$("ordersError").addEventListener("click", (event) => { if (event.target.closest("[data-order-retry]")) loadOrders(); });
$("orderTableBody").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-order-action]");
  if (!button) return;
  const store = selectedStore();
  if (!store) return toast("请先选择店铺", true);
  const action = button.dataset.orderAction;
  const orderId = Number(button.dataset.orderId);
  const message = action === "READY_TO_SHIP"
    ? `确认订单 #${orderId} 已完成备货、可以交付承运方？`
    : `确认订单 #${orderId} 因卖家无法履约而取消？该操作会影响店铺履约指标。`;
  if (!window.confirm(message)) return;
  button.disabled = true;
  try {
    await api("/api/orders/action", { method: "POST", body: JSON.stringify({ store_id: store.id, order_id: orderId, action }) });
    toast(action === "READY_TO_SHIP" ? "订单已提交备货完成" : "取消请求已提交");
    await loadOrders();
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
});

function splitSkuValues(value, maximum = 500) {
  const values = [...new Set(String(value || "").split(/[\s,;，；]+/).map((item) => item.trim()).filter(Boolean))];
  if (values.length > maximum) throw new Error(`一次最多输入 ${maximum} 个 SKU`);
  return values;
}

const LISTING_STATUS = {
  PUBLISHED: "正常销售", CHECKING: "审核中", DISABLED_BY_PARTNER: "已手动隐藏",
  DISABLED_AUTOMATICALLY: "平台自动隐藏", REJECTED_BY_MARKET: "平台拒绝",
  CREATING_CARD: "正在建卡", NO_CARD: "缺少商品卡", NO_STOCKS: "无库存",
  ARCHIVED: "已归档", READY_FOR_PUBLICATION: "待店铺上线",
};

function listingShowcaseUrl(details) {
  const urls = (details?.showcaseUrls || []).filter(Boolean);
  const preferred = urls.find((item) => item?.showcaseType === "B2C") || urls[0];
  return safeOrderUrl(typeof preferred === "string" ? preferred : preferred?.showcaseUrl, true);
}

function listingPicture(details) {
  const first = (details?.pictures || [])[0];
  return safeOrderUrl(typeof first === "string" ? first : first?.url);
}

function updateListingSelection() {
  const visible = new Set(state.listings.map((item) => String(item.offerId || "")));
  for (const value of [...state.listingSelection]) {
    if (!visible.has(value)) state.listingSelection.delete(value);
  }
  $("listingSelectedCount").textContent = String(state.listingSelection.size);
  $("listingDeleteSelected").disabled = state.listingSelection.size === 0;
  const boxes = [...document.querySelectorAll("[data-listing-select]")];
  const selected = boxes.filter((box) => box.checked).length;
  $("listingSelectAll").checked = boxes.length > 0 && selected === boxes.length;
  $("listingSelectAll").indeterminate = selected > 0 && selected < boxes.length;
}

function renderListings(data) {
  const records = data.offers || [];
  state.listings = records;
  state.listingSelection.clear();
  $("listingCount").textContent = String(records.length);
  $("listingPublishedCount").textContent = String(records.filter((item) => item.status === "PUBLISHED").length);
  $("listingIssueCount").textContent = String(records.filter((item) => item.status !== "PUBLISHED" || (item.errors || []).length).length);
  $("listingWarning").textContent = data.warning || "改价通常会在数分钟后生效；价格跳变过大可能进入价格隔离。";
  $("listingTableBody").innerHTML = records.map((item) => {
    const details = item.details || {};
    const name = details.name || item.offerId || "商品";
    const url = listingShowcaseUrl(details);
    const picture = listingPicture(details);
    const product = url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(name)}</a>` : `<strong>${escapeHtml(name)}</strong>`;
    const base = item.basicPrice || {};
    const campaign = item.campaignPrice || {};
    const active = campaign.value !== null && campaign.value !== undefined && campaign.value !== "" ? campaign : base;
    const currency = active.currencyId || base.currencyId || campaign.currencyId || "RUR";
    const current = Number(active.value);
    const discount = active.discountBase;
    const errors = [...(item.errors || []), ...(item.warnings || [])].filter(Boolean).map((entry) => entry.message || entry.comment).filter(Boolean);
    return `<tr data-listing-row="${escapeHtml(item.offerId)}"><td><input type="checkbox" data-listing-select value="${escapeHtml(item.offerId)}" aria-label="选择 SKU ${escapeHtml(item.offerId)}"></td><td><div class="inventory-product listing-product">${picture ? `<img src="${escapeHtml(picture)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span class="inventory-placeholder">链</span>`}<div>${product}<small>SKU ${escapeHtml(item.offerId || "—")}${details.vendor ? ` · ${escapeHtml(details.vendor)}` : ""}</small>${url ? `<small class="listing-url">${escapeHtml(url)}</small>` : `<small>Yandex 暂未返回前台链接</small>`}</div></div></td><td><span class="status-pill listing-status ${item.status === "PUBLISHED" ? "ok" : ""}">${escapeHtml(LISTING_STATUS[item.status] || item.status || "状态未知")}</span><small>${item.available === false ? "当前不可售" : ""}</small>${errors.length ? `<small class="listing-errors">${escapeHtml(errors.join("；"))}</small>` : ""}</td><td><strong>${escapeHtml(formatMoney(base.value, base.currencyId))}</strong><small>${base.updatedAt ? `更新 ${formatDateTime(base.updatedAt)}` : "所有店铺默认价"}</small></td><td><strong>${escapeHtml(formatMoney(campaign.value, campaign.currencyId))}</strong><small>${campaign.value === null || campaign.value === undefined ? "当前使用统一价格" : `店铺单独价格${campaign.updatedAt ? ` · ${formatDateTime(campaign.updatedAt)}` : ""}`}</small></td><td><form class="listing-price-form" data-listing-price data-offer-id="${escapeHtml(item.offerId)}" data-currency="${escapeHtml(currency)}"><label>售价<input type="number" min="0.01" max="100000000" step="0.01" required value="${Number.isFinite(current) ? escapeHtml(current) : ""}"></label><label>划线价<input data-discount-base type="number" min="0.01" max="100000000" step="0.01" value="${discount ? escapeHtml(discount) : ""}" placeholder="可选"></label><div><button class="button secondary" type="submit">保存价格</button><button class="button ghost danger-button" type="button" data-listing-delete data-offer-id="${escapeHtml(item.offerId)}">删除</button></div></form></td></tr>`;
  }).join("");
  $("listingPageLabel").textContent = `第 ${state.listingPageIndex + 1} 页 · 本页 ${records.length} 条`;
  $("listingPrevButton").disabled = state.listingPageIndex === 0;
  $("listingNextButton").disabled = !state.listingNextToken;
  $("listingsState").classList.toggle("hidden", records.length > 0);
  $("listingsState").textContent = records.length ? "" : "当前筛选下没有商品链接。";
  $("listingsContent").classList.toggle("hidden", !records.length);
  updateListingSelection();
}

async function loadListings({ resetPage = false, pageToken = null } = {}) {
  const store = selectedStore();
  if (!store) {
    $("listingsState").textContent = "请先选择店铺。";
    $("listingsState").classList.remove("hidden");
    $("listingsContent").classList.add("hidden");
    return;
  }
  let offerIds;
  try { offerIds = splitSkuValues($("listingSkuFilter").value, 200); } catch (error) { return toast(error.message, true); }
  if (resetPage) {
    state.listingPageTokens = [""]; state.listingPageIndex = 0; state.listingNextToken = "";
  }
  const token = pageToken === null ? state.listingPageTokens[state.listingPageIndex] || "" : pageToken;
  const requestId = ++state.listingRequestId;
  $("listingsState").textContent = "正在读取店铺商品链接…";
  $("listingsState").classList.remove("hidden");
  $("listingsContent").classList.add("hidden");
  try {
    const status = $("listingStatus").value;
    const data = await api("/api/listings", { method: "POST", body: JSON.stringify({ store_id: store.id, offer_ids: offerIds, statuses: status ? [status] : [], page_token: offerIds.length ? "" : token, limit: 100 }) });
    if (requestId !== state.listingRequestId) return;
    state.listingNextToken = data.paging?.nextPageToken || "";
    renderListings(data);
  } catch (error) {
    if (requestId !== state.listingRequestId) return;
    $("listingsState").textContent = `链接读取失败：${error.message}`;
    toast(error.message, true);
  }
}

async function deleteListings(offerIds) {
  const store = selectedStore();
  if (!store || !offerIds.length) return;
  if (!window.confirm(`确定从店铺“${store.alias}”删除 ${offerIds.length} 条商品链接？其他店铺和总商品目录不受影响。`)) return;
  try {
    const data = await api("/api/listings/delete", { method: "POST", body: JSON.stringify({ store_id: store.id, offer_ids: offerIds }) });
    const failed = data.notDeletedOfferIds || [];
    toast(failed.length ? `${data.deleted.length} 条已删除，${failed.length} 条因平台仓库存等原因未删除` : `${data.deleted.length} 条链接已从当前店铺删除`, failed.length > 0);
    await loadListings();
  } catch (error) { toast(error.message, true); }
}

$("listingFilterForm").addEventListener("submit", (event) => { event.preventDefault(); loadListings({ resetPage: true }); });
$("listingPrevButton").addEventListener("click", () => { if (state.listingPageIndex > 0) { state.listingPageIndex -= 1; loadListings({ pageToken: state.listingPageTokens[state.listingPageIndex] || "" }); } });
$("listingNextButton").addEventListener("click", () => { if (state.listingNextToken) { state.listingPageIndex += 1; state.listingPageTokens[state.listingPageIndex] = state.listingNextToken; loadListings({ pageToken: state.listingNextToken }); } });
$("listingSelectAll").addEventListener("change", (event) => {
  document.querySelectorAll("[data-listing-select]").forEach((box) => {
    box.checked = event.target.checked;
    if (box.checked) state.listingSelection.add(box.value); else state.listingSelection.delete(box.value);
  });
  updateListingSelection();
});
$("listingTableBody").addEventListener("change", (event) => {
  const box = event.target.closest("[data-listing-select]");
  if (!box) return;
  if (box.checked) state.listingSelection.add(box.value); else state.listingSelection.delete(box.value);
  updateListingSelection();
});
$("listingDeleteSelected").addEventListener("click", () => deleteListings([...state.listingSelection]));
$("listingTableBody").addEventListener("click", (event) => {
  const button = event.target.closest("[data-listing-delete]");
  if (button) deleteListings([button.dataset.offerId]);
});
$("listingTableBody").addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-listing-price]");
  if (!form) return;
  event.preventDefault();
  const store = selectedStore();
  const value = Number(form.querySelector("input[required]").value);
  const discountText = form.querySelector("[data-discount-base]").value.trim();
  const discountBase = discountText ? Number(discountText) : null;
  if (!Number.isFinite(value) || value <= 0) return toast("请输入有效售价", true);
  if (discountBase !== null) {
    const discount = 1 - value / discountBase;
    if (!Number.isFinite(discountBase) || discount < 0.05 || discount > 0.99) return toast("划线价对应折扣必须在 5%–99% 之间", true);
  }
  if (!window.confirm(`把店铺“${store.alias}”中 SKU ${form.dataset.offerId} 的价格改为 ${formatMoney(value, form.dataset.currency)}？`)) return;
  const button = form.querySelector("button[type=submit]"); button.disabled = true;
  try {
    const data = await api("/api/listings/price", { method: "PUT", body: JSON.stringify({ store_id: store.id, offer_id: form.dataset.offerId, value, currency_id: form.dataset.currency, discount_base: discountBase }) });
    toast(data.priceScope === "business" ? "该柜台仅支持统一价格，已更新所有店铺默认价" : "当前店铺价格已提交更新");
    await loadListings();
  } catch (error) { toast(error.message, true); button.disabled = false; }
});

function initializeOpsDates() {
  const today = new Date();
  const returnsFrom = new Date(today);
  returnsFrom.setDate(today.getDate() - 90);
  $("returnFrom").value = isoDate(returnsFrom);
  $("returnTo").value = isoDate(today);
  const questionsFrom = new Date(today);
  questionsFrom.setDate(today.getDate() - 30);
  $("questionFrom").value = isoDate(questionsFrom);
  $("questionTo").value = isoDate(today);
}

function availableStock(offer) {
  const stocks = (offer.stocks || []).filter((item) => item && typeof item === "object");
  const available = stocks.find((item) => item.type === "AVAILABLE") || stocks.find((item) => item.type === "FIT");
  return available && Number.isFinite(Number(available.count)) ? Number(available.count) : 0;
}

function renderInventory(data) {
  const rows = [];
  const skuCounts = new Map();
  for (const warehouse of data.warehouses || []) {
    for (const offer of warehouse.offers || []) {
      const id = String(offer.offerId || "");
      const current = availableStock(offer);
      skuCounts.set(id, (skuCounts.get(id) || 0) + current);
      rows.push({ warehouse, offer, current });
    }
  }
  $("inventorySkuCount").textContent = String(skuCounts.size);
  $("inventoryAvailableCount").textContent = String([...skuCounts.values()].reduce((sum, count) => sum + count, 0));
  $("inventoryZeroCount").textContent = String([...skuCounts.values()].filter((count) => count <= 0).length);
  $("inventorySource").textContent = `${data.stockMethod === "business" ? "独立仓库库存接口" : "仓库组 / 平台仓库库存接口"} · ${(data.warehouses || []).length} 个仓库`;
  $("inventoryWarning").textContent = data.warning || "";
  $("inventoryTableBody").innerHTML = rows.map(({ warehouse, offer, current }) => {
    const details = offer.details || {};
    const pictureValue = (details.pictures || [])[0];
    const picture = safeOrderUrl(typeof pictureValue === "string" ? pictureValue : pictureValue?.url);
    const showcase = safeOrderUrl((details.showcaseUrls || [])[0], true);
    const name = details.name || offer.offerId || "商品";
    const title = showcase ? `<a href="${escapeHtml(showcase)}" target="_blank" rel="noopener noreferrer">${escapeHtml(name)}</a>` : `<strong>${escapeHtml(name)}</strong>`;
    const stocks = (offer.stocks || []).filter(Boolean).map((stock) => `<span><b>${escapeHtml(stock.type || "UNKNOWN")}</b> ${escapeHtml(stock.count ?? "—")}</span>`).join("");
    const price = details.price || {};
    return `<tr><td><div class="inventory-product">${picture ? `<img src="${escapeHtml(picture)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span class="inventory-placeholder">品</span>`}<div>${title}<small>SKU ${escapeHtml(offer.offerId || "—")}${details.vendor ? ` · ${escapeHtml(details.vendor)}` : ""}</small></div></div></td><td><strong>${escapeHtml(warehouse.warehouseName || `仓库 ${warehouse.warehouseId || "—"}`)}</strong><small>ID ${escapeHtml(warehouse.warehouseId || "—")}</small></td><td><div class="stock-chips">${stocks || "未返回库存构成"}</div><small>更新 ${formatDateTime(offer.updatedAt)}</small></td><td>${formatMoney(price.value, price.currencyId || price.currency)}</td><td><div class="stock-update"><input type="number" min="0" max="2000000000" step="1" value="${escapeHtml(current)}" aria-label="SKU ${escapeHtml(offer.offerId)} 新库存"><button class="button secondary" type="button" data-stock-update data-offer-id="${escapeHtml(offer.offerId)}" data-current-stock="${escapeHtml(current)}">保存</button></div><small>填 0 可设为售罄</small></td></tr>`;
  }).join("");
  $("inventoryPageLabel").textContent = `第 ${state.inventoryPageIndex + 1} 页 · ${skuCounts.size} 个 SKU`;
  $("inventoryPrevButton").disabled = state.inventoryPageIndex === 0;
  $("inventoryNextButton").disabled = !state.inventoryNextToken;
  $("inventoryState").classList.add("hidden");
  $("inventoryContent").classList.remove("hidden");
  if (!rows.length) {
    $("inventoryState").textContent = "当前筛选下没有库存记录。";
    $("inventoryState").classList.remove("hidden");
    $("inventoryContent").classList.add("hidden");
  }
}

async function loadInventory({ resetPage = false, pageToken = null } = {}) {
  const store = selectedStore();
  if (!store) {
    $("inventoryState").textContent = "请先选择店铺。";
    $("inventoryState").classList.remove("hidden");
    $("inventoryContent").classList.add("hidden");
    return;
  }
  let offerIds;
  try { offerIds = splitSkuValues($("inventorySkuFilter").value); } catch (error) { return toast(error.message, true); }
  if (resetPage) {
    state.inventoryPageTokens = [""]; state.inventoryPageIndex = 0; state.inventoryNextToken = "";
  }
  const token = pageToken === null ? state.inventoryPageTokens[state.inventoryPageIndex] || "" : pageToken;
  const requestId = ++state.inventoryRequestId;
  $("inventoryState").textContent = "正在读取库存…";
  $("inventoryState").classList.remove("hidden");
  $("inventoryContent").classList.add("hidden");
  try {
    const data = await api("/api/inventory", { method: "POST", body: JSON.stringify({ store_id: store.id, offer_ids: offerIds, archived: $("inventoryArchived").value === "true", page_token: token, limit: 100 }) });
    if (requestId !== state.inventoryRequestId) return;
    state.inventoryNextToken = data.paging?.nextPageToken || "";
    renderInventory(data);
  } catch (error) {
    if (requestId !== state.inventoryRequestId) return;
    $("inventoryState").textContent = `库存读取失败：${error.message}`;
    toast(error.message, true);
  }
}

$("inventoryFilterForm").addEventListener("submit", (event) => { event.preventDefault(); loadInventory({ resetPage: true }); });
$("inventoryPrevButton").addEventListener("click", () => { if (state.inventoryPageIndex > 0) { state.inventoryPageIndex -= 1; loadInventory({ pageToken: state.inventoryPageTokens[state.inventoryPageIndex] || "" }); } });
$("inventoryNextButton").addEventListener("click", () => { if (state.inventoryNextToken) { state.inventoryPageIndex += 1; state.inventoryPageTokens[state.inventoryPageIndex] = state.inventoryNextToken; loadInventory({ pageToken: state.inventoryNextToken }); } });
$("inventoryTableBody").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-stock-update]");
  if (!button) return;
  const store = selectedStore();
  const input = button.parentElement.querySelector("input");
  const count = Number(input.value);
  const current = Number(button.dataset.currentStock);
  const offerId = button.dataset.offerId;
  if (!Number.isInteger(count) || count < 0 || count > 2000000000) return toast("库存必须是 0–2000000000 的整数", true);
  if (count === current) return toast("库存没有变化");
  if (!window.confirm(`把 SKU ${offerId} 的可售库存从 ${current} 改为 ${count}？${count === 0 ? "商品将无法继续下单。" : ""}`)) return;
  button.disabled = true;
  try {
    await api("/api/inventory/stock", { method: "PUT", body: JSON.stringify({ store_id: store.id, offer_id: offerId, count }) });
    toast(`SKU ${offerId} 库存已提交为 ${count}`);
    await loadInventory();
  } catch (error) { toast(error.message, true); button.disabled = false; }
});

const RETURN_STATUS = {
  STARTED_BY_USER: "买家已发起", REFUND_IN_PROGRESS: "退款处理中", REFUNDED: "已退款", FAILED: "退款失败",
  WAITING_FOR_DECISION: "等待卖家决定", DECISION_MADE: "已作出决定", PREMODERATION_DECISION_WAITING: "等待卖家决定",
  PREMODERATION_DECISION_MADE: "已作出决定", PREMODERATION_DISPUTE: "争议中", CANCELLED: "已取消", REJECTED: "已拒绝",
  COMPLETE_WITHOUT_REFUND: "无需退款", REFUNDED_WITH_BONUSES: "已用积分退款", REFUNDED_BY_SHOP: "卖家已退款",
};
const RETURN_SHIPMENT = { CREATED: "已创建", RECEIVED: "已接收", IN_TRANSIT: "运输中", READY_FOR_PICKUP: "可领取", PICKED: "已领取", LOST: "丢失", EXPIRED: "已过期", CANCELLED: "已取消", FULFILMENT_RECEIVED: "平台仓已接收", UTILIZED: "已销毁" };

function renderReturns(data) {
  const records = data.returns || [];
  const decisionStatuses = new Set(["WAITING_FOR_DECISION", "PREMODERATION_DECISION_WAITING"]);
  $("returnCount").textContent = String(records.length);
  $("returnDecisionCount").textContent = String(records.filter((item) => decisionStatuses.has(item.refundStatus)).length);
  $("returnPickupCount").textContent = String(records.filter((item) => item.shipmentStatus === "READY_FOR_PICKUP").length);
  $("returnList").innerHTML = records.map((item) => {
    const amount = item.amount || {};
    const products = (item.items || []).map((product) => `<li><strong>SKU ${escapeHtml(product.shopSku || "—")}</strong><span>× ${escapeHtml(product.count ?? "—")}</span>${product.marketSku ? `<small>Market SKU ${escapeHtml(product.marketSku)}</small>` : ""}</li>`).join("");
    const point = item.logisticPickupPoint || {};
    const address = point.address || {};
    const pointText = [point.name, address.city, address.street, address.house].filter(Boolean).join(" · ");
    return `<article class="ops-card return-card"><div class="ops-card-head"><div><span class="ops-kicker">${item.returnType === "UNREDEEMED" ? "未取件" : "退货"}</span><h3>退货 #${escapeHtml(item.id)} · 订单 #${escapeHtml(item.orderId)}</h3></div><div class="ops-status-stack"><span class="status-pill">${escapeHtml(RETURN_STATUS[item.refundStatus] || item.refundStatus || "状态未知")}</span><span>${escapeHtml(RETURN_SHIPMENT[item.shipmentStatus] || item.shipmentStatus || "物流未知")}</span></div></div><div class="return-meta"><span>创建 ${formatDateTime(item.creationDate)}</span><span>更新 ${formatDateTime(item.updateDate)}</span><strong>${formatMoney(amount.value ?? item.refundAmount, amount.currencyId || amount.currency || "RUR")}</strong>${item.fastReturn ? `<span class="fast-return">快速退款</span>` : ""}</div><ul class="return-items">${products || "<li>商品明细未返回</li>"}</ul>${pointText ? `<p class="pickup-point"><strong>领取点：</strong>${escapeHtml(pointText)}${item.pickupTillDate ? ` · 截止 ${formatDateTime(item.pickupTillDate)}` : ""}</p>` : ""}</article>`;
  }).join("");
  $("returnPageLabel").textContent = `第 ${state.returnPageIndex + 1} 页 · 本页 ${records.length} 条`;
  $("returnPrevButton").disabled = state.returnPageIndex === 0;
  $("returnNextButton").disabled = !state.returnNextToken;
  $("returnsState").classList.toggle("hidden", records.length > 0);
  $("returnsState").textContent = records.length ? "" : "当前筛选下没有退货或未取件记录。";
  $("returnsContent").classList.toggle("hidden", !records.length);
}

async function loadReturns({ resetPage = false, pageToken = null } = {}) {
  const store = selectedStore();
  if (!store) { $("returnsState").textContent = "请先选择店铺。"; $("returnsState").classList.remove("hidden"); $("returnsContent").classList.add("hidden"); return; }
  if (resetPage) { state.returnPageTokens = [""]; state.returnPageIndex = 0; state.returnNextToken = ""; }
  const token = pageToken === null ? state.returnPageTokens[state.returnPageIndex] || "" : pageToken;
  const requestId = ++state.returnRequestId;
  $("returnsState").textContent = "正在读取退货与未取件…"; $("returnsState").classList.remove("hidden"); $("returnsContent").classList.add("hidden");
  try {
    const status = $("returnStatus").value;
    const data = await api("/api/returns", { method: "POST", body: JSON.stringify({ store_id: store.id, return_type: $("returnType").value, statuses: status ? [status] : [], date_from: $("returnFrom").value || null, date_to: $("returnTo").value || null, page_token: token, limit: 100 }) });
    if (requestId !== state.returnRequestId) return;
    state.returnNextToken = data.paging?.nextPageToken || "";
    renderReturns(data);
  } catch (error) { if (requestId === state.returnRequestId) { $("returnsState").textContent = `读取失败：${error.message}`; toast(error.message, true); } }
}

$("returnFilterForm").addEventListener("submit", (event) => { event.preventDefault(); loadReturns({ resetPage: true }); });
$("returnPrevButton").addEventListener("click", () => { if (state.returnPageIndex > 0) { state.returnPageIndex -= 1; loadReturns({ pageToken: state.returnPageTokens[state.returnPageIndex] || "" }); } });
$("returnNextButton").addEventListener("click", () => { if (state.returnNextToken) { state.returnPageIndex += 1; state.returnPageTokens[state.returnPageIndex] = state.returnNextToken; loadReturns({ pageToken: state.returnNextToken }); } });

function reviewMedia(media) {
  return (media?.photos || []).slice(0, 4).map((value) => safeOrderUrl(value)).filter(Boolean).map((url) => `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer"><img src="${escapeHtml(url)}" alt="评价图片" loading="lazy" referrerpolicy="no-referrer"></a>`).join("");
}

function renderFeedback(data) {
  const records = data.feedbacks || [];
  $("feedbackCount").textContent = String(records.length);
  $("feedbackNeedCount").textContent = String(records.filter((item) => item.needReaction).length);
  $("feedbackList").innerHTML = records.map((item) => {
    const description = item.description || {};
    const stats = item.statistics || {};
    const ids = item.identifiers || {};
    const stars = "★".repeat(Number(stats.rating || 0)) + "☆".repeat(Math.max(0, 5 - Number(stats.rating || 0)));
    const fields = [["优点", description.advantages], ["不足", description.disadvantages], ["评价", description.comment]].filter(([, value]) => value).map(([label, value]) => `<p><strong>${label}</strong>${escapeHtml(value)}</p>`).join("");
    return `<article class="ops-card feedback-card" data-feedback-id="${escapeHtml(item.feedbackId)}"><div class="ops-card-head"><div><span class="review-stars" aria-label="${escapeHtml(stats.rating || 0)} 星">${stars}</span><h3>${escapeHtml(item.author || "匿名买家")}</h3><small>评价 #${escapeHtml(item.feedbackId)} · SKU ${escapeHtml(ids.offerId || "—")} · ${formatDateTime(item.createdAt)}</small></div>${item.needReaction ? `<span class="need-reaction">待回复</span>` : `<span class="handled">已处理</span>`}</div><div class="review-copy">${fields || "<p>买家未填写文字评价</p>"}</div>${reviewMedia(item.media) ? `<div class="review-media">${reviewMedia(item.media)}</div>` : ""}<form class="inline-reply-form" data-feedback-reply><textarea maxlength="4096" required placeholder="输入店铺回复；不得包含店铺联系方式或非 Yandex 链接"></textarea><div><span>${escapeHtml(stats.commentsCount || 0)} 条店铺回复</span><button class="button primary" type="submit">提交回复</button>${item.needReaction ? `<button class="button ghost" type="button" data-feedback-skip>仅标记已处理</button>` : ""}</div></form></article>`;
  }).join("");
  $("feedbackPageLabel").textContent = `第 ${state.feedbackPageIndex + 1} 页 · 本页 ${records.length} 条`;
  $("feedbackPrevButton").disabled = state.feedbackPageIndex === 0;
  $("feedbackNextButton").disabled = !state.feedbackNextToken;
  $("feedbackState").classList.toggle("hidden", records.length > 0);
  $("feedbackState").textContent = records.length ? "" : "当前筛选下没有评价。";
  $("feedbackContent").classList.toggle("hidden", !records.length);
}

async function loadFeedback({ resetPage = false, pageToken = null } = {}) {
  const store = selectedStore();
  if (!store) { $("feedbackState").textContent = "请先选择店铺。"; $("feedbackState").classList.remove("hidden"); $("feedbackContent").classList.add("hidden"); return; }
  let offerIds;
  try { offerIds = splitSkuValues($("feedbackSku").value, 20); } catch (error) { return toast(error.message, true); }
  if (resetPage) { state.feedbackPageTokens = [""]; state.feedbackPageIndex = 0; state.feedbackNextToken = ""; }
  const token = pageToken === null ? state.feedbackPageTokens[state.feedbackPageIndex] || "" : pageToken;
  const requestId = ++state.feedbackRequestId;
  $("feedbackState").textContent = "正在读取商品评价…"; $("feedbackState").classList.remove("hidden"); $("feedbackContent").classList.add("hidden");
  try {
    const rating = Number($("feedbackRating").value);
    const data = await api("/api/feedback", { method: "POST", body: JSON.stringify({ store_id: store.id, reaction_status: $("feedbackReaction").value, rating_values: rating ? [rating] : [], offer_ids: offerIds, page_token: token, limit: 50 }) });
    if (requestId !== state.feedbackRequestId) return;
    state.feedbackNextToken = data.paging?.nextPageToken || "";
    renderFeedback(data);
  } catch (error) { if (requestId === state.feedbackRequestId) { $("feedbackState").textContent = `读取失败：${error.message}`; toast(error.message, true); } }
}

$("feedbackFilterForm").addEventListener("submit", (event) => { event.preventDefault(); loadFeedback({ resetPage: true }); });
$("feedbackPrevButton").addEventListener("click", () => { if (state.feedbackPageIndex > 0) { state.feedbackPageIndex -= 1; loadFeedback({ pageToken: state.feedbackPageTokens[state.feedbackPageIndex] || "" }); } });
$("feedbackNextButton").addEventListener("click", () => { if (state.feedbackNextToken) { state.feedbackPageIndex += 1; state.feedbackPageTokens[state.feedbackPageIndex] = state.feedbackNextToken; loadFeedback({ pageToken: state.feedbackNextToken }); } });
$("feedbackList").addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-feedback-reply]");
  if (!form) return;
  event.preventDefault();
  const store = selectedStore();
  const feedbackId = Number(form.closest("[data-feedback-id]").dataset.feedbackId);
  const text = form.querySelector("textarea").value.trim();
  if (!text) return toast("请输入回复内容", true);
  if (!window.confirm(`以“${store.alias}”回复评价 #${feedbackId}？`)) return;
  const button = form.querySelector("button[type=submit]"); button.disabled = true;
  try { await api("/api/feedback/reply", { method: "POST", body: JSON.stringify({ store_id: store.id, feedback_id: feedbackId, text }) }); toast("评价回复已提交审核"); await loadFeedback(); } catch (error) { toast(error.message, true); button.disabled = false; }
});
$("feedbackList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-feedback-skip]");
  if (!button) return;
  const store = selectedStore();
  const feedbackId = Number(button.closest("[data-feedback-id]").dataset.feedbackId);
  if (!window.confirm(`将评价 #${feedbackId} 标记为已处理且不回复？`)) return;
  button.disabled = true;
  try { await api("/api/feedback/skip", { method: "POST", body: JSON.stringify({ store_id: store.id, feedback_ids: [feedbackId] }) }); toast("评价已标记为处理"); await loadFeedback(); } catch (error) { toast(error.message, true); button.disabled = false; }
});

function renderQuestions(data) {
  const records = data.questions || [];
  const pendingOnly = $("questionNeedAnswer").value === "true";
  $("questionCount").textContent = String(records.length);
  $("questionTotal").textContent = `符合条件 ${data.totalCount ?? "—"} 条`;
  $("questionList").innerHTML = records.map((item) => {
    const ids = item.questionIdentifiers || {};
    const author = item.author || {};
    const votes = item.votes || {};
    return `<article class="ops-card question-card" data-question-id="${escapeHtml(ids.id)}"><div class="ops-card-head"><div><span class="ops-kicker">SKU ${escapeHtml(ids.offerId || "—")}</span><h3>${escapeHtml(author.name || "买家")} 的问题</h3><small>问题 #${escapeHtml(ids.id || "—")} · ${formatDateTime(item.createdAt)} · 👍 ${escapeHtml(votes.likes || 0)} / 👎 ${escapeHtml(votes.dislikes || 0)}</small></div>${pendingOnly ? `<span class="need-reaction">待回答</span>` : ""}</div><blockquote>${escapeHtml(item.text || "")}</blockquote><form class="inline-reply-form" data-question-reply><textarea maxlength="5000" required placeholder="输入准确、清晰的商品回答"></textarea><div><span>回复将公开展示在商品问答中</span><button class="button primary" type="submit">提交回答</button></div></form></article>`;
  }).join("");
  $("questionPageLabel").textContent = `第 ${state.questionPageIndex + 1} 页 · 本页 ${records.length} 条`;
  $("questionPrevButton").disabled = state.questionPageIndex === 0;
  $("questionNextButton").disabled = !state.questionNextToken;
  $("questionsState").classList.toggle("hidden", records.length > 0);
  $("questionsState").textContent = records.length ? "" : "当前筛选下没有商品问题。";
  $("questionsContent").classList.toggle("hidden", !records.length);
}

async function loadQuestions({ resetPage = false, pageToken = null } = {}) {
  const store = selectedStore();
  if (!store) { $("questionsState").textContent = "请先选择店铺。"; $("questionsState").classList.remove("hidden"); $("questionsContent").classList.add("hidden"); return; }
  const from = $("questionFrom").value; const to = $("questionTo").value;
  if (from && to) {
    const days = Math.round((new Date(`${to}T00:00:00`) - new Date(`${from}T00:00:00`)) / 86400000);
    if (days < 0 || days > 30) return toast("问题查询日期必须按顺序且不超过 31 天", true);
  }
  if (resetPage) { state.questionPageTokens = [""]; state.questionPageIndex = 0; state.questionNextToken = ""; }
  const token = pageToken === null ? state.questionPageTokens[state.questionPageIndex] || "" : pageToken;
  const requestId = ++state.questionRequestId;
  $("questionsState").textContent = "正在读取商品问题…"; $("questionsState").classList.remove("hidden"); $("questionsContent").classList.add("hidden");
  try {
    const data = await api("/api/questions", { method: "POST", body: JSON.stringify({ store_id: store.id, need_answer: $("questionNeedAnswer").value === "true", date_from: from || null, date_to: to || null, page_token: token, limit: 50 }) });
    if (requestId !== state.questionRequestId) return;
    state.questionNextToken = data.paging?.nextPageToken || "";
    renderQuestions(data);
  } catch (error) { if (requestId === state.questionRequestId) { $("questionsState").textContent = `读取失败：${error.message}`; toast(error.message, true); } }
}

$("questionFilterForm").addEventListener("submit", (event) => { event.preventDefault(); loadQuestions({ resetPage: true }); });
$("questionPrevButton").addEventListener("click", () => { if (state.questionPageIndex > 0) { state.questionPageIndex -= 1; loadQuestions({ pageToken: state.questionPageTokens[state.questionPageIndex] || "" }); } });
$("questionNextButton").addEventListener("click", () => { if (state.questionNextToken) { state.questionPageIndex += 1; state.questionPageTokens[state.questionPageIndex] = state.questionNextToken; loadQuestions({ pageToken: state.questionNextToken }); } });
$("questionList").addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-question-reply]");
  if (!form) return;
  event.preventDefault();
  const store = selectedStore();
  const questionId = Number(form.closest("[data-question-id]").dataset.questionId);
  const text = form.querySelector("textarea").value.trim();
  if (!text) return toast("请输入回答内容", true);
  if (!window.confirm(`以“${store.alias}”回答问题 #${questionId}？`)) return;
  const button = form.querySelector("button[type=submit]"); button.disabled = true;
  try { await api("/api/questions/reply", { method: "POST", body: JSON.stringify({ store_id: store.id, question_id: questionId, text }) }); toast("商品回答已提交"); form.reset(); } catch (error) { toast(error.message, true); button.disabled = false; }
});

document.querySelectorAll("[data-feedback-tab]").forEach((button) => button.addEventListener("click", () => {
  const reviews = button.dataset.feedbackTab === "reviews";
  document.querySelectorAll("[data-feedback-tab]").forEach((item) => item.classList.toggle("active", item === button));
  $("reviewsPanel").classList.toggle("hidden", !reviews);
  $("questionsPanel").classList.toggle("hidden", reviews);
  if (!reviews) loadQuestions({ resetPage: true });
}));

function renderZeshunStores() {
  $("zeshunStoreCount").textContent = state.zeshunStores.length;
  $("zeshunStoreEmpty").classList.toggle("hidden", state.zeshunStores.length > 0);
  $("zeshunStoreList").innerHTML = state.zeshunStores.map((store) => {
    const actualName = store.store_name || store.business_name || "尚未连接 Yandex 店铺";
    const authLink = store.authorization_url
      ? `<div class="authorization-link-row"><a href="${escapeHtml(store.authorization_url)}" target="_blank" rel="noreferrer">${escapeHtml(store.authorization_url)}</a><button type="button" data-action="copy-authorization-link">复制</button></div>`
      : `<span>未配置授权链接，请编辑店铺补充</span>`;
    return `
      <article class="authorization-card" data-zeshun-store-id="${store.id}">
        <div class="authorization-card-head">
          <div class="authorization-card-title">
            <strong>${escapeHtml(store.alias)}</strong>
            <small>${escapeHtml(actualName)}</small>
          </div>
          <span class="authorization-status${store.authorized ? " ok" : ""}">${store.authorized ? "TOKEN 已更新" : "等待授权"}</span>
        </div>
        <div class="authorization-details">
          <div class="authorization-detail"><span>TG 码</span><code>${escapeHtml(store.tg_code)}</code></div>
          <div class="authorization-detail"><span>授权链接</span>${authLink}</div>
        </div>
        <form class="authorization-result-form" data-authorization-form>
          <div class="field">
            <label>授权后的链接</label>
            <input name="authorizedUrl" type="url" maxlength="8000" autocomplete="off" spellcheck="false" placeholder="粘贴浏览器授权完成后的完整链接">
            <small>支持从 query 或 #fragment 中读取 token</small>
          </div>
          <div class="field">
            <label>token（链接中没有时填写）</label>
            <input name="token" type="password" autocomplete="off" spellcheck="false" placeholder="ACMA:… / access_token">
            <small>重复提交会更新数据库中的 token</small>
          </div>
          <button class="button secondary" type="submit">${store.authorized ? "更新 token" : "保存授权"}</button>
        </form>
        <div class="authorization-card-actions">
          <button type="button" data-action="edit-zeshun-store">编辑名称/授权链接</button>
          <button type="button" data-action="delete-zeshun-store" class="danger-link">删除授权记录</button>
        </div>
      </article>`;
  }).join("");
}

async function loadZeshunStores() {
  const data = await api("/api/zeshun-stores");
  state.zeshunStores = data.stores || [];
  renderZeshunStores();
}

$("zeshunStoreForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const alias = $("zeshunStoreAlias").value.trim();
  const tgCode = $("zeshunTgCode").value.trim();
  const authorizationUrl = $("zeshunAuthorizationUrl").value.trim();
  if (!alias || !tgCode) return toast("请输入自定义店铺名和 TG 码", true);
  const button = $("addZeshunStoreButton");
  button.disabled = true;
  button.textContent = "正在新增…";
  try {
    await api("/api/zeshun-stores", {
      method: "POST",
      body: JSON.stringify({ alias, tg_code: tgCode, authorization_url: authorizationUrl }),
    });
    $("zeshunStoreAlias").value = "";
    $("zeshunTgCode").value = "";
    $("zeshunAuthorizationUrl").value = "";
    await loadZeshunStores();
    toast(`授权店铺已新增：${alias}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "新增店铺";
  }
});

$("zeshunStoreList").addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-authorization-form]");
  if (!form) return;
  event.preventDefault();
  const card = form.closest("[data-zeshun-store-id]");
  const storeId = Number(card.dataset.zeshunStoreId);
  const authorizedUrl = form.elements.authorizedUrl.value.trim();
  const token = form.elements.token.value.trim();
  if (!authorizedUrl && !token) return toast("请粘贴授权后的链接或填写 token", true);
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  button.textContent = "正在验证…";
  try {
    const data = await api(`/api/zeshun-stores/${storeId}/authorize`, {
      method: "POST",
      body: JSON.stringify({ authorized_url: authorizedUrl, token: token || null }),
    });
    await Promise.all([loadZeshunStores(), loadStores(data.store.id)]);
    toast(`token 已更新：${data.store.alias}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("zeshunStoreList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  const card = event.target.closest("[data-zeshun-store-id]");
  if (!button || !card) return;
  const storeId = Number(card.dataset.zeshunStoreId);
  const store = state.zeshunStores.find((item) => item.id === storeId);
  if (!store) return;
  const action = button.dataset.action;
  if (action === "copy-authorization-link") {
    try {
      await navigator.clipboard.writeText(store.authorization_url);
      toast("授权链接已复制");
    } catch (_) {
      window.prompt("复制授权链接", store.authorization_url);
    }
    return;
  }
  if (action === "edit-zeshun-store") {
    const alias = window.prompt("输入新的自定义店铺名", store.alias)?.trim();
    if (!alias) return;
    const authorizationUrl = window.prompt("输入授权链接；留空时使用系统配置", store.authorization_url || "")?.trim();
    if (authorizationUrl === undefined) return;
    try {
      await api(`/api/zeshun-stores/${storeId}`, {
        method: "PATCH",
        body: JSON.stringify({ alias, authorization_url: authorizationUrl }),
      });
      await Promise.all([loadZeshunStores(), loadStores()]);
      toast("授权店铺信息已更新");
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (action === "delete-zeshun-store") {
    if (!window.confirm(`确定删除“${store.alias}”的授权记录吗？已连接的 Yandex 上传店铺不会删除。`)) return;
    try {
      await api(`/api/zeshun-stores/${storeId}`, { method: "DELETE" });
      await loadZeshunStores();
      toast("授权记录已删除");
    } catch (error) { toast(error.message, true); }
  }
});

function validPricePercent() {
  const value = Number(pricePercentInput.value);
  return Number.isFinite(value) && value >= 1 && value <= 1000;
}

function pricePercentValue() {
  if (!validPricePercent()) throw new Error("上架价格比例必须在 1%–1000% 之间");
  return Number(pricePercentInput.value);
}

function packageValue() {
  const values = Object.fromEntries(
    Object.entries(packageInputs).map(([name, input]) => [name, Number(input.value)])
  );
  if (Object.values(values).some((value) => !Number.isFinite(value) || value <= 0 || value > 1000)) {
    throw new Error("请完整填写包装长度、宽度、高度和毛重，数值必须大于 0");
  }
  return values;
}

function validPackage() {
  try { packageValue(); return true; } catch (_) { return false; }
}

function initialStockValue() {
  const value = Number(initialStockInput.value);
  if (!Number.isInteger(value) || value < 1 || value > 2000000000) {
    throw new Error("初始可售库存必须是 1–2000000000 之间的整数");
  }
  return value;
}

function validInitialStock() {
  try { initialStockValue(); return true; } catch (_) { return false; }
}

function updatePackageStatus() {
  const status = $("packageStatus");
  if (validPackage() && validInitialStock()) {
    const values = packageValue();
    const initialStock = initialStockValue();
    status.className = "package-status ready";
    status.textContent = `将提交：${values.length} × ${values.width} × ${values.height} cm / ${values.weight} kg；每个商品初始库存 ${initialStock} 件`;
  } else if (!validPackage()) {
    status.className = "package-status";
    status.textContent = "请完整填写包装长度、宽度、高度和毛重。";
  } else {
    status.className = "package-status";
    status.textContent = "请填写真实的初始可售库存（正整数）。";
  }
  try {
    const raw = Object.fromEntries(
      Object.entries(packageInputs).map(([name, input]) => [name, input.value])
    );
    raw.initialStock = initialStockInput.value;
    window.localStorage.setItem(PACKAGE_STORAGE_KEY, JSON.stringify(raw));
  } catch (_) { /* Browser storage may be disabled; uploading still works. */ }
  updatePublishButton();
}

function restorePackageValues() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(PACKAGE_STORAGE_KEY) || "{}");
    Object.entries(packageInputs).forEach(([name, input]) => {
      if (saved[name] !== undefined) input.value = String(saved[name]);
    });
    if (saved.initialStock !== undefined) initialStockInput.value = String(saved.initialStock);
  } catch (_) { /* Ignore unavailable or malformed browser storage. */ }
  updatePackageStatus();
}

Object.values(packageInputs).forEach((input) => input.addEventListener("input", updatePackageStatus));
initialStockInput.addEventListener("input", updatePackageStatus);

async function loadExchangeRate(forceRefresh = false) {
  const text = $("exchangeRateText");
  const button = $("refreshExchangeRate");
  button.disabled = true;
  text.textContent = "正在读取 RUB/CNY 汇率…";
  try {
    const data = await api(`/api/exchange-rate${forceRefresh ? "?refresh=true" : ""}`);
    state.exchangeRate = data.exchange_rate;
    const rate = Number(state.exchangeRate.rate);
    text.textContent = `1 ₽ = ${rate.toFixed(6)} ¥（央行 ${state.exchangeRate.effective_date}）`;
    if (forceRefresh) toast("RUB/CNY 汇率已更新");
  } catch (error) {
    state.exchangeRate = null;
    text.textContent = "汇率读取失败";
    toast(error.message, true);
  } finally {
    button.disabled = false;
    updatePublishButton();
  }
}

$("refreshExchangeRate").addEventListener("click", () => loadExchangeRate(true));
pricePercentInput.addEventListener("input", updatePublishButton);

function setStoreBadge() {
  const badge = $("connectionBadge");
  const store = selectedStore();
  if (!store) {
    badge.className = "badge neutral";
    badge.innerHTML = "<span></span>未选择店铺";
    return;
  }
  badge.className = "badge connected";
  badge.innerHTML = `<span></span>${escapeHtml(store.alias)} · ${escapeHtml(store.store_name || store.business_name)}`;
}

function renderGlobalStoreSelect() {
  const select = $("globalStoreSelect");
  if (!state.stores.length) {
    select.innerHTML = `<option value="">暂无已连接店铺</option>`;
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = state.stores.map((store) => `<option value="${store.id}">${escapeHtml(store.alias)} · ${escapeHtml(store.placement_type || "Yandex")}</option>`).join("");
  select.value = state.selectedStoreId ? String(state.selectedStoreId) : "";
}

function renderStores() {
  $("storeCount").textContent = state.stores.length;
  $("storeEmpty").classList.toggle("hidden", state.stores.length > 0);
  $("storeList").innerHTML = state.stores.map((store) => {
    const isSelected = store.id === state.selectedStoreId;
    const actualName = store.store_name || store.business_name || `Campaign ${store.campaign_id}`;
    return `
      <article class="store-card${isSelected ? " selected" : ""}" data-store-id="${store.id}">
        <button class="store-select" type="button" data-action="select" aria-label="选择 ${escapeHtml(store.alias)}">
          <span class="radio-dot"></span>
          <span class="store-main">
            <strong>${escapeHtml(store.alias)}</strong>
            <small>Yandex：${escapeHtml(actualName)}</small>
          </span>
        </button>
        <div class="store-meta">
          <span>${escapeHtml(store.placement_type || "模式未知")}</span>
          <span class="availability ${store.api_availability === "AVAILABLE" ? "ok" : ""}">${escapeHtml(store.api_availability || "API 状态未知")}</span>
        </div>
        <div class="store-actions">
          <button type="button" data-action="refresh">重新连接</button>
          <button type="button" data-action="rename">重命名</button>
          <button type="button" data-action="delete" class="danger-link">删除</button>
        </div>
      </article>`;
  }).join("");
  renderGlobalStoreSelect();
  setStoreBadge();
  updatePublishButton();
}

async function loadStores(preferredId = null) {
  const data = await api("/api/stores");
  state.stores = data.stores || [];
  const candidate = preferredId ?? state.selectedStoreId;
  if (state.stores.some((store) => store.id === candidate)) {
    state.selectedStoreId = candidate;
  } else {
    state.selectedStoreId = state.stores[0]?.id || null;
  }
  renderStores();
  if (state.currentView === "orders") await loadOrders({ resetPage: true });
  if (state.currentView === "listings") await loadListings({ resetPage: true });
  if (state.currentView === "inventory") await loadInventory({ resetPage: true });
  if (state.currentView === "returns") await loadReturns({ resetPage: true });
  if (state.currentView === "feedback") await loadFeedback({ resetPage: true });
}

$("globalStoreSelect").addEventListener("change", () => {
  const nextId = Number($("globalStoreSelect").value) || null;
  if (nextId === state.selectedStoreId) return;
  state.selectedStoreId = nextId;
  state.orders = [];
  state.listings = [];
  state.listingSelection.clear();
  state.listingRequestId += 1;
  state.inventoryRequestId += 1;
  state.returnRequestId += 1;
  state.feedbackRequestId += 1;
  state.questionRequestId += 1;
  renderStores();
  if (state.currentView === "orders") loadOrders({ resetPage: true });
  if (state.currentView === "listings") loadListings({ resetPage: true });
  if (state.currentView === "inventory") loadInventory({ resetPage: true });
  if (state.currentView === "returns") loadReturns({ resetPage: true });
  if (state.currentView === "feedback") loadFeedback({ resetPage: true });
});

$("storeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const alias = $("storeAlias").value.trim();
  const token = $("storeToken").value.trim();
  if (!alias || !token) return toast("请输入自定义店铺名和 token", true);
  const button = $("addStoreButton");
  button.disabled = true;
  button.textContent = "正在连接…";
  try {
    const data = await api("/api/stores", {
      method: "POST",
      body: JSON.stringify({ alias, token }),
    });
    $("storeAlias").value = "";
    $("storeToken").value = "";
    await loadStores(data.store.id);
    toast(`${data.created ? "已添加" : "已更新"}店铺：${data.store.alias}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "添加店铺";
  }
});

$("storeList").addEventListener("click", async (event) => {
  const actionButton = event.target.closest("[data-action]");
  const card = event.target.closest("[data-store-id]");
  if (!actionButton || !card) return;
  const storeId = Number(card.dataset.storeId);
  const store = state.stores.find((item) => item.id === storeId);
  if (!store) return;
  const action = actionButton.dataset.action;
  if (action === "select") {
    state.selectedStoreId = storeId;
    state.orders = [];
    renderStores();
    if (state.currentView === "orders") loadOrders({ resetPage: true });
    toast(`当前店铺已切换为：${store.alias}`);
    return;
  }
  if (action === "rename") {
    const alias = window.prompt("输入新的自定义店铺名", store.alias)?.trim();
    if (!alias || alias === store.alias) return;
    try {
      await api(`/api/stores/${storeId}`, { method: "PATCH", body: JSON.stringify({ alias }) });
      await loadStores(storeId);
      toast("店铺名称已更新");
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (action === "delete") {
    if (!window.confirm(`确定删除店铺“${store.alias}”吗？中央数据库中的授权 token 也会删除。`)) return;
    try {
      await api(`/api/stores/${storeId}`, { method: "DELETE" });
      if (state.selectedStoreId === storeId) state.selectedStoreId = null;
      await loadStores();
      toast("店铺已删除");
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (action === "refresh") {
    actionButton.disabled = true;
    try {
      await api(`/api/stores/${storeId}/refresh`, { method: "POST" });
      await loadStores(storeId);
      toast(`店铺连接正常：${store.alias}`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      actionButton.disabled = false;
    }
  }
});

$("toggleStoreToken").addEventListener("click", () => {
  const input = $("storeToken");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  $("toggleStoreToken").textContent = showing ? "显示" : "隐藏";
});

function formatPrice(product) {
  if (!product.price) return "价格待核对";
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(product.price)} ₽`;
}

function updatePublishButton() {
  const store = selectedStore();
  const ratioValid = validPricePercent();
  const packageValid = validPackage();
  const stockValid = validInitialStock();
  publishButton.disabled = state.selectedProducts.size === 0 || !store || !state.exchangeRate || !ratioValid || !packageValid || !stockValid;
  if (!store) {
    publishButton.textContent = "请先选择店铺";
  } else if (!state.exchangeRate) {
    publishButton.textContent = "人民币汇率未就绪";
  } else if (!ratioValid) {
    publishButton.textContent = "价格比例有误";
  } else if (!packageValid) {
    publishButton.textContent = "请填写包装参数";
  } else if (!stockValid) {
    publishButton.textContent = "请填写初始库存";
  } else if (state.selectedProducts.size) {
    publishButton.textContent = `上传 ${state.selectedProducts.size} 个到“${store.alias}”`;
  } else {
    publishButton.textContent = "上传选中商品";
  }
  const readyIds = state.products.filter((p) => p.ready_to_publish).map((p) => p.id);
  $("selectAll").checked = readyIds.length > 0 && readyIds.every((id) => state.selectedProducts.has(id));
}

function renderProducts(products) {
  state.products = products;
  $("resultCount").textContent = products.length;
  resultsSection.classList.toggle("hidden", products.length === 0);
  const grid = $("productGrid");
  grid.innerHTML = products.map((product) => {
    const selected = state.selectedProducts.has(product.id);
    const missing = product.missing_publish_fields || [];
    const image = product.pictures?.[0] || "";
    const pictureCount = product.pictures?.length || 0;
    const specificationCount = Object.keys(product.specifications || {}).length;
    const descriptionLength = Array.from((product.description || "").trim()).length;
    return `
      <article class="product-card${selected ? " selected" : ""}" data-id="${product.id}">
        <input class="product-select" type="checkbox" data-product-id="${product.id}"
          ${selected ? "checked" : ""} ${product.ready_to_publish ? "" : "disabled"}
          aria-label="选择 ${escapeHtml(product.name)}">
        ${image ? `<img class="product-image" src="${escapeHtml(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<div class="product-image"></div>`}
        <div class="product-body">
          <span class="product-tag">国外发货</span>
          <p class="product-name">${escapeHtml(product.name)}</p>
          <div class="product-meta">${escapeHtml(product.vendor || "品牌待核对")} · SKU ${escapeHtml(product.market_sku || "待识别")}</div>
          <div class="product-meta quality-metrics">主图 ${pictureCount} 张 · 规格 ${specificationCount} 项 · 描述 ${descriptionLength} 字</div>
          <div class="product-price">${formatPrice(product)}</div>
          <div class="product-foot">
            <a href="${escapeHtml(product.source_url)}" target="_blank" rel="noreferrer">查看来源 ↗</a>
            <span class="readiness${product.ready_to_publish ? "" : " warn"}">${product.ready_to_publish ? "必要字段齐全，可发布" : `待补全 ${escapeHtml(missing.join(", "))}`}</span>
          </div>
        </div>
      </article>`;
  }).join("");
  grid.querySelectorAll(".product-select").forEach((checkbox) => {
    checkbox.addEventListener("change", (event) => {
      const id = Number(event.target.dataset.productId);
      if (event.target.checked) state.selectedProducts.add(id); else state.selectedProducts.delete(id);
      event.target.closest(".product-card").classList.toggle("selected", event.target.checked);
      updatePublishButton();
    });
  });
  updatePublishButton();
}

function updateSearchStatus(run) {
  statusPanel.classList.remove("hidden");
  $("statusTitle").textContent = ({ queued: "等待开始", running: "正在抓取", completed: "抓取完成", failed: "抓取失败" })[run.status] || run.status;
  $("statusCount").textContent = `${run.found_count} / ${run.requested_count}`;
  $("statusMessage").textContent = run.message || "";
  const percent = Math.min(100, Math.round((run.found_count / run.requested_count) * 100));
  const progress = $("progressBar");
  progress.classList.toggle("indeterminate", run.status === "queued" && !run.found_count);
  progress.style.width = run.status === "completed" ? "100%" : `${percent}%`;
}

async function pollSearch() {
  if (!state.runId) return;
  try {
    const data = await api(`/api/search/${state.runId}`);
    updateSearchStatus(data.run);
    renderProducts(data.products);
    if (["completed", "failed"].includes(data.run.status)) {
      clearInterval(state.searchPollTimer);
      state.searchPollTimer = null;
      searchButton.disabled = false;
      toast(data.run.message, data.run.status === "failed");
    }
  } catch (error) {
    clearInterval(state.searchPollTimer);
    state.searchPollTimer = null;
    searchButton.disabled = false;
    toast(error.message, true);
  }
}

searchButton.addEventListener("click", async () => {
  const keyword = keywordInput.value.trim();
  const count = Number(countInput.value || 200);
  if (!keyword) return toast("请输入搜索关键词", true);
  if (!Number.isInteger(count) || count < 1 || count > Number(countInput.max)) {
    return toast(`商品个数必须在 1–${countInput.max} 之间`, true);
  }
  state.requestedCount = count;
  state.products = [];
  state.selectedProducts.clear();
  renderProducts([]);
  searchButton.disabled = true;
  try {
    const data = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({ keyword, count }),
    });
    state.runId = data.run_id;
    updateSearchStatus({ status: "queued", found_count: 0, requested_count: count, message: "任务已创建" });
    clearInterval(state.searchPollTimer);
    state.searchPollTimer = setInterval(pollSearch, 1600);
    await pollSearch();
  } catch (error) {
    searchButton.disabled = false;
    toast(error.message, true);
  }
});

$("selectAll").addEventListener("change", (event) => {
  state.products.filter((p) => p.ready_to_publish).forEach((product) => {
    if (event.target.checked) state.selectedProducts.add(product.id);
    else state.selectedProducts.delete(product.id);
  });
  renderProducts(state.products);
});

function resetPublishProgress(total, store, pricePercent, packageData, initialStock) {
  clearTimeout(state.publishPollTimer);
  $("publishPanel").classList.remove("hidden");
  $("publishTitle").textContent = "正在上传商品";
  $("publishStoreName").textContent = `目标店铺：${store.alias} · RUB × ${state.exchangeRate.rate.toFixed(6)} × ${pricePercent}% → CNY · 包装 ${packageData.length}×${packageData.width}×${packageData.height} cm / ${packageData.weight} kg · 每个商品库存 ${initialStock} 件`;
  $("publishCount").textContent = `0 / ${total}`;
  $("publishProgressBar").style.width = "0%";
  $("publishProcessed").textContent = "0";
  $("publishSucceeded").textContent = "0";
  $("publishFailed").textContent = "0";
  $("publishResults").innerHTML = `<div class="result-pending">Yandex 正在逐个接收商品，请保持页面打开…</div>`;
  $("publishPanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderPublishJob(job) {
  const processed = Number(job.processed ?? ((job.succeeded || 0) + (job.failed || 0)));
  const total = Number(job.total || 0);
  const percent = total ? Math.min(100, Math.round((processed / total) * 100)) : 0;
  $("publishCount").textContent = `${processed} / ${total}`;
  $("publishProgressBar").style.width = `${percent}%`;
  $("publishProcessed").textContent = processed;
  $("publishSucceeded").textContent = job.succeeded || 0;
  $("publishFailed").textContent = job.failed || 0;
  $("publishTitle").textContent = job.status === "running" ? "正在上传商品" : "上传任务已完成";
  const store = selectedStore();
  if (job.exchange_rate && store) {
    const dimensions = job.package || {};
    const packageText = dimensions.length
      ? ` · 包装 ${dimensions.length}×${dimensions.width}×${dimensions.height} cm / ${dimensions.weight} kg`
      : "";
    const stockText = job.initial_stock
      ? ` · 每个商品库存 ${job.initial_stock} 件${job.warehouse_name ? `（${job.warehouse_name}）` : ""}`
      : "";
    $("publishStoreName").textContent = `目标店铺：${store.alias} · RUB × ${Number(job.exchange_rate).toFixed(6)} × ${Number(job.price_percent)}% → ${job.target_currency || "CNY"}${packageText}${stockText}`;
  }
  const results = job.results || [];
  $("publishResults").innerHTML = results.length ? results.map((result) => `
    <div class="publish-result ${result.pending ? "pending" : (result.success ? "success" : "failure")}">
      <span class="result-icon">${result.pending ? "…" : (result.success ? "✓" : "!")}</span>
      <span class="result-product">
        <strong>${escapeHtml(result.product_name || `商品 #${result.product_id}`)}</strong>
        <small>${escapeHtml(result.offer_id || "未生成 offer ID")}</small>
      </span>
      <span class="result-message">${escapeHtml(result.message || (result.pending ? "等待写库存" : (result.success ? "已提交" : "提交失败")))}</span>
    </div>`).join("") : `<div class="result-pending">Yandex 正在逐个接收商品，请保持页面打开…</div>`;
}

async function pollPublish(jobId) {
  try {
    const data = await api(`/api/publish/${jobId}`);
    const job = data.job;
    renderPublishJob(job);
    if (job.status === "running") {
      state.publishPollTimer = setTimeout(() => pollPublish(jobId), 900);
      return;
    }
    const successfulIds = new Set((job.results || []).filter((item) => item.success).map((item) => item.product_id));
    successfulIds.forEach((id) => state.selectedProducts.delete(id));
    renderProducts(state.products);
    toast(`上传完成：成功 ${job.succeeded}，失败 ${job.failed}`, job.failed > 0);
  } catch (error) {
    toast(error.message, true);
  } finally {
    updatePublishButton();
  }
}

publishButton.addEventListener("click", async () => {
  const store = selectedStore();
  if (!store) return toast("请先选择上传店铺", true);
  if (!state.selectedProducts.size) return;
  let pricePercent;
  try { pricePercent = pricePercentValue(); } catch (error) { return toast(error.message, true); }
  let packageData;
  try { packageData = packageValue(); } catch (error) { return toast(error.message, true); }
  let initialStock;
  try { initialStock = initialStockValue(); } catch (error) { return toast(error.message, true); }
  if (!state.exchangeRate) return toast("请先刷新 RUB/CNY 汇率", true);
  const selectedIds = [...state.selectedProducts];
  const sample = state.products.find((item) => state.selectedProducts.has(item.id) && item.price);
  const example = sample
    ? `例如 ${Number(sample.price).toFixed(2)} RUB → ${(Number(sample.price) * Number(state.exchangeRate.rate) * pricePercent / 100).toFixed(2)} CNY。`
    : "";
  const packageMessage = `本批统一包装：${packageData.length}×${packageData.width}×${packageData.height} cm，毛重 ${packageData.weight} kg。`;
  const stockMessage = `程序会恢复商品展示，并向店铺仓库为每个商品写入 ${initialStock} 件可售库存。`;
  if (!window.confirm(`将把 ${selectedIds.length} 个商品提交到“${store.alias}”。价格按俄罗斯央行汇率换算成人民币，再乘以 ${pricePercent}%。${example}${packageMessage}${stockMessage}请确认库存真实可履约，是否继续？`)) return;
  publishButton.disabled = true;
  resetPublishProgress(selectedIds.length, store, pricePercent, packageData, initialStock);
  try {
    const data = await api("/api/publish", {
      method: "POST",
      body: JSON.stringify({
        store_id: store.id,
        product_ids: selectedIds,
        price_percent: pricePercent,
        package: packageData,
        initial_stock: initialStock,
      }),
    });
    renderPublishJob(data.job);
    await pollPublish(data.job_id);
  } catch (error) {
    $("publishTitle").textContent = "上传任务未能启动";
    $("publishResults").innerHTML = `<div class="publish-result failure"><span class="result-icon">!</span><span class="result-message">${escapeHtml(error.message)}</span></div>`;
    toast(error.message, true);
    updatePublishButton();
  }
});

async function checkBackend() {
  try {
    await api("/api/health");
  } catch (error) {
    const badge = $("connectionBadge");
    badge.className = "badge error";
    badge.innerHTML = "<span></span>本地服务未启动";
    toast(error.message, true);
  }
}

initializeOrderDates();
initializeOpsDates();
restorePackageValues();
Promise.all([checkBackend(), loadZeshunStores(), loadStores(), loadExchangeRate()]).catch((error) => toast(error.message, true));
