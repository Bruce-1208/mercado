import time

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from bit_api import *
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By


# /browser/open 接口会返回 selenium使用的http地址，以及webdriver的path，直接使用即可
res = openBrowser("1495e31cb630406bb690ba187f264fe7") # 窗口ID从窗口配置界面中复制，或者api创建后返回

print(res)

driverPath = res['data']['driver']
debuggerAddress = res['data']['http']

# selenium 连接代码
chrome_options = webdriver.ChromeOptions()
chrome_options.add_experimental_option("debuggerAddress", debuggerAddress)

chrome_service = Service(driverPath)
driver = webdriver.Chrome(service=chrome_service, options=chrome_options)


url='https://global-selling.mercadolibre.com/reputation?reportId=1592911493_1774447895774&reportType=handling_time'


driver.implicitly_wait(10)
driver.get(url)

time.sleep(10)

driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[2]/div/div/button/span/span').click()

