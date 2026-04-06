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


def read_email_info_all(window_id):
    res = openBrowser(window_id)  # 窗口ID从窗口配置界面中复制，或者api创建后返回

    print(res)

    driverPath = res['data']['driver']
    debuggerAddress = res['data']['http']

    # selenium 连接代码
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_experimental_option("debuggerAddress", debuggerAddress)

    chrome_service = Service(driverPath)
    driver = webdriver.Chrome(service=chrome_service, options=chrome_options)
    wait = WebDriverWait(driver, 15)

    driver.implicitly_wait(10)
    driver.execute_script("window.open('https://outlook.live.com/mail/0/', '_blank');")

    time.sleep(3)

    # 3. 关键步骤：切换窗口句柄 (Handles)
    # driver.window_handles 是一个列表，[-1] 表示最新打开的窗口
    driver.switch_to.window(driver.window_handles[-1])
    scraped_data = set()
    # 获取当前所有邮件条目节点
    # 根据你提供的 class: jGG6V 是邮件条目的外层容器

    emails = driver.find_elements(By.CSS_SELECTOR, 'div[data-animatable="true"] .jGG6V')
    scraped_data=get_mail_info(driver)

    ##读取垃圾邮箱
    junk_folder = wait.until(EC.element_to_be_clickable(
        (By.XPATH, "//div[contains(@title, '垃圾邮件') or [contains(@title, '垃圾郵件') or contains(@title, 'Junk Email')]")
    ))
    junk_folder.click()
    time.sleep(3)  # 等待列表刷新

    print("已进入垃圾邮件箱，开始抓取...")
    emails2 = driver.find_elements(By.CSS_SELECTOR, 'div[data-animatable="true"] .jGG6V')
    scraped_data2=get_mail_info(driver)

    print(scraped_data.union(scraped_data2))

def get_mail_info(driver):
    scraped_data = set()
    emails = driver.find_elements(By.CSS_SELECTOR, 'div[data-animatable="true"] .jGG6V')

    for email in emails:
        try:
            # 1. 提取标题 (根据你的 HTML，标题在 .IjzWp 容器下的 span 中)
            title_element = email.find_element(By.CSS_SELECTOR, 'div.IjzWp span')
            title = title_element.get_attribute('title') or title_element.text

            # 2. 提取时间 (根据你的 HTML，时间在 ._rWRU 类名下)
            time_element = email.find_element(By.CSS_SELECTOR, 'span._rWRU')
            email_time = time_element.get_attribute('title')  # 包含具体日期：周三 2026-04-01 21:11

            # 将数据存入集合
            entry = (title, email_time)
            if entry not in scraped_data:
                scraped_data.add(entry)
                print(f"标题: {title} | 时间: {email_time}")

        except Exception as e:
            # 忽略广告节点或未加载完全的节点
            continue
    return scraped_data

if __name__ == '__main__':
    #vngbjkk
    read_email_info_all('1495e31cb630406bb690ba187f264fe7')
    # 跃马扬鞭
    # read_email_info_all('187700d9c3424c0eb6d8a75d92bf3b9c')
    #一跃千里
    read_email_info_all('b2323ff45855401689ab16ed11d4ed20')