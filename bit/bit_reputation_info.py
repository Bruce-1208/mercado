import time
import re
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.wait import WebDriverWait

from bit.bit_utils import get_now_time
from bit.bit_api import *
from bit.bit_runtime_lock import create_window_lease
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
    trip_batch_rate_limit,
    wait_for_batch_resume,
    write_unreadable_site_report,
)
from bit.bit_mercado_limit import (
    get_mercado_page_state as _get_mercado_page_state,
    is_mercado_rate_limited_page,
    is_mercado_logged_out_state,
    is_mercado_rate_limited_text,
)
from bit.bit_mercado_login import open_mercado_backend_page
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
import pyautogui
from bit.bit_switch_country import *
from bit.bit_send_mail import *
import pandas as pd

from datetime import datetime
from pathlib import Path
from bit.bit_db_api import (
    get_latest_infraction_info,
    get_mercado_store_reputation,
    insert_task_record,
    inset_reputation_info,
    list_mercado_store_tokens,
)
from bit.bit_config import list_config_rows


REPUTATION_URL = "https://global-selling.mercadolibre.com/reputation"
SALES_SUMMARY_URL = "https://global-selling.mercadolibre.com/sales-summary"
METRICS_URL = "https://global-selling.mercadolibre.com/metrics#sc-menu"
ACCOUNT_RISK_URLS = {
    "restrictions": "https://global-selling.mercadolibre.com/account-risk?filter=restrictions",
    "warnings": "https://global-selling.mercadolibre.com/account-risk?filter=warnings",
}
LOGIN_TEXT_MARKERS = (
    "fill out your e-mail address to log in",
    "fill out your email address to log in",
    "iniciar sesión",
    "iniciar sesion",
    "iniciar sessão",
    "iniciar sessao",
    "登录您的账户",
    "登录你的账户",
    "登录账号",
    "请登录",
    "填写您的电子邮件地址以登录",
)
SITE_CODE_MAP = {
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
COUNTRY_SWITCH_SELECTORS = (
    "#nav-header-cbt__site-switcher-toggle",
    ".nav-header-cbt__site-switcher-trigger",
    'button[aria-label="Select country"]',
    'button[aria-label*="country" i]',
    'button[aria-label*="país" i]',
    'button[aria-label*="pais" i]',
    'button[aria-label*="国家"]',
    'button[aria-label*="站点"]',
    '.cbt-site-selector button[role="combobox"]',
    '.andes-dropdown__trigger[role="combobox"]',
    'button[role="combobox"]',
    ".nav-header-cbt__site-switcher",
    '[data-testid*="site-switcher"]',
)
SITE_SHORT_CODE_MAP = {
    "MLM": "MX",
    "MLB": "BR",
    "MCO": "CO",
    "MLC": "CL",
    "MLA": "AR",
    "MLU": "UY",
}
SITE_LABEL_ALIASES = {
    "MLM": ("mexico", "méxico"),
    "MLB": ("brazil", "brasil"),
    "MCO": ("colombia",),
    "MLC": ("chile",),
    "MLA": ("argentina",),
    "MLU": ("uruguay",),
}
METRIC_LABEL_ALIASES = {
    "complaints": (
        "complaints",
        "complaint",
        "claims",
        "投诉",
        "买家投诉",
        "客诉",
    ),
    "shipments": (
        "non-compliant shipments",
        "non compliant shipments",
        "late shipments",
        "delayed shipments",
        "shipping delays",
        "handling time",
        "不合规发货",
        "不合规的发货",
        "未按时发货",
        "发货延迟",
        "延迟发货",
        "延误发货",
        "派送时间",
    ),
    "cancellations": (
        "cancellations",
        "cancellation",
        "cancelled orders",
        "canceled orders",
        "取消",
        "订单取消",
        "取消订单",
        "卖家取消",
    ),
}
VISITS_LABEL_ALIASES = ("visits", "visit", "访问量", "访问次数", "访问", "访客量")
CANCELLATION_REVIEW_LABELS = (
    "review in metrics",
    "review metrics",
    "view in metrics",
    "view metrics",
    "see metrics",
    "ver en métricas",
    "ver en metricas",
    "revisar en métricas",
    "revisar en metricas",
    "ver métricas",
    "ver metricas",
    "查看指标",
    "查看详情",
)


class MercadoRateLimitError(RuntimeError):
    """Mercado Libre 或浏览器接口返回访问限频。"""


class MercadoAuthenticationError(RuntimeError):
    """Mercado Libre 登录态已经失效。"""


class MercadoPageStructureError(RuntimeError):
    """Mercado Libre 页面已打开，但预期的业务结构不存在。"""


class BitBrowserWindowError(RuntimeError):
    """比特浏览器窗口配置无效或无法打开。"""


def _is_rate_limited_text(value):
    return is_mercado_rate_limited_text(value)


def _is_spanish_ip_switch_text(value):
    return is_mercado_rate_limited_text(value)


def _is_bit_api_rate_limited(res):
    return _is_rate_limited_text(res)


def _connect_browser(
    window_id,
    max_retries=3,
    retry_delay=30,
    batch_control=False,
    batch_source="声誉采集",
):
    last_res = None
    for attempt in range(1, max_retries + 1):
        res = openBrowser(window_id)  # 窗口ID从窗口配置界面中复制，或者api创建后返回
        last_res = res
        print(res)

        data = res.get("data") if isinstance(res, dict) else None
        if data and data.get("driver") and data.get("http"):
            driverPath = data["driver"]
            debuggerAddress = data["http"]
            break

        msg = res.get("msg", "") if isinstance(res, dict) else str(res)
        if "没有找到相应数据" in msg or "不存在" in msg:
            raise BitBrowserWindowError(f"比特浏览器窗口无效或不存在：{res}")
        is_rate_limited = _is_bit_api_rate_limited(res)
        if is_rate_limited:
            print(
                f"{get_now_time()} 比特浏览器打开窗口被限频："
                f"{window_id}，第 {attempt}/{max_retries} 次，原因：{msg}"
            )
        else:
            print(
                f"{get_now_time()} 比特浏览器打开窗口返回异常，等待 {retry_delay} 秒后重试："
                f"{window_id}，第 {attempt}/{max_retries} 次，返回：{res}"
            )
        if is_rate_limited and batch_control:
            trip_batch_rate_limit(batch_source, msg)
        if attempt < max_retries:
            if is_rate_limited and batch_control:
                wait_for_batch_resume(batch_source)
            else:
                time.sleep(retry_delay)
    else:
        if _is_bit_api_rate_limited(last_res):
            raise MercadoRateLimitError(
                f"比特浏览器打开窗口被限频，已重试 {max_retries} 次：{last_res}"
            )
        raise BitBrowserWindowError(
            f"打开比特浏览器窗口失败，已重试 {max_retries} 次，最后返回：{last_res}"
        )

    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_experimental_option("debuggerAddress", debuggerAddress)

    chrome_service = Service(driverPath)
    driver = webdriver.Chrome(service=chrome_service, options=chrome_options)
    driver.implicitly_wait(10)
    return driver


def _is_mercado_rate_limited_page(driver=None, state=None):
    return is_mercado_rate_limited_page(driver=driver, state=state)


def _is_spanish_ip_switch_page(driver=None, state=None):
    return is_mercado_rate_limited_page(driver=driver, state=state)


def _is_mercado_login_state(state):
    return is_mercado_logged_out_state(state)


def _raise_if_mercado_unavailable(driver=None, state=None, context="页面"):
    state = state or _get_mercado_page_state(driver)
    if _is_mercado_login_state(state):
        raise MercadoAuthenticationError(
            f"{context}登录态失效，已跳转登录页：{state.get('current_url', '')}"
        )
    if _is_mercado_rate_limited_page(state=state):
        raise MercadoRateLimitError(
            f"{context}访问受限：{state.get('current_url', '')} "
            f"{state.get('page_text', '')[:160]}"
        )
    return state


def _open_collection_backend_page(
    driver,
    url,
    *,
    window_id="",
    name="",
    site="",
    context="页面",
    settle_seconds=5,
):
    """采集页面共用入口：自动恢复登录，限频交给批次熔断。"""
    result = open_mercado_backend_page(
        driver,
        url,
        name,
        window_id,
        settle_seconds=settle_seconds,
        max_rate_limit_retries=0,
        max_login_retries=1,
        state_reader=_get_mercado_page_state,
        anomaly_site=site,
        anomaly_source="声誉采集",
    )
    if result.get("status") == "rate_limited":
        raise MercadoRateLimitError(
            f"{name}{site}{context}触发限频；采集任务禁止直接切换全局 "
            "Clash 节点，交给批次熔断统一暂停"
        )
    if result.get("status") == "logged_out":
        raise MercadoAuthenticationError(
            result.get("message") or f"{name}{site}{context}登录态失效"
        )
    if not result.get("ok"):
        raise RuntimeError(result.get("message") or f"{name}{site}{context}打开失败")
    return result.get("state") or {}


def _open_reputation_page_with_validation(
    driver,
    name="",
    site="",
    window_id="",
    max_hongkong_switches=3,
    switch_wait_seconds=8,
    allow_global_ip_switch=False,
):
    """打开并验证声誉页；限频时交给批次熔断，禁止直接切换全局节点。"""
    # 保留旧参数以兼容已有调用，但节点切换已由批次策略全面禁用。
    del max_hongkong_switches, switch_wait_seconds, allow_global_ip_switch
    state = _open_collection_backend_page(
        driver,
        REPUTATION_URL,
        window_id=window_id,
        name=name,
        site=site,
        context="声誉页面",
        settle_seconds=10,
    )

    try:
        WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.CLASS_NAME, "title__page--cbt"))
        )
    except Exception as exc:
        raise MercadoPageStructureError(
            f"{name}{site}声誉页面结构不匹配：{state.get('current_url', '')}"
        ) from exc

    print(f"{get_now_time()}{name}{site}声誉页面打开验证通过")
    return True


def _get_country_name(site):
    country_map = {
        "墨西哥": "Mexico",
        "巴西": "Brazil",
        "哥伦比亚": "Colombia",
        "智利": "Chile",
        "阿根廷": "Argentina",
        "乌拉圭": "Uruguay",
    }
    return country_map.get(site, site)


def _deep_shadow_click(driver, selectors):
    return bool(
        driver.execute_script(
            """
            const selectors = arguments[0];
            function findAndClick(root) {
                for (const selector of selectors) {
                    let node = null;
                    try { node = root.querySelector(selector); } catch (_) {}
                    if (node) {
                        node.scrollIntoView({block: 'center', inline: 'center'});
                        for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                            node.dispatchEvent(new MouseEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                view: window
                            }));
                        }
                        return true;
                    }
                }
                for (const node of root.querySelectorAll('*')) {
                    if (node.shadowRoot && findAndClick(node.shadowRoot)) return true;
                }
                return false;
            }
            return findAndClick(document);
            """,
            list(selectors),
        )
    )


def _get_country_selection_state(driver):
    """读取顶部站点切换器的显式状态，兼容旧版 Header 和新版 Shadow DOM。"""
    try:
        return driver.execute_script(
            """
            function allNodes(root) {
                const nodes = [...root.querySelectorAll('*')];
                for (const node of [...nodes]) {
                    if (node.shadowRoot) nodes.push(...allNodes(node.shadowRoot));
                }
                return nodes;
            }

            function classText(node) {
                return String(node?.className?.baseVal || node?.className || '');
            }

            function clean(value) {
                return String(value || '').replace(/\\s+/g, ' ').trim();
            }

            const nodes = allNodes(document);
            const selected = nodes.find((node) =>
                classText(node).includes('nav-header-cbt__option-selected') ||
                (node.getAttribute('aria-selected') === 'true' && node.getAttribute('data-value'))
            );
            const trigger = nodes.find((node) =>
                node.id === 'nav-header-cbt__site-switcher-toggle' ||
                classText(node).includes('nav-header-cbt__site-switcher-trigger') ||
                (node.matches?.('button[role="combobox"]') &&
                    /country|site|pa[ií]s|国家|站点/i.test(clean([
                        node.getAttribute('aria-label'),
                        node.getAttribute('title'),
                        node.innerText
                    ].join(' '))))
            );
            const triggerNodes = trigger ? [trigger, ...allNodes(trigger)] : [];
            const valueNode = triggerNodes.find((node) =>
                classText(node).includes('nav-header-cbt__site-switcher-value') ||
                classText(node).includes('nav-header-cbt__current-site')
            );
            const flag = triggerNodes.find((node) => node.tagName === 'IMG');
            const available = nodes
                .filter((node) => /^(MLM|MLB|MCO|MLC|MLA|MLU)-(remote|fulfillment)$/.test(
                    node.getAttribute('data-value') || ''
                ))
                .map((node) => ({
                    value: node.getAttribute('data-value') || '',
                    text: clean(node.innerText || node.textContent || node.getAttribute('alt')),
                    selected: node === selected || classText(node).includes('selected')
                }))
                .filter((item, index, items) =>
                    items.findIndex((candidate) => candidate.value === item.value) === index
                );
            const scriptsText = [...document.scripts]
                .map((script) => script.textContent || '')
                .join('\\n');
            const operatingMatch = scriptsText.match(
                /operating_site_id["']?\\s*:\\s*["']([A-Z]{3})["']/
            );

            return {
                selectedRemote: selected?.getAttribute('data-value') ||
                    selected?.querySelector?.('[data-value]')?.getAttribute('data-value') || '',
                selectedText: clean(selected?.innerText || selected?.textContent),
                switcherValue: clean(valueNode?.innerText || valueNode?.textContent),
                currentFlagAlt: clean(flag?.getAttribute('alt') || flag?.getAttribute('title')),
                triggerText: clean(trigger?.innerText || trigger?.textContent),
                operatingSiteId: operatingMatch ? operatingMatch[1] : '',
                available,
                currentUrl: location.href,
                title: document.title
            };
            """
        ) or {}
    except Exception as exc:
        return {"error": str(exc)}


def _country_selection_state_matches(state, site):
    state = state or {}
    site_key = str(site or "").strip()
    site_code = SITE_CODE_MAP.get(site_key) or SITE_CODE_MAP.get(site_key.upper())
    if not site_code:
        return False

    target_remote = f"{site_code}-remote"
    target_short = SITE_SHORT_CODE_MAP.get(site_code, "")
    selected_remote = str(state.get("selectedRemote") or "").strip()
    if selected_remote:
        # 墨西哥 Full 与 Remote 共用 MX/国旗，存在显式 data-value 时必须严格区分。
        return selected_remote == target_remote

    country_aliases = SITE_LABEL_ALIASES.get(
        site_code,
        (_get_country_name(site_key).casefold(),),
    )
    explicit_text = " ".join(
        str(state.get(key) or "")
        for key in ("selectedText", "currentFlagAlt", "triggerText")
    ).casefold()
    return any(
        (
            str(state.get("operatingSiteId") or "").strip().upper() == site_code,
            bool(target_short)
            and str(state.get("switcherValue") or "").strip().upper() == target_short,
            any(alias and alias in explicit_text for alias in country_aliases),
        )
    )


def _wait_for_country_selection(driver, site, timeout=15, poll_seconds=1):
    deadline = time.monotonic() + max(0, float(timeout or 0))
    last_state = {}
    while True:
        last_state = _get_country_selection_state(driver)
        if _country_selection_state_matches(last_state, site):
            return last_state
        _raise_if_mercado_unavailable(driver=driver, context=f"{site}切换站点后的页面")
        if time.monotonic() >= deadline:
            return last_state
        time.sleep(max(0.05, float(poll_seconds or 0)))


def _open_country_switch(driver, timeout=15, poll_seconds=1):
    """等待并打开站点选择器。

    新版站点控件是异步加载的 Shadow DOM 模块，部分账号使用
    ``button[role=combobox]`` 且 aria-label 为西语 ``Seleccionar país``。
    声誉页主体可见不代表该模块已经完成渲染，因此需要单独等待。
    """
    deadline = time.monotonic() + max(0, float(timeout or 0))
    while True:
        if _deep_shadow_click(driver, COUNTRY_SWITCH_SELECTORS):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.05, float(poll_seconds or 0)))


def _select_country(
    driver,
    site,
    shop_name="",
    recovery_url=REPUTATION_URL,
    structure_context="页面",
):
    if not site:
        return True

    site_key = str(site).strip()
    site_code = SITE_CODE_MAP.get(site_key) or SITE_CODE_MAP.get(site_key.upper())
    country = _get_country_name(site)
    for attempt in range(1, 4):
        try:
            current_state = _get_country_selection_state(driver)
            if _country_selection_state_matches(current_state, site):
                print(get_now_time() + shop_name + "当前已是目标站点:", site)
                return True

            opened = _open_country_switch(driver)
            if not opened:
                opened = bool(oepn_country_switch(driver))
            if not opened:
                raise MercadoPageStructureError("没有找到站点选择器")

            time.sleep(1)
            success = False
            if site_code:
                success = _deep_shadow_click(
                    driver,
                    (
                        f'[data-value="{site_code}-remote"]',
                        f'[data-value^="{site_code}-"]',
                    ),
                )
            if not success:
                success = force_select_country(driver, country)
            if not success and site_key != country:
                success = force_select_country(driver, site_key)
            if success:
                selected_state = _wait_for_country_selection(driver, site)
                state = _raise_if_mercado_unavailable(
                    driver=driver,
                    context=f"{shop_name}{site}切换站点后的页面",
                )
                if not _country_selection_state_matches(selected_state, site):
                    raise MercadoPageStructureError(
                        f"{shop_name}{site}切换站点后未确认目标站点，"
                        f"{structure_context}：{state.get('current_url', '')}，"
                        f"站点状态={selected_state}"
                    )
                print(get_now_time() + shop_name + "成功选择站点:", site)
                return True
            raise MercadoPageStructureError(f"没有找到站点选项：{site}")
        except MercadoAuthenticationError:
            if attempt >= 3:
                raise
            _open_collection_backend_page(
                driver,
                recovery_url,
                name=shop_name,
                site=site,
                context="站点切换登录恢复",
                settle_seconds=5,
            )
            print(f"{get_now_time()}{shop_name}{site}登录态已恢复，重试站点切换")
            continue
        except MercadoRateLimitError:
            raise
        except Exception as exc:
            print(
                get_now_time()
                + shop_name
                + f"选择站点失败，第{attempt}/3次:"
                + site,
                exc,
            )
            if attempt < 3:
                try:
                    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                except Exception:
                    pass
                time.sleep(3)
    raise MercadoPageStructureError(f"{shop_name}{site}站点切换失败")


def _click_visits_metric(driver):
    selectors = [
        (By.XPATH, "//*[self::button or @role='button' or @role='tab'][contains(., 'Visits')]"),
        (By.XPATH, "//*[normalize-space()='Visits']"),
        (By.XPATH, "//*[self::button or @role='button' or @role='tab'][contains(., '访问')]"),
        (By.XPATH, "//*[normalize-space()='访问量' or normalize-space()='访问次数']"),
        (
            By.XPATH,
            "/html/body/main/div/div/div[3]/div/div/div[3]/section/div[2]/div[2]/div[2]/div[1]/div/div/div/div[4]",
        ),
    ]
    for by, selector in selectors:
        try:
            WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((by, selector))
            ).click()
            time.sleep(2)
            return
        except Exception:
            continue

    clicked = driver.execute_script(
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

        const labels = arguments[0].map((value) => value.toLocaleLowerCase());
        const visitNode = allNodes(document).find((node) => {
            const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!text || text.length > 80) return false;
            const normalized = text.toLocaleLowerCase();
            return labels.some((label) => normalized === label || normalized.includes(label));
        });
        if (!visitNode) {
            return false;
        }
        const clickable = visitNode.closest('button, a, [role="button"], [role="tab"]') || visitNode;
        clickable.scrollIntoView({block: 'center', inline: 'center'});
        clickable.click();
        return true;
        """,
        list(VISITS_LABEL_ALIASES),
    )
    if not clicked:
        raise MercadoPageStructureError("没有找到 Visits/访问量 入口")
    time.sleep(2)


def _parse_visits_tooltip(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    value = None
    for line in reversed(lines):
        match = re.search(r"[\d,.]+", line)
        if match:
            value = match.group(0).replace(",", "")
            break
    if value is None:
        return None

    date_text = ""
    for line in lines:
        if re.search(r"[A-Za-z]{3,}|月|日|/|-", line) and not re.fullmatch(
            r"[\d,.]+", line
        ):
            date_text = line
            break
    if not date_text:
        date_text = lines[0]

    return {"date": date_text, "visits": value, "raw": text}


def _parse_visit_records_from_json(data, days):
    records = []

    def walk(node, parent=None):
        if isinstance(node, dict):
            lower_keys = {str(key).lower(): key for key in node.keys()}
            value_key = None
            for key in node.keys():
                key_text = str(key).lower()
                if "visit" in key_text and isinstance(node.get(key), (int, float, str)):
                    value_key = key
                    break

            date_key = None
            for key_text, raw_key in lower_keys.items():
                if any(token in key_text for token in ["date", "day", "period", "label"]):
                    date_key = raw_key
                    break

            if value_key is not None:
                records.append(
                    {
                        "date": str(node.get(date_key, "")),
                        "visits": str(node.get(value_key, "")).replace(",", ""),
                        "raw": node,
                    }
                )

            for value in node.values():
                walk(value, node)
        elif isinstance(node, list):
            numeric_items = [item for item in node if isinstance(item, (int, float))]
            if parent and len(numeric_items) >= days:
                parent_text = str(parent).lower()
                if "visit" in parent_text:
                    for value in numeric_items[-days:]:
                        records.append({"date": "", "visits": str(value), "raw": parent})
            for item in node:
                walk(item, parent)

    walk(data)

    cleaned = []
    seen = set()
    for record in records:
        visits = record.get("visits", "")
        if not re.search(r"\d", visits):
            continue
        key = (record.get("date", ""), visits)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(record)
    return cleaned[-days:]


def _extract_visits_from_network(driver, days):
    entries = driver.execute_script(
        """
        return performance.getEntriesByType('resource')
            .map((entry) => entry.name)
            .filter((url) => /metric|visit|traffic|analytics|sales-summary/i.test(url))
            .slice(-30);
        """
    )

    for url in reversed(entries):
        try:
            data = driver.execute_async_script(
                """
                const url = arguments[0];
                const done = arguments[arguments.length - 1];
                fetch(url, {credentials: 'include'})
                    .then((response) => {
                        const contentType = response.headers.get('content-type') || '';
                        if (!contentType.includes('json')) {
                            done(null);
                            return;
                        }
                        return response.json().then((json) => done(json));
                    })
                    .catch(() => done(null));
                """,
                url,
            )
            if not data:
                continue
            visits = _parse_visit_records_from_json(data, days)
            if visits:
                return visits
        except Exception:
            continue
    return []


def _get_tooltip_text(driver):
    tooltip_selectors = [
        ".andes-tooltip",
        ".recharts-tooltip-wrapper",
        ".highcharts-tooltip",
        "[role='tooltip']",
        "[class*='tooltip']",
    ]
    for selector in tooltip_selectors:
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        for element in elements:
            text = element.text.strip()
            if text:
                return text
    return ""


def _get_chart_rect(driver):
    rect = driver.execute_script(
        """
        const candidates = [...document.querySelectorAll('svg, canvas')]
            .map((node) => {
                const rect = node.getBoundingClientRect();
                return {
                    x: rect.left,
                    y: rect.top,
                    width: rect.width,
                    height: rect.height,
                    area: rect.width * rect.height,
                    visible: rect.width > 160 && rect.height > 100 && rect.bottom > 0 && rect.right > 0,
                    tag: node.tagName,
                    text: node.closest('section, main, div')?.innerText || ''
                };
            })
            .filter((item) => item.visible)
            .sort((a, b) => {
                const aVisit = /Visits|访问/i.test(a.text) ? 1 : 0;
                const bVisit = /Visits|访问/i.test(b.text) ? 1 : 0;
                return (bVisit - aVisit) || (b.area - a.area);
            });
        return candidates[0] || null;
        """
    )
    return rect


def _extract_visits_from_dom(driver, days):
    data = driver.execute_script(
        """
        const results = [];
        const candidates = [
            ...document.querySelectorAll('svg circle, svg [class*="dot"], svg [class*="point"]')
        ];
        for (const node of candidates) {
            const aria = node.getAttribute('aria-label') || node.getAttribute('data-testid') || '';
            const title = node.querySelector('title')?.textContent || '';
            const text = `${aria} ${title}`.trim();
            if (/visit|访问/i.test(text) && /\\d/.test(text)) {
                results.push(text);
            }
        }
        return [...new Set(results)].slice(-arguments[0]);
        """,
        days,
    )
    visits = []
    for item in data:
        parsed = _parse_visits_tooltip(item)
        if parsed:
            visits.append(parsed)
    return visits


def _extract_visits_by_hover(driver, days):
    rect = _get_chart_rect(driver)
    if not rect:
        return []

    left = rect["x"] + rect["width"] * 0.08
    right = rect["x"] + rect["width"] * 0.96
    y_list = [
        rect["y"] + rect["height"] * 0.35,
        rect["y"] + rect["height"] * 0.5,
        rect["y"] + rect["height"] * 0.65,
    ]
    visits = []

    for i in range(days):
        x = right - ((days - 1 - i) * (right - left) / max(days - 1, 1))
        for y in y_list:
            try:
                driver.execute_script(
                    """
                    const x = arguments[0];
                    const y = arguments[1];
                    const target = document.elementFromPoint(x, y) || document.body;
                    for (const type of ['pointerover', 'pointermove']) {
                        const eventClass = window.PointerEvent || window.MouseEvent;
                        target.dispatchEvent(new eventClass(type, {
                            bubbles: true,
                            cancelable: true,
                            clientX: x,
                            clientY: y,
                            view: window,
                            pointerType: 'mouse'
                        }));
                    }
                    for (const type of ['mouseover', 'mousemove']) {
                        target.dispatchEvent(new MouseEvent(type, {
                            bubbles: true,
                            cancelable: true,
                            clientX: x,
                            clientY: y,
                            view: window
                        }));
                    }
                    """,
                    x,
                    y,
                )
                time.sleep(0.35)
                tooltip = _get_tooltip_text(driver)
                parsed = _parse_visits_tooltip(tooltip)
                if parsed and parsed not in visits:
                    visits.append(parsed)
                    break
            except Exception:
                continue

    return visits[-days:]


def _to_visit_number_list(visits, days):
    numbers = []
    for item in visits[-days:]:
        value = item.get("visits", item) if isinstance(item, dict) else item
        match = re.search(r"\d+(?:[,.]\d+)*", str(value))
        if not match:
            continue
        numbers.append(int(match.group(0).replace(",", "").replace(".", "")))
    return numbers[-days:]


def get_recent_visits_info(driver,window_id, name, site, days=8):
    # driver = _connect_browser(window_id)
    _open_collection_backend_page(
        driver,
        METRICS_URL,
        window_id=window_id,
        name=name,
        site=site,
        context="流量页面",
        settle_seconds=5,
    )

    # _select_country(driver, site, name)
    _click_visits_metric(driver)
    time.sleep(3)

    visits = _extract_visits_from_network(driver, days)
    if len(visits) < days:
        visits = _extract_visits_from_dom(driver, days)
    if len(visits) < days:
        visits = _extract_visits_by_hover(driver, days)

    if not visits:
        print("没有读取到Visits/访问量流量数据，请确认页面已加载并且折线图可见")
        debug_path = Path(__file__).resolve().parent / "visits_debug.png"
        driver.save_screenshot(str(debug_path))
        print("已保存调试截图:", debug_path)

    result = _to_visit_number_list(visits, days)
    print(f"最近{days}天Visits/访问量流量数据:", result)
    return result


def get_visits_info(driver,window_id, name="", site="", days=8):
    return get_recent_visits_info(driver,window_id, name, site, days)


def _normalize_label(value):
    return re.sub(r"[\W_]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _metric_kind_from_title(title):
    normalized = _normalize_label(title)
    for kind, aliases in METRIC_LABEL_ALIASES.items():
        if any(_normalize_label(alias) in normalized for alias in aliases):
            return kind
    return ""


def _extract_reputation_metrics(driver):
    cards = driver.execute_script(
        """
        return [...document.querySelectorAll('.variable__title')]
          .map((titleNode) => {
            const card = titleNode.closest('.andes-card') ||
              titleNode.closest('[class*="variable"]') || titleNode.parentElement;
            const percentage = card?.querySelector('.variable__percentage');
            return {
              title: (titleNode.innerText || titleNode.textContent || '').trim(),
              percentage: (percentage?.innerText || percentage?.textContent || '').trim()
            };
          })
          .filter((item) => item.title && item.percentage);
        """
    ) or []

    metrics = {}
    for card in cards:
        kind = _metric_kind_from_title(card.get("title", ""))
        if kind and kind not in metrics:
            metrics[kind] = card.get("percentage", "")

    # 中英文翻译版本都保留相同的变量卡片顺序。若平台再次调整译文，
    # 使用结构顺序兜底：投诉在首项、取消在倒数第二项、发货延误在末项。
    if len(cards) >= 3:
        first_kind = _metric_kind_from_title(cards[0].get("title", ""))
        cancel_kind = _metric_kind_from_title(cards[-2].get("title", ""))
        shipment_kind = _metric_kind_from_title(cards[-1].get("title", ""))
        if not first_kind:
            metrics.setdefault("complaints", cards[0].get("percentage", ""))
        if not cancel_kind:
            metrics.setdefault("cancellations", cards[-2].get("percentage", ""))
        if not shipment_kind:
            metrics.setdefault("shipments", cards[-1].get("percentage", ""))

    missing = [
        kind
        for kind in ("complaints", "shipments", "cancellations")
        if not metrics.get(kind)
    ]
    if missing:
        discovered = ", ".join(card.get("title", "") for card in cards) or "无"
        raise MercadoPageStructureError(
            f"声誉指标卡片解析失败，缺少 {','.join(missing)}；页面卡片：{discovered}"
        )
    return metrics


def _normalize_cancellation_order_ids(values):
    """按页面出现顺序去重并过滤明显不是 Mercado 订单号的值。"""
    result = []
    seen = set()
    for value in values or []:
        text = re.sub(r"\D", "", str(value or ""))
        if not 10 <= len(text) <= 20 or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _click_reputation_review_in_metrics(driver, metric_kind, fallback_index):
    """定位指定声誉指标卡片，并点击该卡片内的 Review in Metrics。"""
    before_handles = set(driver.window_handles)
    result = driver.execute_script(
        r"""
        function normalize(value) {
            return String(value || '')
                .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
                .toLowerCase().replace(/[^a-z0-9\u3400-\u9fff]+/g, '').trim();
        }
        const metricAliases = arguments[0].map(normalize);
        const reviewAliases = arguments[1].map(normalize);
        const fallbackIndex = Number(arguments[2]);
        function allElements(root = document) {
            const out = [];
            const visit = (scope) => {
                const elements = scope.querySelectorAll ? Array.from(scope.querySelectorAll('*')) : [];
                for (const element of elements) {
                    out.push(element);
                    if (element.shadowRoot) visit(element.shadowRoot);
                }
            };
            visit(root);
            return out;
        }
        function textOf(element) {
            return normalize([
                element.innerText || element.textContent || '',
                element.getAttribute?.('aria-label') || '',
                element.getAttribute?.('title') || ''
            ].join(' '));
        }
        function isMetricTitle(element) {
            const text = textOf(element);
            return text && text.length < 180 && metricAliases.some(alias => text.includes(alias));
        }
        function isReviewLink(element) {
            const text = textOf(element);
            const href = normalize(element.getAttribute?.('href') || '');
            const clickable = ['A', 'BUTTON'].includes(element.tagName) ||
                ['button', 'link'].includes(element.getAttribute?.('role')) ||
                !!element.onclick || !!href;
            return clickable && (
                reviewAliases.some(alias => text.includes(alias)) ||
                href.includes('metrics')
            );
        }
        function click(element) {
            element.scrollIntoView({block: 'center', inline: 'center'});
            element.click();
        }

        const elements = allElements(document);
        const variableTitles = elements.filter(element =>
            String(element.className || '').includes('variable__title')
        );
        let title = variableTitles.find(isMetricTitle) || elements.find(isMetricTitle);
        if (!title && variableTitles.length) {
            const index = fallbackIndex < 0
                ? variableTitles.length + fallbackIndex
                : fallbackIndex;
            if (index >= 0 && index < variableTitles.length) title = variableTitles[index];
        }
        if (!title) return {clicked: false, has_metric: false, reason: 'metric not found'};

        let container = title;
        for (let depth = 0; container && depth < 9; depth += 1, container = container.parentElement) {
            const candidates = [container, ...allElements(container)];
            const target = candidates.find(isReviewLink);
            if (target) {
                click(target);
                return {
                    clicked: true,
                    has_metric: true,
                    title: textOf(title),
                    href: target.getAttribute?.('href') || ''
                };
            }
        }

        const globalReviewLinks = elements.filter(isReviewLink);
        if (globalReviewLinks.length === 1) {
            click(globalReviewLinks[0]);
            return {clicked: true, has_metric: true, title: textOf(title), fallback: true};
        }
        return {clicked: false, has_metric: true, title: textOf(title), reason: 'review link not found'};
        """,
        list(METRIC_LABEL_ALIASES[metric_kind]),
        list(CANCELLATION_REVIEW_LABELS),
        int(fallback_index),
    ) or {}

    if not result.get("clicked"):
        return result

    for _ in range(30):
        handles = set(driver.window_handles)
        new_handles = list(handles - before_handles)
        if new_handles:
            driver.switch_to.window(new_handles[-1])
        current_url = str(getattr(driver, "current_url", "") or "").lower()
        if "/metrics" in current_url:
            break
        time.sleep(0.5)
    return result


def _click_cancellation_review_in_metrics(driver):
    """定位取消率卡片，并点击该卡片内的 Review in Metrics。"""
    return _click_reputation_review_in_metrics(driver, "cancellations", -2)


def _click_complaint_review_in_metrics(driver):
    """定位 Complaints 卡片，并点击该卡片内的 Review in Metrics。"""
    return _click_reputation_review_in_metrics(driver, "complaints", 0)


def _extract_visible_cancellation_order_ids(driver):
    """从当前 Metrics 页的订单行、链接及订单属性中提取订单号。"""
    result = driver.execute_script(
        r"""
        function normalize(value) {
            return String(value || '').toLowerCase().replace(/\s+/g, ' ').trim();
        }
        function allElements(root = document) {
            const out = [];
            const visit = (scope) => {
                const elements = scope.querySelectorAll ? Array.from(scope.querySelectorAll('*')) : [];
                for (const element of elements) {
                    out.push(element);
                    if (element.shadowRoot) visit(element.shadowRoot);
                }
            };
            visit(root);
            return out;
        }
        const ids = [];
        const seen = new Set();
        const add = value => {
            const id = String(value || '').replace(/\D/g, '');
            if (id.length >= 10 && id.length <= 20 && !seen.has(id)) {
                seen.add(id);
                ids.push(id);
            }
        };
        const prefixedPattern = /(?:order|orders|sale|sales|venta|ventas|venda|vendas|pedido|pedidos|订单|销售单)[^0-9]{0,30}(\d{8,20})/gi;
        const genericPattern = /\b\d{10,20}\b/g;
        for (const element of allElements(document)) {
            for (const attribute of ['data-order-id', 'data-order-number', 'data-sale-id']) {
                const value = element.getAttribute?.(attribute);
                if (value) add(value);
            }
            const href = element.getAttribute?.('href') || '';
            const hrefMatches = href.matchAll(/(?:orders?|sales?|ventas?|pedidos?)[^0-9]{0,20}(\d{8,20})/gi);
            for (const match of hrefMatches) add(match[1]);

            const text = String(element.innerText || element.textContent || '').trim();
            if (!text || text.length > 800) continue;
            for (const match of text.matchAll(prefixedPattern)) add(match[1]);

            const tag = String(element.tagName || '').toLowerCase();
            const role = normalize(element.getAttribute?.('role') || '');
            const className = normalize(element.className || '');
            const rowLike = ['tr', 'li'].includes(tag) || role === 'row' ||
                /row|order|sale|card|item|record/.test(className);
            const normalizedText = normalize(text);
            if (rowLike) {
                for (const match of text.matchAll(genericPattern)) add(match[0]);
            }
        }
        return {
            ids,
            fingerprint: ids.join('|') + ':' + String(document.body?.scrollHeight || 0),
            height: document.body?.scrollHeight || 0
        };
        """,
    ) or {}
    return {
        "ids": _normalize_cancellation_order_ids(result.get("ids") or []),
        "fingerprint": str(result.get("fingerprint") or ""),
        "height": int(result.get("height") or 0),
    }


def _advance_cancellation_orders_page(driver):
    """优先点击加载更多/下一页；没有分页控件时滚动触发懒加载。"""
    return str(
        driver.execute_script(
            r"""
            function normalize(value) {
                return String(value || '')
                    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
                    .toLowerCase().replace(/\s+/g, ' ').trim();
            }
            function allElements(root = document) {
                const out = [];
                const visit = (scope) => {
                    const elements = scope.querySelectorAll ? Array.from(scope.querySelectorAll('*')) : [];
                    for (const element of elements) {
                        out.push(element);
                        if (element.shadowRoot) visit(element.shadowRoot);
                    }
                };
                visit(root);
                return out;
            }
            function visible(element) {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            }
            function disabled(element) {
                return element.disabled || element.getAttribute('aria-disabled') === 'true' ||
                    /disabled/.test(String(element.className || '').toLowerCase());
            }
            function label(element) {
                return normalize([
                    element.innerText || element.textContent || '',
                    element.getAttribute('aria-label') || '',
                    element.getAttribute('title') || ''
                ].join(' '));
            }
            function click(element) {
                element.scrollIntoView({block: 'center'});
                element.click();
            }
            const clickables = allElements(document).filter(element =>
                visible(element) && !disabled(element) && (
                    ['A', 'BUTTON'].includes(element.tagName) ||
                    ['button', 'link'].includes(element.getAttribute('role'))
                )
            );
            const moreLabels = ['load more', 'show more', 'see more', 'view more', 'cargar mas',
                'mostrar mas', 'ver mas', 'ver mais', 'carregar mais', 'mostrar mais', '加载更多', '显示更多', '查看更多'];
            const nextLabels = ['next', 'next page', 'siguiente', 'proxima', 'próxima', '下一页'];
            const more = clickables.find(element => moreLabels.some(value => label(element).includes(value)));
            if (more) {
                click(more);
                return 'clicked_more';
            }
            const next = clickables.find(element => {
                const text = label(element);
                return nextLabels.some(value => text === value || text.includes(value + ' page'));
            });
            if (next) {
                click(next);
                return 'clicked_next';
            }
            const before = window.scrollY;
            window.scrollTo(0, document.body?.scrollHeight || document.documentElement.scrollHeight);
            return window.scrollY === before ? 'done' : 'scrolled';
            """
        ) or "done"
    )


def _get_reputation_metric_order_ids(
    driver,
    name,
    site,
    metric_label,
    click_review,
    max_pages,
):
    """从指定声誉指标的 Metrics 页面读取全部影响声誉的销售单号。"""
    _open_reputation_page_with_validation(driver, name, site)
    _select_country(driver, site, name)
    click_result = click_review(driver)
    if not click_result.get("has_metric"):
        raise MercadoPageStructureError(
            f"{name}{site}没有找到{metric_label}指标卡片：{click_result.get('reason', '')}"
        )
    if not click_result.get("clicked"):
        print(
            f"{get_now_time()}{name}{site}{metric_label}没有可点击的 Review in Metrics，"
            "当前没有可获取的销售单号"
        )
        return []

    time.sleep(3)
    _raise_if_mercado_unavailable(
        driver=driver,
        context=f"{name}{site}{metric_label} Metrics 页面",
    )

    orders = []
    seen = set()
    stagnant_rounds = 0
    previous_fingerprint = ""
    for page_index in range(1, max(1, int(max_pages)) + 1):
        state = _extract_visible_cancellation_order_ids(driver)
        added = 0
        for order_id in state["ids"]:
            if order_id in seen:
                continue
            seen.add(order_id)
            orders.append(order_id)
            added += 1
        print(
            f"{get_now_time()}{name}{site}{metric_label}销售单第{page_index}次读取新增{added}个，"
            f"累计{len(orders)}个"
        )

        fingerprint = state["fingerprint"]
        if added == 0 and fingerprint == previous_fingerprint:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        previous_fingerprint = fingerprint
        if stagnant_rounds >= 2:
            break

        action = _advance_cancellation_orders_page(driver)
        if action == "done":
            break
        time.sleep(2 if action.startswith("clicked") else 1)

    print(f"{get_now_time()}{name}{site}{metric_label}共获取到{len(orders)}个销售单号：{orders}")
    return orders


def get_cancellation_orders(driver, name="", site="", max_pages=100):
    """从声誉页取消率的 Review in Metrics 中读取全部取消订单号。"""
    return _get_reputation_metric_order_ids(
        driver,
        name,
        site,
        "取消率",
        _click_cancellation_review_in_metrics,
        max_pages,
    )


def get_complaint_orders(driver, name="", site="", max_pages=100):
    """从声誉页 Complaints 的 Review in Metrics 中读取全部影响声誉的销售单号。"""
    return _get_reputation_metric_order_ids(
        driver,
        name,
        site,
        "投诉",
        _click_complaint_review_in_metrics,
        max_pages,
    )


def _normalize_reputation_color(text, class_name=""):
    value = f"{text or ''} {class_name or ''}".casefold()
    color_aliases = (
        (
            "无色",
            (
                "you still have no color",
                "no color",
                "sin color",
                "sem cor",
                "无色",
                "暂无颜色",
                "没有颜色",
            ),
        ),
        ("红色", ("red", "rojo", "vermelho", "红色", "红")),
        ("橘色", ("orange", "naranja", "laranja", "橘色", "橙色")),
        ("黄色", ("yellow", "amarillo", "amarelo", "黄色", "黄")),
        ("绿色", ("green", "verde", "绿色", "绿", "mercadoleader", "mercado leader")),
    )
    for translated, aliases in color_aliases:
        if any(alias in value for alias in aliases):
            return translated
    return str(text or "").strip()


def _parse_gradient(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    replacements = (
        (r"decreased?|下降|减少|下滑", "下滑"),
        (r"increased?|上升|上涨|增加|增长", "增长"),
        (r"unchanged|no\s+change|持平|未变化|未变更|无变化|不变", "持平"),
    )
    normalized = text
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

    direction_match = re.search(r"下滑|增长|持平", normalized)
    rate_match = re.search(r"(?:\d+(?:[.,]\d+)?|—|-)\s*%", normalized)
    direction = direction_match.group(0) if direction_match else normalized
    rate = rate_match.group(0).replace(" ", "") if rate_match else normalized
    return direction, rate


def _get_account_risk_links(driver):
    try:
        return driver.execute_script(
            """
            const roots = [...document.querySelectorAll(
                '.andes-message__content, [role="alert"], [class*="message__content"]'
            )];
            return roots.flatMap((root) => [...root.querySelectorAll('a[href*="/account-risk"]')])
                .map((node) => node.href || node.getAttribute('href') || '')
                .filter((href, index, links) => href && links.indexOf(href) === index);
            """
        ) or []
    except Exception:
        return []


def _account_risk_kinds_from_summary(summary_text, links=None):
    sources = [str(summary_text or ""), *(str(link or "") for link in (links or []))]
    text = "\n".join(sources)
    kinds = []
    if re.search(r"go\s+to\s+restrictions?|filter=restrictions?\b", text, re.IGNORECASE):
        kinds.append("restrictions")
    if re.search(r"go\s+to\s+warnings?|filter=warnings?\b", text, re.IGNORECASE):
        kinds.append("warnings")
    return kinds


def _normalize_account_risk_details(raw_details):
    generic_line = re.compile(
        r"^(?:\d+\s*)?(?:go\s+to\s+)?(?:restrictions?|warnings?|"
        r"restricciones?|restri(?:ç|c)[õo]es?|advertencias?|avisos?)$|"
        r"^(?:account\s+risk|riesgo\s+de\s+la\s+cuenta|risco\s+da\s+conta|"
        r"view\s+details?|see\s+details?|learn\s+more|back|all|loading\.\.\.|"
        r"cargando\.\.\.|carregando\.\.\.)$",
        re.IGNORECASE,
    )
    cleaned = []
    for raw_detail in raw_details or []:
        lines = []
        for raw_line in str(raw_detail or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip(" \t\r\n·•")
            line = re.sub(
                r"^(?:\d+\s*)?\[?go\s+to\s+(?:restrictions?|warnings?)\]?"
                r"(?:\([^)]*\))?$",
                "",
                line,
                flags=re.IGNORECASE,
            ).strip()
            if not line or generic_line.fullmatch(line) or re.fullmatch(r"\d+", line):
                continue
            if re.fullmatch(r"\d+\s+go\s+to\s+(?:restrictions?|warnings?)", line, re.IGNORECASE):
                continue
            lines.append(line)
        detail = "\n".join(lines).strip()
        if detail and detail not in cleaned:
            cleaned.append(detail)

    # 同一个卡片常会被父、子容器重复读取；优先保留更具体的子容器文本。
    specific = []
    for detail in cleaned:
        if any(
            other != detail and len(other) >= 8 and other in detail
            for other in cleaned
        ):
            continue
        specific.append(detail)
    return specific or cleaned


def _extract_account_risk_details(driver):
    try:
        return driver.execute_script(
            """
            function visible(node) {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            }
            function clean(node) {
                return String(node?.innerText || node?.textContent || '')
                    .replace(/\\u00a0/g, ' ')
                    .replace(/[ \\t]+/g, ' ')
                    .replace(/\\n{3,}/g, '\\n\\n')
                    .trim();
            }
            function values(root, selectors) {
                const found = [];
                for (const selector of selectors) {
                    for (const node of root.querySelectorAll(selector)) {
                        const text = visible(node) ? clean(node) : '';
                        if (text && text.length >= 4 && !found.includes(text)) found.push(text);
                    }
                }
                return found;
            }

            const root = document.querySelector('main') || document.body;
            const specific = values(root, [
                '[data-testid*="restriction-item" i]',
                '[data-testid*="warning-item" i]',
                '[data-testid*="restriction-card" i]',
                '[data-testid*="warning-card" i]',
                '[class*="restriction-card" i]',
                '[class*="warning-card" i]'
            ]);
            if (specific.length) return specific;

            const cards = values(root, [
                'article',
                '[role="listitem"]',
                '.andes-card',
                '[class*="risk-item" i]',
                '[class*="risk-card" i]'
            ]);
            if (cards.length) return cards;

            const mainText = clean(root);
            return mainText ? [mainText] : [];
            """
        ) or []
    except Exception:
        return []


def _wait_account_risk_details(driver, timeout=12, poll_seconds=1):
    deadline = time.monotonic() + max(0, float(timeout or 0))
    while True:
        details = _normalize_account_risk_details(
            _extract_account_risk_details(driver)
        )
        if details:
            return details
        _raise_if_mercado_unavailable(driver=driver, context="账户风险详情页面")
        if time.monotonic() >= deadline:
            return []
        time.sleep(max(0.05, float(poll_seconds or 0)))


def _collect_account_risk_detail_text(driver, kinds, window_id="", name="", site=""):
    details = []
    for kind in kinds:
        url = ACCOUNT_RISK_URLS.get(kind)
        if not url:
            continue
        _open_collection_backend_page(
            driver,
            url,
            window_id=window_id,
            name=name,
            site=site,
            context="账户限制详情" if kind == "restrictions" else "账户警告详情",
            settle_seconds=3,
        )
        kind_details = _wait_account_risk_details(driver)
        if not kind_details:
            raise MercadoPageStructureError(
                f"{name}{site}{'限制' if kind == 'restrictions' else '警告'}详情页面结构不匹配：{url}"
            )
        for detail in kind_details:
            if detail not in details:
                details.append(detail)
    return "\n".join(details).strip()


def get_reputation_auxiliary_info(
    window_id,
    name,
    site,
    driver=None,
    select_site=True,
):
    """从非声誉页面采集近七天变化、系统告警和流量趋势。"""
    if driver is None:
        driver = _connect_browser(window_id)

    data_warn = "正常"
    direction = ""
    gradient_rate = ""
    auxiliary_errors = []
    try:
        _open_collection_backend_page(
            driver,
            SALES_SUMMARY_URL,
            window_id=window_id,
            name=name,
            site=site,
            context="销售汇总页面",
            settle_seconds=3,
        )
        if select_site:
            _select_country(
                driver,
                site,
                name,
                recovery_url=SALES_SUMMARY_URL,
                structure_context="销售汇总页面",
            )
        try:
            data_warn = (
                WebDriverWait(driver, 10)
                .until(
                    EC.visibility_of_element_located(
                        (By.CLASS_NAME, "andes-message__content")
                    )
                )
                .text
            )
        except Exception:
            data_warn = "正常"
        account_risk_kinds = _account_risk_kinds_from_summary(
            data_warn,
            _get_account_risk_links(driver),
        )

        try:
            data_gradient = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, ".andes-badge .andes-visually-hidden")
                )
            ).text
        except Exception:
            data_gradient = "持平"
        print("近七天变化情况为:", data_gradient)
        direction, gradient_rate = _parse_gradient(data_gradient)
        if account_risk_kinds:
            data_warn = _collect_account_risk_detail_text(
                driver,
                account_risk_kinds,
                window_id=window_id,
                name=name,
                site=site,
            )
            if not data_warn:
                raise MercadoPageStructureError(f"{name}{site}账户风险详情为空")
    except Exception as exc:
        if select_site:
            raise
        auxiliary_errors.append(f"销售汇总{_failure_status(exc).removeprefix('失败：')}")

    print("系统提示为:", data_warn)

    try:
        visits = str(get_visits_info(driver, window_id, name, site, 8))
    except Exception as exc:
        visits = "[]"
        auxiliary_errors.append(f"流量{_failure_status(exc).removeprefix('失败：')}")

    if auxiliary_errors:
        auxiliary_message = "辅助采集失败：" + "；".join(auxiliary_errors)
        data_warn = auxiliary_message if data_warn == "正常" else f"{data_warn}\n{auxiliary_message}"

    return {
        "store_name": name,
        "site": site,
        "direction": direction,
        "gradient_rate": gradient_rate,
        "system_warning": data_warn,
        "updated_at": get_now_time(),
        "visits": visits,
        "error": "；".join(auxiliary_errors),
    }


def get_reputation_info(
    window_id,
    name,
    site,
    driver=None,
    allow_global_ip_switch=False,
):
    if driver is None:
        driver = _connect_browser(window_id)

    _open_reputation_page_with_validation(
        driver,
        name,
        site,
        window_id=window_id,
        allow_global_ip_switch=allow_global_ip_switch,
    )
    _select_country(driver, site, name)

    metrics = _extract_reputation_metrics(driver)
    data_complain = metrics["complaints"]
    data_delay = metrics["shipments"]
    data_cancel = metrics["cancellations"]
    print("提取到的投诉率为:", data_complain)
    print("提取到的延误率为:", data_delay)
    print("提取到的取消率为:", data_cancel)

    color_element = driver.find_element(By.CLASS_NAME, "thermometer__level")
    data_color = _normalize_reputation_color(
        color_element.text,
        color_element.get_attribute("class"),
    )
    print("账号的声誉为:", data_color)

    data_orders = driver.find_element(By.CLASS_NAME, "value__sales").text
    print("总单数为：", data_orders)

    reputation = [
        name,
        site,
        data_color,
        data_orders,
        data_complain,
        data_delay,
        data_cancel,
    ]
    auxiliary = get_reputation_auxiliary_info(
        window_id,
        name,
        site,
        driver=driver,
        select_site=False,
    )
    reputation.extend([
        auxiliary["direction"],
        auxiliary["gradient_rate"],
        auxiliary["system_warning"],
        auxiliary["updated_at"],
        auxiliary["visits"],
    ])
    return reputation


def _failure_status(exc):
    text = re.sub(r"\s+", " ", str(exc or "")).strip()
    lower = text.casefold()
    if isinstance(exc, MercadoAuthenticationError) or any(
        marker in lower for marker in LOGIN_TEXT_MARKERS
    ):
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
    elif "站点切换失败" in text or "没有找到站点" in text:
        reason = "站点切换失败"
    elif isinstance(exc, MercadoPageStructureError):
        reason = "页面结构不匹配"
    elif isinstance(exc, TimeoutException) or "timeout" in lower:
        reason = "页面元素等待超时"
    else:
        reason = text or exc.__class__.__name__ if exc is not None else "未知异常"
    return f"失败：{reason}"[:180]


def _split_sites(value):
    return [site.strip() for site in re.split(r"[，,、;；\n]+", str(value or "")) if site.strip()]


def _build_reputation_failure_row(name, site, failure_status="失败：未知异常"):
    return [
        name,
        site,
        "执行失败",
        "",
        "",
        "",
        "",
        "",
        "",
        failure_status,
        get_now_time(),
        "",
    ]


def _build_reputation_auxiliary_failure(name, site, failure_status="失败：未知异常"):
    return {
        "store_name": name,
        "site": site,
        "direction": "",
        "gradient_rate": "",
        "system_warning": f"辅助采集{failure_status}",
        "updated_at": get_now_time(),
        "visits": "[]",
        "error": failure_status,
    }


def _is_ignored_config_value(value):
    return "忽略" in str(value or "").strip()


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


def _run_reputation_for_browser(row, lease_wait_seconds=0):
    id = row[0]
    name = row[1]
    remark = row[2]
    if _is_ignored_config_value(remark):
        return [], []

    if not row[3]:
        return [], [("获取声誉信息", name, "", "失败：未配置站点", get_now_time())]

    wait_for_batch_resume(f"声誉采集:{name}")
    lease = create_window_lease(
        id,
        owner=f"reputation_collection:{name}",
        shop_name=name,
        task_type="reputation_collection",
    )
    if not lease.acquire(timeout=max(0, float(lease_wait_seconds or 0))):
        print(get_now_time() + name + "窗口已被其他任务占用，跳过本次声誉采集")
        status = "跳过：窗口被其他任务占用"
        sites = _split_sites(row[3])
        return (
            [_build_reputation_failure_row(name, site, status) for site in sites],
            [("获取声誉信息", name, site, status, get_now_time()) for site in sites],
        )

    print(get_now_time() + "开始打开窗口:" + name)
    reputation_info_sum = []
    result = []
    sites = _split_sites(row[3])

    try:
        try:
            driver = _connect_browser(id, batch_control=True, batch_source="声誉采集")
        except Exception as exc:
            status = _failure_status(exc)
            print(get_now_time() + name + "打开窗口失败：" + status, exc)
            for site in sites:
                result.append(("获取声誉信息", name, site, status, get_now_time()))
                reputation_info_sum.append(
                    _build_reputation_failure_row(name, site, status)
                )
            return reputation_info_sum, result

        fatal_profile_error = None
        for site in sites:
            wait_for_batch_resume(f"声誉采集:{name}")
            if fatal_profile_error is not None:
                status = _failure_status(fatal_profile_error)
                result.append(("获取声誉信息", name, site, status, get_now_time()))
                reputation_info_sum.append(
                    _build_reputation_failure_row(name, site, status)
                )
                continue

            succeeded = False
            last_error = None
            for attempt in range(1, 4):
                wait_for_batch_resume(f"声誉采集:{name}")
                try:
                    reputation_info = get_reputation_info(
                        id,
                        name,
                        site,
                        driver=driver,
                        allow_global_ip_switch=False,
                    )
                    reputation_info_sum.append(reputation_info)
                    print(get_now_time() + name + site + "获取声誉信息成功")
                    result.append(("获取声誉信息", name, site, "成功", get_now_time()))
                    succeeded = True
                    break
                except Exception as e:
                    last_error = e
                    print(get_now_time() + name + site + "执行失败", e)
                    if isinstance(e, MercadoAuthenticationError):
                        fatal_profile_error = e
                        break
                    is_rate_limited = isinstance(e, MercadoRateLimitError) or _is_rate_limited_text(e)
                    if is_rate_limited:
                        trip_batch_rate_limit(f"声誉采集:{name}:{site}", str(e))
                    if attempt < 3:
                        if is_rate_limited:
                            wait_for_batch_resume(f"声誉采集:{name}")
                        else:
                            time.sleep(5)

            if not succeeded:
                status = _failure_status(last_error)
                result.append(("获取声誉信息", name, site, status, get_now_time()))
                reputation_info_sum.append(
                    _build_reputation_failure_row(name, site, status)
                )
            time.sleep(10)
    finally:
        print(get_now_time() + "结束，正在关闭窗口")
        try:
            closeBrowser(id, lease=lease)
        except Exception as e:
            print(get_now_time() + name + "关闭窗口失败", e)
        lease.release()
        print(get_now_time() + "已经关闭窗口")

    return reputation_info_sum, result


def _run_reputation_auxiliary_for_browser(row, lease_wait_seconds=0):
    """每家店共用一个浏览器，只采集销售汇总和流量页面。"""
    window_id = row[0]
    name = row[1]
    remark = row[2]
    if _is_ignored_config_value(remark):
        return [], []
    sites = _split_sites(row[3])
    if not sites:
        return [], [
            ("获取声誉辅助信息", name, "", "失败：未配置站点", get_now_time())
        ]

    wait_for_batch_resume(f"声誉辅助采集:{name}")
    lease = create_window_lease(
        window_id,
        owner=f"reputation_auxiliary_collection:{name}",
        shop_name=name,
        task_type="reputation_collection",
    )
    if not lease.acquire(timeout=max(0, float(lease_wait_seconds or 0))):
        status = "跳过：窗口被其他任务占用"
        return (
            [
                _build_reputation_auxiliary_failure(name, site, status)
                for site in sites
            ],
            [
                ("获取声誉辅助信息", name, site, status, get_now_time())
                for site in sites
            ],
        )

    auxiliary_rows = []
    result = []
    try:
        try:
            driver = _connect_browser(
                window_id,
                batch_control=True,
                batch_source="声誉辅助采集",
            )
        except Exception as exc:
            status = _failure_status(exc)
            for site in sites:
                auxiliary_rows.append(
                    _build_reputation_auxiliary_failure(name, site, status)
                )
                result.append(
                    ("获取声誉辅助信息", name, site, status, get_now_time())
                )
            return auxiliary_rows, result

        fatal_profile_error = None
        for site in sites:
            wait_for_batch_resume(f"声誉辅助采集:{name}")
            if fatal_profile_error is not None:
                status = _failure_status(fatal_profile_error)
                auxiliary_rows.append(
                    _build_reputation_auxiliary_failure(name, site, status)
                )
                result.append(
                    ("获取声誉辅助信息", name, site, status, get_now_time())
                )
                continue

            last_error = None
            for attempt in range(1, 4):
                try:
                    auxiliary = get_reputation_auxiliary_info(
                        window_id,
                        name,
                        site,
                        driver=driver,
                        select_site=True,
                    )
                    auxiliary_rows.append(auxiliary)
                    auxiliary_status = (
                        f"部分失败：{auxiliary.get('error')}"[:180]
                        if auxiliary.get("error")
                        else "成功"
                    )
                    result.append((
                        "获取声誉辅助信息",
                        name,
                        site,
                        auxiliary_status,
                        get_now_time(),
                    ))
                    print(
                        f"{get_now_time()}{name}{site}流量趋势及辅助数据"
                        + ("部分失败" if auxiliary.get("error") else "采集成功")
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    print(f"{get_now_time()}{name}{site}辅助数据执行失败", exc)
                    if isinstance(exc, MercadoAuthenticationError):
                        fatal_profile_error = exc
                        break
                    is_rate_limited = isinstance(exc, MercadoRateLimitError) or _is_rate_limited_text(exc)
                    if is_rate_limited:
                        trip_batch_rate_limit(f"声誉辅助采集:{name}:{site}", str(exc))
                    if attempt < 3:
                        if is_rate_limited:
                            wait_for_batch_resume(f"声誉辅助采集:{name}")
                        else:
                            time.sleep(5)
            else:
                last_error = last_error or RuntimeError("未知异常")

            if not result or result[-1][2] != site or not (
                result[-1][3] == "成功" or str(result[-1][3]).startswith("部分失败")
            ):
                status = _failure_status(last_error)
                auxiliary_rows.append(
                    _build_reputation_auxiliary_failure(name, site, status)
                )
                result.append(
                    ("获取声誉辅助信息", name, site, status, get_now_time())
                )
            time.sleep(2)
    finally:
        try:
            closeBrowser(window_id, lease=lease)
        except Exception as exc:
            print(f"{get_now_time()}{name}关闭窗口失败", exc)
        lease.release()
    return auxiliary_rows, result


def _execute_reputation_rows(
    rows,
    max_workers,
    stagger_min_seconds,
    stagger_max_seconds,
    lease_wait_seconds=0,
):
    outcomes = {}
    worker_count = max(1, min(int(max_workers), len(rows))) if rows else 1
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        future_map = {}
        for index, row in enumerate(rows):
            future = executor.submit(
                _run_reputation_for_browser,
                row,
                lease_wait_seconds,
            )
            future_map[future] = row
            if index < len(rows) - 1:
                delay = stagger_sleep(stagger_min_seconds, stagger_max_seconds)
                print(f"{get_now_time()}声誉店铺错峰启动，下一家等待 {delay:.1f} 秒")

        for future in as_completed(future_map):
            row = future_map[future]
            name = row[1]
            try:
                browser_reputations, browser_result = future.result()
            except Exception as exc:
                print(get_now_time() + name + "窗口任务异常", exc)
                status = _failure_status(exc)
                sites = _split_sites(row[3]) or [""]
                browser_reputations = [
                    _build_reputation_failure_row(name, site, status)
                    for site in sites
                    if site
                ]
                browser_result = [
                    ("获取声誉信息", name, site, status, get_now_time())
                    for site in sites
                ]
            outcomes[row_key(row)] = (row, browser_reputations, browser_result)
            print(get_now_time() + name + "窗口任务完成")
    return outcomes


def _execute_reputation_auxiliary_rows(
    rows,
    max_workers,
    stagger_min_seconds,
    stagger_max_seconds,
    lease_wait_seconds=0,
):
    outcomes = {}
    worker_count = max(1, min(int(max_workers), len(rows))) if rows else 1
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        future_map = {}
        for index, row in enumerate(rows):
            future = executor.submit(
                _run_reputation_auxiliary_for_browser,
                row,
                lease_wait_seconds,
            )
            future_map[future] = row
            if index < len(rows) - 1:
                delay = stagger_sleep(stagger_min_seconds, stagger_max_seconds)
                print(
                    f"{get_now_time()}声誉辅助店铺错峰启动，下一家等待 {delay:.1f} 秒"
                )

        for future in as_completed(future_map):
            row = future_map[future]
            name = row[1]
            try:
                auxiliary_rows, browser_result = future.result()
            except Exception as exc:
                status = _failure_status(exc)
                sites = _split_sites(row[3]) or [""]
                auxiliary_rows = [
                    _build_reputation_auxiliary_failure(name, site, status)
                    for site in sites
                    if site
                ]
                browser_result = [
                    ("获取声誉辅助信息", name, site, status, get_now_time())
                    for site in sites
                ]
            outcomes[row_key(row)] = (row, auxiliary_rows, browser_result)
            print(f"{get_now_time()}{name}声誉辅助窗口任务完成")
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


def _prepare_reputation_retry_rows(outcomes, permanent_login_failures=None):
    """修复可处理的登录/配置问题，并返回首轮失败店铺的最新配置。"""
    latest_rows = _deduplicate_config_rows(list_config_rows(include_ignored=False))
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
                )

                login_result = login_one_database_shop(
                    _row_as_login_config(current_row),
                    wait_seconds=int(env_float("BIT_LOGIN_REPAIR_WAIT_SECONDS", 60, 1)),
                    page_load_timeout=int(env_float("BIT_LOGIN_REPAIR_PAGE_TIMEOUT", 20, 1)),
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


def get_reputation_info_all_browser(
    max_workers=DEFAULT_COLLECTION_MAX_WORKERS,
    stagger_min_seconds=None,
    stagger_max_seconds=None,
    retry_failed=True,
    selected_shops=None,
    selected_sites=None,
):
    """历史浏览器采集实现；仅保留兼容，不再由默认业务流程调用。"""
    start = int(time.time())
    print(start)
    root_path = Path(__file__).resolve().parent
    rows = list_config_rows(include_ignored=False)
    rows = [row for row in rows if row and row[0]]
    rows = _deduplicate_config_rows(rows)
    rows = filter_config_rows(
        rows,
        selected_shops=selected_shops,
        selected_sites=selected_sites,
    )
    if (selected_shops or selected_sites) and not rows:
        raise ValueError("所选店铺和站点没有可执行的声誉采集配置")

    outcomes = _execute_reputation_rows(
        rows,
        max_workers=max_workers,
        stagger_min_seconds=stagger_min_seconds,
        stagger_max_seconds=stagger_max_seconds,
    )

    retry_rounds = (
        env_int("BIT_COLLECTION_RETRY_ROUNDS", 2, 0)
        if retry_failed
        else 0
    )
    permanent_login_failures = set()
    for retry_round in range(1, retry_rounds + 1):
        retry_plan = _prepare_reputation_retry_rows(
            outcomes,
            permanent_login_failures=permanent_login_failures,
        )
        if not retry_plan:
            break
        retry_site_count = sum(len(_split_sites(row[3])) for _key, row in retry_plan)
        print(
            f"{get_now_time()}声誉第 {retry_round}/{retry_rounds} 轮补跑 "
            f"{len(retry_plan)} 家、{retry_site_count} 个失败站点"
        )
        wait_for_batch_resume("声誉失败补跑")
        retry_rows = [row for _original_key, row in retry_plan]
        retry_outcomes = _execute_reputation_rows(
            retry_rows,
            max_workers=max_workers,
            stagger_min_seconds=stagger_min_seconds,
            stagger_max_seconds=stagger_max_seconds,
            lease_wait_seconds=env_float(
                "BIT_RETRY_WINDOW_LOCK_WAIT_SECONDS",
                DEFAULT_RETRY_LOCK_WAIT_SECONDS,
            ),
        )
        for original_key, retry_row in retry_plan:
            retry_outcome = retry_outcomes.get(row_key(retry_row))
            if retry_outcome is not None:
                outcomes[original_key] = merge_site_retry_outcome(
                    outcomes[original_key],
                    retry_outcome,
                )

    reputation_info_sum = []
    result = []
    for _row, browser_reputations, browser_result in outcomes.values():
        reputation_info_sum.extend(browser_reputations)
        result.extend(browser_result)

    reputation_info_sum_str = "\n".join(map(str, reputation_info_sum))
    print(reputation_info_sum_str)

    end = int(time.time())
    print(get_now_time() + "总花费", end - start)
    failure_report_path = write_unreadable_site_report("声誉采集", result)
    if failure_report_path:
        print(f"{get_now_time()}无法读取站点已记录：{failure_report_path}")

    df = pd.DataFrame(
        reputation_info_sum,
        columns=[
            "店铺名",
            "站点",
            "声誉颜色",
            "总单量",
            "投诉率",
            "延误率",
            "取消率",
            "增加或减少",
            "近七天变化率",
            "系统告警",
            "更新时间",
            "一周流量趋势"
        ],
    )

    now = datetime.now()
    scoped_collection = bool(selected_shops or selected_sites)
    replace_targets = [
        (str(row[1] or "").strip(), site)
        for row in rows
        for site in _split_sites(row[3])
        if str(row[1] or "").strip() and site
    ]
    date_str = datetime.now().strftime(
        "%Y-%m-%d-%H%M%S" if scoped_collection else "%Y-%m-%d-%H"
    )
    output_dir = root_path / "美客多声誉"
    output_dir.mkdir(parents=True, exist_ok=True)
    scope_suffix = "-选定范围" if scoped_collection else ""
    output_path = output_dir / f"武汉泽顺店铺声誉信息汇总{scope_suffix}{date_str}.xlsx"

    post_errors = []
    for step_name, action in (
        (
            "写入声誉数据",
            lambda: (
                inset_reputation_info(
                    reputation_info_sum,
                    merge_latest=True,
                    replace_targets=replace_targets,
                )
                if scoped_collection
                else inset_reputation_info(reputation_info_sum)
            ),
        ),
        ("写入声誉任务记录", lambda: insert_task_record(result)),
        ("导出声誉汇总", lambda: df.to_excel(output_path, index=False)),
    ):
        try:
            action()
        except Exception as exc:
            post_errors.append(f"{step_name}失败：{exc}")
            print(f"{get_now_time()}{step_name}失败：{exc}")

    email_sent = False
    if output_path.exists():
        email_sent = bool(
            send_info(
                "美客多所有店铺声誉汇总",
                "",
                output_path,
                output_path.name,
            )
        )
        print(get_now_time() + ("发送邮件成功" if email_sent else "发送邮件失败，汇总文件已保留"))

    if post_errors:
        raise RuntimeError("；".join(post_errors))
    return {
        "data": reputation_info_sum,
        "results": result,
        "output_path": str(output_path),
        "failure_report_path": str(failure_report_path) if failure_report_path else "",
        "email_sent": email_sent,
        "selected_shops": list(selected_shops or ()),
        "selected_sites": list(selected_sites or ()),
        "max_workers": max_workers,
        "failed_shops": sorted(
            {
                str(row[1] or "")
                for row in result
                if len(row) >= 4 and str(row[3] or "") != "成功"
            }
        ),
    }


def _normalize_api_site_code(value):
    site = str(value or "").strip()
    if not site:
        return ""
    return SITE_CODE_MAP.get(site) or SITE_CODE_MAP.get(site.upper()) or site.upper()


def _format_api_percentage(value):
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _emit_api_collection_log(message, callback=None):
    text = str(message or "").strip()
    if not text:
        return
    print(f"{get_now_time()} {text}")
    if callback is not None:
        try:
            callback(text)
        except Exception as exc:
            print(f"{get_now_time()} 声誉日志回调失败：{exc}")


def _emit_api_collection_progress(callback, event, **payload):
    if callback is None:
        return
    try:
        callback({"event": event, **payload})
    except Exception as exc:
        print(f"{get_now_time()} 声誉进度回调失败：{exc}")


def _api_reputation_database_row(store_name, api_row, updated_at):
    site_name = str(api_row.get("site_name") or api_row.get("site_id") or "").strip()
    total_orders = api_row.get("sales_completed")
    if total_orders in (None, ""):
        total_orders = api_row.get("transaction_total")
    return [
        store_name,
        site_name,
        str(api_row.get("level_name") or api_row.get("level_id") or "暂无等级"),
        "" if total_orders is None else total_orders,
        _format_api_percentage(api_row.get("claims_rate_percent")),
        _format_api_percentage(api_row.get("delayed_handling_rate_percent")),
        _format_api_percentage(api_row.get("cancellations_rate_percent")),
        "",
        "",
        "正常",
        updated_at,
        "[]",
    ]


def _deduplicate_api_site_rows(rows):
    """旧声誉表按店铺和站点存一行，优先保留 remote 口径。"""
    selected = {}
    for raw_row in rows or ():
        row = dict(raw_row or {})
        site_code = _normalize_api_site_code(row.get("site_id"))
        if not site_code:
            continue
        current = selected.get(site_code)
        if current is None or (
            str(row.get("logistic_type") or "").lower() == "remote"
            and str(current.get("logistic_type") or "").lower() != "remote"
        ):
            selected[site_code] = row
    return [selected[site_code] for site_code in sorted(selected)]


def _api_auxiliary_config_rows(tokens, api_rows):
    """用浏览器配置定位窗口，但站点范围完全取自店铺授权访问统计开关。"""
    canonical_by_alias = {}
    for token in tokens:
        canonical_name = str(
            token.get("display_name") or token.get("nickname") or token.get("id") or ""
        ).strip()
        for alias in (token.get("display_name"), token.get("nickname")):
            alias_key = str(alias or "").strip().casefold()
            if alias_key:
                canonical_by_alias[alias_key] = canonical_name

    api_sites_by_store = {}
    for row in api_rows:
        store_key = str(row.get("store_name") or "").strip().casefold()
        site_code = _normalize_api_site_code(row.get("site_id") or row.get("site_name"))
        if not store_key or not site_code:
            continue
        site_label = str(row.get("site_name") or site_code).strip()
        api_sites_by_store.setdefault(store_key, {})[site_code] = site_label
    selected_rows = []
    claimed_sites = set()
    for raw_row in list_config_rows(include_ignored=False) or ():
        if not raw_row or len(raw_row) < 4 or not str(raw_row[0] or "").strip():
            continue
        original_name = str(raw_row[1] or "").strip()
        canonical_name = canonical_by_alias.get(original_name.casefold())
        if not canonical_name:
            continue
        canonical_key = canonical_name.casefold()
        sites = []
        for site_code, site_label in api_sites_by_store.get(canonical_key, {}).items():
            claimed_key = (canonical_key, site_code)
            if claimed_key not in claimed_sites:
                sites.append(site_label)
        if not sites:
            continue
        row = list(raw_row)
        row[1] = canonical_name
        row[3] = "，".join(sites)
        row = tuple(row)
        claimed_sites.update(
            (canonical_name.casefold(), _normalize_api_site_code(site))
            for site in sites
        )
        selected_rows.append(row)
    return selected_rows


def _token_enabled_site_codes(token, setting_field):
    """只返回授权店铺中显式开启指定任务开关的站点。"""
    settings = [dict(setting or {}) for setting in (token.get("site_settings") or ())]

    def enabled(value):
        if isinstance(value, str):
            return value.strip().casefold() not in ("", "0", "false", "no", "off")
        return bool(value)
    return {
        _normalize_api_site_code(setting.get("site_id"))
        for setting in settings
        if setting_field in setting
        and enabled(setting.get(setting_field))
        and _normalize_api_site_code(setting.get("site_id"))
    }


def _merge_api_auxiliary_rows(api_rows, database_rows, auxiliary_rows):
    auxiliary_by_key = {
        (
            str(row.get("store_name") or "").strip().casefold(),
            _normalize_api_site_code(row.get("site")),
        ): row
        for row in auxiliary_rows
    }
    database_by_key = {
        (
            str(row[0] or "").strip().casefold(),
            _normalize_api_site_code(row[1]),
        ): row
        for row in database_rows
        if len(row) >= 12
    }
    for api_row in api_rows:
        key = (
            str(api_row.get("store_name") or "").strip().casefold(),
            _normalize_api_site_code(api_row.get("site_id") or api_row.get("site_name")),
        )
        auxiliary = auxiliary_by_key.get(key)
        if auxiliary is None:
            continue
        api_row.update({
            "direction": auxiliary.get("direction") or "",
            "gradient_rate": auxiliary.get("gradient_rate") or "",
            "system_warning": auxiliary.get("system_warning") or "正常",
            "updated_at": auxiliary.get("updated_at") or get_now_time(),
            "visits": auxiliary.get("visits") or "[]",
            "auxiliary_error": auxiliary.get("error") or "",
        })
        database_row = database_by_key.get(key)
        if database_row is not None:
            database_row[7] = api_row["direction"]
            database_row[8] = api_row["gradient_rate"]
            database_row[9] = api_row["system_warning"]
            database_row[10] = api_row["updated_at"]
            database_row[11] = api_row["visits"]
    return auxiliary_by_key


def _attach_api_infraction_counts(api_rows, recent_days=30):
    infraction_data = get_latest_infraction_info(recent_days) or {}
    counts_by_key = {
        (
            str(row.get("店铺名") or "").strip().casefold(),
            _normalize_api_site_code(row.get("站点")),
        ): row
        for row in (infraction_data.get("summary") or [])
        if str(row.get("店铺名") or "").strip()
        and str(row.get("站点") or "").strip()
    }
    for api_row in api_rows:
        site_code = _normalize_api_site_code(
            api_row.get("site_id") or api_row.get("site_name")
        )
        store_aliases = (
            api_row.get("store_name"),
            api_row.get("store_nickname"),
        )
        counts = next(
            (
                counts_by_key[(str(alias or "").strip().casefold(), site_code)]
                for alias in store_aliases
                if (str(alias or "").strip().casefold(), site_code)
                in counts_by_key
            ),
            {},
        )
        api_row["infraction_count"] = int(counts.get("侵权") or 0)
        api_row["rights_holder_count"] = int(counts.get("权利人") or 0)
        api_row["infraction_recent_days"] = int(
            infraction_data.get("recent_days") or recent_days
        )
    return api_rows


def get_reputation_info_all(
    max_workers=DEFAULT_COLLECTION_MAX_WORKERS,
    stagger_min_seconds=None,
    stagger_max_seconds=None,
    retry_failed=True,
    selected_shops=None,
    selected_sites=None,
    log_callback=None,
    progress_callback=None,
    send_email=True,
    export_excel=True,
    collect_browser_auxiliary=True,
):
    """通过 Mercado Libre 官方 API 更新声誉并兼容原有声誉数据链路。"""
    started_monotonic = time.monotonic()
    root_path = Path(__file__).resolve().parent
    selected_shop_keys = {
        str(value or "").strip().casefold()
        for value in (selected_shops or ())
        if str(value or "").strip()
    }
    selected_site_codes = {
        _normalize_api_site_code(value)
        for value in (selected_sites or ())
        if _normalize_api_site_code(value)
    }

    token_data = list_mercado_store_tokens() or {}
    tokens = [
        dict(row)
        for row in (token_data.get("rows") or [])
        if int(row.get("id") or 0) > 0
    ]
    if selected_shop_keys:
        tokens = [
            token
            for token in tokens
            if {
                str(token.get("display_name") or "").strip().casefold(),
                str(token.get("nickname") or "").strip().casefold(),
            }
            & selected_shop_keys
        ]
    visit_enabled_tokens = []
    for token in tokens:
        enabled_sites = _token_enabled_site_codes(token, "visit_stats_enabled")
        if selected_site_codes:
            enabled_sites &= selected_site_codes
        if not enabled_sites:
            continue
        token["_visit_stats_site_codes"] = sorted(enabled_sites)
        visit_enabled_tokens.append(token)
    tokens = visit_enabled_tokens
    if (selected_shops or selected_sites) and not tokens:
        raise ValueError("所选店铺或站点未在店铺授权中开启访问数据统计")

    try:
        requested_workers = max(1, int(max_workers or 1))
    except (TypeError, ValueError):
        requested_workers = DEFAULT_COLLECTION_MAX_WORKERS
    worker_count = min(requested_workers, len(tokens)) if tokens else 0
    _emit_api_collection_log(
        f"开始官方 API 声誉更新，共 {len(tokens)} 家授权店铺，并发 {worker_count}",
        log_callback,
    )
    _emit_api_collection_progress(
        progress_callback,
        "initialized",
        total_stores=len(tokens),
    )

    def fetch_one(token):
        token_id = int(token["id"])
        store_name = str(
            token.get("display_name") or token.get("nickname") or token_id
        ).strip()
        _emit_api_collection_log(f"{store_name}：开始读取官方声誉接口", log_callback)
        _emit_api_collection_progress(
            progress_callback,
            "store_started",
            token_id=token_id,
            store_name=store_name,
        )
        attempts = 2 if retry_failed else 1
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                response = get_mercado_store_reputation(token_id) or {}
                api_rows = _deduplicate_api_site_rows(response.get("rows") or [])
                if selected_site_codes:
                    api_rows = [
                        row
                        for row in api_rows
                        if _normalize_api_site_code(row.get("site_id"))
                        in selected_site_codes
                    ]
                enabled_visit_sites = token.get("_visit_stats_site_codes")
                if enabled_visit_sites is not None:
                    enabled_visit_sites = set(enabled_visit_sites)
                    api_rows = [
                        row
                        for row in api_rows
                        if _normalize_api_site_code(row.get("site_id"))
                        in enabled_visit_sites
                    ]
                if not api_rows:
                    scope_text = "所选站点" if selected_site_codes else "任何站点"
                    raise RuntimeError(f"官方接口未返回{scope_text}的声誉数据")
                enriched_rows = []
                database_rows = []
                task_rows = []
                updated_at = get_now_time()
                for api_row in api_rows:
                    enriched = dict(api_row)
                    enriched.update({
                        "token_id": token_id,
                        "store_name": store_name,
                        "store_nickname": str(token.get("nickname") or ""),
                    })
                    enriched_rows.append(enriched)
                    database_rows.append(
                        _api_reputation_database_row(store_name, enriched, updated_at)
                    )
                    task_rows.append((
                        "获取声誉信息",
                        store_name,
                        str(enriched.get("site_name") or enriched.get("site_id") or ""),
                        "成功",
                        updated_at,
                    ))
                return {
                    "token_id": token_id,
                    "store_name": store_name,
                    "api_rows": enriched_rows,
                    "database_rows": database_rows,
                    "task_rows": task_rows,
                }
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    _emit_api_collection_log(
                        f"{store_name}：首次读取失败，立即重试（{exc}）",
                        log_callback,
                    )
        raise last_error

    api_rows = []
    reputation_rows = []
    task_rows = []
    failures = []
    if tokens:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="reputation-api",
        ) as executor:
            futures = {executor.submit(fetch_one, token): token for token in tokens}
            for future in as_completed(futures):
                token = futures[future]
                token_id = int(token["id"])
                store_name = str(
                    token.get("display_name") or token.get("nickname") or token_id
                ).strip()
                try:
                    store_result = future.result()
                    store_api_rows = store_result["api_rows"]
                    api_rows.extend(store_api_rows)
                    reputation_rows.extend(store_result["database_rows"])
                    task_rows.extend(store_result["task_rows"])
                    _emit_api_collection_log(
                        f"{store_name}：成功，返回 {len(store_api_rows)} 个站点",
                        log_callback,
                    )
                    _emit_api_collection_progress(
                        progress_callback,
                        "store_success",
                        token_id=token_id,
                        store_name=store_name,
                        rows=store_api_rows,
                    )
                except Exception as exc:
                    error_text = str(exc)
                    failure = {
                        "token_id": token_id,
                        "store_name": store_name,
                        "error": error_text,
                    }
                    failures.append(failure)
                    task_rows.append((
                        "获取声誉信息",
                        store_name,
                        "",
                        f"失败：{error_text}"[:180],
                        get_now_time(),
                    ))
                    _emit_api_collection_log(
                        f"{store_name}：失败，{error_text}",
                        log_callback,
                    )
                    _emit_api_collection_progress(
                        progress_callback,
                        "store_failure",
                        **failure,
                    )

    auxiliary_rows = []
    auxiliary_task_rows = []
    auxiliary_config_error = ""
    if collect_browser_auxiliary and api_rows:
        try:
            auxiliary_configs = _api_auxiliary_config_rows(tokens, api_rows)
        except Exception as exc:
            auxiliary_configs = []
            auxiliary_config_error = f"读取浏览器配置失败：{exc}"
            _emit_api_collection_log(auxiliary_config_error, log_callback)

        _emit_api_collection_log(
            f"开始浏览器辅助采集：{len(auxiliary_configs)} 家店铺，"
            "仅访问销售汇总和流量页面，不访问 reputation 页面",
            log_callback,
        )
        if auxiliary_configs:
            auxiliary_outcomes = _execute_reputation_auxiliary_rows(
                auxiliary_configs,
                max_workers=requested_workers,
                stagger_min_seconds=stagger_min_seconds,
                stagger_max_seconds=stagger_max_seconds,
            )
            for _row, browser_auxiliary, browser_result in auxiliary_outcomes.values():
                auxiliary_rows.extend(browser_auxiliary)
                auxiliary_task_rows.extend(browser_result)
            task_rows.extend(auxiliary_task_rows)

        auxiliary_by_key = _merge_api_auxiliary_rows(
            api_rows,
            reputation_rows,
            auxiliary_rows,
        )
        for api_row in api_rows:
            store_name = str(api_row.get("store_name") or "").strip()
            site_id = _normalize_api_site_code(
                api_row.get("site_id") or api_row.get("site_name")
            )
            site_name = str(api_row.get("site_name") or site_id).strip()
            key = (store_name.casefold(), site_id)
            auxiliary = auxiliary_by_key.get(key)
            if auxiliary is None:
                error_text = auxiliary_config_error or "未找到匹配的浏览器店铺/站点配置"
                task_rows.append((
                    "获取声誉辅助信息",
                    store_name,
                    site_name,
                    f"失败：{error_text}"[:180],
                    get_now_time(),
                ))
            else:
                error_text = str(auxiliary.get("error") or "").strip()
            if error_text:
                failure = {
                    "token_id": int(api_row.get("token_id") or 0),
                    "store_name": store_name,
                    "site_id": site_id,
                    "stage": "browser_auxiliary",
                    "error": error_text,
                }
                failures.append(failure)
                _emit_api_collection_log(
                    f"{store_name}{site_name}：声誉 API 成功，浏览器辅助数据失败，"
                    f"{error_text}",
                    log_callback,
                )
            else:
                _emit_api_collection_log(
                    f"{store_name}{site_name}：流量趋势及辅助数据合并完成",
                    log_callback,
                )

    api_rows.sort(
        key=lambda row: (
            str(row.get("store_name") or ""),
            str(row.get("site_id") or ""),
        )
    )
    try:
        _attach_api_infraction_counts(api_rows, recent_days=30)
        _emit_api_collection_log("已合并各站点近 30 天侵权及权利人数量", log_callback)
    except Exception as exc:
        _emit_api_collection_log(f"读取侵权数量失败，本次显示为 0：{exc}", log_callback)
        for api_row in api_rows:
            api_row.setdefault("infraction_count", 0)
            api_row.setdefault("rights_holder_count", 0)
            api_row.setdefault("infraction_recent_days", 30)
    failure_report_path = write_unreadable_site_report("声誉API采集", task_rows)
    if failure_report_path:
        _emit_api_collection_log(
            f"失败店铺已记录：{failure_report_path}",
            log_callback,
        )

    scoped_collection = bool(selected_shops or selected_sites)
    replace_targets = [
        (str(row[0] or "").strip(), str(row[1] or "").strip())
        for row in reputation_rows
        if str(row[0] or "").strip() and str(row[1] or "").strip()
    ]
    date_str = datetime.now().strftime(
        "%Y-%m-%d-%H%M%S" if scoped_collection else "%Y-%m-%d-%H"
    )
    output_dir = root_path / "美客多声誉"
    scope_suffix = "-选定范围" if scoped_collection else ""
    output_path = output_dir / f"武汉泽顺店铺声誉信息汇总{scope_suffix}{date_str}.xlsx"

    post_errors = []
    post_steps = [
        (
            "写入声誉数据",
            lambda: (
                inset_reputation_info(
                    reputation_rows,
                    merge_latest=True,
                    replace_targets=replace_targets,
                )
                if scoped_collection
                else inset_reputation_info(reputation_rows)
            ),
        ),
        ("写入声誉任务记录", lambda: insert_task_record(task_rows)),
    ]
    if export_excel:
        output_dir.mkdir(parents=True, exist_ok=True)
        dataframe = pd.DataFrame(
            reputation_rows,
            columns=[
                "店铺名",
                "站点",
                "声誉颜色",
                "总单量",
                "投诉率",
                "延误率",
                "取消率",
                "增加或减少",
                "近七天变化率",
                "系统告警",
                "更新时间",
                "一周流量趋势",
            ],
        )
        post_steps.append(("导出声誉汇总", lambda: dataframe.to_excel(output_path, index=False)))

    for step_name, action in post_steps:
        try:
            action()
            _emit_api_collection_log(f"{step_name}完成", log_callback)
        except Exception as exc:
            post_errors.append(f"{step_name}失败：{exc}")
            _emit_api_collection_log(f"{step_name}失败：{exc}", log_callback)

    email_sent = False
    if send_email and export_excel and output_path.exists():
        try:
            email_sent = bool(
                send_info(
                    "美客多所有店铺声誉汇总",
                    "",
                    output_path,
                    output_path.name,
                )
            )
            _emit_api_collection_log(
                "声誉汇总邮件发送成功" if email_sent else "声誉汇总邮件发送失败，文件已保留",
                log_callback,
            )
        except Exception as exc:
            post_errors.append(f"发送声誉汇总邮件失败：{exc}")
            _emit_api_collection_log(f"发送声誉汇总邮件失败：{exc}", log_callback)

    elapsed_seconds = max(0, round(time.monotonic() - started_monotonic, 3))
    failed_store_names = {
        str(row.get("store_name") or "").strip()
        for row in failures
        if str(row.get("store_name") or "").strip()
    }
    success_stores = max(0, len(tokens) - len(failed_store_names))
    _emit_api_collection_log(
        f"声誉混合更新完成：完整成功 {success_stores} 家，"
        f"异常 {len(failed_store_names)} 家，"
        f"共 {len(api_rows)} 个站点，耗时 {elapsed_seconds} 秒",
        log_callback,
    )
    if post_errors:
        raise RuntimeError("；".join(post_errors))
    return {
        "data": reputation_rows,
        "api_rows": api_rows,
        "results": task_rows,
        "output_path": str(output_path) if export_excel else "",
        "failure_report_path": str(failure_report_path) if failure_report_path else "",
        "email_sent": email_sent,
        "selected_shops": list(selected_shops or ()),
        "selected_sites": list(selected_sites or ()),
        "max_workers": worker_count,
        "total_stores": len(tokens),
        "completed_stores": len(tokens),
        "success_stores": success_stores,
        "failed_stores": len(failed_store_names),
        "total_sites": len(api_rows),
        "failures": failures,
        "failed_shops": sorted(failed_store_names),
        "auxiliary_rows": auxiliary_rows,
        "elapsed_seconds": elapsed_seconds,
    }


def main(**kwargs):
    return get_reputation_info_all(**kwargs)


if __name__ == "__main__":
    # get_reputation_info('22139511815a4bf588fe96d5fdafded6','四季如春','墨西哥')
    main()
