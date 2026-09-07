import contextlib
import importlib
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime
from pathlib import Path

import pandas as pd
from bit.bit_api import closeBrowser, openBrowser
from bit.bit_collection_control import (
    DEFAULT_COLLECTION_MAX_WORKERS,
    DEFAULT_RETRY_LOCK_WAIT_SECONDS,
    env_float,
    env_int,
    failed_sites,
    filter_config_rows,
    merge_site_retry_outcome,
    outcome_failed,
    outcome_has_marker,
    outcome_is_permanent_failure,
    row_key,
    stagger_sleep,
    terminate_process_pool,
    trip_batch_rate_limit,
    wait_for_batch_resume,
    write_unreadable_site_report,
)
from bit.bit_config import list_config_rows
from bit.bit_mercado_limit import (
    get_mercado_backend_status,
    is_mercado_logged_out_state,
    is_mercado_rate_limited_text,
)
from bit.bit_mercado_login import (
    is_human_verification_result,
    try_record_login_anomaly,
)
from bit.bit_runtime_lock import create_window_lease
from bit.bit_db_api import (
    insert_task_record,
    inset_infraction_info,
    list_mercado_store_tokens,
)
from bit.bit_send_mail import send_info
from bit.bit_utils import get_now_time
from bit.mercado_click_delay import install_playwright_click_delay


install_playwright_click_delay()


INFRACTIONS_URL = "https://global-selling.mercadolibre.com/noindex/pppi/infractions?tab=detections&offset=0"
INFRACTIONS_TAB_URLS = {
    "侵权": "https://global-selling.mercadolibre.com/noindex/pppi/infractions?tab=detections&offset=0",
    "权利人": "https://global-selling.mercadolibre.com/noindex/pppi/infractions?tab=denounces&offset=0",
}

SITE_PREFIX_MAP = {
    "墨西哥": "MLM",
    "MX": "MLM",
    "MLM": "MLM",
    "巴西": "MLB",
    "BR": "MLB",
    "MLB": "MLB",
    "哥伦比亚": "MCO",
    "CO": "MCO",
    "MCO": "MCO",
    "智利": "MLC",
    "CL": "MLC",
    "MLC": "MLC",
    "阿根廷": "MLA",
    "AR": "MLA",
    "MLA": "MLA",
    "乌拉圭": "MLU",
    "UY": "MLU",
    "MLU": "MLU",
}

# 美客多页面在店铺并发运行时偶尔需要较长时间才能完成首屏、站点切换和分页。
# 这些值是“最长等待上限”，页面提前就绪会立即继续，不会固定等待到超时。
INFRACTIONS_ELEMENT_TIMEOUT_MS = env_int(
    "BIT_DAILY_ELEMENT_TIMEOUT_MS",
    60_000,
    minimum=1_000,
)
INFRACTIONS_PAGE_READY_TIMEOUT_MS = env_int(
    "BIT_DAILY_PAGE_READY_TIMEOUT_MS",
    90_000,
    minimum=1_000,
)
INFRACTIONS_NAVIGATION_TIMEOUT_MS = env_int(
    "BIT_DAILY_NAVIGATION_TIMEOUT_MS",
    120_000,
    minimum=1_000,
)

SITE_NAME_BY_PREFIX = {
    "MLM": "墨西哥",
    "MLB": "巴西",
    "MCO": "哥伦比亚",
    "MLC": "智利",
    "MLA": "阿根廷",
    "MLU": "乌拉圭",
}

SITE_SWITCH_SELECTOR_MAP = {
    "墨西哥": 'div[data-value="MLM-remote"]',
    "MX": 'div[data-value="MLM-remote"]',
    "MLM": 'div[data-value="MLM-remote"]',
    "巴西": 'div[data-value="MLB-remote"]',
    "BR": 'div[data-value="MLB-remote"]',
    "MLB": 'div[data-value="MLB-remote"]',
    "哥伦比亚": 'div[data-value="MCO-remote"]',
    "CO": 'div[data-value="MCO-remote"]',
    "MCO": 'div[data-value="MCO-remote"]',
    "智利": 'div[data-value="MLC-remote"]',
    "CL": 'div[data-value="MLC-remote"]',
    "MLC": 'div[data-value="MLC-remote"]',
    "阿根廷": 'div[data-value="MLA-remote"]',
    "AR": 'div[data-value="MLA-remote"]',
    "MLA": 'div[data-value="MLA-remote"]',
    "乌拉圭": 'div[data-value="MLU-remote"]',
    "UY": 'div[data-value="MLU-remote"]',
    "MLU": 'div[data-value="MLU-remote"]',
}

_PLAYWRIGHT_API = None


class MercadoRateLimitError(RuntimeError):
    """Mercado Libre 或浏览器接口返回访问限频。"""


class MercadoAuthenticationError(RuntimeError):
    """Mercado Libre 登录态已经失效。"""


class MercadoPageStructureError(RuntimeError):
    """Mercado Libre 页面已打开，但预期的侵权页结构不存在。"""


class BitBrowserWindowError(RuntimeError):
    """比特浏览器窗口配置无效或无法打开。"""


RIGHTS_HOLDER_LABELS = (
    "Reported by rights holders",
    "Reported by rights holder",
    "权利人举报",
    "由权利人举报",
    "权利持有人举报",
    "权利人报告",
)
DETECTED_LABELS = (
    "Detected by Mercado Libre",
    "Detected by MercadoLibre",
    "Detected",
    "Mercado Libre 检测到",
    "由 Mercado Libre 检测到",
    "美客多检测到",
    "平台检测",
    "系统检测",
)


def _is_rate_limited_text(value):
    return is_mercado_rate_limited_text(value)


def _is_spanish_ip_switch_text(value):
    return is_mercado_rate_limited_text(value)


def _get_page_text(page):
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def _is_login_page(current_url, body_text):
    return is_mercado_logged_out_state(
        {
            "current_url": current_url,
            "page_text": body_text,
            "title": "",
        }
    )


def _raise_if_page_unavailable(page, context="侵权页面"):
    """识别限频和登录跳转，避免把登录页误判为“0 条侵权”。"""
    body_text = _get_page_text(page)
    current_url = page.url or ""
    backend_status = get_mercado_backend_status(
        state={
            "current_url": current_url,
            "page_text": body_text,
            "title": "",
        }
    )
    if is_human_verification_result(body_text):
        try_record_login_anomaly(
            body_text,
            getattr(page, "_bit_mercado_window_id", ""),
            getattr(page, "_bit_mercado_shop_name", ""),
            getattr(page, "_bit_mercado_site", ""),
            "侵权采集",
        )
        raise MercadoAuthenticationError(
            f"{context}检测到人机验证，需要人工处理：{body_text[:300]}"
        )
    if backend_status == "logged_out":
        try_record_login_anomaly(
            {
                "status": "logged_out",
                "message": f"{context}检测到美客多账号退出登录：{current_url}",
            },
            getattr(page, "_bit_mercado_window_id", ""),
            getattr(page, "_bit_mercado_shop_name", ""),
            getattr(page, "_bit_mercado_site", ""),
            "侵权采集",
        )
        raise MercadoAuthenticationError(f"{context}登录态失效，已跳转登录页：{current_url}")
    if backend_status == "rate_limited":
        raise MercadoRateLimitError(f"Mercado 页面限频：{current_url} {body_text[:300]}")
    return body_text


def _raise_if_page_rate_limited(page):
    """兼容旧调用；现在同时校验登录状态。"""
    return _raise_if_page_unavailable(page)


def _validate_infractions_page(page):
    body_text = _raise_if_page_unavailable(page)
    current_url = page.url or ""
    if "/noindex/pppi/infractions" not in current_url:
        raise MercadoPageStructureError(f"页面未停留在侵权中心：{current_url}")

    has_structure = page.locator(
        ".infractions__tab, .infraction-item__id, .infraction-item"
    ).count() > 0
    semantic_markers = (
        "intellectual property",
        "infringement",
        "propiedad intelectual",
        "propriedade intelectual",
        "知识产权",
        "侵权",
        "违规",
    )
    if not has_structure and not any(
        marker in body_text.casefold() for marker in semantic_markers
    ):
        raise MercadoPageStructureError(
            f"侵权页面结构不匹配：{current_url} {body_text[:200]}"
        )
    return True


def _load_playwright_sync_api():
    """
    This project has local Playwright helper modules. Temporarily hide the project
    root so Python imports the official ``playwright.sync_api`` package.
    """
    global _PLAYWRIGHT_API
    if _PLAYWRIGHT_API is not None:
        return _PLAYWRIGHT_API

    project_root = Path(__file__).resolve().parent.parent
    original_path = list(sys.path)
    local_parent = sys.modules.get("bit_playwright")
    current_module = sys.modules.get(__name__)
    removed_modules = {}

    for name, module in list(sys.modules.items()):
        if name == "bit_playwright" or (name.startswith("bit_playwright.") and name != __name__):
            removed_modules[name] = module
            sys.modules.pop(name, None)

    sys.path = [
        path
        for path in sys.path
        if Path(path or ".").resolve() != project_root.resolve()
    ]

    try:
        sync_api = importlib.import_module("playwright.sync_api")
        _PLAYWRIGHT_API = (sync_api.sync_playwright, sync_api.TimeoutError)
        return _PLAYWRIGHT_API
    finally:
        sys.path = original_path
        if local_parent is not None:
            sys.modules["bit_playwright"] = local_parent
        if current_module is not None:
            sys.modules[__name__] = current_module


def _site_key(site):
    return str(site or "").strip()


def _site_selector(site):
    key = _site_key(site)
    return SITE_SWITCH_SELECTOR_MAP.get(key) or SITE_SWITCH_SELECTOR_MAP.get(key.upper())


def _site_prefix(site):
    key = _site_key(site)
    return SITE_PREFIX_MAP.get(key) or SITE_PREFIX_MAP.get(key.upper(), "")


def _is_ignored_config_value(value):
    return "忽略" in str(value or "").strip()


def _split_sites(value):
    return [site.strip() for site in re.split(r"[，,、;；\n]+", str(value or "")) if site.strip()]


def _setting_flag_enabled(value):
    if isinstance(value, str):
        return value.strip().casefold() not in ("", "0", "false", "no", "off")
    return bool(value)


def _stop_requested(stop_event=None):
    try:
        return bool(stop_event is not None and stop_event.is_set())
    except (BrokenPipeError, EOFError, OSError):
        return True


def _wait_or_stop(seconds, stop_event=None):
    seconds = max(0, float(seconds or 0))
    if _stop_requested(stop_event):
        return True
    if stop_event is not None:
        try:
            return bool(stop_event.wait(seconds))
        except (BrokenPipeError, EOFError, OSError):
            return True
    time.sleep(seconds)
    return False


def _authorized_visit_stats_scope(token_data=None):
    """按授权店铺别名返回显式开启访问数据统计的站点。"""
    if token_data is None:
        token_data = list_mercado_store_tokens() or {}
    scope = {}
    for token in token_data.get("rows") or ():
        if not bool(token.get("enabled", True)):
            continue
        enabled_sites = []
        for setting in token.get("site_settings") or ():
            setting = dict(setting or {})
            if not _setting_flag_enabled(setting.get("visit_stats_enabled")):
                continue
            site_code = _site_prefix(setting.get("site_id"))
            if site_code in SITE_NAME_BY_PREFIX and site_code not in enabled_sites:
                enabled_sites.append(site_code)
        if not enabled_sites:
            continue
        for alias in (token.get("display_name"), token.get("nickname")):
            alias_key = str(alias or "").strip().casefold()
            if not alias_key:
                continue
            target = scope.setdefault(alias_key, [])
            for site_code in enabled_sites:
                if site_code not in target:
                    target.append(site_code)
    return scope


def _authorized_visit_stats_rows(rows, token_data=None):
    """兼容旧调用：再次按店铺授权的访问统计开关收紧站点。"""
    scope = _authorized_visit_stats_scope(token_data)
    selected = []
    for raw_row in rows or ():
        if not isinstance(raw_row, (list, tuple)) or len(raw_row) < 4:
            continue
        enabled_sites = scope.get(str(raw_row[1] or "").strip().casefold(), ())
        if not enabled_sites:
            continue
        row = list(raw_row)
        row[3] = "，".join(SITE_NAME_BY_PREFIX[site] for site in enabled_sites)
        selected.append(tuple(row))
    return selected


def _filter_rows_by_shop_sites(rows, selected_shop_sites=None):
    """按“店铺 -> 站点”精确缩小采集范围，避免全局站点交叉匹配。"""
    if not selected_shop_sites:
        return list(rows or ())
    scope = {}
    for shop_name, sites in dict(selected_shop_sites).items():
        shop_key = str(shop_name or "").strip().casefold()
        if not shop_key:
            continue
        if isinstance(sites, str):
            sites = (sites,)
        normalized_sites = {
            SITE_NAME_BY_PREFIX[site_code]
            for site in sites or ()
            for site_code in (_site_prefix(site),)
            if site_code in SITE_NAME_BY_PREFIX
        }
        if normalized_sites:
            scope.setdefault(shop_key, set()).update(normalized_sites)

    selected = []
    for raw_row in rows or ():
        if not isinstance(raw_row, (list, tuple)) or len(raw_row) < 4:
            continue
        allowed_sites = scope.get(str(raw_row[1] or "").strip().casefold())
        if not allowed_sites:
            continue
        target_sites = [
            site
            for site in _split_sites(raw_row[3])
            if site in allowed_sites
        ]
        if not target_sites:
            continue
        row = list(raw_row)
        row[3] = "，".join(target_sites)
        selected.append(tuple(row))
    return selected


def _failure_status(exc):
    text = re.sub(r"\s+", " ", str(exc or "")).strip()
    lower = text.casefold()
    if is_human_verification_result(exc):
        reason = "需要人机验证"
    elif isinstance(exc, MercadoAuthenticationError) or _is_login_page("", text):
        reason = "登录失效"
    elif isinstance(exc, MercadoRateLimitError) or _is_rate_limited_text(text):
        reason = "访问限频"
    elif isinstance(exc, BitBrowserWindowError) and (
        "没有找到相应数据" in text or "missing" in lower or "不存在" in text
    ):
        reason = "窗口ID不存在"
    elif isinstance(exc, BitBrowserWindowError) and "timeout" in lower:
        reason = "窗口打开超时"
    elif "窗口正在被其他任务占用" in text:
        reason = "窗口被其他任务占用"
    elif "站点切换失败" in text:
        reason = "站点切换失败"
    elif isinstance(exc, MercadoPageStructureError):
        reason = "页面结构不匹配"
    elif "timeout" in lower:
        reason = "页面元素等待超时"
    else:
        reason = text or (exc.__class__.__name__ if exc is not None else "未知异常")
    return f"失败：{reason}"[:180]


def _deduplicate_config_rows(rows):
    unique_rows = []
    seen = set()
    for row in rows:
        key = (str(row[0]).strip(), str(row[1]).strip(), str(row[3] or "").strip())
        if key in seen:
            print(f"{get_now_time()}跳过重复配置：{row[1]} {row[3]}")
            continue
        seen.add(key)
        unique_rows.append(row)
    return unique_rows


def _open_bitbrowser(
    window_id,
    max_retries=3,
    retry_delay=10,
    batch_control=False,
    batch_source="侵权采集",
):
    last_result = None
    for attempt in range(1, max_retries + 1):
        result = openBrowser(window_id)
        last_result = result
        print(result)
        data = result.get("data") if isinstance(result, dict) else None
        if data and (data.get("ws") or data.get("http")):
            return result

        message = result.get("msg", "") if isinstance(result, dict) else str(result)
        if "没有找到相应数据" in message or "不存在" in message:
            raise BitBrowserWindowError(f"比特浏览器窗口无效或不存在：{result}")
        is_rate_limited = _is_rate_limited_text(result)
        if is_rate_limited and batch_control:
            trip_batch_rate_limit(batch_source, message)
        if attempt < max_retries:
            print(
                f"{get_now_time()}比特浏览器打开窗口失败，第{attempt}/{max_retries}次："
                f"{message}"
            )
            if is_rate_limited and batch_control:
                wait_for_batch_resume(batch_source)
            else:
                time.sleep(retry_delay)

    if _is_rate_limited_text(last_result):
        raise MercadoRateLimitError(
            f"比特浏览器打开窗口被限频，已重试{max_retries}次：{last_result}"
        )
    raise BitBrowserWindowError(
        f"比特浏览器打开窗口失败，已重试{max_retries}次：{last_result}"
    )


def _text_list(page, selector, timeout=INFRACTIONS_ELEMENT_TIMEOUT_MS):
    try:
        page.wait_for_selector(selector, timeout=timeout)
    except Exception:
        pass
    values = page.locator(selector).all_text_contents()
    return [value.strip() for value in values if value and value.strip()]


def _get_page_signature(page):
    try:
        ids = _text_list(page, ".infraction-item__id", timeout=5000)
        return tuple(ids)
    except Exception:
        return tuple()


def _wait_infractions_ready(page, timeout=INFRACTIONS_PAGE_READY_TIMEOUT_MS):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    try:
        page.wait_for_function(
            """
            () => location.href.includes('/noindex/pppi/infractions') &&
              (document.readyState === 'complete' || document.readyState === 'interactive')
            """,
            timeout=timeout,
        )
    except Exception:
        pass

 
def _safe_goto_infractions(page, url, timeout=INFRACTIONS_NAVIGATION_TIMEOUT_MS):
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        if response is not None and response.status in (401, 403):
            raise MercadoAuthenticationError(
                f"Mercado 页面返回 HTTP {response.status}：{url}"
            )
    except Exception as exc:
        _raise_if_page_unavailable(page)
        if isinstance(exc, (MercadoAuthenticationError, MercadoRateLimitError)):
            raise
        if _is_rate_limited_text(exc):
            raise MercadoRateLimitError(str(exc)) from exc
        current_url = page.url or ""
        if "/noindex/pppi/infractions" not in current_url:
            raise
        print(f"页面自动跳转，继续使用当前侵权页: {current_url}")
    _raise_if_page_unavailable(page)
    _wait_infractions_ready(page)
    _validate_infractions_page(page)
    return page.url


def _current_infraction_type(page):
    try:
        selected_tab = page.evaluate(
            """() => window.__PRELOADED_STATE__?.body?.container?.selectedTab || ''"""
        )
    except Exception:
        selected_tab = ""
    if selected_tab == "denounces":
        return "权利人"
    if selected_tab == "detections":
        return "侵权"

    current_url = page.url or ""
    match = re.search(r"[?&]tab=([^&]+)", current_url)
    current_tab = match.group(1) if match else ""
    if current_tab == "denounces":
        return "权利人"
    if current_tab == "detections":
        return "侵权"
    return "侵权"


def _infraction_type_total(page, infraction_type):
    field = "denouncesTotal" if infraction_type == "权利人" else "detectionsTotal"
    try:
        value = page.evaluate(
            """field => window.__PRELOADED_STATE__?.body?.container?.[field]""",
            field,
        )
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    except Exception:
        return None


def _goto_infractions_type(page, infraction_type):
    target_url = INFRACTIONS_TAB_URLS[infraction_type]
    _safe_goto_infractions(page, target_url)
    _reset_current_offset(page)
    actual_type = _current_infraction_type(page)
    if actual_type != infraction_type:
        print(f"请求打开{infraction_type}标签，但页面实际停留在{actual_type}标签: {page.url}")
    return actual_type


def _extract_last_submit_times(page):
    return page.evaluate(
        """
        () => {
          const cards = [...document.querySelectorAll('.infraction-item__id')]
            .map((idNode) =>
              idNode.closest('.infraction-item') ||
              idNode.closest('[class*="infraction-item"]') ||
              idNode.closest('li') ||
              idNode.parentElement
            )
            .filter(Boolean);

          const labelPattern = /(last\\s+submitted|last\\s+submission|submitted|submission|最后提交|提交时间|提交日期|已提交)/i;
          const datePattern = /(?:\\d{4}年\\d{1,2}月\\d{1,2}日(?:\\s+\\d{1,2}:\\d{2}(?::\\d{2})?)?|\\d{4}[-/]\\d{1,2}[-/]\\d{1,2}(?:\\s+\\d{1,2}:\\d{2}(?::\\d{2})?)?|\\d{1,2}[-/]\\d{1,2}[-/]\\d{4}(?:\\s+\\d{1,2}:\\d{2}(?::\\d{2})?)?|[A-Z][a-z]{2,8}\\.?\\s+\\d{1,2},?\\s*\\d{4}(?:\\s+\\d{1,2}:\\d{2}(?:\\s*[AP]M)?)?|\\d{1,2}\\s+[A-Z][a-z]{2,8}\\.?\\s+\\d{4}(?:\\s+\\d{1,2}:\\d{2})?|\\d{1,2}\\s+de\\s+[a-záéíóúñç]+\\s+de\\s+\\d{4})/i;

          function clean(text) {
            return (text || '').replace(/\\s+/g, ' ').trim();
          }

          function normalizeCandidate(text) {
            const value = clean(text);
            if (!value) return '';
            const match = value.match(datePattern);
            if (match) return match[0].trim();
            return value.replace(labelPattern, '').replace(/^[:：\\s-]+/, '').trim();
          }

          function fromLines(text) {
            const lines = (text || '')
              .split(/\\n+/)
              .map((line) => clean(line))
              .filter(Boolean);
            for (let index = 0; index < lines.length; index += 1) {
              const line = lines[index];
              if (!labelPattern.test(line)) continue;

              const sameLine = normalizeCandidate(line);
              if (sameLine && !labelPattern.test(sameLine)) return sameLine;

              for (let offset = 1; offset <= 4 && index + offset < lines.length; offset += 1) {
                const nextLine = normalizeCandidate(lines[index + offset]);
                if (nextLine && !labelPattern.test(nextLine)) return nextLine;
              }
            }
            return '';
          }

          return cards.map((card) => {
            const fromCardText = fromLines(card.innerText || card.textContent || '');
            if (fromCardText) return fromCardText;

            const candidates = [
              ...card.querySelectorAll(
                '[class*="submit"], [class*="submission"], [class*="submitted"], [class*="date"], li, p, span, div'
              ),
            ]
              .map((el) => clean(el.textContent || ''))
              .filter(Boolean);

            const labeled = candidates.find((text) => labelPattern.test(text));
            if (labeled) {
              return normalizeCandidate(labeled);
            }
            return '';
          });
        }
        """
    )


def _read_current_infractions_page(page, name, site, infraction_type="侵权"):
    ids = _text_list(
        page,
        ".infraction-item__id",
        timeout=INFRACTIONS_ELEMENT_TIMEOUT_MS,
    )
    titles = _text_list(page, ".infraction-item__title", timeout=5000)
    dates = _text_list(page, ".infraction-denounce__date", timeout=5000)
    submit_times = _extract_last_submit_times(page)

    rows = []
    submit_count = sum(1 for value in submit_times if value)
    if ids:
        print(f"{name}{site}{infraction_type}提交时间解析成功 {submit_count}/{len(ids)} 条")
    prefix = _site_prefix(site)
    for index, id_text in enumerate(ids):
        title = titles[index] if index < len(titles) else ""
        date = dates[index] if index < len(dates) else ""
        submit_time = submit_times[index] if index < len(submit_times) else ""
        rows.append(
            [
                name,
                site,
                id_text.replace("#", prefix),
                title,
                date,
                submit_time,
                get_now_time(),
                infraction_type,
            ]
        )
    return rows


def _is_next_button_disabled(element):
    try:
        return element.evaluate(
            """
            el => {
              const cls = String(el.className || '').toLowerCase();
              const parentCls = String(el.closest('li')?.className || '').toLowerCase();
              return !!el.disabled || el.getAttribute('aria-disabled') === 'true' ||
                cls.includes('disabled') || parentCls.includes('disabled');
            }
            """
        )
    except Exception:
        return True


def _find_next_button(page):
    selectors = [
        ".andes-pagination__button--next a",
        ".andes-pagination__button--next button",
        'a:has(span.andes-pagination__arrow-title:has-text("Next"))',
        'button:has(span.andes-pagination__arrow-title:has-text("Next"))',
        'a:has(span.andes-pagination__arrow-title:has-text("下一页"))',
        'button:has(span.andes-pagination__arrow-title:has-text("下一页"))',
        "a[aria-label*='Next']",
        "button[aria-label*='Next']",
        "a[aria-label*='下一']",
        "button[aria-label*='下一']",
        "a[title*='Next']",
        "button[title*='Next']",
        "a[title*='下一']",
        "button[title*='下一']",
        "text=Next",
        "text=下一页",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()
            for index in range(count):
                element = locator.nth(index)
                if element.is_visible():
                    return element
        except Exception:
            continue
    return None


def _offset_url(current_url, previous_signature):
    page_size = max(1, len(previous_signature or ()))
    match = re.search(r"([?&]offset=)(\d+)", current_url)
    if match:
        next_offset = int(match.group(2)) + page_size
        return current_url[: match.start(2)] + str(next_offset) + current_url[match.end(2) :]

    separator = "&" if "?" in current_url else "?"
    return f"{current_url}{separator}offset={page_size}"


def _goto_next_offset(page, previous_signature):
    next_url = _offset_url(page.url, previous_signature)
    _safe_goto_infractions(page, next_url)
    page.wait_for_function(
        """
        previous => {
          const ids = [...document.querySelectorAll('.infraction-item__id')]
            .map(el => (el.textContent || '').trim())
            .filter(Boolean);
          return ids.length && ids.join('|') !== previous.join('|');
        }
        """,
        arg=list(previous_signature),
        timeout=INFRACTIONS_PAGE_READY_TIMEOUT_MS,
    )
    return True


def _reset_current_offset(page):
    next_url = re.sub(r"([?&]offset=)\d+", r"\g<1>0", page.url)
    if next_url != page.url:
        _safe_goto_infractions(page, next_url)
        time.sleep(2)


def _collect_current_infractions_tab(page, name, site, infraction_type):
    infractions_list = []
    seen_ids = set()
    page_no = 1
    expected_total = _infraction_type_total(page, infraction_type)
    while True:
        _validate_infractions_page(page)
        page_rows = _read_current_infractions_page(page, name, site, infraction_type)
        new_count = 0
        for row in page_rows:
            row_key = (row[2], row[7])
            if row_key in seen_ids:
                continue
            seen_ids.add(row_key)
            infractions_list.append(row)
            new_count += 1

        print(
            f"{get_now_time()}{name}{site}{infraction_type}第{page_no}页抓取{len(page_rows)}条，新增{new_count}条"
        )
        previous_signature = _get_page_signature(page)
        if not previous_signature:
            print(f"当前页面没有{infraction_type}数据，结束当前标签抓取")
            break

        if not _click_next_page(page, previous_signature, page_no):
            break
        page_no += 1

    if expected_total is not None and len(infractions_list) < expected_total:
        raise MercadoPageStructureError(
            f"{name}{site}{infraction_type}预计 {expected_total} 条，"
            f"实际只完整读取 {len(infractions_list)} 条"
        )
    return infractions_list


def _open_rights_holder_report_tab(page):
    clicked = page.evaluate(
        """
        (targetTexts) => {
          const normalizedTargets = targetTexts.map((text) => text.toLocaleLowerCase());
          const isVisible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
              rect.width > 0 && rect.height > 0;
          };
          const nodes = [...document.querySelectorAll('a, button, [role="tab"]')]
            .filter((el) => {
              const text = (el.textContent || '').toLocaleLowerCase();
              return isVisible(el) && normalizedTargets.some((target) => text.includes(target));
            });
          if (!nodes.length) return false;
          const clickable = nodes[0];
          clickable.scrollIntoView({block: 'center', inline: 'center'});
          clickable.click();
          return true;
        }
        """,
        list(RIGHTS_HOLDER_LABELS),
    )
    if not clicked:
        return False

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=INFRACTIONS_PAGE_READY_TIMEOUT_MS,
        )
    except Exception:
        pass
    time.sleep(3)
    _reset_current_offset(page)
    return True


def _open_detected_report_tab(page):
    clicked = page.evaluate(
        """
        (texts) => {
          const normalizedTexts = texts.map((text) => text.toLocaleLowerCase());
          const isVisible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
              rect.width > 0 && rect.height > 0;
          };
          const nodes = [...document.querySelectorAll('a, button, [role="tab"]')]
            .filter((el) => {
              const text = (el.textContent || '').toLocaleLowerCase();
              return isVisible(el) && normalizedTexts.some((target) => text.includes(target));
            });
          if (!nodes.length) return false;
          const clickable = nodes[0];
          clickable.scrollIntoView({block: 'center', inline: 'center'});
          clickable.click();
          return true;
        }
        """,
        list(DETECTED_LABELS),
    )
    if not clicked:
        return False

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=INFRACTIONS_PAGE_READY_TIMEOUT_MS,
        )
    except Exception:
        pass
    time.sleep(3)
    _reset_current_offset(page)
    return True


def _collect_type_once(page, name, site, infraction_type, collected_types):
    actual_type = _current_infraction_type(page)
    if actual_type != infraction_type:
        raise MercadoPageStructureError(
            f"{name}{site}请求采集{infraction_type}，页面实际标签为{actual_type}"
        )
    if actual_type in collected_types:
        print(f"{name}{site}{actual_type}已采集，跳过重复采集")
        return []
    collected_types.add(actual_type)
    return _collect_current_infractions_tab(page, name, site, actual_type)


def _click_next_page(page, previous_signature, page_no):
    for attempt in range(3):
        previous_url = page.url
        try:
            next_button = _find_next_button(page)
            if next_button is None or _is_next_button_disabled(next_button):
                print("当前已经是最后一页，翻页结束")
                return False

            next_button.scroll_into_view_if_needed(timeout=INFRACTIONS_ELEMENT_TIMEOUT_MS)
            next_button.click(timeout=INFRACTIONS_ELEMENT_TIMEOUT_MS)
            page.wait_for_function(
                """
                ([previous, previousUrl]) => {
                  const ids = [...document.querySelectorAll('.infraction-item__id')]
                    .map(el => (el.textContent || '').trim())
                    .filter(Boolean);
                  return (ids.length && ids.join('|') !== previous.join('|')) ||
                    location.href !== previousUrl;
                }
                """,
                arg=[list(previous_signature), previous_url],
                timeout=INFRACTIONS_PAGE_READY_TIMEOUT_MS,
            )
            page.wait_for_function(
                """
                previous => {
                  const ids = [...document.querySelectorAll('.infraction-item__id')]
                    .map(el => (el.textContent || '').trim())
                    .filter(Boolean);
                  return ids.length && ids.join('|') !== previous.join('|');
                }
                """,
                arg=list(previous_signature),
                timeout=INFRACTIONS_PAGE_READY_TIMEOUT_MS,
            )
            _validate_infractions_page(page)
            print(f"成功点击下一页，当前第{page_no + 1}页")
            time.sleep(1)
            return True
        except Exception as exc:
            print(f"点击下一页不稳定，尝试 offset 兜底，第{attempt + 1}次: {exc}")
            try:
                if _goto_next_offset(page, previous_signature):
                    print(f"offset兜底翻页成功，当前第{page_no + 1}页")
                    return True
            except Exception as fallback_error:
                print("offset兜底翻页失败", fallback_error)
            time.sleep(2)

    print("翻页多次失败，结束当前站点抓取")
    return False


def _switch_site_if_needed(page, name, site, retries=3):
    selector = _site_selector(site)
    if not selector:
        return

    for attempt in range(1, retries + 1):
        try:
            page.locator(".nav-header-cbt__site-switcher").click(
                timeout=INFRACTIONS_ELEMENT_TIMEOUT_MS
            )
            print(f"{name}打开站点选择器")
            page.locator(selector).click(timeout=INFRACTIONS_ELEMENT_TIMEOUT_MS)
            try:
                page.reload(
                    wait_until="domcontentloaded",
                    timeout=INFRACTIONS_NAVIGATION_TIMEOUT_MS,
                )
            except Exception:
                current_url = page.url or ""
                if "/noindex/pppi/infractions" not in current_url:
                    _raise_if_page_unavailable(page, context=f"{name}{site}切换站点后的页面")
                    raise
                print(f"{name}{site}切换站点后页面自动跳转，继续使用当前页: {current_url}")
            _raise_if_page_unavailable(page, context=f"{name}{site}切换站点后的页面")
            _wait_infractions_ready(page)
            time.sleep(3)
            _validate_infractions_page(page)
            print(get_now_time() + name + site + "选择站点成功")
            return
        except (MercadoAuthenticationError, MercadoRateLimitError):
            raise
        except Exception as exc:
            print(get_now_time() + name + site + f"重新执行选择站点，第{attempt}次: {exc}")
            time.sleep(3)
    raise MercadoPageStructureError(f"{name}{site} 站点切换失败")


def _connect_bitbrowser_with_playwright(playwright, open_result):
    data = open_result.get("data") or {}
    endpoint = data.get("ws")
    if not endpoint and data.get("http"):
        endpoint = f"http://{data['http']}"
    if not endpoint:
        raise BitBrowserWindowError(f"BitBrowser open result missing ws/http: {open_result}")

    browser = playwright.chromium.connect_over_cdp(endpoint)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    page.set_default_timeout(INFRACTIONS_ELEMENT_TIMEOUT_MS)
    page.set_default_navigation_timeout(INFRACTIONS_NAVIGATION_TIMEOUT_MS)
    return browser, page


def _collect_site_infractions(
    page,
    name,
    site,
    window_id="",
    switch_site=True,
    include_rights_holder=True,
):
    """在已连接的同一 BitBrowser 页面中采集一个站点。

    ``include_rights_holder=False`` 时只读取 detections/infringements，
    不打开 denounces/reports 权利人举报标签。
    """
    for attribute, value in (
        ("_bit_mercado_window_id", window_id),
        ("_bit_mercado_shop_name", name),
        ("_bit_mercado_site", site),
    ):
        try:
            setattr(page, attribute, str(value or "").strip())
        except Exception:
            pass
    _safe_goto_infractions(page, INFRACTIONS_URL)
    time.sleep(5)
    _validate_infractions_page(page)
    if switch_site:
        _switch_site_if_needed(page, name, site)
        _validate_infractions_page(page)

    infractions_list = []
    collected_types = set()
    current_type = _current_infraction_type(page)
    if not include_rights_holder and current_type != "侵权":
        _goto_infractions_type(page, "侵权")
        current_type = _current_infraction_type(page)
        if current_type != "侵权":
            raise MercadoPageStructureError(
                f"{name}{site}无法打开 infringements 标签"
            )
    print(get_now_time() + name + site + f"当前实际侵权标签: {current_type}")
    infractions_list.extend(
        _collect_type_once(page, name, site, current_type, collected_types)
    )

    if "侵权" not in collected_types:
        detected_total = _infraction_type_total(page, "侵权")
        if detected_total == 0:
            collected_types.add("侵权")
            print(get_now_time() + name + site + "普通侵权报告为 0 条")
        else:
            print(get_now_time() + name + site + "尝试切换到普通侵权报告")
            opened = _open_detected_report_tab(page)
            if not opened or _current_infraction_type(page) != "侵权":
                _goto_infractions_type(page, "侵权")
            if _current_infraction_type(page) != "侵权":
                detected_total = _infraction_type_total(page, "侵权")
                if detected_total == 0:
                    collected_types.add("侵权")
                    print(get_now_time() + name + site + "普通侵权报告为 0 条")
                else:
                    raise MercadoPageStructureError(
                        f"{name}{site}无法打开普通侵权报告标签"
                    )
            if "侵权" not in collected_types:
                infractions_list.extend(
                    _collect_type_once(page, name, site, "侵权", collected_types)
                )

    if include_rights_holder and "权利人" not in collected_types:
        rights_total = _infraction_type_total(page, "权利人")
        if rights_total == 0:
            collected_types.add("权利人")
            print(get_now_time() + name + site + "权利人侵权报告为 0 条")
        else:
            opened = _open_rights_holder_report_tab(page)
            if not opened or _current_infraction_type(page) != "权利人":
                _goto_infractions_type(page, "权利人")
            if _current_infraction_type(page) != "权利人":
                rights_total = _infraction_type_total(page, "权利人")
                if rights_total == 0:
                    collected_types.add("权利人")
                    print(get_now_time() + name + site + "权利人侵权报告为 0 条")
                else:
                    raise MercadoPageStructureError(
                        f"{name}{site}无法打开权利人侵权报告标签"
                    )
            if "权利人" not in collected_types:
                print(get_now_time() + name + site + "开始抓取权利人侵权报告")
                infractions_list.extend(
                    _collect_type_once(page, name, site, "权利人", collected_types)
                )

    return infractions_list


def get_infractions_info(
    window_id,
    name,
    site,
    isSwitch=1,
    batch_control=False,
    include_rights_holder=True,
):
    """兼容单站点调用；批量任务会在店铺内复用同一个页面连接。"""
    sync_playwright, _ = _load_playwright_sync_api()
    res = _open_bitbrowser(
        window_id,
        batch_control=batch_control,
        batch_source=f"侵权采集:{name}:{site}",
    )

    with sync_playwright() as playwright:
        _browser, page = _connect_bitbrowser_with_playwright(playwright, res)
        try:
            return _collect_site_infractions(
                page,
                name,
                site,
                window_id=window_id,
                switch_site=isSwitch == 1,
                include_rights_holder=include_rights_holder,
            )
        finally:
            try:
                page.close()
            except Exception:
                pass


def _run_infractions_for_browser_locked(row, stop_event=None):
    browser_id = row[0]
    name = row[1]
    remark = row[2]
    if _is_ignored_config_value(remark):
        return [], []

    print(get_now_time() + "开始打开窗口:" + name)
    if not row[3]:
        return [], [("获取侵权信息", name, "", "失败：未配置站点", get_now_time())]

    site_list = _split_sites(row[3])
    infraction_info_sum = []
    result = []
    fatal_profile_error = None
    try:
        try:
            open_result = _open_bitbrowser(
                browser_id,
                batch_control=True,
                batch_source=f"侵权采集:{name}",
            )
            sync_playwright, _ = _load_playwright_sync_api()
            with sync_playwright() as playwright:
                _browser, page = _connect_bitbrowser_with_playwright(
                    playwright,
                    open_result,
                )
                try:
                    for site in site_list:
                        if _stop_requested(stop_event):
                            print(get_now_time() + name + "收到停止请求，结束侵权遍历")
                            break
                        wait_for_batch_resume(f"侵权采集:{name}")
                        if fatal_profile_error is not None:
                            result.append(
                                (
                                    "获取侵权信息",
                                    name,
                                    site,
                                    _failure_status(fatal_profile_error),
                                    get_now_time(),
                                )
                            )
                            continue

                        succeeded = False
                        last_error = None
                        for attempt in range(1, 4):
                            if _stop_requested(stop_event):
                                break
                            wait_for_batch_resume(f"侵权采集:{name}")
                            try:
                                infraction_info = _collect_site_infractions(
                                    page,
                                    name,
                                    site,
                                    window_id=browser_id,
                                    switch_site=True,
                                )
                                infraction_info_sum.extend(infraction_info)
                                print(get_now_time() + name + site + "成功")
                                result.append(
                                    (
                                        "获取侵权信息",
                                        name,
                                        site,
                                        "成功",
                                        get_now_time(),
                                    )
                                )
                                succeeded = True
                                break
                            except Exception as exc:
                                last_error = exc
                                print(get_now_time() + name + site + "执行失败", exc)
                                if isinstance(
                                    exc,
                                    (MercadoAuthenticationError, BitBrowserWindowError),
                                ):
                                    fatal_profile_error = exc
                                    break
                                is_rate_limited = isinstance(
                                    exc,
                                    MercadoRateLimitError,
                                ) or _is_rate_limited_text(exc)
                                if is_rate_limited:
                                    trip_batch_rate_limit(
                                        f"侵权采集:{name}:{site}",
                                        str(exc),
                                    )
                                if attempt < 3:
                                    if is_rate_limited:
                                        wait_for_batch_resume(f"侵权采集:{name}")
                                    else:
                                        if _wait_or_stop(5, stop_event):
                                            break
                        if not succeeded:
                            result.append(
                                (
                                    "获取侵权信息",
                                    name,
                                    site,
                                    _failure_status(last_error),
                                    get_now_time(),
                                )
                            )
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
        except Exception as exc:
            status = _failure_status(exc)
            print(get_now_time() + name + "打开窗口失败：" + status, exc)
            result = [
                ("获取侵权信息", name, site, status, get_now_time())
                for site in site_list
            ]
    finally:
        print(get_now_time() + "结束，正在关闭窗口")
        try:
            closeBrowser(browser_id)
        except Exception as exc:
            print(get_now_time() + name + "关闭窗口失败", exc)
        print(get_now_time() + "已经关闭窗口")
    return infraction_info_sum, result


def _run_infractions_for_browser(
    row,
    lease_wait_seconds=0,
    log_path=None,
    stop_event=None,
):
    if log_path:
        with open(log_path, "a", encoding="utf-8", buffering=1) as log_file:
            with contextlib.redirect_stdout(log_file), contextlib.redirect_stderr(log_file):
                return _run_infractions_for_browser(
                    row,
                    lease_wait_seconds,
                    stop_event=stop_event,
                )
    browser_id = row[0]
    name = row[1]
    if _stop_requested(stop_event):
        return [], [
            ("获取侵权信息", name, site, "已停止", get_now_time())
            for site in (_split_sites(row[3]) or [""])
        ]
    wait_for_batch_resume(f"侵权采集:{name}")
    lease = create_window_lease(
        browser_id,
        owner=f"infraction_collection:{name}",
        shop_name=name,
        task_type="infraction_collection",
    )
    if not lease.acquire(timeout=max(0, float(lease_wait_seconds or 0))):
        print(get_now_time() + name + "窗口已被其他任务占用，跳过本次侵权采集")
        sites = _split_sites(row[3]) or [""]
        return [], [
            ("获取侵权信息", name, site, "跳过：窗口被其他任务占用", get_now_time())
            for site in sites
        ]
    try:
        return _run_infractions_for_browser_locked(row, stop_event=stop_event)
    finally:
        lease.release()


def _force_close_infraction_windows(rows):
    browser_ids = list(dict.fromkeys(str(row[0] or "") for row in (rows or ())))

    def close_windows():
        for browser_id in browser_ids:
            if not browser_id:
                continue
            try:
                closeBrowser(browser_id, force=True, request_timeout=3)
            except Exception as exc:
                print(get_now_time() + f"停止时关闭窗口 {browser_id} 失败：{exc}")

    threading.Thread(
        target=close_windows,
        name="infraction-window-cleanup",
        daemon=True,
    ).start()


def _execute_infraction_rows(
    rows,
    max_workers,
    stagger_min_seconds,
    stagger_max_seconds,
    lease_wait_seconds=0,
    log_path=None,
    stop_event=None,
):
    outcomes = {}
    worker_count = max(1, min(int(max_workers), len(rows))) if rows else 1
    print(
        f"{get_now_time()}侵权遍历使用 {worker_count} 个进程并发处理 "
        f"{len(rows)} 家店铺"
    )
    executor = ProcessPoolExecutor(max_workers=worker_count)
    future_map = {}
    for index, row in enumerate(rows):
        future = executor.submit(
            _run_infractions_for_browser,
            row,
            lease_wait_seconds,
            log_path,
            stop_event,
        )
        future_map[future] = row
        if index < len(rows) - 1:
            delay = stagger_sleep(stagger_min_seconds, stagger_max_seconds)
            print(f"{get_now_time()}侵权店铺错峰启动，下一家等待 {delay:.1f} 秒")

    pending = set(future_map)
    forced_stop = False
    try:
        while pending:
            completed, pending = wait(
                pending,
                timeout=0.25,
                return_when=FIRST_COMPLETED,
            )
            for future in completed:
                row = future_map[future]
                name = row[1]
                try:
                    browser_infractions, browser_result = future.result()
                except Exception as exc:
                    print(get_now_time() + name + "窗口任务异常", exc)
                    status = _failure_status(exc)
                    sites = _split_sites(row[3]) or [""]
                    browser_infractions = []
                    browser_result = [
                        ("获取侵权信息", name, site, status, get_now_time())
                        for site in sites
                    ]
                outcomes[row_key(row)] = (row, browser_infractions, browser_result)
                print(get_now_time() + name + "窗口任务完成")
            if _stop_requested(stop_event):
                for future in pending:
                    future.cancel()
                print(get_now_time() + "收到停止请求，立即终止侵权遍历进程")
                forced_stop = True
                break
    finally:
        if forced_stop:
            terminate_process_pool(executor)
            _force_close_infraction_windows(rows)
        else:
            executor.shutdown(wait=True, cancel_futures=True)
    return outcomes


def _row_as_login_config(row):
    fields = (
        "window_id",
        "shop_name",
        "status",
        "sites",
        "sequence_no",
        "salesperson",
        "email",
    )
    return {field: row[index] if index < len(row) else "" for index, field in enumerate(fields)}


def _prepare_infraction_retry_rows(
    outcomes,
    permanent_login_failures=None,
    authorization_flag="visit_stats_enabled",
):
    latest_rows = _deduplicate_config_rows(
        list_config_rows(
            include_ignored=False,
            authorization_flag=authorization_flag,
        )
    )
    latest_by_name = {str(row[1]).strip(): row for row in latest_rows if row and row[1]}
    retry_plan = []
    permanent_login_failures = (
        permanent_login_failures if permanent_login_failures is not None else set()
    )

    for original_key, (original_row, _data, browser_result) in outcomes.items():
        if not outcome_failed(browser_result):
            continue
        name = str(original_row[1] or "").strip()
        current_row = latest_by_name.get(name, original_row)

        if outcome_has_marker(browser_result, "窗口ID不存在"):
            if str(current_row[0] or "").strip() == str(original_row[0] or "").strip():
                print(
                    f"{get_now_time()}{name} 窗口ID仍为无效值，需先在配置库修正，本轮不盲目重试"
                )
                continue
            print(f"{get_now_time()}{name} 已读取到更新后的窗口ID，将按新配置补跑")

        if outcome_has_marker(browser_result, "登录失效"):
            if name in permanent_login_failures:
                print(f"{get_now_time()}{name} 登录配置为确定性失败，不再重复提交")
                continue
            print(f"{get_now_time()}{name} 开始串行修复 Mercado 登录态")
            try:
                from bit.bit_mercado_login import (
                    LOGIN_EMAIL_MISSING,
                    LOGIN_EMAIL_REJECTED,
                    LOGIN_SAVED_PASSWORD_INCORRECT,
                    LOGIN_SAVED_PASSWORD_MISSING,
                    login_one_database_shop,
                    sync_login_results_to_window_anomalies,
                )

                login_result = login_one_database_shop(
                    _row_as_login_config(current_row),
                    wait_seconds=int(env_float("BIT_LOGIN_REPAIR_WAIT_SECONDS", 60, 1)),
                    page_load_timeout=int(env_float("BIT_LOGIN_REPAIR_PAGE_TIMEOUT", 20, 1)),
                )
                sync_login_results_to_window_anomalies(
                    [login_result],
                    source="侵权采集",
                )
            except Exception as exc:
                print(f"{get_now_time()}{name} 登录修复异常，本轮不重试：{exc}")
                continue
            if not login_result.get("ok"):
                if login_result.get("status") in {
                    LOGIN_EMAIL_MISSING,
                    LOGIN_EMAIL_REJECTED,
                    LOGIN_SAVED_PASSWORD_INCORRECT,
                    LOGIN_SAVED_PASSWORD_MISSING,
                }:
                    permanent_login_failures.add(name)
                print(
                    f"{get_now_time()}{name} 登录修复未通过，本轮不重试："
                    f"{login_result.get('message') or login_result.get('status')}"
                )
                continue
            print(f"{get_now_time()}{name} 登录态修复成功")

        if outcome_is_permanent_failure(browser_result) and not outcome_has_marker(
            browser_result, "登录失效"
        ):
            continue
        sites_to_retry = failed_sites(browser_result)
        configured_sites = _split_sites(current_row[3])
        sites_to_retry = [site for site in configured_sites if site in sites_to_retry]
        if not sites_to_retry:
            continue
        retry_row = list(current_row)
        retry_row[3] = "，".join(sites_to_retry)
        retry_plan.append((original_key, tuple(retry_row)))
    return retry_plan


def get_infractions_info_all(
    max_workers=DEFAULT_COLLECTION_MAX_WORKERS,
    stagger_min_seconds=None,
    stagger_max_seconds=None,
    retry_failed=True,
    selected_shops=None,
    selected_sites=None,
    selected_shop_sites=None,
    authorization_flag="visit_stats_enabled",
    persist=True,
    log_path=None,
    stop_event=None,
):
    """并发采集侵权；支持按店铺站点精确限定范围及仅返回本轮实时数据。"""
    start = int(time.time())
    print(start)
    bit_dir = Path(__file__).resolve().parent.parent / "bit"
    rows = list_config_rows(
        include_ignored=False,
        authorization_flag=authorization_flag,
    )
    rows = [row for row in rows if row and row[0]]
    rows = _deduplicate_config_rows(rows)
    rows = _filter_rows_by_shop_sites(rows, selected_shop_sites)
    rows = filter_config_rows(
        rows,
        selected_shops=selected_shops,
        selected_sites=selected_sites,
    )
    if (selected_shops or selected_sites or selected_shop_sites) and not rows:
        switch_label = "申诉" if authorization_flag == "appeal_enabled" else "访问数据统计"
        raise ValueError(f"所选店铺或站点未在授权店铺中开启{switch_label}")

    outcomes = _execute_infraction_rows(
        rows,
        max_workers=max_workers,
        stagger_min_seconds=stagger_min_seconds,
        stagger_max_seconds=stagger_max_seconds,
        log_path=log_path,
        stop_event=stop_event,
    )

    retry_rounds = (
        env_int("BIT_COLLECTION_RETRY_ROUNDS", 2, 0)
        if retry_failed and not _stop_requested(stop_event)
        else 0
    )
    permanent_login_failures = set()
    for retry_round in range(1, retry_rounds + 1):
        retry_plan = _prepare_infraction_retry_rows(
            outcomes,
            permanent_login_failures=permanent_login_failures,
            authorization_flag=authorization_flag,
        )
        if not retry_plan:
            break
        retry_site_count = sum(len(_split_sites(row[3])) for _key, row in retry_plan)
        print(
            f"{get_now_time()}侵权第 {retry_round}/{retry_rounds} 轮补跑 "
            f"{len(retry_plan)} 家、{retry_site_count} 个失败站点"
        )
        wait_for_batch_resume("侵权失败补跑")
        retry_rows = [row for _original_key, row in retry_plan]
        retry_outcomes = _execute_infraction_rows(
            retry_rows,
            max_workers=max_workers,
            stagger_min_seconds=stagger_min_seconds,
            stagger_max_seconds=stagger_max_seconds,
            lease_wait_seconds=env_float(
                "BIT_RETRY_WINDOW_LOCK_WAIT_SECONDS",
                DEFAULT_RETRY_LOCK_WAIT_SECONDS,
            ),
            log_path=log_path,
            stop_event=stop_event,
        )
        for original_key, retry_row in retry_plan:
            retry_outcome = retry_outcomes.get(row_key(retry_row))
            if retry_outcome is not None:
                outcomes[original_key] = merge_site_retry_outcome(
                    outcomes[original_key],
                    retry_outcome,
                )

    infraction_info_sum = []
    result = []
    for _row, browser_infractions, browser_result in outcomes.values():
        infraction_info_sum.extend(browser_infractions)
        result.extend(browser_result)

    infraction_info_sum_str = "\n".join(map(str, infraction_info_sum))
    print(infraction_info_sum_str)

    end = int(time.time())
    print(get_now_time() + "总花费", end - start)
    failure_report_path = write_unreadable_site_report("侵权采集", result)
    if failure_report_path:
        print(f"{get_now_time()}无法读取站点已记录：{failure_report_path}")

    output_path = ""
    email_sent = False
    if persist and not _stop_requested(stop_event):
        df = pd.DataFrame(
            infraction_info_sum,
            columns=["店铺名", "站点", "编号", "标题", "侵权时间", "提交时间", "执行时间", "类型"],
        )
        scoped_collection = bool(selected_shops or selected_sites or selected_shop_sites)
        replace_targets = [
            (str(row[1] or "").strip(), site)
            for row in rows
            for site in _split_sites(row[3])
            if str(row[1] or "").strip() and site
        ]
        date_str = datetime.now().strftime(
            "%Y-%m-%d-%H%M%S" if scoped_collection else "%Y-%m-%d-%H"
        )
        scope_suffix = "-选定范围" if scoped_collection else ""
        output_path = bit_dir / f"美客多-武汉泽顺店铺侵权信息汇总{scope_suffix}-{date_str}.xlsx"
        post_errors = []
        for step_name, action in (
            (
                "写入侵权数据",
                lambda: (
                    inset_infraction_info(
                        infraction_info_sum,
                        merge_latest=True,
                        replace_targets=replace_targets,
                    )
                    if scoped_collection
                    else inset_infraction_info(infraction_info_sum)
                ),
            ),
            ("写入侵权任务记录", lambda: insert_task_record(result)),
            ("导出侵权汇总", lambda: df.to_excel(output_path, index=False)),
        ):
            try:
                action()
            except Exception as exc:
                post_errors.append(f"{step_name}失败：{exc}")
                print(f"{get_now_time()}{step_name}失败：{exc}")

        if output_path.exists():
            email_sent = bool(
                send_info(
                    "美客多所有店铺侵权汇总",
                    infraction_info_sum_str,
                    output_path,
                    output_path.name,
                )
            )
            print(get_now_time() + ("发送邮件成功" if email_sent else "发送邮件失败，汇总文件已保留"))

        if post_errors:
            raise RuntimeError("；".join(post_errors))
    return {
        "data": infraction_info_sum,
        "results": result,
        "output_path": str(output_path),
        "failure_report_path": str(failure_report_path) if failure_report_path else "",
        "email_sent": email_sent,
        "selected_shops": list(selected_shops or ()),
        "selected_sites": list(selected_sites or ()),
        "selected_shop_sites": dict(selected_shop_sites or {}),
        "max_workers": max_workers,
        "failed_shops": sorted(
            {
                str(row[1] or "")
                for row in result
                if len(row) >= 4 and str(row[3] or "") != "成功"
            }
        ),
    }


if __name__ == "__main__":
    get_infractions_info_all()
