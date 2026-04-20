"""
# 适用环境python3
"""
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from pydantic.v1.datetime_parse import parse_date
from selenium.webdriver.chrome.service import Service

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import random

from bit.utils import get_latest_modified_file, get_bit_path, parser_delay_date, get_now_time
from bit_api import *
from AI_Agent.qianwen import *
import pandas as pd
from datetime import datetime, timedelta
from datetime import datetime
from AI_Agent.deepseek import *
import re
from openpyxl import load_workbook


def use_one_browser_run_task(info):
    # /browser/open 接口会返回 selenium使用的http地址，以及webdriver的path，直接使用即可
    name = info[0]
    site = info[1]
    form = info[2]

    try:
        ip_usable = True
        if ip_usable:
            while True:
                print("ip检测通过，打开店铺平台主页")

                try:
                    shensu(name, site, form, "")
                except Exception as e:
                    traceback.print_exc()
                    print("申诉执行异常", e)
                finally:
                    time.sleep(1800)
                    continue

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

    driverPath = res['data']['driver']
    debuggerAddress = res['data']['http']

    # selenium 连接代码
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_experimental_option("debuggerAddress", debuggerAddress)

    chrome_service = Service(driverPath)
    driver = webdriver.Chrome(service=chrome_service, options=chrome_options)

    driver.implicitly_wait(10)
    # 设置最长等待时间为 10 秒
    wait = WebDriverWait(driver, 30)
    driver.get("https://global-selling.mercadolibre.com/help/chat/v3?parent_skill=MLCX")
    words = []
    if (form == "延误"):
        words = [
            '亲爱的客服，我叫Bruce！这些订单因合作物流车辆临时出现故障，导致未能及时揽收，并非我这边发货延误，麻烦您帮忙处理一下，消除对店铺声誉的影响，非常感谢！',
            '亲爱的客服，我叫Bruce！这些订单因为菜鸟，并非我这边发货延误，麻烦您帮忙处理一下，消除对店铺声誉的影响，非常感谢！'
        ]
    if (form == "侵权"):
        words = ['亲爱的客服，我叫Bruce！这些产品是通用品牌产品，他们被系统误检测为侵权产品，你能帮我消除记录吗？']

    if (form == "投诉"):
        print("正在进行申诉")

    words_random = random.choice(words)

    i = 0
    while (i < 3):
        i = i + 1
        try:
            # 打开站点选择器
            WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CLASS_NAME, "nav-header-cbt__site-switcher"))).click()

            print(name + "打开站点选择器")
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
                EC.element_to_be_clickable((By.CSS_SELECTOR, path))).click()

            driver.refresh()
            time.sleep(3)
            print(get_now_time() + name + site + "选择站点：", site)
            break
        except Exception as e:
            print(get_now_time() + name + site + "重新执行选择站点")
            time.sleep(10)
            continue

    orders_random = get_delay_orders_random(name, site, 10)
    infraction_random = get_infraction_orders_random(name, site, 5)
    try:
        print(get_now_time() + name + site + "开始打开listing选项卡寻找客服")
        # 跳转listing
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH,
                                        "//p[text()='Listings']"))).click()

        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH,
                                        "//p[text()='Issues while listing or modifying a product']"))).click()

        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH,
                                        "//p[text()='Choose how to stay in contact']"))).click()

        # 包含We will send you a message in less than 进入人工客服
        try:
            WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//button[contains(., 'We will send you a message in less than')]"))).click()
        except Exception as e:
            print(get_now_time() + name + site + "没有人工客服")
            return None
        # 发消息
        print("进入人工客服")

        if (message == ""):

            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.XPATH,
                                                "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
                words_random)
            time.sleep(3)
            WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))).click()
            print("自动发送:" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), words_random)
            time.sleep(3)

            if (form == "延误"):
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.XPATH,
                                                    "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
                    orders_random)
                time.sleep(3)
                WebDriverWait(driver, 30).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))).click()
                print(get_now_time() + name + site + "发送延误订单:", orders_random)
                chat_ai(driver)
            if (form == "侵权"):
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.XPATH,
                                                    "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
                    infraction_random)
                time.sleep(3)
                WebDriverWait(driver, 30).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))).click()
                print(get_now_time() + name + site + "发送侵权的id:", infraction_random)
                chat_ai(driver, form)
        else:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.XPATH,
                                                "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
                message)
            time.sleep(3)
            WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))).click()
            print("自动发送:" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), words_random)
            chat_ai(driver, form)



    except Exception as e:
        print(get_now_time() + name + site + "继续与客服对话")
        # 全部聊天记录
        chat_ai(driver, form)


def get_delay_orders_random(name, site, nums):
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
    return order_random


def get_infraction_orders_random(name, site, nums):
    delay_folder_path = get_bit_path() / "美客多侵权"
    delay_file = get_latest_modified_file(delay_folder_path)
    delay_file_path = delay_folder_path / delay_file
    fifteen_days_ago = datetime.now() - timedelta(days=15)
    inf_list = []
    df = pd.read_excel(delay_file_path, engine='openpyxl')
    for index, row in df.iterrows():
        # print(row)
        line_name = row['店铺名']
        line_site = row['站点']
        id = row['编号']
        if (name == line_name and site == line_site):
            inf_list.append(id)

    if len(inf_list) >= nums:
        inf_list = str(random.sample(inf_list, nums))
    else:
        inf_list = str(inf_list)
    print(get_now_time() + name + site + "随机得到的侵权单号为", inf_list)
    return inf_list


def chat_ai(driver, form):
    i = 0
    while (i < 5):
        i = i + 1
        messages = driver.find_elements(By.CLASS_NAME, 'chat-ui-message-bubble.chat-ui-message-bubble--from-agent')
        lines = ""
        for message in messages:
            print(message.text)
            lines = lines + message.text + "\n"
        response = ""
        if (form == "延误"):
            words = lines + "|这是我跟美客多客服的对话，我叫Bruce，我正在找他申诉我延误的订单，麻烦你帮我用不超过二十个字的自然语言回复他，如果你理解他拒绝了我的申请，麻烦返回：好的，我明白了,感谢您的回复"
            response = get_ai_response(words)
            print("AI客服回复", response)
        if (form == "侵权"):
            words = lines + "|这是我跟美客多客服的对话，我叫Bruce，我正在找他申诉我侵权的商品，帮我想话术让客服相信这不是侵权产品,麻烦你帮我用不超过二十个字的自然语言回复他，如果你理解他拒绝了我的申请，麻烦返回：好的，我明白了,感谢您的回复"
            response = get_ai_response(words)
            print("AI客服回复", response)
        if (form == "投诉"):
            words = lines + "|这是我跟美客多客服的对话，我叫Bruce，我正在给他我被投诉的订单号，帮我想办法让这些订单不影响我的声誉，麻烦你帮我用不超过二十个字的自然语言回复他，如果你理解他拒绝了我的申请，麻烦返回：好的，我明白了,感谢您的回复"
            response = get_ai_response(words)
            print("AI客服回复", response)
        try:
            # 发消息
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.XPATH,
                                                "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
                response)
            time.sleep(3)
            WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))).click()
            print("自动发送:" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), response)
        except Exception as e:
            print("聊天结束，发送失败")
            ###把聊天记录发到数据库里面去

        if (response.__contains__("好的，我明白了,感谢您的回复") or i == 5):
            print("结束当前聊天窗口")
            # 关闭页面
            WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/header/div/div[2]/button"))).click()

            time.sleep(3)
            WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.XPATH, "//span[text()='Understood']"))).click()

            break

        time.sleep(180)


def chat_script(driver):
    return None


def use_all_browser_run_task(browser_list):
    """
    循环打开所有店铺运行脚本
    :param browser_list: 店铺列表
    """
    for browser in browser_list:
        use_one_browser_run_task(browser)


def use_all_browser_run_task_with_thread_pool(browser_list, max_threads=3):
    """
    使用线程池控制最大并发线程数
    :param browser_list: 店铺列表
    :param max_threads: 最大并发线程数
    """
    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        executor.map(use_one_browser_run_task, browser_list)


if __name__ == '__main__':
    # long
    # use_one_browser_run_task('9812f185f7ab49d98f3988994d9e8ebf','墨西哥')
    # 跃马扬鞭
    use_one_browser_run_task(('vngbjkk', '墨西哥', '侵权'))
    browser_list = [
        ('龙马精神', '阿根廷', '延误'),
        ('跃马扬鞭', '阿根廷', '延误'),
        ('德德智慧', '墨西哥', '延误')

    ]
    use_all_browser_run_task_with_thread_pool(browser_list)
