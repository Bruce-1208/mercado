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
        summary = reputation._extract_account_risk_page_text(scripts)
        kinds = reputation._account_risk_kinds_from_summary(summary, reputation._get_account_risk_links(scripts))
        if not kinds and any(reputation._account_risk_summary_count(summary, kind) != 0
                             for kind in ("restriction", "warning")):
            raise RuntimeError("无法确认系统告警数量")
        messages = []
        for kind in kinds:
            open_site(session, reputation.ACCOUNT_RISK_URLS[kind], site)
            details = reputation._normalize_account_risk_details(reputation._extract_account_risk_details(scripts))
            if not details:
                raise RuntimeError("无法读取系统告警详情")
            messages.extend(details)
        row["system_warning"] = "\n".join(dict.fromkeys(messages)) or "正常"
    except Exception as exc:
        errors.append(f"系统告警：{exc}")
    row["error"] = "；".join(errors)
    return row


def collect(configs, stop_event=None):
    rows = []
    for config in configs:
        if stop_event and stop_event.is_set():
            break
        window_id, name = config[:2]
        sites = split_config_sites(config[3])
        if not window_id:
            from bit.bit_api import getBrowserIdByName, listBrowsers
            try:
                browsers = listBrowsers()
                for alias in (config[4] if len(config) > 4 else [name]):
                    try:
                        window_id = getBrowserIdByName(alias, browsers=browsers)
                        break
                    except RuntimeError:
                        continue
                if not window_id:
                    raise RuntimeError(f"执行电脑未找到店铺浏览器：{name}")
            except Exception as exc:
                rows.extend({"store_name": name, "site": site, "error": str(exc)} for site in sites)
                continue
        lease = create_window_lease(window_id, owner=f"reputation_browser:{name}",
                                    shop_name=name, task_type="reputation_collection")
        acquired = lease.acquire(timeout=0)
        try:
            if not acquired:
                raise RuntimeError("窗口被其他任务占用")
            with BitPlaywrightSession(window_id, close_on_exit=True) as session:
                for site in sites:
                    if stop_event and stop_event.is_set():
                        break
                    rows.append(collect_site(session, name, site))
        except Exception as exc:
            rows.extend({"store_name": name, "site": site, "error": str(exc)} for site in sites)
        finally:
            if acquired:
                lease.release()
    failures = sum(bool(row.get("error")) for row in rows)
    message = f"浏览器同步完成，异常站点 {failures} 个"
    if failures:
        message += "；" + "；".join(f"{row['store_name']}/{row['site']}：{row['error']}"
                                   for row in rows if row.get("error"))[:1500]
    return {"rows": rows, "status": "partial" if failures else "success", "message": message}


def persist(result, configs):
    from bit.bit_mysql import update_reputation_browser_fields
    allowed = {(str(row[1]), site) for row in configs for site in split_config_sites(row[3])}
    rows = result.get("rows") or []
    if any((row.get("store_name"), row.get("site")) not in allowed for row in rows):
        raise ValueError("浏览器同步结果包含未授权店铺或站点")
    return update_reputation_browser_fields(rows)
