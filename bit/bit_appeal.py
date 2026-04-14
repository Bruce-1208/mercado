"""
# 适用环境python3
"""

import traceback
from concurrent.futures import ThreadPoolExecutor
from selenium.webdriver.chrome.service import Service

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import random
from bit_api import *



def use_one_browser_run_task(window_id):
    # /browser/open 接口会返回 selenium使用的http地址，以及webdriver的path，直接使用即可
    res = openBrowser(window_id)  # 窗口ID从窗口配置界面中复制，或者api创建后返回

    print(res)

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
        driver.implicitly_wait(60)
        ip_usable = True
        if ip_usable:
            while True:
                print("ip检测通过，打开店铺平台主页")
                #找客服页面
                url="https://global-selling.mercadolibre.com"
                open_launcher_page(driver, url)
                # 打开店铺平台主页后进行后续自动化操作
                # todo 后续的自动化操作y
                try:
                    shensu_ai(driver)
                    # shensu(browser,driver)
                    time.sleep(1800)
                except Exception as e:
                    print("申诉执行异常",e)
                finally:
                    time.sleep(1800)
                    continue

        else:
            print("ip检测不通过，请检查")
    except:
        print("脚本运行异常:" + traceback.format_exc())
    finally:
        driver.quit()
        print(f"=====关闭店铺：{store_name}=====")
        close_store(store_id)

def shensu_ai(driver):
    driver.get("https://global-selling.mercadolibre.com/help/v2")
    driver.switch_to.frame("Meli AI Chat")
    messages=driver.find_elements(By.CLASS_NAME,"mlc-scroll-paginate_item")
    print(messages)

#申诉
def shensu(driver):
    driver.get("https://global-selling.mercadolibre.com/help/chat/v3?parent_skill=MLCX")

    words=[
        '亲爱的客服，我叫Jack，因为菜鸟没有及时揽收我的物流，对我店铺声誉造成了影响，我总结了下面这些订单，你能帮我消除对我声誉的影响吗？',

        '亲爱的客服，我叫Mike，因为菜鸟没有及时揽收我的物流，对我店铺声誉造成了影响，我总结了下面这些订单，你能帮我消除对我声誉的影响吗？',
        '亲爱的客服，我叫Bruce！这些订单因合作物流车辆临时出现故障，导致未能及时揽收，并非我这边发货延误，麻烦您帮忙处理一下，消除对店铺声誉的影响，非常感谢！'

    ]
    sheet_name = browser.get("browserName")


    print("sheet_name--------------------------",sheet_name)
    order_list=get_orders.get_order_number_excel(sheet_name,r'C:\Users\Admin\PycharmProjects\MercadoApp\订单延误.xlsx')
    order_random=""
    if len(order_list) >= 10:
        order_random = str(random.sample(order_list, 10))
    else:
        order_random=str(order_list)

    words_random=random.choice(words)


    #打开站点选择/html/body/header/div/div/div[2]/div[1]/spany
    WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.XPATH,
                                        "/html/body/header/div/div/div[2]/div[1]/span"))).click()
    time.sleep(3)
    #选择站点/html/body/header/div/div/div[2]/div[2]/div/div[1]
    site_xpth=""
    if site == "墨西哥":
        id=str(1+isFull)
        site_xpth="/html/body/header/div/div/div[2]/div[2]/div/div["+id+"]"
    if site == "巴西":
        id = str(2 + isFull)
        site_xpth = "/html/body/header/div/div/div[2]/div[2]/div/div[" + id + "]"
    if site == "阿根廷":
        id = str(3 + isFull)
        site_xpth = "/html/body/header/div/div/div[2]/div[2]/div/div[" + id + "]"
    if site == "智利":
        id = str(4 + isFull)
        site_xpth = "/html/body/header/div/div/div[2]/div[2]/div/div[" + id + "]"
    if site == "哥伦比亚":
        id = str(5 + isFull)
        site_xpth = "/html/body/header/div/div/div[2]/div[2]/div/div[" + id + "]"
    if site == "乌拉圭":
        id = str(6 + isFull)
        site_xpth = "/html/body/header/div/div/div[2]/div[2]/div/div[" + id + "]"

    WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.XPATH,
                                        site_xpth))).click()
    time.sleep(3)
    driver.refresh()
    time.sleep(3)
    print("选择站点：",site)
    try:
        #跳转listing
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH, "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[1]/div[3]/div/div/div[2]/div[1]/div/div/div[2]/ul/li[1]/button"))).click()

        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[1]/div[5]/div/div/div[2]/div[1]/div/div/div[2]/ul/li[1]/button/p"))).click()
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[1]/div[7]/div/div/div[2]/div[1]/div/div/div[2]/ul/li/button/p"))).click()
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH,
                                            "/html/body/div[2]/div/div/div[2]/div/div/div/div/section/div/div/div/main/button[1]"))).click()
        time.sleep(3)


    except Exception as  e:
        print(e,"进入客服对话异常")
    try:
        # 发消息
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
            words_random)
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[2]/div[3]/div/button/span"))).click()
        print("!!!!!"+time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),words_random)
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
            order_random)
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH,
                                        "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[2]/div[3]/div/button/span"))).click()

        time.sleep(60)
        print("!!!!!"+time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),order_random)

        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p"))).send_keys(
            "亲爱的客服，没关系，我会耐心等您的，希望你能带来给我好运")
        time.sleep(3)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH,
                                        "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/div[2]/div/div[2]/div/div/div[2]/div[3]/div/button/span"))).click()
        time.sleep(1200)
        print("!!!!!"+time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),"亲爱的客服，没关系，我会耐心等您的，希望你能带来给我好运")
        # 关闭页面
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH,
                                            "/html/body/main/div/div[2]/div/div/div/div[4]/div/div/div/div/div/div/header/div/div[2]/button"))).click()

        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH,
                                            "/html/body/div[2]/div/div/div[2]/div[3]/div/button/span"))).click()
    except Exception as e:
        print(e,"=====发送消息异常=====")

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
