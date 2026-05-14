import time

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
from bit.bit_mysql import *
from bit.bit_clash import *
from AI_Agent.deepseek import *


def check_yuanyou_title():
    start = int(time.time())
    res = openBrowser("1495e31cb630406bb690ba187f264fe7")
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
    wait = WebDriverWait(driver, 10)

    driver.get("https://www.erpyuanyou.com/#/collects/list")

    # locator = (By.XPATH, "//span[@class='ant-select-selection-item']//span[contains(text(), '50条/页')]")
    # wait.until(EC.visibility_of_element_located(locator)).click()
    # option_locator = (By.XPATH, "//div[@class='ant-select-item-option-content' and .//span[text()='500条/页']]")
    #
    # wait.until(EC.element_to_be_clickable(option_locator)).click()
    i=0
    list_title=[]
    list_id=[]
    while i<10:
        i=i+1
        try:
            titles = wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "product-title")))
            ids= wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "id-link")))

            for title in titles:
                list_title.append(title.text)
            for id in ids:
                list_id.append(id.text)

            # 等待“下一页”按钮出现并可以点击，最长等 5 秒
            next_page = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'li[title="下一页"]'))
            )
            next_page.click()
            time.sleep(5)
            print("点击翻页成功")

            # 在这里加入等待数据加载的逻辑...

        except TimeoutException:
            print("没有下一页了，抓取结束。")
            break
    combined = list(zip(list_title, list_id))
    line=""
    for i in combined:
        line=line+str(i)+"\n"
    print(line)
    print(get_ai_response(line+"这组数据每一行是产品标题和产品编号，韩国品牌IP,日本动漫IP一般不为侵权,帮我找出所有疑似侵权的产品，返回编号给我"))
    end = int(time.time())
    print("检查采集列表总数量为:",len(combined))
    print("花费时间为:",end-start)



if __name__ == '__main__':
    check_yuanyou_title()
