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
import traceback
import re



def check_yuanyou_title(number):
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
    wait = WebDriverWait(driver, 5)

    driver.get("https://meli.zying.net/#/product")

    # locator = (By.XPATH, "//span[@class='ant-select-selection-item']//span[contains(text(), '50条/页')]")
    # wait.until(EC.visibility_of_element_located(locator)).click()
    # option_locator = (By.XPATH, "//div[@class='ant-select-item-option-content' and .//span[text()='500条/页']]")
    #
    # wait.until(EC.element_to_be_clickable(option_locator)).click()
    n = 0
    list_response = []
    list_response2 = []
    page = int(number)
    errors=set()
    next_page=1
    while n < page:
        list_title = []
        list_id = []
        n = n + 1
        try:
            page_now = wait.until(EC.visibility_of_element_located((By.CLASS_NAME, "ant-pagination-item-active")))
            page_now=int(page_now.get_attribute("title"))
            print("当前页数为",page_now)
            if (page_now !=next_page):
                for i in range(next_page-page_now):
                    driver.find_element(By.XPATH, "//li[@title='下一页']//button").click()
                    time.sleep(1)
                page_now = wait.until(EC.visibility_of_element_located(
                    (By.CLASS_NAME, "ant-pagination-item-active")))
                page_now = int(page_now.get_attribute("title"))
                print("重新跳转到当前页数为", page_now)

            titles = wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "f12.product-title")))
            isSuccess=True
            for title in titles:
                # time.sleep(0.5)
                title_text = title.text
                print(title_text)
                list_title.append(title_text)
                if title_text  in errors:
                    continue
                title.click()
                id=0
                try:
                    id=wait.until(EC.visibility_of_element_located((By.XPATH,
                                             '/html/body/div[1]/div/div[3]/div/div[2]/div[3]/div/div[1]/div[1]'))).text
                except Exception as e:
                    errors.add(title_text)
                    print("报错")
                    driver.find_element(By.XPATH, "//span[text()='返回首页']").click()
                    print("返回首页")
                    next_page=page_now
                    n=n-1
                    isSuccess=False
                    break
                print(id)
                list_id.append(id)
            if(isSuccess==False):
                continue
            # 下一页
            combined = list(zip(list_title, list_id))
            line = ""
            for i in combined:
                line = line + str(i) + "\n"
            print(line)
            response = get_ai_response(
                line + "这组数据每一行是产品标题和产品编号，韩国品牌IP,日本动漫IP一般不为侵权,帮我找出所有疑似侵权的产品，返回编号给我")
            print("宽松检测侵权产品有:",response)
            response2 = get_ai_response(
                line + "这组数据每一行是产品标题和产品编号，帮我找出所有疑似侵权的产品，返回编号给我")
            print("严格检测侵权产品有:",response2)

            list_response.append(response)
            list_response2.append(response2)
            driver.find_element(By.XPATH, "//li[@title='下一页']//button").click()

            print("点击翻页成功")
            next_page=page_now+1
            time.sleep(5)

            # 在这里加入等待数据加载的逻辑...

        except Exception as e:
            print(e)
            traceback.print_exc()
            print("网页出错")
            # break
    end = int(time.time())
    print("花费时间为:", end - start)
    print("宽松检查要求疑似侵权")
    for i in list_response:
        print(i)
    print("严格检查要求疑似侵权")
    for i in list_response2:
        print(i)
    print("宽松检查要求疑似侵权产品编号")
    get_all_ids(str(list_response))
    print("严格检查要求疑似侵权产品编号")
    get_all_ids(str(list_response2))




def get_all_ids(text):


    # 使用正则表达式匹配 9 位数字
    # \b 表示单词边界，确保不会匹配到 10 位或更多位数中的前 9 位
    product_ids = re.findall(r'\b\d{9}\b', text)

    # # 打印结果，为了方便查看，每行显示一个编号
    # for item in product_ids:
    #     print(item)

    # 如果你需要一个去重后的列表，可以使用 set
    unique_ids = sorted(list(set(product_ids)))
    for id in unique_ids:
        print(id)

if __name__ == '__main__':

    check_yuanyou_title(100)
