"""
美客多 AI 客服申诉自动化脚本。

主要职责：
1. 连接比特浏览器中指定店铺窗口。
2. 切换到指定美客多站点。
3. 打开 Mercado Libre Help 页面里的 AI 客服悬浮窗。
4. 针对延误、侵权、投诉等场景生成申诉话术并发送。
5. 对侵权编号进行分组发送，并根据 AI 回复自动补充站点/误判说明。

适用环境：Python 3 + Selenium + 比特浏览器本地 API。
"""

import time
import traceback
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import math


# 允许脚本被直接运行时也能正常导入项目内的 bit、AI_Agent 等包。
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests
from pydantic.v1.datetime_parse import parse_date
from selenium.webdriver.chrome.service import Service

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
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

# 聊天记录入库接口；AI 与人工客服回复都会通过这个接口记录。
CHAT_INFO_API_URL = "https://zeshun.nat100.top/api/v1/chat"

# 美客多帮助中心入口，AI 客服悬浮窗通常挂在这些页面中。
HELP_URL = "https://global-selling.mercadolibre.com/help"
AI_RECOLLECT_INTERVAL_SECONDS = 600

# AI 悬浮窗 iframe 的特征。不同账号/页面版本的 src/title 可能不同，所以这里保留多个标记。
AI_FRAME_URL_MARKERS = ("meli-ai-chat", "maxwell/new-chat")
AI_FRAME_MARKERS = ("meli-ai-chat", "maxwell", "new-chat", "ai chat", "assistant", "chat", "meli")
AI_HELP_URLS = (
    HELP_URL,
    "https://global-selling.mercadolibre.com/help/v2",
    "https://global-selling.mercadolibre.com/help/chat/v2",
)
SITE_OPTION_MENU_OPTIONS = (
    "Mexico (Direct to consumer)",
    "Mexico (Fulfillment)",
    "Brazil",
    "Chile",
    "Colombia",
    "Argentina",
    "Uruguay",
)

# 将中文站点名、平台代码统一转成内部站点码，便于回复 AI 的站点确认问题。
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

# 美客多顶部站点切换器中各站点对应的 data-value。
SITE_SWITCH_SELECTOR_MAP = {
    "墨西哥": 'div[data-value="MLM-remote"]',
    "巴西": 'div[data-value="MLB-remote"]',
    "哥伦比亚": 'div[data-value="MCO-remote"]',
    "智利": 'div[data-value="MLC-remote"]',
    "阿根廷": 'div[data-value="MLA-remote"]',
    "乌拉圭": 'div[data-value="MLU-remote"]',
}


def insert_chat_info_by_api(name, site, message, chat, response, time):
    """把客服对话记录通过公网接口写入数据库。"""
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
    """打开指定比特浏览器窗口，并通过 Selenium 连接到该窗口的调试端口。"""
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
    """根据店铺名从“比特配置文件.xlsx”中读取比特浏览器窗口 ID。"""
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
    """在美客多全球销售后台顶部站点切换器中切换到指定站点。"""
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
    """根据申诉类型构造首条发送给 AI 客服的申诉话术。"""
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


def get_frame_info(driver, frame):
    """读取 iframe 的关键属性与位置，用来判断它是不是 AI 客服悬浮窗。"""
    return driver.execute_script(
        """
        const frame = arguments[0];
        const rect = frame.getBoundingClientRect();
        return {
            src: frame.getAttribute('src') || '',
            title: frame.getAttribute('title') || '',
            name: frame.getAttribute('name') || '',
            id: frame.getAttribute('id') || '',
            cls: String(frame.className || ''),
            top: rect.top,
            left: rect.left,
            right: rect.right,
            bottom: rect.bottom,
            width: rect.width,
            height: rect.height,
            visible: !!(rect.width || rect.height || frame.getClientRects().length)
        };
        """,
        frame,
    )


def is_ai_frame_info(info):
    """根据 iframe 的 src/title/name/id/class 等文本特征识别 AI 客服 iframe。"""
    text = " ".join(str(info.get(key, "")) for key in ("src", "title", "name", "id", "cls")).lower()
    return any(marker in text for marker in AI_FRAME_MARKERS)


def switch_to_ai_chat_frame(driver, require_input=False, max_depth=2):
    """递归切换到 AI 客服 iframe。

    require_input=True 时，会进一步确认 iframe 内存在聊天输入框，避免误进帮助页顶部搜索框。
    """
    driver.switch_to.default_content()

    def search_frames(depth):
        """按页面位置和 AI 特征给 iframe 打分，优先尝试右下方的悬浮窗。"""
        frames = driver.find_elements(By.TAG_NAME, "iframe")
        frame_infos = []
        for frame in frames:
            try:
                info = get_frame_info(driver, frame)
                if not info.get("visible"):
                    continue
                score = info.get("bottom", 0) + info.get("right", 0)
                if is_ai_frame_info(info):
                    score += 20000
                if info.get("top", 0) > 100:
                    score += 2000
                frame_infos.append((score, frame, info))
            except Exception:
                continue

        for _, frame, info in sorted(frame_infos, key=lambda item: item[0], reverse=True):
            driver.switch_to.parent_frame() if depth > 0 else driver.switch_to.default_content()
            try:
                driver.switch_to.frame(frame)
            except Exception:
                continue

            if is_ai_frame_info(info):
                if not require_input or find_chat_input(driver, timeout=2, allow_default_content=False):
                    return True

            if depth < max_depth and search_frames(depth + 1):
                return True

        if depth > 0:
            try:
                driver.switch_to.parent_frame()
            except Exception:
                driver.switch_to.default_content()
        return False

    try:
        if search_frames(0):
            return True
    except Exception:
        driver.switch_to.default_content()

    driver.switch_to.default_content()
    return False


def switch_to_ai_chat_frame_old(driver):
    """旧版 iframe 定位逻辑，仅按 src 判断，保留用于对照排查。"""
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
    """打印当前页面所有 iframe 的调试信息，方便定位 AI 悬浮窗加载失败原因。"""
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
    """AI 悬浮窗打开失败时保存截图和 HTML，便于后续人工分析页面结构。"""
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
    """打印页面上疑似 AI/Help/Contact 入口的元素信息。"""
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
    """在当前 iframe 内查找 AI 客服真正的 textarea 输入框。

    这里优先匹配 id=chat-input、aria-label、placeholder，并给页面下半部分元素更高分，
    目的是避开页面顶部的帮助中心搜索框。
    """
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
    """查找并点击 AI 客服输入框旁的发送按钮，失败时交给回车发送兜底。"""
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
    """向 AI 客服窗口发送一条消息。

    Mercado 的 textarea 有时不接受普通 click/send_keys，所以优先用 JS 原生 setter 写值并触发 input/change 事件，
    如果页面仍未接收到内容，再使用 ActionChains 兜底。
    """
    if not switch_to_ai_chat_frame(driver):
        raise RuntimeError("没有找到 AI 客服聊天窗口")

    input_box = find_chat_input(driver, timeout=5, allow_default_content=False)
    if input_box is None:
        raise RuntimeError("没有找到 AI 客服输入框")

    if (input_box.tag_name or "").lower() == "textarea":
        driver.execute_script(
            """
            const input = arguments[0];
            const value = arguments[1];
            input.scrollIntoView({block: 'center', inline: 'center'});
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
        current_value = driver.execute_script("return arguments[0].value || '';", input_box)
        if current_value != message:
            driver.execute_script("arguments[0].focus();", input_box)
            ActionChains(driver).send_keys(message).perform()
    else:
        try:
            input_box.click()
            input_box.clear()
            input_box.send_keys(message)
        except Exception:
            driver.execute_script(
                """
                const input = arguments[0];
                const value = arguments[1];
                input.scrollIntoView({block: 'center', inline: 'center'});
                input.focus();
                input.value = value;
                input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                input_box,
                message,
            )
    time.sleep(1)
    if not click_send_button(driver):
        try:
            input_box.send_keys(Keys.ENTER)
        except Exception:
            ActionChains(driver).send_keys(Keys.ENTER).perform()
    time.sleep(3)


def safe_get_agent_messages(driver):
    """安全读取 AI 客服消息，读取失败时返回空列表，避免打断主流程。"""
    try:
        return get_agent_messages(driver)
    except Exception as e:
        print(f"{get_now_time()} 获取AI客服消息失败：{e}<br>")
        return []


def wait_for_ai_agent_reply(driver, previous_messages, timeout=60):
    """等待 AI 客服出现相对 previous_messages 的新回复。"""
    previous_messages = previous_messages or []
    previous_count = len(previous_messages)
    previous_last = previous_messages[-1] if previous_messages else ""
    end_time = time.time() + timeout
    latest_messages = previous_messages
    while time.time() < end_time:
        latest_messages = safe_get_agent_messages(driver)
        if len(latest_messages) > previous_count:
            new_messages = latest_messages[previous_count:]
            for message in reversed(new_messages):
                if is_site_option_question(message):
                    return message, latest_messages
            return new_messages[-1], latest_messages
        if latest_messages and latest_messages[-1] != previous_last:
            for message in reversed(latest_messages[-5:]):
                if is_site_option_question(message):
                    return message, latest_messages
            return latest_messages[-1], latest_messages
        time.sleep(5)
    return "", latest_messages


def should_intervene_ai_response(response_text):
    """判断 AI 回复是否需要脚本继续补充说明或坚持申诉。"""
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
    """根据当前站点生成 AI 要求选择站点时的标准回复。"""
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


def contains_site_option_menu(response_text):
    """识别 AI 发出的完整站点选项列表。

    AI 有时只返回“Mexico ... Uruguay”这类纯选项列表，不一定带“可选”或问句；
    只要看到多个站点选项同时出现，就按站点确认问题处理。
    """
    lower_text = re.sub(r"\s+", " ", response_text or "").lower()
    lower_text = lower_text.replace("（", "(").replace("）", ")")
    compact_text = re.sub(r"[\s。．.、,，:：;；]+", "", lower_text)
    compact_menu = "".join(SITE_OPTION_MENU_OPTIONS).lower()
    compact_menu = re.sub(r"[\s。．.、,，:：;；]+", "", compact_menu)

    # 硬规则：只要文本包含完整“Mexico ... Uruguay”菜单，就必须自动回复站点。
    if compact_menu in compact_text:
        return True

    compact_options = [
        re.sub(r"[\s。．.、,，:：;；]+", "", option.lower())
        for option in SITE_OPTION_MENU_OPTIONS
    ]
    matched_count = sum(1 for option in compact_options if option in compact_text)
    if matched_count >= 5:
        return True

    return ("可选" in lower_text or "option" in lower_text or "站点" in lower_text) and matched_count >= 3


def find_site_option_message(messages):
    """从最近聊天消息中找出包含站点选项菜单的 AI 回复。"""
    for message in reversed(messages or []):
        if is_site_option_question(message):
            return message
    joined_text = "\n".join(messages or [])
    if is_site_option_question(joined_text):
        return joined_text
    return ""


def is_site_option_question(response_text):
    """识别 AI 是否在询问“针对哪个站点/选项提出咨询”。"""
    if contains_site_option_menu(response_text):
        return True

    lower_text = (response_text or "").lower()
    option_markers = [
        "mexico (direct to consumer)",
        "mexico (fulfillment)",
        "brazil",
        "chile",
        "colombia",
        "argentina",
        "uruguay",
        "可选",
        "it's helpful",
        "it's not helpful",
    ]
    question_markers = [
        "which country",
        "country",
        "option",
        "site",
        "站点",
        "对应的是",
        "哪个国家",
        "哪个站点",
        "针对哪个",
        "哪个选项",
        "这条咨询",
        "请问你是",
        "选项",
        "确认",
        "Mexico (Direct to consumer)、Mexico (Fulfillment)、Brazil、Chile、Colombia、Argentina、Uruguay"
    ]
    return (
        any(marker in lower_text for marker in option_markers)
        and any(marker in lower_text for marker in question_markers)
    )


def build_infraction_followup_message(infraction_ids, site):
    """生成侵权申诉被 AI 拒绝或要求确认后继续坚持复核的话术。"""
    return (
        f"{infraction_ids} 请继续帮我人工复核。我的店铺对应的是 {build_site_option_reply(site)} 站点，"
        f"这些商品是通用品牌/通用款产品，并非侵权产品，也没有使用他人品牌商标，"
        f"这是系统误判。麻烦坚持帮我重新核查并删除侵权记录，谢谢。"
    )


def reply_site_option_menu_if_present(driver, name, site, timeout=20):
    """只要当前 AI 窗口文本包含完整站点选项菜单，就按当前站点自动回复。"""
    end_time = time.time() + timeout
    while time.time() < end_time:
        messages = safe_get_agent_messages(driver)
        site_option_message = find_site_option_message(messages)
        if site_option_message:
            reply = build_site_option_reply(site)
            print(f"{get_now_time()} {name} {site} 检测到完整站点选项菜单，自动回复：{reply}<br>")
            send_ai_chat_message(driver, reply)
            append_chat_log(
                name,
                site,
                "auto_reply_site_option_menu",
                message=reply,
                response=site_option_message,
                chat=messages,
            )
            return True, site_option_message, messages
        time.sleep(1)
    return False, "", []


def send_infraction_message_with_retry(driver, huashu, infraction_ids, name, site, group_index, total_groups):
    """旧版侵权分组发送逻辑；后面同名函数会覆盖此定义。"""
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
    """发送一组侵权申诉，并根据 AI 回复自动补充站点或误判说明。

    这是当前实际生效的同名函数：发送失败会重试；发送后会等待 AI 回复；
    如果 AI 正在处理、拒绝处理或询问站点，会继续介入回复。
    """
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
            replied_site_option, site_option_response, site_option_messages = reply_site_option_menu_if_present(
                driver,
                name,
                site,
                timeout=25,
            )
            if replied_site_option:
                previous_messages = site_option_messages
                response = site_option_response
                print(f"{get_now_time()} {name} {site} 已完成站点选项自动回复，继续等待后续AI回复<br>")
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
    site_option_message = response if is_site_option_question(response) else find_site_option_message(latest_messages)
    if site_option_message:
        response = site_option_message
        print(f"{get_now_time()} {name} {site} 从AI聊天记录识别到站点选项问题：{response}<br>")

    if not should_intervene_ai_response(response):
        return

    is_site_reply = bool(site_option_message) or is_site_option_question(response)
    if is_site_reply:
        followup = build_site_option_reply(site)
        print(f"{get_now_time()} {name} {site} 识别到AI站点选项问题，按当前站点回复：{followup}<br>")
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
            if is_site_reply:
                site_reply_messages = safe_get_agent_messages(driver)
                next_response, next_messages = wait_for_ai_agent_reply(driver, site_reply_messages, timeout=75)
                append_chat_log(
                    name,
                    site,
                    "agent_reply_after_site_option",
                    message=followup,
                    response=next_response,
                    chat=next_messages,
                    extra=base_extra,
                )
                if next_response:
                    print(f"{get_now_time()} {name} {site} site option后AI回复: {next_response}<br>")
                if next_response and should_intervene_ai_response(next_response) and not is_site_option_question(next_response):
                    insist_message = build_infraction_followup_message(infraction_ids, site)
                    print(f"{get_now_time()} {name} {site} site option后继续坚持说明: {insist_message}<br>")
                    send_ai_chat_message(driver, insist_message)
                    append_chat_log(
                        name,
                        site,
                        "send_insist_after_site_option",
                        message=insist_message,
                        response=next_response,
                        chat=next_messages,
                        extra=base_extra,
                    )
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
    """点击帮助页底部或页面中的 Contact us 入口。"""
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
    """优先按常见文案点击 AI Assistant 入口。

    页面可能使用 Shadow DOM，所以通过 JS 深度遍历元素，而不是只用普通 CSS 查找。
    """
    driver.switch_to.default_content()
    entry_texts = ("Ask the assistant", "AI Assistant", "Assistant")
    for text in entry_texts:
        try:
            clicked = driver.execute_script(
                """
                const text = arguments[0];
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
                const needle = text.toLowerCase();
                const candidates = deepElements()
                    .filter((node) => ['BUTTON', 'A', 'DIV', 'SPAN'].includes(node.tagName))
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
    """AI 入口兜底点击：按 maxwell/assistant/chat 等关键词寻找最像悬浮入口的元素。"""
    driver.switch_to.default_content()
    try:
        clicked = driver.execute_script(
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
            const candidates = deepElements()
                .filter((node) => ['BUTTON', 'A', 'DIV', 'SPAN'].includes(node.tagName))
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
    """在限定时间内等待 AI 客服 iframe 出现并可切换。"""
    end_time = time.time() + timeout
    while time.time() < end_time:
        if switch_to_ai_chat_frame(driver):
            return True
        time.sleep(0.5)
    dump_iframe_debug_info(driver)
    return False


def open_ai_contact_window(driver, name, site):
    """打开 Help 页面并进入 AI 客服悬浮窗。

    不同账号的入口页和按钮文案可能不同，所以依次尝试多个 URL、多种入口点击方式；
    若失败会保存页面截图、HTML 和候选元素信息。
    """
    opened = False
    for url in AI_HELP_URLS:
        driver.switch_to.default_content()
        driver.get(url)
        print(f"{get_now_time()} {name} {site} 打开AI客服入口页面：{url}<br>")
        time.sleep(5)

        for attempt in range(1, 5):
            print(f"{get_now_time()} {name} {site} 尝试打开AI客服悬浮窗，第 {attempt} 次<br>")
            if wait_for_ai_chat_frame(driver, timeout=2):
                opened = True
                break

            if click_ai_assistant_entry(driver, name, site):
                if wait_for_ai_chat_frame(driver, timeout=6):
                    opened = True
                    break

            if click_ai_entry_fallback(driver, name, site):
                if wait_for_ai_chat_frame(driver, timeout=6):
                    opened = True
                    break

            if click_contact_us(driver, name, site):
                if wait_for_ai_chat_frame(driver, timeout=6):
                    opened = True
                    break

            driver.switch_to.default_content()
            time.sleep(2)

        if opened:
            break

    if not opened:
        dump_iframe_debug_info(driver)
        dump_ai_entry_debug_info(driver)
        save_ai_open_debug_artifacts(driver, name, site)
        raise RuntimeError("没有找到 AI 客服悬浮窗 iframe")

    if not switch_to_ai_chat_frame(driver, require_input=False):
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
    """循环使用单个比特浏览器窗口执行申诉任务。"""
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
    """把用户传入的中文站点、平台代码或英文缩写统一为内部站点码。"""
    key = str(site or "").strip().upper()
    if key in SITE_CODE_MAP:
        return SITE_CODE_MAP[key]
    return SITE_CODE_MAP.get(str(site or "").strip(), key or "MX")


def _require_cdp_runner():
    """加载 CDP 稳定版运行器，缺失时给出明确错误。"""
    if mercado_cdp_runner is None:
        raise RuntimeError("没有找到 mercado_appeal_runner.py，无法执行稳定版 AI 侵权申诉流程")
    return mercado_cdp_runner


def _appeal_loop_log_path(name, site_code):
    """生成稳定版循环日志文件路径。"""
    return Path(__file__).resolve().parent / f"{name}_{site_code}_ai_recollect_loop.log"


def _appeal_loop_log(path, message):
    """同时写入日志文件和控制台，便于前端实时展示。"""
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
    """按固定间隔循环重新读取侵权编号并执行 AI 申诉。"""
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
    """AI 客服申诉主入口，根据 form 分发到延误、侵权或投诉处理逻辑。"""
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
    """处理侵权申诉：读取侵权编号，按固定数量分组，并逐组发送给 AI 客服。"""
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
    """延误申诉预留入口，目前主流程仍使用通用话术发送。"""
    print("开始处理侵权")


def handle_complain(driver, name, site, message, nickname):
    """投诉申诉预留入口，目前主流程仍使用通用话术发送。"""
    print("开始处理侵权")


def get_delay_orders_random(name, site, nums):
    """从最新延误表中随机抽取最近 15 天内的延误订单号。"""
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
    """从当前侵权列表中随机抽取指定数量的侵权编号。"""
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
    """读取当前店铺、站点下全部侵权编号。"""
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
    """检查 AI 客服会话是否已经结束。"""
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
    """读取 AI 客服窗口中客服侧的消息文本。"""
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
                # 不按文本去重：AI 对不同分组可能连续回复完全相同的站点选项菜单，
                # 去重会导致后续相同回复无法被 wait_for_ai_agent_reply 识别为新消息。
                if text:
                    messages.append(text)
            except Exception:
                continue
        if messages:
            break
    try:
        rich_texts = driver.execute_script(
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
            const candidates = [];
            const bodyText = document.body ? document.body.innerText : '';
            const bodyHtml = document.body ? document.body.innerHTML : '';
            candidates.push(bodyText, bodyHtml);
            for (const el of deepElements()) {
                const text = [
                    el.innerText || '',
                    el.textContent || '',
                    el.innerHTML || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                ].join(' ');
                if (/Mexico\\s*\\(Direct\\s+to\\s+consumer\\)/i.test(text) && /Uruguay/i.test(text)) {
                    candidates.push(text);
                }
            }
            return candidates
                .filter(Boolean)
                .map(text => String(text).replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim())
                .filter(Boolean)
                .slice(-20);
            """
        )
        for text in rich_texts or []:
            if contains_site_option_menu(text):
                messages.append(text)
    except Exception:
        pass
    print("AI客服回复:",messages)
    return messages


def chat_script(driver):
    """旧版聊天脚本预留函数。"""
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
    """根据最新延误报表筛选延误率较高的店铺，并批量发起延误申诉。"""
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
    """命令行调试入口：支持执行一轮或循环执行稳定版 AI 侵权申诉。"""
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
    use_one_browser_run_task(('虎虎生威（fti）', '墨西哥', '侵权', ''))
