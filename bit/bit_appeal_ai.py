"""
# 适用环境python3
"""

import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests
from pydantic.v1.datetime_parse import parse_date
from selenium.webdriver.chrome.service import Service

from selenium import webdriver
from selenium.webdriver.common.by import By
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


CHAT_INFO_API_URL = "https://zeshun.nat100.top/api/v1/chat"
HELP_URL = "https://global-selling.mercadolibre.com/help"

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
    frame_selectors = [
        (By.XPATH, "//iframe[contains(@title, 'Meli AI Chat') or contains(@name, 'Meli AI Chat')]"),
        (By.XPATH, "//iframe[contains(@src, 'chat') or contains(@src, 'help')]"),
    ]
    for by, selector in frame_selectors:
        frames = driver.find_elements(by, selector)
        for frame in frames:
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
                if find_chat_input(driver, timeout=3):
                    return True
            except Exception:
                continue

    driver.switch_to.default_content()
    return find_chat_input(driver, timeout=3) is not None


def find_chat_input(driver, timeout=30):
    input_selectors = [
        (By.XPATH, "//div[@aria-placeholder='Write your question or problem']"),
        (By.XPATH, "//div[@contenteditable='true']"),
        (By.CSS_SELECTOR, "textarea"),
        (By.CSS_SELECTOR, "input[type='text']"),
    ]
    end_time = time.time() + timeout
    while time.time() < end_time:
        for by, selector in input_selectors:
            elements = driver.find_elements(by, selector)
            for element in elements:
                try:
                    if element.is_displayed() and element.is_enabled():
                        return element
                except Exception:
                    continue
        time.sleep(0.5)
    return None


def click_send_button(driver):
    send_selectors = [
        (By.CSS_SELECTOR, 'button[title="Send"]'),
        (By.XPATH, "//button[contains(., 'Send')]"),
        (By.XPATH, "//button[@type='submit']"),
    ]
    for by, selector in send_selectors:
        buttons = driver.find_elements(by, selector)
        for button in buttons:
            try:
                if button.is_displayed() and button.is_enabled():
                    button.click()
                    return True
            except Exception:
                continue
    return False


def send_ai_chat_message(driver, message):
    if not switch_to_ai_chat_frame(driver):
        raise RuntimeError("没有找到 AI 客服聊天窗口")

    input_box = find_chat_input(driver)
    if input_box is None:
        raise RuntimeError("没有找到 AI 客服输入框")

    input_box.click()
    input_box.send_keys(message)
    time.sleep(1)
    if not click_send_button(driver):
        input_box.send_keys(Keys.ENTER)
    time.sleep(3)


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


def open_ai_contact_window(driver, name, site):
    driver.get(HELP_URL)
    time.sleep(8)
    if not click_contact_us(driver, name, site):
        raise RuntimeError("没有找到页面底部 Contact us")

    WebDriverWait(driver, 30).until(lambda d: switch_to_ai_chat_frame(d))
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
                    time.sleep(1800)

        else:
            print("ip检测不通过，请检查")
    except:
        print("脚本运行异常:" + traceback.format_exc())


def shensu_ai(driver):
    driver.get("https://global-selling.mercadolibre.com/help/v2")
    driver.switch_to.frame("Meli AI Chat")
    messages = driver.find_elements(By.CLASS_NAME, "mlc-scroll-paginate_item")
    print(messages)


# 申诉
def shensu(name, site, form, message):
    print(f"{name} {site} 开始进行{form}申诉，话术为{message}<br>")
    window_id = get_window_id_by_shop_name(name)
    driver, res = connect_bit_browser(window_id)
    name = res["data"]["name"]

    nickname_list = ["Bruce", "Jack", "Lucy", "James"]
    nickname = random.choice(nickname_list)

    try:
        driver.get(HELP_URL)
        time.sleep(8)
        select_site(driver, name, site)
        huashu = build_appeal_message(window_id, name, site, form, message, nickname)
        if huashu == "":
            print(f"{get_now_time()} {name} {site} 没有可以申诉的数据<br>")
            return "没有可以申诉的数据"

        open_ai_contact_window(driver, name, site)
        send_ai_chat_message(driver, huashu)
        print(f"{get_now_time()} {name} {site} 自动发送AI客服申诉话术：{huashu}<br>")
        chat_ai(driver, name, site, form, huashu, nickname)
    except Exception as e:
        print(f"{get_now_time()} {name} {site} AI客服申诉执行失败<br>")
        print(e)
        traceback.print_exc()
    finally:
        print(f"{get_now_time()} {name}{site}AI客服申诉执行完毕<br>")
        print(f"{get_now_time()} {name}{site} 关闭浏览器<br>")


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
        print("获取延误表格信息失败",e)
    return order_random


def get_infraction_orders_random(window_id,name, site, nums):
    inf_list=[]
    try:
        infos=get_infractions_info(window_id,name,site)
        for i in infos:
            inf_list.append(i[2])
        if len(inf_list) >= nums:
            inf_list = str(random.sample(inf_list, nums))
        else:
            inf_list = str(inf_list)
        print(get_now_time() + name + site + "随机得到的侵权单号为", inf_list)
    except Exception as e:
        print("获取侵权订单信息失败",e)
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


def close_ai_chat_if_needed(driver, name, site):
    switch_to_ai_chat_frame(driver)
    close_selectors = [
        (By.CSS_SELECTOR, "button[aria-label='Close']"),
        (By.CSS_SELECTOR, "button[title='Close']"),
        (By.XPATH, "//button[contains(., 'Close')]"),
        (By.XPATH, "//button[contains(., 'Understood')]"),
    ]
    for by, selector in close_selectors:
        buttons = driver.find_elements(by, selector)
        for button in buttons:
            try:
                if button.is_displayed() and button.is_enabled():
                    button.click()
                    print(f"{get_now_time()} {name}{site}点击关闭聊天窗口<br>")
                    return True
            except Exception:
                continue
    return False


def chat_ai(driver, name, site, form, huashu, nickname):
    i = 0
    chat_rerord = set()
    chat_list = []
    while i < 5:
        i = i + 1
        lines = ""
        response = ""
        isEnd = checkChatEnd(driver, name, site)
        if isEnd:
            break

        try:
            print(
                f"{get_now_time()} {name}{site}+'进入人工客服处理流程，循环回复第{i}次'<br>"
            )
            messages = get_agent_messages(driver)

            for mes in messages:
                print(mes)
                lines = lines + mes + "\n"
            if lines == "":
                print(f"{get_now_time()} {name}{site}AI客服暂未回复，继续等待<br>")
                continue
            if lines in chat_rerord:
                print(f"{get_now_time()} {name}{site}+'客服已经至少三分钟没有回复'<br>")
                # 客服没有回消息，不用再次回复他
                continue

            chat_rerord.add(lines)

            if form == "延误":
                words = (
                    lines
                    + f"|这是我跟美客多客服的对话，我叫{nickname}，我正在找他申诉我延误的订单，麻烦你帮我用不超过三十个字的自然语言回复他，如果你理解他拒绝了我的申请，麻烦返回：好的，我明白了,感谢您的回复"
                )
                response = get_ai_response(words)
                print(f"{get_now_time()} {name}{site}AI回复:{response}<br>")
            if form == "侵权":
                words = (
                    lines
                    + f"|这是我跟美客多客服的对话，我叫{nickname}，我正在找他申诉我侵权的商品，帮我想话术让客服相信这不是侵权产品,麻烦你帮我用不超过三十个字的自然语言回复他，如果你理解他拒绝了我的申请，麻烦返回：好的，我明白了,感谢您的回复"
                )
                response = get_ai_response(words)
                print(f"{get_now_time()} {name}{site}AI回复:{response}<br>")
            if form == "投诉":
                words = (
                    lines
                    + f"|这是我跟美客多客服的对话，我叫{nickname}，我正在给他我被投诉的订单号，帮我想办法让这些订单不影响我的声誉，麻烦你帮我用不超过三十个字的自然语言回复他，如果你理解他拒绝了我的申请，麻烦返回：好的，我明白了,感谢您的回复"
                )
                response = get_ai_response(words)
                print(f"{get_now_time()} {name}{site}AI回复:{response}<br>")
            try:
                # 发消息
                send_ai_chat_message(driver, response)
                print(f"{get_now_time()} {name}{site}自动发送消息:{response}<br>")
                # 聊天记录插入数据库
                result = insert_chat_info_by_api(name, site, huashu, lines, response, get_now_time())
                print(f"{get_now_time()} {name}{site}聊天记录接口入库成功:{result}<br>")

            except Exception as e:
                print(f"{get_now_time()} {name}{site}发送消息失败<br>")
                print(e)
                traceback.print_exc()

            if response.__contains__("好的，我明白了,感谢您的回复") or i == 5:
                print(f"{get_now_time()} {name}{site}客服拒绝，点击结束聊天<br>")
                close_ai_chat_if_needed(driver, name, site)
                break
        except Exception as e:
            print(e)
            traceback.print_exc()
        finally:
            print(f"{get_now_time()} {name}{site}等待1分钟<br>")
            time.sleep(60)

    print(f"{get_now_time()} {name}{site}结束AI客服回复<br>")


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


if __name__ == "__main__":
    # long
    # use_one_browser_run_task('9812f185f7ab49d98f3988994d9e8ebf','墨西哥')
    # 跃马扬鞭
    # use_one_browser_run_task(('跃马扬鞭', '墨西哥', '侵权','MLM2872391307 - MLM2872380671 - MLM5204725168 - MLM5199341964 - MLM2870050527 - MLM2870047371 - MLM2870043695 - MLM5199197738 - MLM5199251620 - MLM4811240116 亲爱的客服，这些产品是通用品牌产品，他们被系统误判为侵权，你能帮我重新激活并且恢复我的账户吗？'))
    use_one_browser_run_task(('vngbjkk','墨西哥','侵权',''))
    browser_list = [
        (
            "龙",
            "阿根廷",
            "延误",
            "2000015835896308, 2000015760415040, 2000015657210554, 2000015755669242, 2000015413354104亲爱的客服，这几个产品是菜鸟没有及时揽收造成了延误，你能帮我取消对我声誉的影响吗？",
        ),
        (
            "飞黄腾达5",
            "阿根廷",
            "投诉",
            "#2000012217587531 亲爱的客服，我的产品如描述一致，客户并没有证据证明我的产品有问题，中介把钱判给了我，你能帮我消除对我声誉的影响吗",
        ),
        (
            "鸿运当头",
            "墨西哥",
            "投诉",
            "2000012334909743 亲爱的客服，我的产品如描述一致，客户并没有证据证明我的产品有问题，是他自己不会使用，你能帮我消除对我声誉的影响吗",
        ),
        (
            "飞黄腾达5",
            "巴西",
            "投诉",
            "#2000012373200625 亲爱的客服，我的产品如描述一致数量没错，客户并没有证据证明我的产品有问题，明显是想免费购物，你能帮我消除对我声誉的影响吗",
        ),
        (
            "腾",
            "墨西哥",
            "延误",
            "2000015674360964、2000015591983456、2000015552663062、2000015371004100、2000015370997788 ，2000015237834384亲爱的客服，这几个产品是菜鸟没有及时揽收造成了延误，你能帮我取消对我声誉的影响吗？",
        ),
        (
            "梁山好汉666",
            "墨西哥",
            "延误",
            """'2000015974674620
'2000015974297176
'2000015961496590
'2000015956536118
'2000015944080040
'2000015930028184
'2000015902497014
'2000015852788368亲爱的客服，这几个产品是菜鸟没有及时揽收造成了延误，你能帮我取消对我声誉的影响吗？
""",
        ),
    ]
    # use_all_browser_run_task_with_thread_pool(browser_list)

    # auto_appeal_delay()
