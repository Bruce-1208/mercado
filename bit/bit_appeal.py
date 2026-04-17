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

from bit.utils import get_latest_modified_file, get_bit_path, parser_delay_date
from bit_api import *
from AI_Agent.qianwen import *
import pandas as pd
from datetime import datetime, timedelta
from datetime import datetime


def use_one_browser_run_task(window_id, site):
    # /browser/open 接口会返回 selenium使用的http地址，以及webdriver的path，直接使用即可
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

    try:
        # 设置元素查找等待时间-全局隐式等待
        driver.implicitly_wait(20)
        ip_usable = True
        if ip_usable:
            while True:
                print("ip检测通过，打开店铺平台主页")
                # 找客服页面
                # 打开店铺平台主页后进行后续自动化操作
                # todo 后续的自动化操作y
                try:
                    # shensu_ai(driver)
                    shensu(driver, name, site)
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
    finally:
        driver.quit()
        print(f"=====关闭店铺=====")
        closeBrowser(window_id)


def shensu_ai(driver):
    driver.get("https://global-selling.mercadolibre.com/help/v2")
    driver.switch_to.frame("Meli AI Chat")
    messages = driver.find_elements(By.CLASS_NAME, "mlc-scroll-paginate_item")
    print(messages)


# 申诉
def shensu(driver, name, site):
    driver.get("https://global-selling.mercadolibre.com/help/chat/v3?parent_skill=MLCX")

    words = [
        '亲爱的客服，我叫Jack，因为菜鸟没有及时揽收我的物流，对我店铺声誉造成了影响，我总结了下面这些订单，你能帮我消除对我声誉的影响吗？',

        '亲爱的客服，我叫Mike，因为菜鸟没有及时揽收我的物流，对我店铺声誉造成了影响，我总结了下面这些订单，你能帮我消除对我声誉的影响吗？',
        '亲爱的客服，我叫Bruce！这些订单因合作物流车辆临时出现故障，导致未能及时揽收，并非我这边发货延误，麻烦您帮忙处理一下，消除对店铺声誉的影响，非常感谢！'

    ]


    words_random = random.choice(words)

    # 打开站点选择器
    WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.CLASS_NAME, "nav-header-cbt__site-switcher"))).click()

    print("打开站点选择器")
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
    print("选择站点：", site)

    orders_random = get_delay_orders_random(name, site, 10)

    try:

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
            print("没有人工客服")
            chat_ai(driver)
        # 发消息
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
            words_random)
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))).click()
        print("自动发送:" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), words_random)
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
            orders_random)
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))).click()

        time.sleep(60)
        print("自动发送:" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), orders_random)


    except Exception as e:
        print("正在客服对话中")
        # 全部聊天记录
        chat_ai(driver)

    chat_ai(driver)
    try:
        # 发消息
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
            words_random)
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))).click()
        print("自动发送:" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), words_random)
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
            order_random)
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))).click()

        chat_ai(driver)

        # time.sleep(60)
        # print("自动发送:" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), order_random)
        #
        # WebDriverWait(driver, 30).until(
        #     EC.presence_of_element_located((By.XPATH,
        #                                     "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
        #     "亲爱的客服，没关系，我会耐心等您的，希望你能带来给我好运")
        # time.sleep(3)
        # WebDriverWait(driver, 30).until(
        #     EC.element_to_be_clickable((By.XPATH,
        #                                 "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[2]/div[3]/div/button/span"))).click()
        # time.sleep(1200)
        # print("自动发送:" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        #       "亲爱的客服，没关系，我会耐心等您的，希望你能带来给我好运")

    except Exception as e:
        print(e, "=====发送消息异常=====")


def get_delay_orders_random(name, site, nums):
    delay_folder_path = get_bit_path() / "美客多延误"
    delay_file = get_latest_modified_file(delay_folder_path)
    delay_file_path = delay_folder_path / delay_file
    fifteen_days_ago = datetime.now() - timedelta(days=15)
    order_list = []
    df = pd.read_excel(delay_file_path, engine='openpyxl')
    for index, row in df.iterrows():
        # print(row)
        line_name=row['店铺']
        line_site=row['站点']
        order_date=row['下单时间']
        order_num = row['销售单号']
        dispatch_date=row['实际揽收时间']
        if (line_name == name and line_site == site and dispatch_date != "Not yet dispatched"):
            order_date = parser_delay_date(order_date)
            if (order_date > fifteen_days_ago):
                order_list.append(order_num)
    print(name + site + "最近15天的延误个数:", len(order_list))
    if len(order_list) >= nums:
        order_random = str(random.sample(order_list, nums))
    else:
        order_random = str(order_list)
    order_random_line = ""
    for i in order_random:
        order_random_line = order_random_line + i + ","
    return order_random_line


def chat_ai(driver):
    i = 0
    while (i < 5):
        i = i + 1
        messages = driver.find_elements(By.CLASS_NAME, 'chat-ui-message-bubble.chat-ui-message-bubble--from-agent')
        lines = ""
        for message in messages:
            print(message.text)
            lines = lines + message.text + "\n"
        words = "|这是我跟美客多客服的对话，帮我继续回答他的话，如果他表达拒绝的态度，你回复 '好的，我明白了,感谢您的回复' 即可"
        response = get_ai_response(words)
        print("AI客服回复", response)
        # 发消息
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
            response)
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))).click()
        print("自动发送:" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), response)
        if (response == "好的，我明白了,感谢您的回复" or i==5):
            print("结束当前聊天窗口")
            # 关闭页面
            WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/header/div/div[2]/button"))).click()

            WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[title="Send"]'))).click()
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
    use_one_browser_run_task('187700d9c3424c0eb6d8a75d92bf3b9c', '墨西哥')
