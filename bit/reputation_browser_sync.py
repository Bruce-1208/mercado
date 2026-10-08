"""Playwright-only collection of reputation traffic and account warnings."""
from bit import bit_reputation_info as reputation
from bit.bit_collection_control import split_config_sites
from bit.bit_runtime_lock import create_window_lease
from bit_playwright.common import BitPlaywrightSession, open_mercado_backend_page, select_country


class PageScripts:
    """Reuse the existing chart/card parsers against a Playwright page."""
    def __init__(self, page):
        self.page = page

    def execute_script(self, source, *args):
        return self.page.evaluate('(args) => { return (function(){' + source + '\n}).apply(null, args); }', list(args))

    def execute_async_script(self, source, *args):
        return self.page.evaluate('(args) => new Promise(resolve => { (function(){' + source + '\n}).apply(null, [...args, resolve]); })', list(args))


def open_site(session, url, site, *, choose_site=False):
    access = open_mercado_backend_page(session, url, site=site, source="声誉浏览器同步")
    if not access.get("ok"):
        raise RuntimeError(access.get("message") or "页面无法访问")
    if choose_site and not select_country(session.page, site):
        raise RuntimeError(f"无法确认站点：{site}")


def collect_site(session, name, site):
    row = {"store_name": name, "site": site}
    errors = []
    scripts = PageScripts(session.page)
    try:
        open_site(session, reputation.METRICS_URL, site, choose_site=True)
        diagnostics = []
        records = reputation._extract_visits_from_metrics_api(scripts, 8, diagnostics)
        if len(records) < 8:
            records = reputation._merge_visit_candidates([
                records, reputation._extract_visits_from_dom(scripts, 8),
                reputation._extract_visits_by_hover(scripts, 8),
            ], 8)
        numbers = reputation._to_visit_number_list(records, 8)
        if len(numbers) != 8:
            raise RuntimeError("流量数据不完整：" + "；".join(diagnostics))
        row["visits"] = str(numbers)
    except Exception as exc:
        errors.append(f"流量：{exc}")
    try:
        # Account-risk uses the seller selected in Global Selling and has no country picker.
        # Re-select independently so a failed traffic step cannot leak another site's warnings.
        open_site(session, reputation.SALES_SUMMARY_URL, site, choose_site=True)
        open_site(session, reputation.ACCOUNT_RISK_URL, site)
        summary = reputation._wait_account_risk_summary(scripts)
        kinds = reputation._account_risk_kinds_from_summary(summary, reputation._get_account_risk_links(scripts))
        if not kinds and any(reputation._account_risk_summary_count(summary, kind) != 0
                             for kind in ("restriction", "warning")):
            raise RuntimeError("无法确认系统告警数量")
        messages = []
        for kind in kinds:
            open_site(session, reputation.ACCOUNT_RISK_URLS[kind], site)
            details = reputation._wait_account_risk_details(scripts)
            if not details:
                raise RuntimeError("无法读取系统告警详情")
            messages.extend(details)
        row["system_warning"] = reputation._latest_account_risk_message(messages) or "正常"
    except Exception as exc:
        errors.append(f"系统告警：{exc}")
    row["error"] = "；".join(errors)
    return row


def collect(configs, stop_event=None):
    raise RuntimeError("声誉浏览器同步已停用，请通过 API 更新声誉")


def persist(result, configs):
    from bit.bit_mysql import update_reputation_browser_fields
    allowed = {(str(row[1]), site) for row in configs for site in split_config_sites(row[3])}
    rows = result.get("rows") or []
    if any((row.get("store_name"), row.get("site")) not in allowed for row in rows):
        raise ValueError("浏览器同步结果包含未授权店铺或站点")
    return update_reputation_browser_fields(rows)
