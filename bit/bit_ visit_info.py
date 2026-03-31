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


def get_visits_info(window_id, site):
    # /browser/open 接口会返回 selenium使用的http地址，以及webdriver的path，直接使用即可
    # vngbjkk
    # window_id="1495e31cb630406bb690ba187f264fe7"
    # 龙
    window_id = '9812f185f7ab49d98f3988994d9e8ebf'
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

    driver.get("https://global-selling.mercadolibre.com/metrics#sc-menu")
    driver.refresh()
    time.sleep(5)

    site = '阿根廷'
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
    print('成功选择站点')


    try:
        ##点击流量窗口
        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH,
                                        "/html/body/main/div/div/div[3]/div/div/div[3]/section/div[2]/div[2]/div[2]/div[1]/div/div/div/div[4]"))).click()
    except Exception as e:
        print(e)



    #其他邮件/html/body/div[1]/div/div[3]/div/div[2]/div[2]/div/div[1]/div[1]/div/div/div[1]/div/div/div/div/div[1]/div[2]/div/div[2]/div/div/div[2]/div/span[1]
    closeBrowser(window_id)

if __name__ == '__main__':
    get_visits_info('','')