import time

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.wait import WebDriverWait

from bit.bit_utils import get_now_time
from bit_api import *
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
import pyautogui
from switch_country import *
from openpyxl import load_workbook
from send_mail import *
import pandas as pd

from datetime import datetime
from pathlib import Path
from bit_mysql import *
from bit_clash import *


def get_reputation_info(window_id, name, site):
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
    wait = WebDriverWait(driver, 10)

    driver.get("https://global-selling.mercadolibre.com/reputation")
    time.sleep(10)
    i=0
    while (i<3):
        i=i+1
        try:
            WebDriverWait(driver, 10).until(
                EC.visibility_of_element_located(
                    (By.CLASS_NAME,"title__page--cbt"))
            )

        except Exception as e:
            print(f"美客多限频，正在第{i}次切换网络")
            switch_random_hongkong_node()
            get_public_ip()

    i = 0
    while (i < 3):
        i = i + 1
        try:

            # 打开站点选择器
            oepn_country_switch(driver)
            # 选择站点
            country = ''
            if site == '墨西哥':
                country = 'Mexico'
            if site == '巴西':
                country = 'Brazil'
            if site == '哥伦比亚':
                country = 'Colombia'
            if site == '智利':
                country = 'Chile'
            if site == '阿根廷':
                country = 'Argentina'
            if site == '乌拉圭':
                country = 'Uruguay'
            #
            success=force_select_country(driver, country)
            if(success):
                print(get_now_time() + name+'成功选择站点:', site)
                break
            else:
                print(get_now_time() + name+'选择站点失败:', site)
                time.sleep(10)
        except Exception as e:
            print(get_now_time() + name+'选择站点失败:', site)


    # 1. 先定位包含 "Complaints" 文本的父级卡片元素
    # 这里使用 XPath 寻找：包含 h2 且 h2 文本为 Complaints 的那个 div
    card_element = WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located(
            (By.XPATH, "//div[contains(@class, 'andes-card')][.//h2[text()='Complaints']]"))
    )

    # 2. 在这个卡片范围内，寻找类名为 variable__percentage 的元素
    # 注意：使用 card_element.find_element 是在当前节点下查找
    data_complain = WebDriverWait(card_element, 10).until(
        EC.visibility_of_element_located(
            (By.CLASS_NAME, "variable__percentage"))
    ).text
    print("提取到的投诉率为:", data_complain)

    # 1. 先定位包含 "Complaints" 文本的父级卡片元素
    # 这里使用 XPath 寻找：包含 h2 且 h2 文本为 Complaints 的那个 div
    card_element = WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located(
            (By.XPATH, "//div[contains(@class, 'andes-card')][.//h2[text()='Non-compliant shipments']]"))
    )

    # 2. 在这个卡片范围内，寻找类名为 variable__percentage 的元素
    # 注意：使用 card_element.find_element 是在当前节点下查找
    data_delay = WebDriverWait(card_element, 10).until(
        EC.visibility_of_element_located(
            (By.CLASS_NAME, "variable__percentage"))
    ).text


    print("提取到的延误率为:", data_delay)
    data_color = driver.find_element(By.CLASS_NAME, 'thermometer__level').text
    print("账号的声誉为:", data_color)

    data_orders = driver.find_element(By.CLASS_NAME, 'value__sales').text
    print("总单数为：", data_orders)

    list = []
    if (data_color.__contains__("green")):
        data_color = '绿色'
    if (data_color.__contains__("yellow")):
        data_color = '黄色'
    if (data_color.__contains__("orange")):
        data_color = '橘色'
    if (data_color.__contains__("red")):
        data_color = '红色'
    if (data_color.__contains__("You still have no color")):
        data_color = '无色'
    list.append(name)
    list.append(site)
    list.append(data_color)
    list.append(data_orders)
    list.append(data_complain)
    list.append(data_delay)


    driver.get("https://global-selling.mercadolibre.com/sales-summary")
    data_warn = ""
    try:
        data_warn = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located(
                (By.CLASS_NAME, "andes-message__content"))
        ).text
    except Exception as e:
        data_warn = "正常"
    print("系统提示为:", data_warn)

    data_gradient = driver.find_element(By.CSS_SELECTOR, ".andes-badge .andes-visually-hidden").text
    if (data_gradient.__contains__("Decreased")):
        data_gradient = data_gradient.replace("Decreased", "下滑")
    else:
        data_gradient = data_gradient.replace("Increased", "增长")
    print("近七天变化情况为:", data_gradient)

    list_gradient=data_gradient.split(" ")
    if(len(list_gradient)==2):
        list.append(list_gradient[0])
        list.append(list_gradient[1])
    else:
        list.append(data_gradient)
        list.append(data_gradient)

    list.append(data_warn)
    list.append(get_now_time())

    return list


def get_reputation_info_all():
    start = int(time.time())
    print(start)
    root_path = Path(__file__).resolve().parent
    file_path = root_path / "比特配置文件.xlsx"
    # file_path = root_path / "比特配置文件测试.xlsx"

    wb = load_workbook(file_path)
    sheet = wb.active
    reputation_info_sum = []
    result = []
    # 使用 min_row=2 跳过第一行
    for row in sheet.iter_rows(min_row=2, values_only=True):
        print(row)  # row 是一个元组，包含该行所有数据
        id = row[0]
        name = row[1]
        remark = row[2]
        if remark == '忽略':
            continue
        print(get_now_time() + "开始打开窗口:" + name)
        site_list = row[3].split("，")
        for site in site_list:
            i = 0
            while (i < 3):
                i = i + 1
                try:
                    reputation_info = get_reputation_info(id, name, site)
                    reputation_info_sum.append(reputation_info)
                    print(get_now_time() + name + site + "获取声誉信息成功")
                    result.append(('获取声誉信息',name,site,"成功",get_now_time()))
                    break
                except Exception as e:
                    print(get_now_time() + name + site + "执行失败", e)
                    if(i==3):
                        result.append(('获取声誉信息',name,site,"失败",get_now_time()))
                        reputation_info=[name,site,"执行失败","","","","","","",get_now_time()]
                        reputation_info_sum.append(reputation_info)
                    # 随机切换香港IP节点
                    switch_random_hongkong_node()
                    get_public_ip()
            time.sleep(30)

        print(get_now_time() + "结束，正在关闭窗口")

        try:
            closeBrowser(id)
        except Exception as e:
            continue
        print(get_now_time() + "已经关闭窗口")

    reputation_info_sum_str = "\n".join(map(str, reputation_info_sum))
    print(reputation_info_sum_str)

    end = int(time.time())
    print(get_now_time() + "总花费", end - start)
    df = pd.DataFrame(reputation_info_sum,
                      columns=['店铺名', '站点', '声誉颜色', '总单量', '投诉率', '延误率', '增加或减少','近七天变化率',
                               '系统告警','更新时间'])

    now = datetime.now()
    date_str = datetime.now().strftime("%Y-%m-%d-%H")

    df.to_excel(root_path / ("美客多声誉/武汉泽顺店铺声誉信息汇总" + date_str + ".xlsx"), index=False)

    send_info('美客多所有店铺声誉汇总', "",
              root_path / ("美客多声誉/武汉泽顺店铺声誉信息汇总" + date_str + ".xlsx"),
              r"武汉泽顺店铺声誉信息汇总" + date_str + ".xlsx")
    print(get_now_time() + "发送邮件成功")

    inset_reputation_info(reputation_info_sum)
    insert_task_record(result)

if __name__ == '__main__':

    # get_reputation_info('22139511815a4bf588fe96d5fdafded6','四季如春','阿根廷')
    get_reputation_info_all()
