import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.wait import WebDriverWait

from bit.bit_utils import get_now_time
from bit.bit_api import *
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
import pyautogui
from bit.bit_switch_country import *
from openpyxl import load_workbook
from bit.bit_send_mail import *
import pandas as pd

from datetime import datetime
from pathlib import Path
from bit.bit_db_api import insert_task_record, inset_reputation_info
from bit.bit_clash import *


def _is_bit_api_rate_limited(res):
    text = str(res or "")
    return "请求太过频繁" in text or "每秒最多可以发起" in text


def _connect_browser(window_id, max_retries=3, retry_delay=3):
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
        if _is_bit_api_rate_limited(res):
            print(
                f"{get_now_time()} 比特浏览器打开窗口被限频，等待 {retry_delay} 秒后重试："
                f"{window_id}，第 {attempt}/{max_retries} 次，原因：{msg}"
            )
        else:
            print(
                f"{get_now_time()} 比特浏览器打开窗口返回异常，等待 {retry_delay} 秒后重试："
                f"{window_id}，第 {attempt}/{max_retries} 次，返回：{res}"
            )
        time.sleep(retry_delay)
    else:
        raise RuntimeError(f"打开比特浏览器窗口失败，已重试 {max_retries} 次，最后返回：{last_res}")

    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_experimental_option("debuggerAddress", debuggerAddress)

    chrome_service = Service(driverPath)
    driver = webdriver.Chrome(service=chrome_service, options=chrome_options)
    driver.implicitly_wait(10)
    return driver


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


def _select_country(driver, site, shop_name=""):
    if not site:
        return

    country = _get_country_name(site)
    for i in range(3):
        try:
            oepn_country_switch(driver)
            success = force_select_country(driver, country)
            if success:
                print(get_now_time() + shop_name + "成功选择站点:", site)
                return
            print(get_now_time() + shop_name + "选择站点失败:", site)
            time.sleep(10)
        except Exception as e:
            print(get_now_time() + shop_name + "选择站点失败:", site, e)
            time.sleep(10)


def _click_visits_metric(driver):
    selectors = [
        (By.XPATH, "//*[self::button or @role='button' or @role='tab'][contains(., 'Visits')]"),
        (By.XPATH, "//*[normalize-space()='Visits']"),
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

        const visitNode = allNodes(document)
            .find((node) => node.innerText && node.innerText.trim() === 'Visits');
        if (!visitNode) {
            return false;
        }
        visitNode.scrollIntoView({block: 'center', inline: 'center'});
        visitNode.click();
        return true;
        """
    )
    if not clicked:
        raise RuntimeError("没有找到 Visits 入口")
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
                const aVisit = /Visits/i.test(a.text) ? 1 : 0;
                const bVisit = /Visits/i.test(b.text) ? 1 : 0;
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
            if (/visit/i.test(text) && /\\d/.test(text)) {
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
    driver.get("https://global-selling.mercadolibre.com/metrics#sc-menu")
    driver.refresh()
    time.sleep(5)

    # _select_country(driver, site, name)
    _click_visits_metric(driver)
    time.sleep(3)

    visits = _extract_visits_from_network(driver, days)
    if len(visits) < days:
        visits = _extract_visits_from_dom(driver, days)
    if len(visits) < days:
        visits = _extract_visits_by_hover(driver, days)

    if not visits:
        print("没有读取到Visits流量数据，请确认页面已加载并且Visits折线图可见")
        debug_path = Path(__file__).resolve().parent / "visits_debug.png"
        driver.save_screenshot(str(debug_path))
        print("已保存调试截图:", debug_path)

    result = _to_visit_number_list(visits, days)
    print("最近8天Visits流量数据:", result)
    return result


def get_visits_info(driver,window_id, name="", site="", days=8):
    return get_recent_visits_info(driver,window_id, name, site, days)


def get_reputation_info(window_id, name, site, driver=None):
    if driver is None:
        driver = _connect_browser(window_id)

    # 设置最长等待时间为 10 秒
    wait = WebDriverWait(driver, 10)

    driver.get("https://global-selling.mercadolibre.com/reputation")
    time.sleep(10)
    i = 0
    while i < 3:
        i = i + 1
        try:
            WebDriverWait(driver, 10).until(
                EC.visibility_of_element_located((By.CLASS_NAME, "title__page--cbt"))
            )

        except Exception as e:
            print(f"美客多限频，正在第{i}次切换网络")
            switch_random_hongkong_node()
            get_public_ip()

    i = 0
    while i < 3:
        i = i + 1
        try:

            # 打开站点选择器
            oepn_country_switch(driver)
            # 选择站点
            country = ""
            if site == "墨西哥":
                country = "Mexico"
            if site == "巴西":
                country = "Brazil"
            if site == "哥伦比亚":
                country = "Colombia"
            if site == "智利":
                country = "Chile"
            if site == "阿根廷":
                country = "Argentina"
            if site == "乌拉圭":
                country = "Uruguay"
            #
            success = force_select_country(driver, country)
            if success:
                print(get_now_time() + name + "成功选择站点:", site)
                break
            else:
                print(get_now_time() + name + "选择站点失败:", site)
                time.sleep(10)
        except Exception as e:
            print(get_now_time() + name + "选择站点失败:", site)

    # 1. 先定位包含 "Complaints" 文本的父级卡片元素
    # 这里使用 XPath 寻找：包含 h2 且 h2 文本为 Complaints 的那个 div
    card_element = WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                "//div[contains(@class, 'andes-card')][.//h2[text()='Complaints']]",
            )
        )
    )

    # 2. 在这个卡片范围内，寻找类名为 variable__percentage 的元素
    # 注意：使用 card_element.find_element 是在当前节点下查找
    data_complain = (
        WebDriverWait(card_element, 10)
        .until(
            EC.visibility_of_element_located((By.CLASS_NAME, "variable__percentage"))
        )
        .text
    )
    print("提取到的投诉率为:", data_complain)

    # 1. 先定位包含 "Non-compliant shipments" 文本的父级卡片元素
    # 这里使用 XPath 寻找：包含 h2 且 h2 文本为 Non-compliant shipments 的那个 div
    card_element = WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                "//div[contains(@class, 'andes-card')][.//h2[text()='Non-compliant shipments']]",
            )
        )
    )

    # 2. 在这个卡片范围内，寻找类名为 variable__percentage 的元素
    # 注意：使用 card_element.find_element 是在当前节点下查找
    data_delay = (
        WebDriverWait(card_element, 10)
        .until(
            EC.visibility_of_element_located((By.CLASS_NAME, "variable__percentage"))
        )
        .text
    )

    print("提取到的延误率为:", data_delay)

    # 1. 先定位包含 "Cancellations" 文本的父级卡片元素
    # 这里使用 XPath 寻找：包含 h2 且 h2 文本为 Cancellations 的那个 div
    card_element = WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                "//div[contains(@class, 'andes-card')][.//h2[text()='Cancellations']]",
            )
        )
    )

    # 2. 在这个卡片范围内，寻找类名为 variable__percentage 的元素
    # 注意：使用 card_element.find_element 是在当前节点下查找
    data_cancel = (
        WebDriverWait(card_element, 10)
        .until(
            EC.visibility_of_element_located((By.CLASS_NAME, "variable__percentage"))
        )
        .text
    )

    print("提取到的取消率率为:", data_cancel)
    data_color = driver.find_element(By.CLASS_NAME, "thermometer__level").text
    print("账号的声誉为:", data_color)

    data_orders = driver.find_element(By.CLASS_NAME, "value__sales").text
    print("总单数为：", data_orders)

    list = []
    if data_color.__contains__("green"):
        data_color = "绿色"
    if data_color.__contains__("yellow"):
        data_color = "黄色"
    if data_color.__contains__("orange"):
        data_color = "橘色"
    if data_color.__contains__("red"):
        data_color = "红色"
    if data_color.__contains__("You still have no color"):
        data_color = "无色"
    list.append(name)
    list.append(site)
    list.append(data_color)
    list.append(data_orders)
    list.append(data_complain)
    list.append(data_delay)
    list.append(data_cancel)

    driver.get("https://global-selling.mercadolibre.com/sales-summary")
    data_warn = ""
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
    except Exception as e:
        data_warn = "正常"
    print("系统提示为:", data_warn)

    data_gradient = driver.find_element(
        By.CSS_SELECTOR, ".andes-badge .andes-visually-hidden"
    ).text
    if data_gradient.__contains__("Decreased"):
        data_gradient = data_gradient.replace("Decreased", "下滑")
    else:
        data_gradient = data_gradient.replace("Increased", "增长")
    print("近七天变化情况为:", data_gradient)

    list_gradient = data_gradient.split(" ")
    if len(list_gradient) == 2:
        list.append(list_gradient[0])
        list.append(list_gradient[1])
    else:
        list.append(data_gradient)
        list.append(data_gradient)

    list.append(data_warn)
    list.append(get_now_time())

    visits=str(get_visits_info(driver,window_id,"","",8))
    list.append(visits)

    return list


def _build_reputation_failure_row(name, site):
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
        "",
        get_now_time(),
        "",
    ]


def _run_reputation_for_browser(row):
    id = row[0]
    name = row[1]
    remark = row[2]
    if remark == "忽略":
        return [], []

    if not row[3]:
        return [], [("获取声誉信息", name, "", "失败：未配置站点", get_now_time())]

    print(get_now_time() + "开始打开窗口:" + name)
    driver = _connect_browser(id)
    reputation_info_sum = []
    result = []

    try:
        site_list = row[3].split("，")
        for site in site_list:
            site = str(site).strip()
            if not site:
                continue

            for i in range(1, 4):
                try:
                    reputation_info = get_reputation_info(id, name, site, driver=driver)
                    reputation_info_sum.append(reputation_info)
                    print(get_now_time() + name + site + "获取声誉信息成功")
                    result.append(("获取声誉信息", name, site, "成功", get_now_time()))
                    break
                except Exception as e:
                    print(get_now_time() + name + site + "执行失败", e)
                    if i == 3:
                        result.append(
                            ("获取声誉信息", name, site, "失败", get_now_time())
                        )
                        reputation_info_sum.append(
                            _build_reputation_failure_row(name, site)
                        )
                    else:
                        switch_random_hongkong_node()
                        get_public_ip()
                        time.sleep(5)
            time.sleep(10)
    finally:
        print(get_now_time() + "结束，正在关闭窗口")
        try:
            closeBrowser(id)
        except Exception as e:
            print(get_now_time() + name + "关闭窗口失败", e)
        print(get_now_time() + "已经关闭窗口")

    return reputation_info_sum, result


def get_reputation_info_all(max_workers=10):
    start = int(time.time())
    print(start)
    root_path = Path(__file__).resolve().parent
    file_path = root_path / "比特配置文件.xlsx"
    # file_path = root_path / "比特配置文件测试.xlsx"

    wb = load_workbook(file_path)
    sheet = wb.active
    reputation_info_sum = []
    result = []
    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    rows = [row for row in rows if row and row[0] and row[2] != "忽略"]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_run_reputation_for_browser, row): row for row in rows
        }
        for future in as_completed(future_map):
            row = future_map[future]
            name = row[1]
            try:
                browser_reputations, browser_result = future.result()
                reputation_info_sum.extend(browser_reputations)
                result.extend(browser_result)
                print(get_now_time() + name + "窗口任务完成")
            except Exception as e:
                print(get_now_time() + name + "窗口任务异常", e)
                result.append(("获取声誉信息", name, "", "失败", get_now_time()))

    reputation_info_sum_str = "\n".join(map(str, reputation_info_sum))
    print(reputation_info_sum_str)

    end = int(time.time())
    print(get_now_time() + "总花费", end - start)
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
    date_str = datetime.now().strftime("%Y-%m-%d-%H")

    df.to_excel(
        root_path / ("美客多声誉/武汉泽顺店铺声誉信息汇总" + date_str + ".xlsx"),
        index=False,
    )

    send_info(
        "美客多所有店铺声誉汇总",
        "",
        root_path / ("美客多声誉/武汉泽顺店铺声誉信息汇总" + date_str + ".xlsx"),
        r"武汉泽顺店铺声誉信息汇总" + date_str + ".xlsx",
    )
    print(get_now_time() + "发送邮件成功")

    inset_reputation_info(reputation_info_sum)
    insert_task_record(result)


def main():
    return get_reputation_info_all()


if __name__ == "__main__":
    # get_reputation_info('22139511815a4bf588fe96d5fdafded6','四季如春','墨西哥')
    main()
