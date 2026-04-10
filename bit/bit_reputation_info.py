import time

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
import pyautogui
from switch_country import *
from openpyxl import load_workbook
from send_mail import *
import pandas as pd

from datetime import datetime


def get_reputation_info(window_id, site):
    # /browser/open 接口会返回 selenium使用的http地址，以及webdriver的path，直接使用即可
    # vngbjkk
    # window_id="1495e31cb630406bb690ba187f264fe7"
    # 龙
    # window_id = '9812f185f7ab49d98f3988994d9e8ebf'
    #一跃千里
    # window_id='b2323ff45855401689ab16ed11d4ed20'
    #龙争虎斗
    # window_id='df2d33b20d0b4d72949fc490f7ff075a'
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
    driver.refresh()
    time.sleep(5)

    # 这段 JS 脚本会自动寻找页面上所有隐藏的 Shadow DOM 并在其中搜索目标
    deep_click_script = """
    function findAndClick(root, selector) {
        // 1. 在当前层级寻找
        const node = root.querySelector(selector);
        if (node) {
            node.click();
            return true;
        }

        // 2. 递归寻找所有子节点的 Shadow DOM
        const allNodes = root.querySelectorAll('*');
        for (let i = 0; i < allNodes.length; i++) {
            if (allNodes[i].shadowRoot) {
                if (findAndClick(allNodes[i].shadowRoot, selector)) {
                    return true;
                }
            }
        }
        return false;
    }
    return findAndClick(document, 'button[aria-label="Select country"]');
    """
    # 打开选择器
    success = driver.execute_script(deep_click_script)
    # 选择站点
    name = ''
    if site == '墨西哥':
        name = 'Mexico'
    if site == '巴西':
        name = 'Brazil'
    if site == '哥伦比亚':
        name = 'Colombia'
    if site == '智利':
        name = 'Chile'
    if site == '阿根廷':
        name = 'Argentina'
    if site == '乌拉圭':
        name = 'Uruguay'
    #
    force_select_country(driver, name)
    print('成功选择站点:',site)

    time.sleep(3)

    # 1. 先定位包含 "Complaints" 文本的父级卡片元素
    # 这里使用 XPath 寻找：包含 h2 且 h2 文本为 Complaints 的那个 div
    card_element = driver.find_element(By.XPATH, "//div[contains(@class, 'andes-card')][.//h2[text()='Complaints']]")

    # 2. 在这个卡片范围内，寻找类名为 variable__percentage 的元素
    # 注意：使用 card_element.find_element 是在当前节点下查找
    data_complain= card_element.find_element(By.CLASS_NAME, "variable__percentage").text


    print(f"提取到的投诉率为: {data_complain}")

    # 1. 先定位包含 "Complaints" 文本的父级卡片元素
    # 这里使用 XPath 寻找：包含 h2 且 h2 文本为 Complaints 的那个 div
    card_element = driver.find_element(By.XPATH, "//div[contains(@class, 'andes-card')][.//h2[text()='Non-compliant shipments']]")

    # 2. 在这个卡片范围内，寻找类名为 variable__percentage 的元素
    # 注意：使用 card_element.find_element 是在当前节点下查找
    data_delay = card_element.find_element(By.CLASS_NAME, "variable__percentage").text

    print(f"提取到的延误率为: {data_delay}")
    data_color=driver.find_element(By.CLASS_NAME,'thermometer__level').text
    print("账号的声誉为:",data_color)

    data_orders=driver.find_element(By.CLASS_NAME,'value__sales').text
    print("总单数为：",data_orders)

    list=[]
    if(data_color.__contains__("green")):
        data_color='绿色'
    if (data_color.__contains__("yellow")):
        data_color = '黄色'
    if (data_color.__contains__("orange")):
        data_color = '橘色'
    if (data_color.__contains__("red")):
        data_color = '红色'
    if(data_color.__contains__("You still have no color")):
        data_color = '无色'

    list.append(data_color)
    list.append(data_orders)
    list.append(data_complain)
    list.append(data_delay)

    return list

    # closeBrowser(window_id)



if __name__ == '__main__':

    # get_reputation_info('df2d33b20d0b4d72949fc490f7ff075a','墨西哥')
    # time.sleep(10000)

    start=int(time.time())
    print(start)
    wb = load_workbook(r'D:\比特配置文件.xlsx')
    sheet = wb.active
    reputation_info_sum=[]
    # 使用 min_row=2 跳过第一行
    for row in sheet.iter_rows(min_row=2, values_only=True):
        print(row)  # row 是一个元组，包含该行所有数据
        id=row[0]
        name = row[1]
        remark= row[2]
        if remark=='忽略':
            continue
        print("开始打开窗口:",name)
        site_list=row[3].split("，")
        for site in site_list:
            try:
                reputation_info=get_reputation_info(id,site)
                reputation_info.append(name)
                reputation_info.append(site)
                print(reputation_info)
                reputation_info_sum.append(reputation_info)
            except Exception as e:
                print("窗口"+name+"执行失败")
                print(e)
                time.sleep(300)
                try:
                    reputation_info = get_reputation_info(id, site)
                    reputation_info.append(name)
                    reputation_info.append(site)
                    print(reputation_info)
                    reputation_info_sum.append(reputation_info)
                    print("窗口" + name + site+"重试成功")
                except Exception as e:
                    print("窗口" + name + site+"重试失败")
                    reputation_info_sum.append([name,site,"读取窗口失败"])
            time.sleep(5)
        print("结束，正在关闭窗口")
        # closeBrowser(id)
        print("已经关闭窗口")
        time.sleep(5)
    # for info in reputation_info_sum:
    #     print(info)
    result = "\n".join(map(str, reputation_info_sum))
    print(result)

    end=int(time.time())
    print("总花费",end-start)
    df = pd.DataFrame(reputation_info_sum, columns=['声誉颜色', '总单量', '投诉率', '延误率', '店铺名', '站点'])
    now=datetime.now()
    date_str=datetime.now().strftime("%Y-%m-%d-%H")
    df.to_excel(r"D:\美客多声誉\武汉泽顺店铺声誉信息汇总"+date_str+".xlsx", index=False)

    send_reputation_info('美客多所有店铺声誉汇总',result,r"D:\美客多声誉\武汉泽顺店铺声誉信息汇总"+date_str+".xlsx",r"武汉泽顺店铺声誉信息汇总"+date_str+".xlsx")

