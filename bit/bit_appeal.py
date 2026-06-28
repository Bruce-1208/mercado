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
                    shensu(name, site, form, message,"人工客服")
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


# 申诉
def shensu(name, site, form, message, mode="人工客服"):
    print(f"{name} {site} 开始进行{form}申诉，话术为{message}<br>")
    config_path = get_bit_path() / "比特配置文件.xlsx"
    wb = load_workbook(config_path)
    sheet = wb.active
    config_info = []

    reuslt = []
    # 使用 min_row=2 跳过第一行
    window_id = ""
    for row in sheet.iter_rows(min_row=2, values_only=True):
        window_id = row[0]
        window_name = row[1]
        if window_name == name:
            break

    res = openBrowser(window_id)  # 窗口ID从窗口配置界面中复制，或者api创建后返回

    print(res)
    name = res["data"]["name"]

    driverPath = res["data"]["driver"]
    debuggerAddress = res["data"]["http"]

    # selenium 连接代码
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_experimental_option("debuggerAddress", debuggerAddress)

    chrome_service = Service(driverPath)
    driver = webdriver.Chrome(service=chrome_service, options=chrome_options)

    driver.implicitly_wait(10)
    # 设置最长等待时间为 10 秒
    wait = WebDriverWait(driver, 15)

    # driver.switch_to.new_window('tab') 决定是否打开新窗口
    driver.get("https://global-selling.mercadolibre.com/help/hub/30928?source")
    i = 0
    while i < 3:
        i = i + 1
        try:
            WebDriverWait(driver, 10).until(
                EC.visibility_of_element_located(
                    (By.XPATH, "//a[text()='Mercado Libre International Selling']")
                )
            )
        except Exception as e:
            print(f"{name} {site}美客多限频，正在第{i}次切换网络<br>")
            switch_random_hongkong_node()
            get_public_ip()

    words = []
    nickname_list = ["Bruce", "Jack", "Lucy", "James"]
    nickname = random.choice(nickname_list)
    if form == "延误":
        words = [
            f"亲爱的客服，我叫{nickname}！这些订单因合作物流车辆临时出现故障，导致未能及时揽收，并非我这边发货延误，麻烦您帮忙处理一下，消除对店铺声誉的影响，非常感谢！",
            f"亲爱的客服，我叫{nickname}！这些订单因为菜鸟，并非我这边发货延误，麻烦您帮忙处理一下，消除对店铺声誉的影响，非常感谢！",
        ]

    if form == "侵权":
        words = [
            f"亲爱的客服，我叫{nickname}！这些产品是通用品牌产品，他们被系统误检测为侵权产品，你能帮我消除记录吗？",
            f"亲爱的客服，我叫{nickname}！这些产品是通用品牌产品，他们被系统误检测为侵权产品，你能帮我消除记录吗？",
        ]

    if form == "投诉":
        words = [
            f"亲爱的客服，我叫{nickname}！我的产品没有任何质量问题，客户没有给出确凿的证据证明他出了问题，我认为客户是想免费购物，你能消除对我声誉的影响吗"
        ]

    words_random = random.choice(words)

    i = 0
    while i < 3:
        i = i + 1
        try:
            # 打开站点选择器
            WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CLASS_NAME, "nav-header-cbt__site-switcher")
                )
            ).click()

            print(f"{get_now_time()} {name} {site} '打开站点选择器'<br>")
            time.sleep(5)
            path = 'div[data-value="MLM-remote"]'
            if site == "墨西哥":
                path = 'div[data-value="MLM-remote"]'
            if site == "巴西":
                path = 'div[data-value="MLB-remote"]'
            if site == "哥伦比亚":
                path = 'div[data-value="MCO-remote"]'
            if site == "智利":
                path = 'div[data-value="MLC-remote"]'
            if site == "阿根廷":
                path = 'div[data-value="MLA-remote"]'
            if site == "乌拉圭":
                path = 'div[data-value="MLU-remote"]'

            WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, path))
            ).click()

            driver.refresh()
            time.sleep(3)
            print(f"{get_now_time()} {name} {site} '选择站点成功'<br>")
            break
        except Exception as e:
            print(f"{get_now_time()} {name} {site} '重新执行选择站点'<br>")
            time.sleep(10)
            continue
    orders_random=""
    infraction_random=""

    if(form=="延误"):
        orders_random = get_delay_orders_random(name, site, 10)
        if (orders_random == "" and message == ""):
            return "没有可以申诉的订单"
    if (form == "侵权"):
        infraction_random = get_infraction_orders_random(window_id,name, site, 10)
    driver.get("https://global-selling.mercadolibre.com/help/hub/30928?source")
    try:

        WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//button[contains(., 'We’ll send you a message in less than 5 min')]"))).click()

        # 发消息
        print(f"{get_now_time()} {name} {site} '进入人工客服'<br>")

        if message == "":

            if form == "延误":
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.XPATH, "//div[@aria-placeholder='Write your question or problem']"))).send_keys(
                    orders_random+words_random)
                time.sleep(3)
                WebDriverWait(driver, 30).until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, 'button[title="Send"]')
                    )
                ).click()
                print(
                    f"{get_now_time()} {name}  {site} 发送延误订单：{orders_random}{words_random}<br>"
                )
                chat_ai(
                    driver, name, site, form, orders_random + words_random, nickname
                )
            if form == "侵权":
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.XPATH, "//div[@aria-placeholder='Write your question or problem']"))).send_keys(
                    infraction_random+words_random)
                time.sleep(3)
                WebDriverWait(driver, 30).until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, 'button[title="Send"]')
                    )
                ).click()
                print(
                    f"{get_now_time()} {name} {site} '发送侵权的 id：{infraction_random}{words_random}<br>"
                )
                chat_ai(
                    driver, name, site, form, infraction_random + words_random, nickname
                )
        else:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p",
                    )
                )
            ).send_keys(message)
            time.sleep(3)
            WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))
            ).click()
            print(f"{get_now_time()} {name} {site} 自动发送自定义话术：{message}<br>")
            chat_ai(
                driver, name, site, form, infraction_random + words_random, nickname
            )

    except Exception as e:
        driver.get("https://global-selling.mercadolibre.com/help/chat/v2")
        print(get_now_time() + name + site + "继续与客服对话")
        # 全部聊天记录
        chat_ai(driver, name, site, form, infraction_random + words_random, nickname)
    finally:
        print(f"{get_now_time()} {name}{site}找客服执行完毕<br>")
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
        infos=get_infractions_info(window_id,name,site,1)
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
        target_element = wait.until(
            EC.visibility_of_element_located(
                (By.XPATH, "//p[contains(text(), 'This chat has ended')]")
            )
        )
        print(f"{get_now_time()} {name}{site}聊天已经结束,结束AI找客服<br>")
        return True
    except Exception as e:
        return False
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
            messages = driver.find_elements(
                By.CLASS_NAME,
                "chat-ui-message-bubble.chat-ui-message-bubble--from-agent",
            )

            for mes in messages:
                print(mes.text)
                lines = lines + mes.text + "\n"
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
                WebDriverWait(driver, 30).until(

                    EC.presence_of_element_located((By.XPATH, "//div[@aria-placeholder='Write your question or problem']"))).send_keys(
                    response)
                time.sleep(3)
                WebDriverWait(driver, 30).until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, 'button[title="Send"]')
                    )
                ).click()
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
                # 关闭页面
                WebDriverWait(driver, 30).until(
                    EC.element_to_be_clickable(
                        (
                            By.XPATH,
                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/header/div/div[2]/button",
                        )
                    )
                ).click()

                time.sleep(3)
                WebDriverWait(driver, 30).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//span[text()='Understood']")
                    )
                ).click()
                print(f"{get_now_time()} {name}{site}点击关闭聊天窗口<br>")
                break
        except Exception as e:
            print(e)
            traceback.print_exc()
        finally:
            print(f"{get_now_time()} {name}{site}等待三分钟<br>")
            time.sleep(180)

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


def main():
    # 在这里指定要运行的店铺参数，不使用命令行传参。
    shop_name = "跃马扬鞭"
    site = "墨西哥"
    appeal_type = "侵权"
    message = ""

    use_one_browser_run_task((shop_name, site, appeal_type, message))



if __name__ == "__main__":
    main()
