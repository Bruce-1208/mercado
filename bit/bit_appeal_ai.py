"""
# 适用环境python3
"""

import time
import traceback
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import math


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests
from pydantic.v1.datetime_parse import parse_date
from selenium.webdriver.chrome.service import Service

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import random

from bit.bit_utils import get_latest_modified_file, get_bit_path, parser_delay_date, get_now_time, getWindowidByName
from bit.bit_api import *
from AI_Agent.qianwen import *
import pandas as pd
from datetime import datetime, timedelta
from datetime import datetime
from AI_Agent.deepseek import *
import re
from openpyxl import load_workbook
from bit.bit_clash import *
import traceback
from bit_infractions_info import *

try:
    from bit.chat_log import append_chat_log
except Exception:
    from chat_log import append_chat_log

try:
    import mercado_appeal_runner as mercado_cdp_runner
except Exception:
    mercado_cdp_runner = None

CHAT_INFO_API_URL = "https://zeshun.nat100.top/api/v1/chat"
HELP_URL = "https://global-selling.mercadolibre.com/help"
AI_RECOLLECT_INTERVAL_SECONDS = 600
AI_FRAME_URL_MARKERS = ("meli-ai-chat", "maxwell/new-chat")

SITE_CODE_MAP = {
    "MX": "MX",
    "MLM": "MX",
    "墨西哥": "MX",
    "BR": "BR",
    "MLB": "BR",
    "巴西": "BR",
    "AR": "AR",
    "MLA": "AR",
    "阿根廷": "AR",
    "CL": "CL",
    "MLC": "CL",
    "智利": "CL",
    "CO": "CO",
    "MCO": "CO",
    "哥伦比亚": "CO",
    "UY": "UY",
    "MLU": "UY",
    "Uruguay": "UY",
    "乌拉圭": "UY",
}

SITE_SWITCH_SELECTOR_MAP = {
    "墨西哥": 'div[data-value="MLM-remote"]',
    "巴西": 'div[data-value="MLB-remote"]',
    "哥伦比亚": 'div[data-value="MCO-remote"]',
    "智利": 'div[data-value="MLC-remote"]',
    "阿根廷": 'div[data-value="MLA-remote"]',
    "乌拉圭": 'div[data-value="MLU-remote"]',
}


def insert_chat_info_by_api(name, site, message, chat, response, time):
    payload = {
        "name": name,
        "site": site,
        "message": message,
        "chat": chat,
        "response": response,
        "time": time
    }
    res = requests.post(CHAT_INFO_API_URL, json=payload, timeout=10)
    res.raise_for_status()
    return res.json()


def connect_bit_browser(window_id):
    res = openBrowser(window_id)
    print(res)

    driver_path = res["data"]["driver"]
    debugger_address = res["data"]["http"]

    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_experimental_option("debuggerAddress", debugger_address)

    chrome_service = Service(driver_path)
    driver = webdriver.Chrome(service=chrome_service, options=chrome_options)
    driver.implicitly_wait(10)
    return driver, res


def get_window_id_by_shop_name(name):
    config_path = get_bit_path() / "比特配置文件.xlsx"
    wb = load_workbook(config_path)
    sheet = wb.active

    for row in sheet.iter_rows(min_row=2, values_only=True):
        window_id = row[0]
        window_name = row[1]
        if window_name == name:
            return window_id
    raise RuntimeError(f"未在比特配置文件中找到店铺窗口: {name}")


def select_site(driver, name, site):
    for i in range(3):
        try:
            WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CLASS_NAME, "nav-header-cbt__site-switcher")
                )
            ).click()

            print(f"{get_now_time()} {name} {site} '打开站点选择器'<br>")
            time.sleep(5)
            path = SITE_SWITCH_SELECTOR_MAP.get(site, 'div[data-value="MLM-remote"]')
            WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, path))
            ).click()

            driver.refresh()
            time.sleep(3)
            print(f"{get_now_time()} {name} {site} '选择站点成功'<br>")
            return True
        except Exception as e:
            print(f"{get_now_time()} {name} {site} '重新执行选择站点'<br>")
            time.sleep(10)
    return False


def build_appeal_message(window_id, name, site, form, message, nickname):
    if message:
        return message

    words = []
    if form == "延误":
        orders_random = get_delay_orders_random(name, site, 10)
        if orders_random == "":
            return ""
        words = [
            f"亲爱的客服，我叫{nickname}！这些订单因合作物流车辆临时出现故障，导致未能及时揽收，并非我这边发货延误，麻烦您帮忙处理一下，消除对店铺声誉的影响，非常感谢！",
            f"亲爱的客服，我叫{nickname}！这些订单因为菜鸟物流原因，并非我这边发货延误，麻烦您帮忙处理一下，消除对店铺声誉的影响，非常感谢！",
        ]
        return orders_random + random.choice(words)

    if form == "侵权":
        infraction_random = get_infraction_orders_random(window_id, name, site, 10)
        words = [
            f"亲爱的客服，我叫{nickname}！这些产品是通用品牌产品，被系统误检测为侵权产品，你能帮我重新核查并消除记录吗？",
            f"亲爱的客服，我叫{nickname}！这些产品是通用产品，并没有侵犯品牌权益，麻烦你帮我重新审核并恢复产品，谢谢！",
        ]
        return infraction_random + random.choice(words)

    if form == "投诉":
        words = [
            f"亲爱的客服，我叫{nickname}！我的产品没有质量问题，客户没有提供确凿证据证明产品存在问题，麻烦你帮我重新核查并消除对声誉的影响。"
        ]
        return random.choice(words)

    return message


def switch_to_ai_chat_frame(driver):
    driver.switch_to.default_content()
    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for frame in frames:
        try:
            if not frame.is_displayed():
                continue
            src = (frame.get_attribute("src") or "").lower()
            if not any(marker in src for marker in AI_FRAME_URL_MARKERS):
                continue
            driver.switch_to.frame(frame)
            return True
        except Exception:
            driver.switch_to.default_content()
            continue

    driver.switch_to.default_content()
    return False


def dump_iframe_debug_info(driver):
    driver.switch_to.default_content()
    try:
        frames = driver.execute_script(
            """
            return [...document.querySelectorAll('iframe')].map((frame, index) => {
                const rect = frame.getBoundingClientRect();
                return {
                    index,
                    title: frame.getAttribute('title') || '',
                    name: frame.getAttribute('name') || '',
                    src: frame.getAttribute('src') || '',
                    top: Math.round(rect.top),
                    bottom: Math.round(rect.bottom),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    visible: !!(rect.width || rect.height || frame.getClientRects().length)
                };
            });
            """
        )
        print(f"{get_now_time()} 当前页面 iframe 信息：{frames}<br>")
    except Exception as e:
        print(f"{get_now_time()} 获取 iframe 调试信息失败：{e}<br>")


def save_ai_open_debug_artifacts(driver, name, site):
    driver.switch_to.default_content()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = Path(__file__).resolve().parent / f"ai_chat_open_failed_{name}_{site}_{timestamp}"
    try:
        driver.save_screenshot(str(prefix.with_suffix(".png")))
        print(f"{get_now_time()} 已保存AI悬浮窗失败截图：{prefix.with_suffix('.png')}<br>")
    except Exception as e:
        print(f"{get_now_time()} 保存AI悬浮窗失败截图失败：{e}<br>")
    try:
        prefix.with_suffix(".html").write_text(driver.page_source, encoding="utf-8")
        print(f"{get_now_time()} 已保存AI悬浮窗失败HTML：{prefix.with_suffix('.html')}<br>")
    except Exception as e:
        print(f"{get_now_time()} 保存AI悬浮窗失败HTML失败：{e}<br>")


def dump_ai_entry_debug_info(driver):
    driver.switch_to.default_content()
    try:
        entries = driver.execute_script(
            """
            return [...document.querySelectorAll('button, a, div, span')]
                .map((node, index) => {
                    const rect = node.getBoundingClientRect();
                    const label = [
                        node.innerText || '',
                        node.getAttribute('aria-label') || '',
                        node.getAttribute('title') || '',
                        node.getAttribute('data-testid') || '',
                        String(node.className || '')
                    ].join(' ').trim();
                    return {
                        index,
                        tag: node.tagName,
                        label: label.slice(0, 180),
                        top: Math.round(rect.top),
                        left: Math.round(rect.left),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                        visible: rect.width > 0 && rect.height > 0
                    };
                })
                .filter((item) =>
                    item.visible &&
                    /assistant|chat|help|maxwell|contact/i.test(item.label)
                )
                .slice(0, 30);
            """
        )
        print(f"{get_now_time()} 当前页面AI入口候选：{entries}<br>")
    except Exception as e:
        print(f"{get_now_time()} 获取AI入口候选失败：{e}<br>")


def find_chat_input(driver, timeout=30, allow_default_content=False):
    end_time = time.time() + timeout
    while time.time() < end_time:
        element = driver.execute_script(
            """
            function deepElements(root = document) {
                const out = [];
                const walk = (node) => {
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
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return !!(rect.width || rect.height || el.getClientRects().length)
                    && style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && !el.disabled;
            }

            const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
            const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
            const candidates = deepElements().filter((el) => {
                const placeholder = el.getAttribute('placeholder') || '';
                const aria = el.getAttribute('aria-label') || '';
                const tag = el.tagName || '';
                return visible(el) && tag === 'TEXTAREA' && (
                    el.id === 'chat-input' ||
                    aria.includes('Chat message input') ||
                    placeholder === 'Ask me' ||
                    placeholder.includes('Ask the assistant')
                );
            }).map((el) => {
                const rect = el.getBoundingClientRect();
                const placeholder = el.getAttribute('placeholder') || '';
                const aria = el.getAttribute('aria-label') || '';
                let score = rect.bottom + rect.right;
                if (el.id === 'chat-input') score += 10000;
                if (aria.includes('Chat message input')) score += 5000;
                if (placeholder === 'Ask me') score += 3000;
                if (rect.top > viewportHeight * 0.30) score += 8000;
                if (rect.left > viewportWidth * 0.35) score += 1000;
                return { el, rect, score };
            });

            const bottomCandidates = candidates.filter((item) =>
                item.rect.top > viewportHeight * 0.25 || item.rect.bottom > viewportHeight * 0.55
            );
            const pool = bottomCandidates.length ? bottomCandidates : candidates;
            pool.sort((a, b) => b.score - a.score);
            return pool.length ? pool[0].el : null;
            """
        )
        if element:
            try:
                driver.execute_script("arguments[0].focus();", element)
                element.click()
                return element
            except Exception:
                try:
                    driver.execute_script("arguments[0].focus();", element)
                    return element
                except Exception:
                    pass
        time.sleep(0.2)
    return None


def click_send_button(driver):
    button = driver.execute_script(
        """
        function deepElements(root = document) {
            const out = [];
            const walk = (node) => {
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
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return !!(rect.width || rect.height || el.getClientRects().length)
                && style.visibility !== 'hidden'
                && style.display !== 'none'
                && !el.disabled;
        }

        const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
        const candidates = deepElements().filter((el) => {
            const aria = el.getAttribute('aria-label') || '';
            const title = el.getAttribute('title') || '';
            const cls = String(el.className || '');
            return visible(el) && el.tagName === 'BUTTON' && (
                aria.includes('Send') ||
                title.includes('Send') ||
                cls.includes('new-chat-input__right-button') ||
                el.type === 'submit'
            );
        }).map((el) => {
            const rect = el.getBoundingClientRect();
            let score = rect.bottom + rect.right;
            const aria = el.getAttribute('aria-label') || '';
            const cls = String(el.className || '');
            if (aria.includes('Send message')) score += 10000;
            if (cls.includes('new-chat-input__right-button')) score += 8000;
            if (rect.top > viewportHeight * 0.30) score += 5000;
            return { el, score };
        });
        candidates.sort((a, b) => b.score - a.score);
        return candidates.length ? candidates[0].el : null;
        """
    )
    if not button:
        return False
    try:
        driver.execute_script("arguments[0].click();", button)
        return True
    except Exception:
        try:
            button.click()
            return True
        except Exception:
            return False


def send_ai_chat_message(driver, message):
    if not switch_to_ai_chat_frame(driver):
        raise RuntimeError("没有找到 AI 客服聊天窗口")

    input_box = find_chat_input(driver, timeout=5, allow_default_content=False)
    if input_box is None:
        raise RuntimeError("没有找到 AI 客服输入框")

    input_box.click()
    if (input_box.tag_name or "").lower() == "textarea":
        driver.execute_script(
            """
            const input = arguments[0];
            const value = arguments[1];
            input.focus();
            const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
                || Object.getOwnPropertyDescriptor(input.constructor.prototype, 'value')?.set;
            if (setter) {
                setter.call(input, value);
            } else {
                input.value = value;
            }
            input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'a' }));
            input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'a' }));
            """,
            input_box,
            message,
        )
    else:
        input_box.clear()
        input_box.send_keys(message)
    time.sleep(1)
    if not click_send_button(driver):
        input_box.send_keys(Keys.ENTER)
    time.sleep(3)


def safe_get_agent_messages(driver):
    try:
        return get_agent_messages(driver)
    except Exception as e:
        print(f"{get_now_time()} 获取AI客服消息失败：{e}<br>")
        return []


def wait_for_ai_agent_reply(driver, previous_messages, timeout=90):
    previous_set = set(previous_messages or [])
    end_time = time.time() + timeout
    latest_messages = previous_messages or []
    while time.time() < end_time:
        latest_messages = safe_get_agent_messages(driver)
        new_messages = [message for message in latest_messages if message not in previous_set]
        if new_messages:
            return new_messages[-1], latest_messages
        time.sleep(5)
    return "", latest_messages


def should_intervene_ai_response(response_text):
    if not response_text:
        return False
    if is_site_option_question(response_text):
        return True
    keywords = [
        "无法", "不能", "不可以", "拒绝", "不支持", "没有权限", "无法删除",
        "无法撤销", "无法回滚", "rollback", "not possible", "can't", "cannot",
        "processing", "正在处理", "稍后", "等待", "try again",
    ]
    lower_text = response_text.lower()
    return any(keyword.lower() in lower_text for keyword in keywords)


def build_site_option_reply(site):
    site_code = normalize_site_code(site)
    option_map = {
        "MX": "Mexico (Direct to consumer)",
        "BR": "Brazil",
        "CL": "Chile",
        "CO": "Colombia",
        "AR": "Argentina",
        "UY": "Uruguay",
    }
    return option_map.get(site_code, "Mexico (Direct to consumer)")


def is_site_option_question(response_text):
    lower_text = (response_text or "").lower()
    option_markers = [
        "mexico (direct to consumer)",
        "mexico (fulfillment)",
        "brazil",
        "chile",
        "colombia",
        "argentina",
        "uruguay",
    ]
    question_markers = [
        "which country",
        "country",
        "option",
        "对应的是",
        "哪个国家",
        "选项",
        "确认",
    ]
    return (
        any(marker in lower_text for marker in option_markers)
        and any(marker in lower_text for marker in question_markers)
    )


def build_infraction_followup_message(infraction_ids, site):
    return (
        f"{infraction_ids} 请继续帮我人工复核。我的店铺对应的是 {build_site_option_reply(site)} 站点，"
        f"这些商品是通用品牌/通用款产品，并非侵权产品，也没有使用他人品牌商标，"
        f"这是系统误判。麻烦坚持帮我重新核查并删除侵权记录，谢谢。"
    )


def send_infraction_message_with_retry(driver, huashu, infraction_ids, name, site, group_index, total_groups):
    max_attempts = 4
    previous_messages = safe_get_agent_messages(driver)
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"{get_now_time()} {name} {site} 尝试发送第 {group_index}/{total_groups} 组，第 {attempt} 次<br>")
            send_ai_chat_message(driver, huashu)
            print(f"{get_now_time()} {name} {site} 第 {group_index}/{total_groups} 组发送成功<br>")
            break
        except Exception as e:
            print(f"{get_now_time()} {name} {site} 第 {group_index}/{total_groups} 组发送失败，第 {attempt} 次：{e}<br>")
            if attempt == max_attempts:
                raise
            time.sleep(12 * attempt)

    response, latest_messages = wait_for_ai_agent_reply(driver, previous_messages, timeout=90)
    if not response:
        print(f"{get_now_time()} {name} {site} 第 {group_index}/{total_groups} 组暂未等到AI回复，等待后继续<br>")
        time.sleep(20)
        return

    print(f"{get_now_time()} {name} {site} AI最新回复：{response}<br>")
    if not should_intervene_ai_response(response):
        return

    if is_site_option_question(response):
        followup = build_site_option_reply(site)
    else:
        followup = build_infraction_followup_message(infraction_ids, site)
    print(f"{get_now_time()} {name} {site} AI回复需要介入，补充说明：{followup}<br>")
    for attempt in range(1, max_attempts + 1):
        try:
            send_ai_chat_message(driver, followup)
            print(f"{get_now_time()} {name} {site} 第 {group_index}/{total_groups} 组补充说明发送成功<br>")
            time.sleep(8)
            return
        except Exception as e:
            print(f"{get_now_time()} {name} {site} 补充说明发送失败，第 {attempt} 次：{e}<br>")
            if attempt == max_attempts:
                raise
            time.sleep(12 * attempt)


def send_infraction_message_with_retry(driver, huashu, infraction_ids, name, site, group_index, total_groups):
    max_attempts = 4
    previous_messages = safe_get_agent_messages(driver)
    base_extra = {
        "group_index": group_index,
        "total_groups": total_groups,
        "infraction_ids": infraction_ids,
    }

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"{get_now_time()} {name} {site} send group {group_index}/{total_groups}, attempt {attempt}<br>")
            send_ai_chat_message(driver, huashu)
            append_chat_log(
                name,
                site,
                "send_infraction",
                message=huashu,
                chat=previous_messages,
                extra={**base_extra, "attempt": attempt},
            )
            print(f"{get_now_time()} {name} {site} group {group_index}/{total_groups} sent<br>")
            break
        except Exception as e:
            append_chat_log(
                name,
                site,
                "send_infraction_error",
                message=huashu,
                chat=previous_messages,
                extra={**base_extra, "attempt": attempt, "error": str(e)},
            )
            print(f"{get_now_time()} {name} {site} send group {group_index}/{total_groups} failed: {e}<br>")
            if attempt == max_attempts:
                raise
            time.sleep(12 * attempt)

    response, latest_messages = wait_for_ai_agent_reply(driver, previous_messages, timeout=90)
    append_chat_log(
        name,
        site,
        "agent_reply",
        message=huashu,
        response=response,
        chat=latest_messages,
        extra=base_extra,
    )
    if not response:
        print(f"{get_now_time()} {name} {site} no AI reply for group {group_index}/{total_groups}<br>")
        time.sleep(20)
        return

    print(f"{get_now_time()} {name} {site} AI reply: {response}<br>")
    if not should_intervene_ai_response(response):
        return

    if is_site_option_question(response):
        followup = build_site_option_reply(site)
    else:
        followup = build_infraction_followup_message(infraction_ids, site)

    print(f"{get_now_time()} {name} {site} send followup: {followup}<br>")
    for attempt in range(1, max_attempts + 1):
        try:
            send_ai_chat_message(driver, followup)
            append_chat_log(
                name,
                site,
                "send_followup",
                message=followup,
                response=response,
                chat=latest_messages,
                extra={
                    **base_extra,
                    "attempt": attempt,
                    "site_option_reply": is_site_option_question(response),
                },
            )
            print(f"{get_now_time()} {name} {site} followup sent for group {group_index}/{total_groups}<br>")
            time.sleep(8)
            return
        except Exception as e:
            append_chat_log(
                name,
                site,
                "send_followup_error",
                message=followup,
                response=response,
                chat=latest_messages,
                extra={**base_extra, "attempt": attempt, "error": str(e)},
            )
            print(f"{get_now_time()} {name} {site} followup failed: {e}<br>")
            if attempt == max_attempts:
                raise
            time.sleep(12 * attempt)


def click_contact_us(driver, name, site):
    driver.switch_to.default_content()
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)

    contact_selectors = [
        (By.XPATH, "//*[self::a or self::button][contains(normalize-space(), 'Contact us')]"),
        (By.XPATH, "//*[contains(normalize-space(), 'Contact us')]"),
    ]
    for by, selector in contact_selectors:
        elements = driver.find_elements(by, selector)
        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                    element,
                )
                time.sleep(1)
                element.click()
                print(f"{get_now_time()} {name} {site} 点击 Contact us<br>")
                return True
            except Exception:
                continue

    clicked = driver.execute_script(
        """
        const candidates = [...document.querySelectorAll('a, button, span, div')]
            .filter((node) => node.innerText && node.innerText.trim().includes('Contact us'));
        const node = candidates.find((item) => {
            const rect = item.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        });
        if (!node) return false;
        node.scrollIntoView({block: 'center', inline: 'center'});
        node.click();
        return true;
        """
    )
    if clicked:
        print(f"{get_now_time()} {name} {site} JS 点击 Contact us<br>")
        return True
    return False


def click_ai_assistant_entry(driver, name, site):
    driver.switch_to.default_content()
    entry_texts = ("Ask the assistant", "AI Assistant", "Assistant")
    for text in entry_texts:
        try:
            clicked = driver.execute_script(
                """
                const text = arguments[0];
                const needle = text.toLowerCase();
                const candidates = [...document.querySelectorAll('button, a, div, span')]
                    .map((node) => {
                        const rect = node.getBoundingClientRect();
                        const label = [
                            node.innerText || '',
                            node.getAttribute('aria-label') || '',
                            node.getAttribute('title') || '',
                            node.getAttribute('data-testid') || '',
                            String(node.className || '')
                        ].join(' ').toLowerCase();
                        let score = rect.bottom + rect.right;
                        if (label.includes(needle)) score += 10000;
                        if (label.includes('assistant') || label.includes('maxwell')) score += 6000;
                        if (label.includes('chat')) score += 2500;
                        if (rect.top > window.innerHeight * 0.35) score += 3000;
                        if (rect.left > window.innerWidth * 0.45) score += 1500;
                        return {node, rect, label, score};
                    })
                    .filter((item) => item.rect.width > 0 && item.rect.height > 0)
                    .filter((item) =>
                        item.label.includes(needle) ||
                        item.label.includes('assistant') ||
                        item.label.includes('maxwell')
                    )
                    .sort((a, b) => b.score - a.score);
                const node = candidates.length ? candidates[0].node : null;
                if (!node) return false;
                node.scrollIntoView({block: 'center', inline: 'center'});
                node.click();
                return true;
                """,
                text,
            )
            if clicked:
                print(f"{get_now_time()} {name} {site} 点击 {text}<br>")
                return True
        except Exception:
            continue
    return False


def click_ai_entry_fallback(driver, name, site):
    driver.switch_to.default_content()
    try:
        clicked = driver.execute_script(
            """
            const candidates = [...document.querySelectorAll('button, a, div, span')]
                .map((node) => {
                    const rect = node.getBoundingClientRect();
                    const label = [
                        node.innerText || '',
                        node.getAttribute('aria-label') || '',
                        node.getAttribute('title') || '',
                        node.getAttribute('data-testid') || '',
                        String(node.className || '')
                    ].join(' ').toLowerCase();
                    let score = rect.bottom + rect.right;
                    if (label.includes('maxwell')) score += 10000;
                    if (label.includes('assistant')) score += 8000;
                    if (label.includes('chat')) score += 5000;
                    if (label.includes('help')) score += 1500;
                    if (rect.top > window.innerHeight * 0.45) score += 3500;
                    if (rect.left > window.innerWidth * 0.55) score += 2500;
                    return {node, rect, label, score};
                })
                .filter((item) => item.rect.width > 0 && item.rect.height > 0)
                .filter((item) =>
                    item.label.includes('maxwell') ||
                    item.label.includes('assistant') ||
                    item.label.includes('chat')
                )
                .sort((a, b) => b.score - a.score);
            const node = candidates.length ? candidates[0].node : null;
            if (!node) return false;
            node.scrollIntoView({block: 'center', inline: 'center'});
            node.click();
            return true;
            """
        )
        if clicked:
            print(f"{get_now_time()} {name} {site} 点击AI入口兜底候选<br>")
            return True
    except Exception:
        pass
    return False


def wait_for_ai_chat_frame(driver, timeout=15):
    end_time = time.time() + timeout
    while time.time() < end_time:
        if switch_to_ai_chat_frame(driver):
            return True
        time.sleep(0.5)
    dump_iframe_debug_info(driver)
    return False


def open_ai_contact_window(driver, name, site):
    driver.get(HELP_URL)
    time.sleep(5)

    for attempt in range(1, 6):
        print(f"{get_now_time()} {name} {site} 尝试打开AI客服悬浮窗，第 {attempt} 次<br>")
        if wait_for_ai_chat_frame(driver, timeout=2):
            break

        if click_ai_assistant_entry(driver, name, site):
            if wait_for_ai_chat_frame(driver, timeout=6):
                break

        if click_ai_entry_fallback(driver, name, site):
            if wait_for_ai_chat_frame(driver, timeout=6):
                break

        if click_contact_us(driver, name, site):
            if wait_for_ai_chat_frame(driver, timeout=6):
                break

        driver.switch_to.default_content()
        time.sleep(2)
    else:
        dump_iframe_debug_info(driver)
        dump_ai_entry_debug_info(driver)
        save_ai_open_debug_artifacts(driver, name, site)
        raise RuntimeError("没有找到 AI 客服悬浮窗 iframe")

    if not switch_to_ai_chat_frame(driver):
        dump_iframe_debug_info(driver)
        dump_ai_entry_debug_info(driver)
        save_ai_open_debug_artifacts(driver, name, site)
        raise RuntimeError("没有切换到 AI 客服悬浮窗 iframe")
    if not find_chat_input(driver, timeout=15, allow_default_content=False):
        dump_iframe_debug_info(driver)
        dump_ai_entry_debug_info(driver)
        save_ai_open_debug_artifacts(driver, name, site)
        raise RuntimeError("AI 客服悬浮窗已打开，但没有找到输入框")
    print(f"{get_now_time()} {name} {site} 进入 AI 客服悬浮窗<br>")


def use_one_browser_run_task(info):
    # /browser/open 接口会返回 selenium使用的http地址，以及webdriver的path，直接使用即可
    name = info[0]
    site = info[1]
    form = info[2]
    message = info[3]

    try:
        ip_usable = True
        if ip_usable:
            while True:
                print("ip检测通过，打开店铺平台主页")

                try:
                    shensu(name, site, form, message)
                except Exception as e:
                    traceback.print_exc()
                    print("申诉执行异常", e)
                finally:
                    window_id = getWindowidByName(name)
                    try:
                        closeBrowser(window_id)
                    except Exception as e:
                        continue
                # 5分钟执行一次这个方法
                time.sleep(300)
        else:
            print("ip检测不通过，请检查")
    except:
        print("脚本运行异常:" + traceback.format_exc())


def normalize_site_code(site):
    key = str(site or "").strip().upper()
    if key in SITE_CODE_MAP:
        return SITE_CODE_MAP[key]
    return SITE_CODE_MAP.get(str(site or "").strip(), key or "MX")


def _require_cdp_runner():
    if mercado_cdp_runner is None:
        raise RuntimeError("没有找到 mercado_appeal_runner.py，无法执行稳定版 AI 侵权申诉流程")
    return mercado_cdp_runner


def _appeal_loop_log_path(name, site_code):
    return Path(__file__).resolve().parent / f"{name}_{site_code}_ai_recollect_loop.log"


def _appeal_loop_log(path, message):
    line = f"[{get_now_time()}] {message}"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def appeal_ai_recollect_once(name, site="MX"):
    """
    稳定版侵权 AI 申诉：
    1. 打开指定比特浏览器窗口。
    2. 进入当前站点侵权列表并重新读取所有当前编号。
    3. 按 3 个一组重新分组。
    4. 进入 Help 页 AI Assistant 悬浮窗逐组发送申诉话术。
    """
    runner = _require_cdp_runner()
    site_code = normalize_site_code(site)
    log_path = _appeal_loop_log_path(name, site_code)
    _appeal_loop_log(log_path, f"START window={name} site={site_code}")

    cdp_http = runner.open_bitbrowser(name)
    first, ids = runner.collect_infractions(cdp_http, site_code)
    groups = list(runner.chunks(ids, 3))

    prefix = f"{name}_{site_code}_loop_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    payload = {
        "window": name,
        "site": site_code,
        "time": get_now_time(),
        "first": first,
        "ids": ids,
        "aiGroups": groups,
    }
    out_dir = Path(__file__).resolve().parent
    (out_dir / f"{prefix}_ids.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _appeal_loop_log(log_path, f"COLLECTED ids={len(ids)} groups={len(groups)}")
    if not groups:
        _appeal_loop_log(log_path, "DONE no current infringement ids")
        return {"ids": len(ids), "groups": 0, "sent": 0}

    ai = runner.open_ai_assistant(cdp_http)
    sent_count = 0
    try:
        for idx, group in enumerate(groups, start=1):
            message = f"{runner.PRODUCT_SEPARATOR.join(group)}{runner.AI_APPEAL_SUFFIX}"
            before_chat = runner.ai_recent_chat(ai)
            result = runner.send_ai_message(ai, message)
            time.sleep(7)
            chat = runner.ai_recent_chat(ai)
            append_chat_log(
                name,
                site_code,
                "recollect_send_ai_group",
                message=message,
                chat=chat,
                extra={
                    "group_index": idx,
                    "total_groups": len(groups),
                    "group_ids": group,
                    "send_result": result,
                    "before_chat": before_chat,
                },
            )
            runner.maybe_reply_site_option(ai, site_code, name)
            shot = out_dir / f"{prefix}_ai_group_{idx}.png"
            ai.screenshot(shot)
            sent_count += 1
            _appeal_loop_log(
                log_path,
                "SENT "
                + f"group={idx}/{len(groups)} ids={','.join(group)} "
                + f"method={result.get('method')} typed={result.get('typed')} screenshot={shot}",
            )
    finally:
        ai.close()

    _appeal_loop_log(log_path, f"DONE ids={len(ids)} sent_groups={sent_count}")
    return {"ids": len(ids), "groups": len(groups), "sent": sent_count}


def appeal_ai_recollect_loop(name, site="MX", interval=AI_RECOLLECT_INTERVAL_SECONDS):
    site_code = normalize_site_code(site)
    log_path = _appeal_loop_log_path(name, site_code)
    cycle = 1
    while True:
        started = time.time()
        try:
            _appeal_loop_log(log_path, f"CYCLE {cycle} BEGIN")
            result = appeal_ai_recollect_once(name, site_code)
            _appeal_loop_log(log_path, f"CYCLE {cycle} OK {result}")
        except Exception as e:
            _appeal_loop_log(log_path, f"CYCLE {cycle} ERROR {type(e).__name__}: {e}")
            traceback.print_exc()

        sleep_seconds = max(0, interval - (time.time() - started))
        _appeal_loop_log(log_path, f"CYCLE {cycle} SLEEP seconds={sleep_seconds:.1f}")
        time.sleep(sleep_seconds)
        cycle += 1


# 申诉
def shensu(name, site, form, message):
    print(f"{name} {site} 开始进行{form}申诉，自定义话术为{message}<br>")
    window_id = get_window_id_by_shop_name(name)
    driver, res = connect_bit_browser(window_id)
    name = res["data"]["name"]

    nickname_list = ["Bruce", "Jack", "Lucy", "James"]
    nickname = random.choice(nickname_list)

    try:
        driver.get(HELP_URL)
        time.sleep(8)
        select_site(driver, name, site)

        if form == "延误":
            handle_delay(driver, name, site, message, nickname)

        if form == "侵权":
            handle_infraction(window_id, driver, name, site, message, nickname)
            return

        if form == "投诉":
            handle_complain(driver, name, site, message, nickname)

        huashu = build_appeal_message(window_id, name, site, form, message, nickname)
        if huashu == "":
            print(f"{get_now_time()} {name} {site} 没有可以申诉的数据<br>")
            return "没有可以申诉的数据"

        open_ai_contact_window(driver, name, site)
        send_ai_chat_message(driver, huashu)
        append_chat_log(
            name,
            site,
            "send_initial_appeal",
            message=huashu,
            extra={"form": form, "window_id": window_id},
        )
        print(f"{get_now_time()} {name} {site} 自动发送AI客服申诉话术：{huashu}<br>")
        # chat_ai(driver, name, site, form, huashu, nickname)
    except Exception as e:
        print(f"{get_now_time()} {name} {site} AI客服申诉执行失败<br>")
        print(e)
        traceback.print_exc()
    finally:
        print(f"{get_now_time()} {name}{site}AI客服申诉执行完毕<br>")
        # print(f"{get_now_time()} {name}{site} 关闭浏览器<br>")


def handle_infraction(window_id, driver, name, site, message, nickname):
    group = 3
    inf_list = get_infraction_orders(window_id, name, site)
    if not inf_list:
        print(f"{get_now_time()} {name} {site} 没有可以申诉的侵权编号<br>")
        return

    groups = [inf_list[i:i + group] for i in range(0, len(inf_list), group)]
    print(f"{get_now_time()} {name} {site} 侵权编号共 {len(inf_list)} 个，按每组 {group} 个分为 {len(groups)} 组<br>")

    appeal_suffix = (
        f"这几个产品是通用品牌产品，并非侵权产品，这是系统误判，"
        f"麻烦帮我重新核查并删除侵权记录，谢谢"
    )

    open_ai_contact_window(driver, name, site)
    for index, current_group in enumerate(groups, start=1):
        infraction_ids = "、".join(str(item) for item in current_group)
        huashu = f"{infraction_ids}{message}" if message else f"{infraction_ids}{appeal_suffix}"
        print(f"{get_now_time()} {name} {site} 开始发送第 {index}/{len(groups)} 组侵权申诉：{huashu}<br>")
        send_infraction_message_with_retry(driver, huashu, infraction_ids, name, site, index, len(groups))
        print(f"{get_now_time()} {name} {site} 第 {index}/{len(groups)} 组侵权申诉处理完成<br>")
        if index < len(groups):
            time.sleep(20)



def handle_delay(driver, name, site, message, nickname):
    print("开始处理侵权")


def handle_complain(driver, name, site, message, nickname):
    print("开始处理侵权")


def get_delay_orders_random(name, site, nums):
    order_random = ""
    try:
        delay_folder_path = get_bit_path() / "美客多延误"
        delay_file = get_latest_modified_file(delay_folder_path)
        delay_file_path = delay_folder_path / delay_file
        fifteen_days_ago = datetime.now() - timedelta(days=15)
        order_list = []
        df = pd.read_excel(delay_file_path, engine='openpyxl')
        for index, row in df.iterrows():
            # print(row)
            line_name = row['店铺']
            line_site = row['站点']
            order_date = row['下单时间']
            order_num = row['销售单号']
            dispatch_date = row['实际揽收时间']
            if (line_name == name and line_site == site and dispatch_date != "Not yet dispatched"):
                order_date = parser_delay_date(order_date)
                if (order_date > fifteen_days_ago):
                    order_list.append(order_num)
        print(get_now_time() + name + site + "最近15天的延误个数:", len(order_list))

        if len(order_list) >= nums:
            order_random = str(random.sample(order_list, nums))
        else:
            order_random = str(order_list)
        order_random = re.sub(r'[^\d,]', '', order_random)

        print(get_now_time() + name + site + "随机得到的延误销售单号为", order_random)
    except Exception as e:
        print("获取延误表格信息失败", e)
    return order_random


def get_infraction_orders_random(window_id, name, site, nums):
    inf_list = []
    try:
        infos = get_infractions_info(window_id, name, site,0)
        for i in infos:
            inf_list.append(i[2])
        if len(inf_list) >= nums:
            inf_list = str(random.sample(inf_list, nums))
        else:
            inf_list = str(inf_list)
        print(get_now_time() + name + site + "随机得到的侵权单号为", inf_list)
    except Exception as e:
        print("获取侵权订单信息失败", e)
    return inf_list


def get_infraction_orders(window_id, name, site):
    inf_list = []
    try:
        infos = get_infractions_info(window_id, name, site,0)
        for i in infos:
            inf_list.append(i[2])
        print(get_now_time() + name + site + "得到的侵权编号为", inf_list)
    except Exception as e:
        print("获取侵权订单信息失败", e)
    return inf_list


# 检查聊天是否结束
def checkChatEnd(driver, name, site):
    try:
        switch_to_ai_chat_frame(driver)
        WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located(
                (
                    By.XPATH,
                    "//*[contains(text(), 'This chat has ended') or contains(text(), 'chat has ended')]",
                )
            )
        )
        print(f"{get_now_time()} {name}{site}聊天已经结束,结束AI找客服<br>")
        return True
    except Exception as e:
        return False
    return False


def get_agent_messages(driver):
    switch_to_ai_chat_frame(driver)
    message_selectors = [
        (By.CSS_SELECTOR, ".chat-ui-message-bubble.chat-ui-message-bubble--from-agent"),
        (By.CSS_SELECTOR, ".mlc-scroll-paginate_item"),
        (By.CSS_SELECTOR, "[class*='message'][class*='agent']"),
        (By.CSS_SELECTOR, "[class*='bubble']"),
        (By.XPATH, "//*[contains(@class, 'message') or contains(@class, 'bubble')]"),
    ]
    messages = []
    for by, selector in message_selectors:
        elements = driver.find_elements(by, selector)
        for element in elements:
            try:
                text = element.text.strip()
                if text and text not in messages:
                    messages.append(text)
            except Exception:
                continue
        if messages:
            break
    return messages


def chat_script(driver):
    return None


def use_all_browser_run_task(browser_list):
    """
    循环打开所有店铺运行脚本
    :param browser_list: 店铺列表
    """
    for browser in browser_list:
        use_one_browser_run_task(browser)


def use_all_browser_run_task_with_thread_pool(browser_list, max_threads=10):
    """
    使用线程池控制最大并发线程数
    :param browser_list: 店铺列表
    :param max_threads: 最大并发线程数
    """
    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        executor.map(use_one_browser_run_task, browser_list)


def auto_appeal_delay():
    fold_path = get_bit_path() / "美客多延误"
    file_path = fold_path / get_latest_modified_file(fold_path)
    wb = load_workbook(file_path)
    sheet = wb.active
    # 使用 min_row=2 跳过第一行

    name_site = set()
    for row in sheet.iter_rows(min_row=2, values_only=True):
        delayrate = row[2]
        if delayrate != None and delayrate != "":
            delay_value = 0.0
            if type(delayrate) == str:
                delay_value = float(delayrate.strip("%")) / 100
            else:
                delay_value = float(delayrate)
            if delay_value >= 0.07:
                name_site.add((row[0], row[1], delay_value))

    print(len(name_site))

    list_appeal = []
    for i in name_site:
        list_appeal.append((i[0], i[1], "延误", ""))

    print(list_appeal)

    use_all_browser_run_task_with_thread_pool(list_appeal, 5)


def run_ai_recollect_cli(argv=None):
    parser = argparse.ArgumentParser(description="比特浏览器 Mercado Libre AI 侵权申诉")
    parser.add_argument("--window", default="vngbjkk", help="比特浏览器窗口名")
    parser.add_argument("--site", default="MX", help="站点，如 MX/墨西哥/BR/巴西")
    parser.add_argument("--interval", type=int, default=AI_RECOLLECT_INTERVAL_SECONDS, help="循环间隔秒数")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ai-recollect-once", action="store_true", help="重新读取侵权列表并发送一轮")
    mode.add_argument("--ai-recollect-loop", action="store_true", help="每隔 interval 秒重新读取并循环发送")
    args = parser.parse_args(argv)

    if args.ai_recollect_once:
        result = appeal_ai_recollect_once(args.window, args.site)
        print(f"AI申诉完成：读取 {result['ids']} 个编号，发送 {result['sent']} 组")
    else:
        appeal_ai_recollect_loop(args.window, args.site, args.interval)


if __name__ == "__main__":
    use_one_browser_run_task(('虎虎生威（fti）', '巴西', '侵权', ''))
