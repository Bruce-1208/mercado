"""Read authoritative PPPI visibility; moderation/case APIs have a wider scope."""

from datetime import datetime
from urllib.parse import urlencode

from erp.mercadolibre_infraction_store import replace_pppi_snapshot


SITE_TITLES = {
    "MLM": "Mexico", "MLB": "Brazil", "MLC": "Chile", "MCO": "Colombia",
    "MLA": "Argentina", "MLU": "Uruguay",
}


def normalize_page(state, site_id, tab, offset):
    """Reject login pages, wrong sites/tabs and incomplete/changed pagination."""
    if not isinstance(state, dict) or state.get("selectedTab") != tab:
        raise ValueError("PPPI 页面未加载或标签不匹配，保留上次快照")
    if not str(state.get("title") or "").endswith(f"- {SITE_TITLES[site_id]}"):
        raise ValueError(f"PPPI 页面站点不是 {site_id}，保留上次快照")
    data = state.get("infractions")
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("PPPI 明细结构异常，保留上次快照")
    paging = data.get("paging") or {}
    total = int(state["detectionsTotal" if tab == "detections" else "denouncesTotal"])
    if int(paging.get("total", -1)) != total or int(paging.get("offset", -1)) != offset:
        raise ValueError("PPPI 分页与总数不一致，保留上次快照")
    limit = int(paging.get("limit", 0))
    if total < 0 or limit <= 0 or len(data["results"]) != min(limit, max(0, total - offset)):
        raise ValueError("PPPI 分页不完整，保留上次快照")
    rows = []
    for raw in data["results"]:
        item_id = str(raw.get("element_id") or "").upper()
        source_id = str(raw.get("case_id") or "")
        if not item_id.startswith(site_id) or not source_id:
            raise ValueError("PPPI 商品或案件编号缺失/站点不匹配")
        item = raw.get("item") or {}
        code = str(raw.get("related_filter_name") or "")
        rows.append({
            "source_type": "detection" if tab == "detections" else "rights_holder",
            "source_id": source_id, "item_id": item_id, "site_id": site_id,
            "title": str(item.get("title") or ""),
            "thumbnail_url": str(item.get("picture") or ""),
            "occurred_at": datetime.strptime(raw["date_created"], "%m/%d/%y").strftime("%Y-%m-%d 00:00:00"),
            "reason_code": code or "PPPI_PAGE",
            "reason": "Intellectual policy infringement" if tab == "detections" else code,
            "rights_holder": str(raw.get("denouncer") or "") if tab == "denounces" else "",
        })
    return rows, total, limit


def collect_tab(page, site_id, tab):
    rows, seen, expected, offset = [], set(), None, 0
    while True:
        page.goto(
            "https://global-selling.mercadolibre.com/noindex/pppi/infractions?"
            + urlencode({"tab": tab, "offset": offset}),
            wait_until="domcontentloaded", timeout=120000,
        )
        page.wait_for_function(
            "() => window.__PRELOADED_STATE__?.body?.container?.infractions",
            timeout=60000,
        )
        state = page.evaluate("window.__PRELOADED_STATE__.body.container")
        batch, total, limit = normalize_page(state, site_id, tab, offset)
        if expected is not None and expected != total:
            raise ValueError("PPPI 读取期间总数发生变化，请重试；保留上次快照")
        expected = total
        for row in batch:
            if row["source_id"] in seen:
                raise ValueError("PPPI 分页重复，保留上次快照")
            seen.add(row["source_id"])
            rows.append(row)
        if len(rows) == total:
            return rows
        offset += limit


def sync_site_page(page, record, site_id):
    detections = collect_tab(page, site_id, "detections")
    reports = collect_tab(page, site_id, "denounces")
    replace_pppi_snapshot(record, site_id, [*detections, *reports])
    return len(detections), len(reports)


def sync_store_pages(record, site_ids=None):
    # Lazy import keeps API-only collection usable without a browser runtime.
    from bit.bit_api import openBrowser, closeBrowser, listBrowsers
    from bit.bit_config import _resolve_authorized_window_id
    from bit.bit_runtime_lock import create_window_lease
    from bit_playwright.bit_infractions_info import _switch_site_if_needed, INFRACTIONS_URL
    from playwright.sync_api import sync_playwright

    sites = list(dict.fromkeys(site_ids if site_ids is not None else (
        row["site_id"] for row in record.get("site_settings") or []
        if row.get("site_id") in SITE_TITLES
    )))
    if not sites:
        raise ValueError("店铺没有可读取的授权站点")
    name = str(record.get("display_name") or record.get("nickname"))
    window_id = _resolve_authorized_window_id(record, listBrowsers())
    lease = create_window_lease(window_id, owner="pppi_snapshot", task_type="infraction_collection")
    if not lease.acquire(timeout=0):
        raise RuntimeError(f"{name} 浏览器窗口正在被其他任务占用，请稍后重试")
    result = {"store": name, "token_id": int(record["id"]), "status": "success",
              "detection_scanned": 0, "detection_matched": 0, "rights_holder": 0, "message": ""}
    opened = False
    try:
        opened_result = openBrowser(window_id)
        if not opened_result.get("success"):
            raise RuntimeError(opened_result.get("msg") or "打开浏览器失败")
        opened = True
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(opened_result["data"]["ws"])
            page = browser.contexts[0].new_page()
            try:
                for site in sites:
                    page.goto(INFRACTIONS_URL, wait_until="domcontentloaded", timeout=120000)
                    title = page.evaluate("window.__PRELOADED_STATE__?.body?.container?.title || ''")
                    if not title.endswith(f"- {SITE_TITLES[site]}"):
                        _switch_site_if_needed(page, name, site)
                    detection_count, report_count = sync_site_page(page, record, site)
                    result["detection_scanned"] += detection_count
                    result["detection_matched"] += detection_count
                    result["rights_holder"] += report_count
            finally:
                page.close()
        return result
    finally:
        try:
            if opened:
                closeBrowser(window_id)
        finally:
            lease.release()
