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
from mercado_util import *


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
WebDriverWait(driver, 10).until(
    EC.element_to_be_clickable((By.XPATH,
                                "/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[4]/div[3]/div/a"))).click()

#投诉率/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[1]/div[2]/h3
data_complain=driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[1]/div[2]/h3').text

#延误率/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[4]/div[2]/h3
data_delay=driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[4]/div[2]/h3').text

#声誉/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[1]/p
data_color=driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[1]/p').text

#总单数/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[2]/div/div[1]/p[1]
data_orders=driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[2]/div/div[1]/p[1]').text





print(data_complain)
print(data_color)
print(data_delay)
print(data_orders)



