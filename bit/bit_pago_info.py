import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from bit.bit_api import closeBrowser, openBrowser
from bit.bit_collection_control import (
    DEFAULT_COLLECTION_MAX_WORKERS,
    batch_pause_remaining,
    env_float,
    stagger_sleep,
    trip_batch_rate_limit,
)
from bit.bit_mercado_limit import (
    get_mercado_backend_status,
    get_mercado_page_state,
    is_mercado_rate_limited_text,
)
from bit.bit_mercado_login import (
    bind_mercado_shop_context,
    try_record_login_anomaly,
)
from bit.bit_runtime_lock import create_window_lease
from bit.bit_db_api import inset_pago_info as api_inset_pago_info
from bit.bit_db_api import insert_task_record as api_insert_task_record
from bit.bit_send_mail import send_info
from bit.bit_utils import get_now_time
from bit.bit_config import list_config_rows


PAGO_HOME_URL = "https://global-selling.mercadopago.com/home"
ALL_PAGO_SITES = ["墨西哥", "巴西", "哥伦比亚", "智利", "阿根廷", "乌拉圭"]
DEFAULT_PAGO_SHOP_LIMIT = 5
COUNTRY_NAME_MAP = {
    "墨西哥": "Mexico",
    "巴西": "Brazil",
    "哥伦比亚": "Colombia",
    "智利": "Chile",
    "阿根廷": "Argentina",
    "乌拉圭": "Uruguay",
}
COUNTRY_ALIASES = {
    "墨西哥": ["Mexico", "México"],
    "巴西": ["Brazil", "Brasil"],
    "哥伦比亚": ["Colombia"],
    "智利": ["Chile"],
    "阿根廷": ["Argentina"],
    "乌拉圭": ["Uruguay"],
}
SITE_REMOTE_VALUE_MAP = {
    "墨西哥": "MLM-remote",
    "巴西": "MLB-remote",
    "哥伦比亚": "MCO-remote",
    "智利": "MLC-remote",
    "阿根廷": "MLA-remote",
    "乌拉圭": "MLU-remote",
}
SITE_ID_MAP = {
    "墨西哥": "MLM",
    "巴西": "MLB",
    "哥伦比亚": "MCO",
    "智利": "MLC",
    "阿根廷": "MLA",
    "乌拉圭": "MLU",
}
SITE_SHORT_CODE_MAP = {
    "墨西哥": "MX",
    "巴西": "BR",
    "哥伦比亚": "CO",
    "智利": "CL",
    "阿根廷": "AR",
    "乌拉圭": "UY",
}


class PagoRateLimitError(RuntimeError):
    pass


class PagoCollectionCancelled(RuntimeError):
    pass


def _safe_insert_pago_info(pago_info_sum):
    if not pago_info_sum:
        return

    try:
        api_inset_pago_info(pago_info_sum)
        print(get_now_time() + "款项数据已通过接口写入数据库")
        return
    except Exception as e:
        print(get_now_time() + f"款项数据接口写入失败，尝试本地 MySQL 兜底：{e}")

    try:
        from bit.bit_mysql import inset_pago_info as local_inset_pago_info

        local_inset_pago_info(pago_info_sum)
        print(get_now_time() + "款项数据已通过本地 MySQL 写入数据库")
    except Exception as e:
        print(get_now_time() + f"款项数据本地 MySQL 写入也失败，已保留 Excel 文件：{e}")


def _safe_insert_task_record(result):
    if not result:
        return

    try:
        api_insert_task_record(result)
        print(get_now_time() + "款项任务记录已通过接口写入数据库")
        return
    except Exception as e:
        print(get_now_time() + f"款项任务记录接口写入失败，尝试本地 MySQL 兜底：{e}")

    try:
        from bit.bit_mysql import insert_task_record as local_insert_task_record

        local_insert_task_record(result)
        print(get_now_time() + "款项任务记录已通过本地 MySQL 写入数据库")
    except Exception as e:
        print(get_now_time() + f"款项任务记录本地 MySQL 写入也失败，已跳过：{e}")


def _raise_if_cancelled(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise PagoCollectionCancelled("资金采集任务已终止")


def _interruptible_wait(seconds, stop_event=None):
    seconds = max(0.0, float(seconds or 0))
    if seconds <= 0:
        _raise_if_cancelled(stop_event)
        return
    if stop_event is not None:
        if stop_event.wait(seconds):
            raise PagoCollectionCancelled("资金采集任务已终止")
        return
    time.sleep(seconds)


def _wait_for_pago_batch_resume(source, stop_event=None):
    announced = False
    while True:
        _raise_if_cancelled(stop_event)
        remaining = batch_pause_remaining()
        if remaining <= 0:
            return
        if not announced:
            print(
                f"{source} 检测到批次限频熔断，统一暂停约 {int(remaining + 0.999)} 秒",
                flush=True,
            )
            announced = True
        _interruptible_wait(min(5.0, remaining), stop_event)


def _is_bit_api_rate_limited(res):
    return is_mercado_rate_limited_text(res)


def _connect_browser(
    window_id,
    max_retries=3,
    retry_delay=30,
    stop_event=None,
    batch_source="资金采集",
):
    last_res = None
    for attempt in range(1, max_retries + 1):
        _wait_for_pago_batch_resume(batch_source, stop_event)
        res = openBrowser(window_id)
        last_res = res
        print(res)

        data = res.get("data") if isinstance(res, dict) else None
        if data and data.get("driver") and data.get("http"):
            driver_path = data["driver"]
            debugger_address = data["http"]
            break

        msg = res.get("msg", "") if isinstance(res, dict) else str(res)
        if _is_bit_api_rate_limited(res):
            trip_batch_rate_limit(f"{batch_source}:比特浏览器", msg)
            print(
                f"{get_now_time()} 比特浏览器打开窗口被限频，进入批次暂停后重试："
                f"{window_id}，第 {attempt}/{max_retries} 次，原因：{msg}"
            )
            if attempt < max_retries:
                _wait_for_pago_batch_resume(batch_source, stop_event)
        else:
            print(
                f"{get_now_time()} 比特浏览器打开窗口返回异常，等待 {retry_delay} 秒后重试："
                f"{window_id}，第 {attempt}/{max_retries} 次，返回：{res}"
            )
            if attempt < max_retries:
                _interruptible_wait(retry_delay, stop_event)
    else:
        raise RuntimeError(f"打开比特浏览器窗口失败，已重试 {max_retries} 次，最后返回：{last_res}")

    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_experimental_option("debuggerAddress", debugger_address)

    chrome_service = Service(driver_path)
    driver = webdriver.Chrome(service=chrome_service, options=chrome_options)
    driver.implicitly_wait(10)
    driver.set_page_load_timeout(45)
    bind_mercado_shop_context(driver, window_id=window_id, source="资金采集")
    return driver


def _get_country_name(site):
    return COUNTRY_NAME_MAP.get(site, site)


def _get_country_aliases(site):
    return COUNTRY_ALIASES.get(site, [_get_country_name(site), site])


def _open_country_switch(driver):
    return driver.execute_script(
        """
        function allNodes(root) {
            const nodes = [...root.querySelectorAll('*')];
            for (const node of [...nodes]) {
                if (node.shadowRoot) {
                    nodes.push(...allNodes(node.shadowRoot));
                }
            }
            return nodes;
        }

        function isVisible(node) {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 &&
                rect.height > 0 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                Number(style.opacity || '1') > 0;
        }

        function clickNode(node) {
            node.scrollIntoView({block: 'center', inline: 'center'});
            for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                node.dispatchEvent(new MouseEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    view: window
                }));
            }
        }

        const nodes = allNodes(document);
        const classSwitcher = nodes.find((node) =>
            isVisible(node) && String(node.className || '').includes('nav-header-cbt__site-switcher')
        );
        if (classSwitcher) {
            clickNode(classSwitcher);
            return {clicked: true, text: classSwitcher.innerText || classSwitcher.getAttribute('aria-label') || ''};
        }

        const direct = nodes.find((node) => {
            const label = [
                node.getAttribute('aria-label') || '',
                node.getAttribute('title') || '',
                node.getAttribute('data-testid') || '',
                node.innerText || ''
            ].join(' ').replace(/\\s+/g, ' ').trim();
            return isVisible(node) &&
                node.tagName === 'BUTTON' &&
                /select\\s+(country|site)|country|site|pa[ií]s/i.test(label);
        });
        if (direct) {
            clickNode(direct);
            return {clicked: true, text: direct.innerText || direct.getAttribute('aria-label') || ''};
        }

        const roleButton = nodes.find((node) => {
            const role = node.getAttribute('role') || '';
            const label = [
                node.getAttribute('aria-label') || '',
                node.getAttribute('title') || '',
                node.innerText || ''
            ].join(' ').replace(/\\s+/g, ' ').trim();
            return isVisible(node) &&
                /button|combobox|listbox/i.test(role) &&
                /select\\s+(country|site)|country|site|pa[ií]s|Mexico|Brazil|Colombia|Chile|Argentina|Uruguay/i.test(label);
        });
        if (roleButton) {
            clickNode(roleButton);
            return {clicked: true, text: roleButton.innerText || roleButton.getAttribute('aria-label') || ''};
        }

        return {clicked: false, text: ''};
        """
    )


def _has_country_switch(driver):
    try:
        return bool(
            driver.execute_script(
                """
                function allNodes(root) {
                    const nodes = [...root.querySelectorAll('*')];
                    for (const node of [...nodes]) {
                        if (node.shadowRoot) {
                            nodes.push(...allNodes(node.shadowRoot));
                        }
                    }
                    return nodes;
                }

                function isVisible(node) {
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        rect.bottom > 0 &&
                        rect.right > 0 &&
                        style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        Number(style.opacity || '1') > 0;
                }

                return allNodes(document).some((node) => {
                    if (!isVisible(node)) {
                        return false;
                    }
                    const label = [
                        node.getAttribute('aria-label') || '',
                        node.getAttribute('title') || '',
                        node.getAttribute('data-testid') || '',
                        node.innerText || '',
                        String(node.className || '')
                    ].join(' ').replace(/\\s+/g, ' ').trim();
                    return /nav-header-cbt__site-switcher|select\\s+(country|site)|country|site|pa[ií]s|Mexico|Brazil|Colombia|Chile|Argentina|Uruguay/i.test(label);
                });
                """
            )
        )
    except Exception:
        return False


def _wait_country_switch_or_login(driver, timeout=12, stop_event=None):
    end_time = time.time() + timeout
    while time.time() < end_time:
        _raise_if_cancelled(stop_event)
        if _is_not_logged_in(driver):
            return "login"
        if _has_country_switch(driver):
            return "switcher"
        _interruptible_wait(1, stop_event)
    return "timeout"


def _click_country_option(driver, site):
    aliases = _get_country_aliases(site)
    remote_value = SITE_REMOTE_VALUE_MAP.get(site, "")
    return driver.execute_script(
        """
        const aliases = arguments[0].map((item) => String(item || '').trim()).filter(Boolean);
        const remoteValue = String(arguments[1] || '').trim();
        const aliasLower = aliases.map((item) => item.toLowerCase());

        function allNodes(root) {
            const nodes = [...root.querySelectorAll('*')];
            for (const node of [...nodes]) {
                if (node.shadowRoot) {
                    nodes.push(...allNodes(node.shadowRoot));
                }
            }
            return nodes;
        }

        function isVisible(node) {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 &&
                rect.height > 0 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                Number(style.opacity || '1') > 0;
        }

        function nodeText(node) {
            const title = node.querySelector('[data-andes-listbox-title="true"]')?.textContent || '';
            const text = title || node.innerText || node.textContent || '';
            return text.replace(/\\s+/g, ' ').trim();
        }

        function clickNode(node) {
            node.scrollIntoView({block: 'center', inline: 'center'});
            for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                node.dispatchEvent(new MouseEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    view: window
                }));
            }
        }

        const nodes = allNodes(document).filter((node) => {
            if (!isVisible(node)) {
                return false;
            }
            const tag = node.tagName;
            const role = node.getAttribute('role') || '';
            return ['LI', 'BUTTON', 'A', 'SPAN', 'DIV'].includes(tag) || /option|menuitem|button/i.test(role);
        });

        if (remoteValue) {
            const byDataValue = nodes.find((node) => node.getAttribute('data-value') === remoteValue);
            if (byDataValue) {
                const clickable = byDataValue.closest('li, button, a, [role="option"], [role="menuitem"]') || byDataValue;
                clickNode(clickable);
                return {
                    clicked: true,
                    text: nodeText(byDataValue) || remoteValue,
                    method: 'data-value',
                    candidates: [nodeText(byDataValue) || remoteValue]
                };
            }
        }

        const matches = nodes
            .map((node) => ({node, text: nodeText(node)}))
            .filter((item) => {
                const textLower = item.text.toLowerCase();
                return aliasLower.some((alias) => textLower === alias || textLower.includes(alias));
            });

        if (!matches.length) {
            return {clicked: false, text: '', candidates: []};
        }

        const preferred = matches.find((item) => !/full|completo/i.test(item.text)) || matches[0];
        const clickable = preferred.node.closest('li, button, a, [role="option"], [role="menuitem"]') || preferred.node;
        clickNode(clickable);
        return {
            clicked: true,
            text: preferred.text,
            method: 'text',
            candidates: matches.slice(0, 10).map((item) => item.text)
        };
        """,
        aliases,
        remote_value,
    )


def _get_current_country_snapshot(driver):
    return driver.execute_script(
        """
        const countries = ['Mexico', 'México', 'Brazil', 'Brasil', 'Colombia', 'Chile', 'Argentina', 'Uruguay'];

        function allNodes(root) {
            const nodes = [...root.querySelectorAll('*')];
            for (const node of [...nodes]) {
                if (node.shadowRoot) {
                    nodes.push(...allNodes(node.shadowRoot));
                }
            }
            return nodes;
        }

        function isVisible(node) {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 &&
                rect.height > 0 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                Number(style.opacity || '1') > 0;
        }

        const texts = [];
        for (const node of allNodes(document)) {
            if (!isVisible(node)) {
                continue;
            }
            const role = node.getAttribute('role') || '';
            const label = [
                node.getAttribute('aria-label') || '',
                node.getAttribute('title') || '',
                node.getAttribute('aria-selected') === 'true' ? node.innerText || '' : '',
                node.tagName === 'BUTTON' ? node.innerText || '' : '',
                /button|combobox/i.test(role) ? node.innerText || '' : ''
            ].join(' ').replace(/\\s+/g, ' ').trim();
            if (countries.some((country) => label.includes(country))) {
                texts.push(label);
            }
        }
        return [...new Set(texts)].slice(0, 20);
        """
    )


def _snapshot_has_site(snapshot, site):
    aliases = [item.lower() for item in _get_country_aliases(site)]
    text = " ".join(str(item or "") for item in snapshot).lower()
    return any(alias and alias in text for alias in aliases)


def _snapshot_has_other_site(snapshot, site):
    text = " ".join(str(item or "") for item in snapshot).lower()
    if not text:
        return False
    for other_site, aliases in COUNTRY_ALIASES.items():
        if other_site == site:
            continue
        if any(alias.lower() in text for alias in aliases):
            return True
    return False


def _safe_filename_part(value):
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", str(value or "").strip())
    return text[:60] or "unknown"


def _get_pago_site_state(driver):
    try:
        return driver.execute_script(
            """
            function textOf(selector) {
                const node = document.querySelector(selector);
                return node ? (node.innerText || node.textContent || '').trim() : '';
            }

            const available = [...document.querySelectorAll('#nav-header-cbt__switcher [data-value]')]
                .map((node) => ({
                    value: node.getAttribute('data-value') || '',
                    text: (node.innerText || node.textContent || node.getAttribute('alt') || '').trim(),
                    selected: String(node.className || '').includes('nav-header-cbt__option-selected')
                }))
                .filter((item, index, arr) =>
                    item.value && arr.findIndex((other) => other.value === item.value) === index
                );
            const selected = available.find((item) => item.selected) || null;

            const scriptsText = [...document.scripts]
                .map((script) => script.textContent || '')
                .join('\\n');
            const operatingMatch = scriptsText.match(/operating_site_id["']?\\s*:\\s*["']([A-Z]{3})["']/);
            const melidataMatch = scriptsText.match(/"siteId"\\s*:\\s*"([A-Z]{3})"/);

            const cookieMatch = document.cookie.match(/(?:^|;\\s*)cbtSiteId=([^;]+)/);
            return {
                currentShort: textOf('.nav-header-cbt__current-site'),
                selectedRemote: selected ? selected.value : '',
                selectedText: selected ? selected.text : '',
                operatingSiteId: operatingMatch ? operatingMatch[1] : '',
                melidataSiteId: melidataMatch ? melidataMatch[1] : '',
                cookieRemote: cookieMatch ? decodeURIComponent(cookieMatch[1]) : '',
                available,
                url: location.href,
                title: document.title
            };
            """
        ) or {}
    except Exception as e:
        return {"error": str(e)}


def _state_matches_site(state, site):
    target_remote = SITE_REMOTE_VALUE_MAP.get(site, "")
    target_id = SITE_ID_MAP.get(site, "")
    target_short = SITE_SHORT_CODE_MAP.get(site, "")
    return any(
        [
            target_remote and state.get("selectedRemote") == target_remote,
            target_id and state.get("operatingSiteId") == target_id,
            target_short and state.get("currentShort") == target_short,
        ]
    )


def _site_available_in_state(state, site):
    target_remote = SITE_REMOTE_VALUE_MAP.get(site, "")
    available = state.get("available") or []
    return any(item.get("value") == target_remote for item in available)


def _wait_pago_site_options(driver, timeout=20, stop_event=None):
    end_time = time.time() + timeout
    last_state = {}
    while time.time() < end_time:
        _raise_if_cancelled(stop_event)
        if _is_not_logged_in(driver):
            return "login", last_state
        state = _get_pago_site_state(driver)
        last_state = state
        if state.get("available") or state.get("currentShort") or state.get("operatingSiteId"):
            return "ready", state
        _interruptible_wait(1, stop_event)
    return "timeout", last_state


def _set_pago_site_cookie(driver, remote_value):
    try:
        driver.delete_cookie("cbtSiteId")
    except Exception:
        pass
    try:
        driver.add_cookie({"name": "cbtSiteId", "value": remote_value, "path": "/"})
    except Exception:
        pass
    driver.execute_script(
        """
        const value = arguments[0];
        document.cookie = `cbtSiteId=${value}; path=/`;
        """,
        remote_value,
    )


def _reload_pago_home(
    driver,
    name="",
    site="",
    stop_event=None,
    batch_source="资金采集",
):
    return _open_pago_home_with_retry(
        driver,
        name,
        site,
        reason="切换站点后刷新",
        stop_event=stop_event,
        batch_source=batch_source,
    )


def _save_pago_debug(driver, name, site, reason):
    try:
        debug_dir = Path(__file__).resolve().parent / "美客多款项" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = (
            f"pago_{_safe_filename_part(name)}_"
            f"{_safe_filename_part(site)}_{_safe_filename_part(reason)}_{timestamp}"
        )
        html_path = debug_dir / f"{base}.html"
        png_path = debug_dir / f"{base}.png"
        html_path.write_text(driver.page_source or "", encoding="utf-8")
        driver.save_screenshot(str(png_path))
        print(get_now_time() + f"{name}{site}已保存 Pago 调试文件：{html_path}，{png_path}")
        return str(html_path)
    except Exception as e:
        print(get_now_time() + f"{name}{site}保存 Pago 调试文件失败：{e}")
        return ""


def _select_country(
    driver,
    site,
    shop_name="",
    stop_event=None,
    batch_source="资金采集",
):
    if not site:
        return {"ok": True, "status": "未配置站点", "detail": ""}

    target_remote = SITE_REMOTE_VALUE_MAP.get(site)
    if not target_remote:
        return {
            "ok": False,
            "status": "未知站点",
            "detail": f"未配置站点映射：{site}",
        }

    ready_state, state = _wait_pago_site_options(driver, stop_event=stop_event)
    if ready_state == "login":
        return {"ok": False, "status": "未登录", "detail": "切换站点前检测到登录页面"}
    if ready_state == "timeout":
        debug_file = _save_pago_debug(driver, shop_name, site, "no_site_options")
        return {
            "ok": False,
            "status": "站点列表未出现",
            "detail": f"等待站点列表超时，state={state}",
            "debug_file": debug_file,
        }

    if _state_matches_site(state, site):
        print(get_now_time() + shop_name + "当前已是目标站点:", site)
        return {"ok": True, "status": "已在目标站点", "detail": str(state)}

    if not _site_available_in_state(state, site):
        available_text = ",".join(
            f"{item.get('value')}:{item.get('text')}" for item in state.get("available", [])
        )
        debug_file = _save_pago_debug(driver, shop_name, site, "site_unavailable")
        return {
            "ok": False,
            "status": "站点未开通",
            "detail": f"目标={target_remote}，可选={available_text}",
            "debug_file": debug_file,
        }

    last_state = state
    for attempt in range(1, 4):
        _wait_for_pago_batch_resume(batch_source, stop_event)
        print(
            get_now_time()
            + shop_name
            + f"开始切换 Pago 站点：{site} -> {target_remote}，第 {attempt}/3 次"
        )
        try:
            _set_pago_site_cookie(driver, target_remote)
            _reload_pago_home(
                driver,
                shop_name,
                site,
                stop_event=stop_event,
                batch_source=batch_source,
            )
            ready_state, new_state = _wait_pago_site_options(
                driver,
                timeout=12,
                stop_event=stop_event,
            )
            last_state = new_state
            if ready_state == "login":
                return {"ok": False, "status": "未登录", "detail": "刷新后进入登录页面"}
            if _state_matches_site(new_state, site):
                print(get_now_time() + shop_name + "成功切换 Pago 站点:", site)
                return {"ok": True, "status": "成功", "detail": str(new_state)}
            print(get_now_time() + shop_name + f"Pago 站点切换后校验失败：{new_state}")
            _interruptible_wait(2, stop_event)
        except PagoCollectionCancelled:
            raise
        except Exception as e:
            last_state = {"error": str(e), "state": last_state}
            print(get_now_time() + shop_name + "Pago 站点切换异常:", site, e)
            _interruptible_wait(2, stop_event)

    debug_file = _save_pago_debug(driver, shop_name, site, "cookie_switch_failed")
    return {
        "ok": False,
        "status": "站点切换失败",
        "detail": f"目标={target_remote}，最后状态={last_state}",
        "debug_file": debug_file,
    }


def _is_not_logged_in(driver):
    if get_mercado_backend_status(
        driver=driver,
        state=get_mercado_page_state(driver),
    ) == "logged_out":
        return True
    try:
        text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return False

    login_markers = [
        "Fill out your e-mail address to log in",
        "Fill out your email address to log in",
        "Enter your e-mail",
        "Enter your email",
        "Log in",
        "Iniciar sesión",
    ]
    return any(marker in text for marker in login_markers)


def _record_pago_logged_out(driver, window_id, name, site, detail=""):
    detail = str(detail or "").strip()
    return try_record_login_anomaly(
        {
            "status": "logged_out",
            "message": (
                f"{name}{site} 检测到美客多账号退出登录"
                f"{f'：{detail}' if detail else ''}"
            ),
        },
        window_id,
        name,
        site,
        "资金采集",
        driver=driver,
    )


def _wait_pago_home_ready(driver, stop_event=None):
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    for _ in range(30):
        _raise_if_cancelled(stop_event)
        if _is_not_logged_in(driver):
            return True
        ready = driver.execute_script(
            """
            const text = document.body ? document.body.innerText : "";
            return /(?:US\\$|USD|\\$)\\s*[-+]?\\d/i.test(text) ||
                /available|released|pending|to be released|balance/i.test(text);
            """
        )
        if ready:
            return True
        _interruptible_wait(1, stop_event)
    return False


def _pago_page_text(driver):
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        body_text = ""
    try:
        title = driver.title
    except Exception:
        title = ""
    try:
        current_url = driver.current_url
    except Exception:
        current_url = ""
    return "\n".join((str(title or ""), str(current_url or ""), str(body_text or "")))


def _is_pago_rate_limited_page(driver):
    return is_mercado_rate_limited_text(_pago_page_text(driver))


def _is_pago_open_failure(driver):
    try:
        text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return True
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if not normalized:
        return True
    failure_markers = [
        "something went wrong",
        "try again later",
        "too many requests",
        "temporarily unavailable",
        "access denied",
        "request blocked",
        "service unavailable",
        "gateway timeout",
        "bad gateway",
        "page not found",
        "we couldn't",
        "we could not",
    ]
    return any(marker in normalized for marker in failure_markers)


def _open_pago_home_with_retry(
    driver,
    name="",
    site="",
    reason="打开 Pago 首页",
    max_retries=3,
    stop_event=None,
    batch_source="资金采集",
    allow_global_ip_switch=False,
):
    # 保留参数兼容旧调用；普通打开失败不得切换节点，只有统一入口识别到
    # 指定西语限频文案时才进入限频处理。
    del allow_global_ip_switch
    last_error = ""
    last_was_rate_limit = False
    for attempt in range(1, max_retries + 1):
        _wait_for_pago_batch_resume(batch_source, stop_event)
        try:
            print(get_now_time() + f"{name}{site}{reason}，第 {attempt}/{max_retries} 次")
            driver.get(PAGO_HOME_URL)
            ready = _wait_pago_home_ready(driver, stop_event=stop_event)
            if _is_pago_rate_limited_page(driver):
                last_error = _pago_page_text(driver)[:500] or "Pago 页面触发限频"
                last_was_rate_limit = True
                trip_batch_rate_limit(f"{batch_source}:{name}:{site}", last_error)
                if attempt < max_retries:
                    _wait_for_pago_batch_resume(batch_source, stop_event)
                    continue
                break
            state = _get_pago_site_state(driver)
            if ready and not _is_pago_open_failure(driver):
                return True
            if state.get("available") or state.get("currentShort") or state.get("operatingSiteId"):
                return True
            last_error = f"页面未就绪，ready={ready}，state={state}"
            last_was_rate_limit = False
        except PagoCollectionCancelled:
            raise
        except Exception as e:
            last_error = str(e)
            last_was_rate_limit = is_mercado_rate_limited_text(last_error)
            if last_was_rate_limit:
                trip_batch_rate_limit(f"{batch_source}:{name}:{site}", last_error)

        if attempt < max_retries:
            if last_was_rate_limit:
                _wait_for_pago_batch_resume(batch_source, stop_event)
            else:
                print(get_now_time() + f"{name}{site}Pago 页面打开失败，等待后重试：{last_error}")
                _interruptible_wait(8, stop_event)

    debug_file = _save_pago_debug(driver, name, site, "open_home_failed")
    error_type = PagoRateLimitError if last_was_rate_limit else RuntimeError
    raise error_type(f"Pago 页面打开失败，已重试 {max_retries} 次：{last_error}，debug={debug_file}")


def _amount_to_number(amount_text):
    text = str(amount_text or "")
    match = re.search(r"[-+]?\d[\d.,]*", text)
    if not match:
        return 0.0
    number = match.group(0).replace(",", "")
    try:
        return float(number)
    except ValueError:
        return 0.0


def _normalize_money_text(value):
    text = str(value or "").replace("\xa0", " ")
    match = re.search(r"(?:US\$|USD|\$)\s*[-+]?\d[\d.,]*", text, flags=re.I)
    if not match:
        return ""
    amount = re.sub(r"\s+", " ", match.group(0)).strip()
    if amount.startswith("$"):
        amount = "US" + amount
    return amount


def _extract_money_candidates(driver):
    return driver.execute_script(
        """
        const moneyRegex = /(?:US\\$|USD|\\$)\\s*[-+]?\\d[\\d.,]*/i;

        function allNodes(root) {
            const nodes = [...root.querySelectorAll('*')];
            for (const node of [...nodes]) {
                if (node.shadowRoot) {
                    nodes.push(...allNodes(node.shadowRoot));
                }
            }
            return nodes;
        }

        function isVisible(node, rect, style) {
            return rect.width > 0 &&
                rect.height > 0 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                Number(style.opacity || '1') > 0;
        }

        function hasMoneyChild(node) {
            for (const child of node.children || []) {
                if (moneyRegex.test(child.innerText || child.textContent || '')) {
                    return true;
                }
            }
            return false;
        }

        const candidates = [];
        for (const node of allNodes(document)) {
            const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!moneyRegex.test(text)) {
                continue;
            }
            if (hasMoneyChild(node)) {
                continue;
            }
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            if (!isVisible(node, rect, style)) {
                continue;
            }
            const parentText = (node.closest('section, article, div, li, main')?.innerText || text)
                .replace(/\\s+/g, ' ')
                .trim()
                .slice(0, 500);
            const colorText = style.color || '';
            const colorParts = colorText.match(/\\d+(?:\\.\\d+)?/g) || [];
            const rgb = colorParts.slice(0, 3).map(Number);
            const darkColor = rgb.length === 3 && (rgb[0] + rgb[1] + rgb[2]) < 360;
            const fontWeight = Number(style.fontWeight) || 400;
            const fontSize = parseFloat(style.fontSize) || 0;
            const context = parentText.toLowerCase();
            const pendingContext = /pending|to be released|unreleased|not released|not available|reten|retained|por liberar/.test(context);
            const releasedContext = /available|released|withdraw|balance|liberado|disponible/.test(context) && !pendingContext;
            candidates.push({
                text,
                amountText: text.match(moneyRegex)?.[0] || '',
                parentText,
                top: rect.top + window.scrollY,
                left: rect.left + window.scrollX,
                width: rect.width,
                height: rect.height,
                fontWeight,
                fontSize,
                darkColor,
                pendingContext,
                releasedContext,
                tagName: node.tagName,
                className: String(node.className || ''),
            });
        }

        const seen = new Set();
        return candidates
            .sort((a, b) => (a.top - b.top) || (a.left - b.left))
            .filter((item) => {
                const key = [item.amountText, Math.round(item.top), Math.round(item.left)].join('|');
                if (seen.has(key)) {
                    return false;
                }
                seen.add(key);
                return true;
            });
        """
    )


def _extract_pago_amounts(driver):
    candidates = _extract_money_candidates(driver)
    if not candidates:
        body_text = ""
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:1000]
        except Exception:
            pass
        return {
            "released_usd": "",
            "unreleased_usd": "",
            "raw_text": body_text,
            "candidate_count": 0,
        }

    for item in candidates:
        item["amountText"] = _normalize_money_text(item.get("amountText") or item.get("text"))
        item["amountNumber"] = _amount_to_number(item["amountText"])

    candidates = [item for item in candidates if item.get("amountText")]
    if not candidates:
        return {
            "released_usd": "",
            "unreleased_usd": "",
            "raw_text": "",
            "candidate_count": 0,
        }

    pending_candidates = [item for item in candidates if item.get("pendingContext")]
    released_candidates = [item for item in candidates if item.get("releasedContext")]

    def released_score(item):
        return (
            (100 if item.get("releasedContext") else 0)
            + (40 if item.get("fontWeight", 0) >= 600 else 0)
            + (20 if item.get("darkColor") else 0)
            + float(item.get("fontSize") or 0) * 2
            + min(float(item.get("amountNumber") or 0), 999999) / 100000
            - (80 if item.get("pendingContext") else 0)
        )

    released = max(released_candidates or candidates, key=released_score)

    below_candidates = [
        item
        for item in candidates
        if item is not released and item.get("top", 0) >= released.get("top", 0) - 5
    ]
    if pending_candidates:
        unreleased = min(
            pending_candidates,
            key=lambda item: (
                abs(float(item.get("top", 0)) - float(released.get("top", 0))),
                -float(item.get("fontSize") or 0),
            ),
        )
    elif below_candidates:
        unreleased = min(
            below_candidates,
            key=lambda item: (
                abs(float(item.get("top", 0)) - float(released.get("top", 0))),
                0 if float(item.get("fontSize") or 0) <= float(released.get("fontSize") or 0) else 1,
            ),
        )
    else:
        unreleased = None

    raw_text_parts = []
    for item in candidates[:12]:
        raw_text_parts.append(item.get("parentText") or item.get("text") or "")
    raw_text = "\n".join(dict.fromkeys(raw_text_parts))

    return {
        "released_usd": released.get("amountText", ""),
        "unreleased_usd": unreleased.get("amountText", "") if unreleased else "",
        "raw_text": raw_text,
        "candidate_count": len(candidates),
    }


def get_pago_info(
    window_id,
    name,
    site,
    driver=None,
    stop_event=None,
    batch_source="资金采集",
):
    if driver is None:
        driver = _connect_browser(
            window_id,
            stop_event=stop_event,
            batch_source=batch_source,
        )
    bind_mercado_shop_context(driver, window_id, name, site, "资金采集")

    _open_pago_home_with_retry(
        driver,
        name,
        site,
        stop_event=stop_event,
        batch_source=batch_source,
    )
    if _is_not_logged_in(driver):
        print(get_now_time() + name + site + "未登录 Mercado Pago")
        _record_pago_logged_out(driver, window_id, name, site, "打开 Pago 首页后进入登录页")
        return [
            name,
            site,
            "",
            "",
            "未登录",
            get_now_time(),
            "",
        ]

    switch_result = _select_country(
        driver,
        site,
        name,
        stop_event=stop_event,
        batch_source=batch_source,
    )
    if not switch_result.get("ok"):
        if _is_not_logged_in(driver):
            print(get_now_time() + name + site + "未登录 Mercado Pago")
            _record_pago_logged_out(
                driver,
                window_id,
                name,
                site,
                switch_result.get("detail", "") or "切换站点时进入登录页",
            )
            return [
                name,
                site,
                "",
                "",
                "未登录",
                get_now_time(),
                switch_result.get("detail", ""),
            ]
        return [
            name,
            site,
            "",
            "",
            switch_result.get("status", "站点切换失败"),
            get_now_time(),
            switch_result.get("detail", "") or switch_result.get("debug_file", ""),
        ]

    _open_pago_home_with_retry(
        driver,
        name,
        site,
        reason="读取款项前刷新 Pago 首页",
        stop_event=stop_event,
        batch_source=batch_source,
    )
    if _is_not_logged_in(driver):
        print(get_now_time() + name + site + "未登录 Mercado Pago")
        _record_pago_logged_out(driver, window_id, name, site, "读取款项前刷新后进入登录页")
        return [
            name,
            site,
            "",
            "",
            "未登录",
            get_now_time(),
            "",
        ]

    amounts = _extract_pago_amounts(driver)
    released = amounts.get("released_usd", "")
    unreleased = amounts.get("unreleased_usd", "")
    status = "成功" if released or unreleased else "未读取到金额"
    if switch_result.get("status") == "已点击站点但未能验证" and status == "成功":
        status = "成功-站点未验证"

    print(
        get_now_time()
        + f"{name}{site}款项读取结果：已释放={released or '-'}，未释放={unreleased or '-'}，状态={status}"
    )
    return [
        name,
        site,
        released,
        unreleased,
        status,
        get_now_time(),
        amounts.get("raw_text", ""),
    ]


def _build_pago_failure_row(name, site, status="执行失败", message=""):
    return [
        name,
        site,
        "",
        "",
        status,
        get_now_time(),
        message,
    ]


def _is_ignored_config_value(value):
    return "忽略" in str(value or "").strip()


def _split_config_sites(value):
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return []
    sites = []
    for site in re.split(r"[，,、/;\s]+", text):
        site = site.strip()
        if site and site not in sites:
            sites.append(site)
    return sites


def _get_shop_limit():
    value = str(os.environ.get("BIT_PAGO_SHOP_LIMIT", DEFAULT_PAGO_SHOP_LIMIT)).strip()
    if value in ("", "0", "all", "ALL", "全部"):
        return None
    try:
        return max(1, int(value))
    except ValueError:
        return DEFAULT_PAGO_SHOP_LIMIT


def _run_pago_for_browser(row, sites=None, stop_event=None):
    window_id = row[0]
    name = row[1]
    sites = list(sites or [])
    if not sites:
        return [], [("获取款项信息", name, "", "失败：未配置站点", get_now_time())]

    _wait_for_pago_batch_resume(f"资金采集:{name}", stop_event)
    lease = create_window_lease(
        window_id,
        owner=f"pago_collection:{name}",
        shop_name=name,
        task_type="pago_collection",
    )
    if not lease.acquire(timeout=0):
        print(get_now_time() + name + "窗口已被其他任务占用，跳过本次款项采集")
        return [], [("获取款项信息", name, "", "跳过：窗口被其他任务占用", get_now_time())]

    print(get_now_time() + "开始打开窗口:" + name)
    pago_info_sum = []
    result = []

    try:
        driver = _connect_browser(
            window_id,
            stop_event=stop_event,
            batch_source=f"资金采集:{name}",
        )
        for site_index, site in enumerate(sites):
            _raise_if_cancelled(stop_event)
            login_expired = False
            for i in range(1, 4):
                try:
                    _wait_for_pago_batch_resume(f"资金采集:{name}", stop_event)
                    pago_info = get_pago_info(
                        window_id,
                        name,
                        site,
                        driver=driver,
                        stop_event=stop_event,
                        batch_source=f"资金采集:{name}",
                    )
                    pago_info_sum.append(pago_info)
                    status = pago_info[4] or "成功"
                    task_status = "失败：登录失效" if status == "未登录" else status
                    print(get_now_time() + name + site + "获取款项信息" + task_status)
                    result.append(("获取款项信息", name, site, task_status, get_now_time()))
                    login_expired = status == "未登录"
                    break
                except PagoCollectionCancelled:
                    raise
                except Exception as e:
                    print(get_now_time() + name + site + "执行失败", e)
                    is_rate_limited = (
                        isinstance(e, PagoRateLimitError)
                        or is_mercado_rate_limited_text(e)
                    )
                    if is_rate_limited:
                        trip_batch_rate_limit(f"资金采集:{name}:{site}", str(e))
                    if i == 3:
                        status = "失败：限频" if is_rate_limited else "失败"
                        result.append(("获取款项信息", name, site, status, get_now_time()))
                        pago_info_sum.append(
                            _build_pago_failure_row(
                                name,
                                site,
                                "限频" if is_rate_limited else "执行失败",
                                str(e),
                            )
                        )
                    else:
                        if is_rate_limited:
                            _wait_for_pago_batch_resume(f"资金采集:{name}", stop_event)
                        else:
                            _interruptible_wait(5, stop_event)
            if login_expired:
                for remaining_site in sites[site_index + 1:]:
                    pago_info_sum.append(
                        _build_pago_failure_row(
                            name,
                            remaining_site,
                            "未登录",
                            "同一店铺已检测到登录失效，等待自动登录修复",
                        )
                    )
                    result.append(
                        (
                            "获取款项信息",
                            name,
                            remaining_site,
                            "失败：登录失效",
                            get_now_time(),
                        )
                    )
                break
            if site_index < len(sites) - 1:
                _interruptible_wait(5, stop_event)
    except PagoCollectionCancelled:
        completed_sites = {
            str(item[1] or "")
            for item in pago_info_sum
            if isinstance(item, (list, tuple)) and len(item) > 1
        }
        for site in sites:
            if site in completed_sites:
                continue
            pago_info_sum.append(_build_pago_failure_row(name, site, "已终止", "任务已终止"))
            result.append(("获取款项信息", name, site, "已终止", get_now_time()))
    finally:
        print(get_now_time() + "结束，正在关闭窗口")
        try:
            closeBrowser(window_id, lease=lease)
        except Exception as e:
            print(get_now_time() + name + "关闭窗口失败", e)
        lease.release()
        print(get_now_time() + "已经关闭窗口")

    return pago_info_sum, result


def _selection_set(values):
    if values is None:
        return set()
    if isinstance(values, str):
        values = (values,)
    return {
        str(value or "").strip()
        for value in values
        if str(value or "").strip()
    }


def _display_salesperson(value):
    return str(value or "").strip() or "未分配"


def _build_pago_collection_jobs(
    apply_shop_limit=True,
    selected_shops=None,
    selected_window_ids=None,
    salesperson="",
):
    rows = list_config_rows(include_ignored=False)
    rows = [row for row in rows if row and row[0] and row[1]]
    shop_names = _selection_set(selected_shops)
    window_ids = _selection_set(selected_window_ids)
    owner = str(salesperson or "").strip()
    if shop_names:
        rows = [row for row in rows if str(row[1] or "").strip() in shop_names]
    if window_ids:
        rows = [row for row in rows if str(row[0] or "").strip() in window_ids]
    if owner:
        rows = [
            row
            for row in rows
            if _display_salesperson(row[5] if len(row) > 5 else "") == owner
        ]
    shop_limit = _get_shop_limit() if apply_shop_limit else None
    if shop_limit is not None:
        rows = rows[:shop_limit]
    jobs = [
        (row, _split_config_sites(row[3] if len(row) > 3 else ""))
        for row in rows
    ]
    return jobs, shop_limit


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
    return {
        field: row[index] if index < len(row) else ""
        for index, field in enumerate(fields)
    }


def _execute_pago_jobs(
    jobs,
    max_workers=DEFAULT_COLLECTION_MAX_WORKERS,
    stop_event=None,
    stagger_min_seconds=None,
    stagger_max_seconds=None,
):
    outcomes = {}
    if not jobs:
        return outcomes

    worker_count = max(1, min(int(max_workers), len(jobs)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {}
        for index, (row, sites) in enumerate(jobs):
            if stop_event is not None and stop_event.is_set():
                break
            future = executor.submit(_run_pago_for_browser, row, sites, stop_event)
            future_map[future] = (row, sites)
            if index < len(jobs) - 1:
                try:
                    delay = stagger_sleep(
                        stagger_min_seconds,
                        stagger_max_seconds,
                        sleep=lambda seconds: _interruptible_wait(seconds, stop_event),
                    )
                    print(f"{get_now_time()}资金店铺错峰启动，下一家等待 {delay:.1f} 秒")
                except PagoCollectionCancelled:
                    break

        if stop_event is not None and stop_event.is_set():
            for future in future_map:
                future.cancel()

        for future in as_completed(future_map):
            row, sites = future_map[future]
            name = row[1]
            if future.cancelled():
                browser_pagos = [
                    _build_pago_failure_row(name, site, "已终止", "任务已终止")
                    for site in sites
                ]
                browser_result = [
                    ("获取款项信息", name, site, "已终止", get_now_time())
                    for site in sites
                ]
            else:
                try:
                    browser_pagos, browser_result = future.result()
                except PagoCollectionCancelled:
                    browser_pagos = [
                        _build_pago_failure_row(name, site, "已终止", "任务已终止")
                        for site in sites
                    ]
                    browser_result = [
                        ("获取款项信息", name, site, "已终止", get_now_time())
                        for site in sites
                    ]
                except Exception as e:
                    print(get_now_time() + name + "窗口任务异常", e)
                    browser_pagos = [
                        _build_pago_failure_row(name, site, "执行失败", str(e))
                        for site in sites
                    ]
                    browser_result = [
                        ("获取款项信息", name, site, "失败", get_now_time())
                        for site in sites
                    ]
            outcomes[(str(row[0]), str(row[1]))] = (
                row,
                browser_pagos,
                browser_result,
            )
            print(get_now_time() + name + f"窗口任务完成，站点：{','.join(sites)}")
    return outcomes


def _repair_pago_logins(outcomes, stop_event=None):
    for key, (row, _pago_rows, task_rows) in list(outcomes.items()):
        if stop_event is not None and stop_event.is_set():
            break
        if not any("登录失效" in str(item[3] or "") for item in task_rows if len(item) >= 4):
            continue
        name = str(row[1] or "").strip()
        print(f"{get_now_time()}{name} 开始串行修复 Mercado 登录态")
        try:
            from bit.bit_mercado_login import (
                login_one_database_shop,
                sync_login_results_to_window_anomalies,
            )

            login_result = login_one_database_shop(
                _row_as_login_config(row),
                wait_seconds=int(env_float("BIT_LOGIN_REPAIR_WAIT_SECONDS", 60, 1)),
                page_load_timeout=int(env_float("BIT_LOGIN_REPAIR_PAGE_TIMEOUT", 20, 1)),
            )
            sync_login_results_to_window_anomalies([login_result], source="资金采集")
        except Exception as e:
            print(f"{get_now_time()}{name} 登录修复异常，本轮不重试：{e}")
            continue
        if not login_result.get("ok"):
            print(
                f"{get_now_time()}{name} 登录修复未通过，本轮不重试："
                f"{login_result.get('message') or login_result.get('status')}"
            )
            continue
        if stop_event is not None and stop_event.is_set():
            break
        print(f"{get_now_time()}{name} 登录态修复成功，重新采集全部配置站点")
        sites = _split_config_sites(row[3] if len(row) > 3 else "")
        outcomes[key] = (row, *_run_pago_for_browser(row, sites, stop_event))
    return outcomes


def _collect_pago_jobs(
    jobs,
    max_workers=DEFAULT_COLLECTION_MAX_WORKERS,
    stop_event=None,
    stagger_min_seconds=None,
    stagger_max_seconds=None,
    auto_login=True,
):
    outcomes = _execute_pago_jobs(
        jobs,
        max_workers=max_workers,
        stop_event=stop_event,
        stagger_min_seconds=stagger_min_seconds,
        stagger_max_seconds=stagger_max_seconds,
    )
    if auto_login and not (stop_event is not None and stop_event.is_set()):
        _repair_pago_logins(outcomes, stop_event=stop_event)
    pago_info_sum = []
    result = []
    for row, sites in jobs:
        outcome = outcomes.get((str(row[0]), str(row[1])))
        if outcome is None:
            name = str(row[1] or "")
            pago_info_sum.extend(
                _build_pago_failure_row(name, site, "已终止", "任务已终止")
                for site in sites
            )
            result.extend(
                ("获取款项信息", name, site, "已终止", get_now_time())
                for site in sites
            )
            continue
        pago_info_sum.extend(outcome[1])
        result.extend(outcome[2])
    return pago_info_sum, result


def get_pago_info_for_shop(
    shop_name="",
    window_id="",
    stop_event=None,
    auto_login=True,
):
    """采集指定店铺配置的全部站点；未配置的国家不会被额外执行。"""
    shop_name = str(shop_name or "").strip()
    window_id = str(window_id or "").strip()
    if not shop_name and not window_id:
        raise ValueError("请指定店铺名或窗口 ID")

    rows = [
        row
        for row in list_config_rows(include_ignored=False)
        if row and row[0] and row[1]
    ]
    row = next(
        (
            item
            for item in rows
            if (not shop_name or str(item[1]).strip() == shop_name)
            and (not window_id or str(item[0]).strip() == window_id)
        ),
        None,
    )
    if row is None:
        identifier = shop_name or window_id
        raise RuntimeError(f"未找到有效店铺配置：{identifier}")

    name = str(row[1]).strip()
    sites = _split_config_sites(row[3] if len(row) > 3 else "")
    if sites:
        pago_info_sum, result = _collect_pago_jobs(
            [(row, sites)],
            max_workers=1,
            stop_event=stop_event,
            stagger_min_seconds=0,
            stagger_max_seconds=0,
            auto_login=auto_login,
        )
    else:
        pago_info_sum = [_build_pago_failure_row(name, "", "未配置站点", "")]
        result = [("获取款项信息", name, "", "失败：未配置站点", get_now_time())]

    _safe_insert_pago_info(pago_info_sum)
    _safe_insert_task_record(result)
    return pago_info_sum


def get_pago_info_all(
    max_workers=DEFAULT_COLLECTION_MAX_WORKERS,
    apply_shop_limit=True,
    selected_shops=None,
    selected_window_ids=None,
    salesperson="",
    stop_event=None,
    stagger_min_seconds=None,
    stagger_max_seconds=None,
    auto_login=True,
):
    start = int(time.time())
    print(start)
    root_path = Path(__file__).resolve().parent
    output_dir = root_path / "美客多款项"
    output_dir.mkdir(parents=True, exist_ok=True)

    jobs, shop_limit = _build_pago_collection_jobs(
        apply_shop_limit=apply_shop_limit,
        selected_shops=selected_shops,
        selected_window_ids=selected_window_ids,
        salesperson=salesperson,
    )
    if (selected_shops or selected_window_ids or salesperson) and not jobs:
        raise ValueError("当前执行范围内没有有效店铺")
    print(
        get_now_time()
        + f"本次款项采集店铺数：{len(jobs)}，限制：{'全部' if shop_limit is None else shop_limit}"
    )
    for row, sites in jobs:
        print(get_now_time() + f"款项采集计划：{row[1]} -> {','.join(sites) if sites else '未配置站点'}")

    pago_info_sum = []
    result = []
    runnable_jobs = []
    for row, sites in jobs:
        if sites:
            runnable_jobs.append((row, sites))
        else:
            name = row[1]
            result.append(("获取款项信息", name, "", "失败：未配置站点", get_now_time()))
            pago_info_sum.append(_build_pago_failure_row(name, "", "未配置站点", ""))

    if runnable_jobs:
        browser_pagos, browser_result = _collect_pago_jobs(
            runnable_jobs,
            max_workers=max_workers,
            stop_event=stop_event,
            stagger_min_seconds=stagger_min_seconds,
            stagger_max_seconds=stagger_max_seconds,
            auto_login=auto_login,
        )
        pago_info_sum.extend(browser_pagos)
        result.extend(browser_result)

    print("\n".join(map(str, pago_info_sum)))
    end = int(time.time())
    print(get_now_time() + "总花费", end - start)

    df = pd.DataFrame(
        pago_info_sum,
        columns=[
            "店铺名",
            "站点",
            "已释放美元",
            "未释放美元",
            "状态",
            "更新时间",
            "页面原始信息",
        ],
    )

    date_str = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    output_path = output_dir / ("武汉泽顺店铺款项信息汇总" + date_str + ".xlsx")
    df.to_excel(output_path, index=False)

    _safe_insert_pago_info(pago_info_sum)
    _safe_insert_task_record(result)

    if not (stop_event is not None and stop_event.is_set()):
        send_info(
            "美客多店铺款项汇总",
            "",
            output_path,
            "武汉泽顺店铺款项信息汇总" + date_str + ".xlsx",
        )
        print(get_now_time() + "发送邮件成功")
    else:
        print(get_now_time() + "资金采集任务已终止，保留已采集数据但跳过汇总邮件")
    return pago_info_sum


def main():
    return get_pago_info_all()


if __name__ == "__main__":
    main()
