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
from bit.bit_clash import *
from AI_Agent.deepseek import *
from selenium.webdriver.common.action_chains import ActionChains
import pyautogui
import time
from pyautogui import ImageNotFoundException


def click_aoxia_icon_by_image(ICON_PATH):
    # =================Configuration=================
    # 模板图片路径

    # 识别置信度 (0.0 到 1.0)
    # 0.8 是一个均衡的值。如果图标稍微有点透明或背景变了，可以尝试调低到 0.7
    # 必须安装 opencv-python 才能使用此参数
    CONFIDENCE_LEVEL = 0.7

    # 最大等待时间（秒）
    TIMEOUT = 10
    # ===============================================

    print(f"正在屏幕上寻找图标: {ICON_PATH} ...")
    print("请确保目标图标未被编辑器窗口遮挡。")

    start_time = time.time()

    while True:
        try:
            # 1. 尝试在屏幕上查找图标
            # minSearchTime=0.5 允许 PyAutoGUI 在报错前尝试半秒
            location = pyautogui.locateOnScreen(ICON_PATH, confidence=CONFIDENCE_LEVEL)

            if location:
                # 2. 获取图标的中心坐标
                center_point = pyautogui.center(location)
                print(f"✔ 找到图标，坐标: {center_point}")

                # 3. 移动鼠标并点击 (添加稍微的偏移或渐变移动会让操作看起来更自然)
                pyautogui.moveTo(center_point, duration=0.2)  # 0.2秒平滑移动
                pyautogui.click()
                print("✔ 已点击。")
                return True

        except (ImageNotFoundException, TypeError):
            # 未找到图片或返回 None 时继续尝试
            pass

        # 4. 超时检查
        if time.time() - start_time > TIMEOUT:
            print(f"❌ 错误：在 {TIMEOUT} 秒内未在屏幕上找到目标图标。")
            print("请检查：")
            print("1. aoxia_icon.png 截图是否与当前屏幕显示一致。")
            print("2. 目标图标是否被其他窗口遮挡。")
            print("3. Windows系统显示设置中的'缩放与布局'是否为 100%。")
            return False

        time.sleep(0.5)  # 每0.5秒尝试一次，防止 CPU 占用过高


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

driver.get("https://meli.zying.net/#/product")

pics=driver.find_elements(By.CLASS_NAME,"product-pic")

for pic in pics:
    print(pic.get_attribute("src"))

    actions = ActionChains(driver)

    # 3. 移动到图片上以激活图标显示
    actions.move_to_element(pic).perform()

    click_aoxia_icon_by_image(r'C:\Users\Admin\PycharmProjects\MercadoApp\bit\pyauto\img_1.png')
    click_aoxia_icon_by_image(r'C:\Users\Admin\PycharmProjects\MercadoApp\bit\pyauto\img_2.png')
    images=driver.find_elements(By.CLASS_NAME, "product-image")

    for image in images:
        print(image.get_attribute("src"))
    time.sleep(60)

    # # 定位那个带图标的触发按钮
    # trigger_btn =pic.find_element(By.CSS_SELECTOR, "div.cbu-aibuy--dropdown-trigger")
    # trigger_btn.click()
    # time.sleep(60)

    # icon_xpath = "//img[contains(@src, 'ecpkhbhhpfjkkcedaejmpaabpdgcaegc')]"
    # wait = WebDriverWait(driver, 10)
    # plugin_icon = wait.until(EC.visibility_of_element_located((By.XPATH, icon_xpath)))
    # actions.move_to_element(plugin_icon).perform()
    # element =driver.find_element(By.XPATH, "//div[text()='图搜同款']")
    # element.click()
    # pic.click()


    # # 1. 移动到图标 -> 2. 执行右键点击
    # actions.move_to_element(pic).context_click().perform()
    # for _ in range(7):
    #     actions.send_keys(Keys.ARROW_DOWN).perform()
    #     time.sleep(0.2)
    # actions.send_keys(Keys.ENTER).perform()
