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
import  pyautogui



# /browser/open 接口会返回 selenium使用的http地址，以及webdriver的path，直接使用即可
#vngbjkk
window_id="1495e31cb630406bb690ba187f264fe7"
res = openBrowser(window_id) # 窗口ID从窗口配置界面中复制，或者api创建后返回

print(res)

driverPath = res['data']['driver']
debuggerAddress = res['data']['http']

# selenium 连接代码
chrome_options = webdriver.ChromeOptions()
chrome_options.add_experimental_option("debuggerAddress", debuggerAddress)

chrome_service = Service(driverPath)
driver = webdriver.Chrome(service=chrome_service, options=chrome_options)

driver.implicitly_wait(60)

driver.get("https://global-selling.mercadolibre.com/reputation")
#点击下载
# WebDriverWait(driver, 10).until(
#     EC.element_to_be_clickable((By.XPATH,
#                                 "/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[4]/div[3]/div/a"))).click()

#投诉率/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[1]/div[2]/h3
data_complain=driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[1]/div[2]/h3').text

#延误率/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[4]/div[2]/h3
data_delay=driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[4]/div[2]/h3').text

#声誉/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[1]/p
data_color=driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[1]/p').text

#总单数/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[2]/div/div[1]/p[1]
data_orders=driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[2]/div/div[1]/p[1]').text


#邮件搜索/html/body/div[1]/div/div[1]/div/div[1]/div[2]/div/div/div/div/div/div[1]/div[2]/div/div/div/div/div[1]/div/div[2]/div/input


print(data_complain)
print(data_color)
print(data_delay)
print(data_orders)


driver.execute_script("window.open('https://outlook.live.com/mail/0/', '_blank');")

# 3. 关键步骤：切换窗口句柄 (Handles)
# driver.window_handles 是一个列表，[-1] 表示最新打开的窗口
driver.switch_to.window(driver.window_handles[-1])

#邮件搜索/html/body/div[1]/div/div[1]/div/div[1]/div[2]/div/div/div/div/div/div[1]/div[2]/div/div/div/div/div[1]/div/div[2]/div/input

# # 找到所有符合条件的元素
# elements = driver.find_elements(By.CSS_SELECTOR, '[style*="overflow-anchor: none"]')
#
# print(f"共找到 {len(elements)} 个匹配元素")
#
# # 遍历并打印这些元素的标签名或文本
# for index, el in enumerate(elements):
#     print(f"元素 {index}: 标签={el.tag_name}, ID={el.get_attribute('id')}")


# 找到当前页面所有 class 为 TtcXM 的元素
mail_items = driver.find_elements(By.CLASS_NAME, "TtcXM")

print(f"当前可见邮件数量: {len(mail_items)}")

# 遍历并处理
for item in mail_items:
    # 可以在这里进一步查找标题或点击
    print(item.text)

    if item.text=='Your orders that you shipped with delay report is ready':
        item.click()
        driver.find_element(By.XPATH,'/html/body/div[1]/div/div[2]/div/div[2]/div[2]/div/div[1]/div[1]/div/div/div[3]/div/div/div[3]/div/div/div/div[1]/div/div/div[2]/div/div[2]/div[1]/div/div/div/div/div/div[3]/div[1]/div/div/div/div/div/div/table/tbody/tr/td/table[2]/tbody/tr/td/table/tbody/tr[2]/td/table/tbody/tr[1]/td[2]/table/tbody/tr/td/table/tbody/tr/td/table/tbody/tr[2]/td/table/tbody/tr/td/table/tbody/tr/td/a').click()
        time.sleep(10)
        # 或者：直接用 JS 点击包含特定文本的 Andes 按钮
        target_text = "Download"
        # JS 逻辑：找到所有类名为 TtcXM 或 andes-button__content 的元素，匹配文本并点击
        js_script = "document.querySelectorAll('.andes-button__content')[1].click();"
        driver.execute_script(js_script)
        print("已下载延误文件")


        break


