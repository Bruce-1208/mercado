import time

from pandas.io.formats.format import return_docstring
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.wait import WebDriverWait

from bit_api import *
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
import  pyautogui
from switch_country import *
from openpyxl import load_workbook
from datetime import datetime

from utils import *


def read_email_info_all(driver):

    driver.execute_script("window.open('https://outlook.live.com/mail/0/', '_blank');")

    time.sleep(5)

    # 3. 关键步骤：切换窗口句柄 (Handles)
    # driver.window_handles 是一个列表，[-1] 表示最新打开的窗口
    driver.switch_to.window(driver.window_handles[-1])
    wait = WebDriverWait(driver, 30)

    scraped_data = set()
    # 获取当前所有邮件条目节点
    # 根据你提供的 class: jGG6V 是邮件条目的外层容器
    emails=wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'div[data-animatable="true"] .jGG6V')))
    scraped_data=get_mail_info(driver,'普通邮件')
    ##读取垃圾邮箱
    junk_folder = wait.until(EC.element_to_be_clickable(
        (By.XPATH,
         "//div[contains(@title, '垃圾邮件') or contains(@title, '垃圾郵件') or contains(@title, 'Junk Email')]")
    ))
    junk_folder.click()
    time.sleep(5)  # 等待列表刷新

    print("已进入垃圾邮件箱，开始抓取...")
    emails2=wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'div[data-animatable="true"] .jGG6V')))

    scraped_data2=get_mail_info(driver,'垃圾邮件')


    scraped_data_all=scraped_data2.union(scraped_data)
    sorted_data = sorted(list(scraped_data_all), key=lambda x: x[1],reverse=True)
    # 打印结果
    return  sorted_data

def get_mail_info(driver,text):
    wait = WebDriverWait(driver, 30)
    scraped_data = set()
    time.sleep(5)

    emails=wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'div[data-animatable="true"] .jGG6V')))

    for email in emails:
        try:
            # 1. 提取标题 (根据你的 HTML，标题在 .IjzWp 容器下的 span 中)
            title_element = email.find_element(By.CSS_SELECTOR, '.TtcXM')
            title = title_element.get_attribute('title') or title_element.text

            # 2. 提取时间 (根据你的 HTML，时间在 ._rWRU 类名下)
            time_element = email.find_element(By.CSS_SELECTOR, 'span._rWRU')
            email_time = time_element.get_attribute('title')  # 包含具体日期：周三 2026-04-01 21:11

            # 将数据存入集合
            entry = (title, parse_chinese_date(email_time),email,text)
            if entry not in scraped_data:
                scraped_data.add(entry)

        except Exception as e:
            print(e)
            # 忽略广告节点或未加载完全的节点
            continue
    print("scraped_data数量为",len(scraped_data))
    return scraped_data


def parse_chinese_date(date_str):
    date_str=convert_text(date_str)
    if(date_str.__contains__('上午') or date_str.__contains__('下午')):
        parts = date_str.split()
        date_part = parts[1]  # 2026/4/6
        period = parts[2]  # 下午
        time_part = parts[3]  # 03:06]

        # 2. 初步解析为 datetime 对象 (使用 %I 处理 12 小时制)
        if(int(date_part.split('/')[0])>2025):
            dt = datetime.strptime(f"{date_part} {time_part}", "%Y/%m/%d %I:%M")
        else:
            dt = datetime.strptime(f"{date_part} {time_part}", "%d/%m/%Y %I:%M")

        # 3. 核心逻辑：根据“上午/下午”转换 24 小时制
        if period == "下午" and dt.hour < 12:
            dt = dt.replace(hour=dt.hour + 12)
        elif period == "上午" and dt.hour == 12:
            dt = dt.replace(hour=0)
    else:
        parts = date_str.split()
        date_part = parts[1]  # 2026/4/6
        time_part = parts[2]  # 03:06]
        if (int(date_part.split('/')[0]) > 2025):
            dt = datetime.strptime(f"{date_part} {time_part}", "%Y/%m/%d %I:%M")
        else:
            dt = datetime.strptime(f"{date_part} {time_part}", "%d/%m/%Y %I:%M")
    return dt

if __name__ == '__main__':
    # /browser/open 接口会返回 selenium使用的http地址，以及webdriver的path，直接使用即可
    # res = openBrowser('b2323ff45855401689ab16ed11d4ed20')  # 窗口ID从窗口配置界面中复制，或者api创建后返回
    #跃马扬鞭

    #龙凤呈祥
    res=openBrowser('38fcac77fbf641ed8b6cbc1c2aedc5b2')

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
    email_infos=read_email_info_all(driver)
    print(email_infos)
