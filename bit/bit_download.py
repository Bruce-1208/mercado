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
# window_id="1495e31cb630406bb690ba187f264fe7"
#龙
window_id='9812f185f7ab49d98f3988994d9e8ebf'
res = openBrowser(window_id) # 窗口ID从窗口配置界面中复制，或者api创建后返回

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

site='阿根廷'

# 定位 class 为 andes-dropdown__trigger 且拥有特定 aria-label 的按钮
selector = "button.andes-dropdown__trigger[aria-label='Select country']"
element = driver.find_element(By.CSS_SELECTOR, selector)
element.click()

isFull=0
if site=='墨西哥':
    site_xpath='/html/body/main/div/div[3]/div/div[1]/div/div[2]/div/section//div/div/div/div/div/div/ul/li['+str(isFull+1)+']'
if site=='巴西':
    site_xpath='/html/body/main/div/div[3]/div/div[1]/div/div[2]/div/section//div/div/div/div/div/div/ul/li['+str(isFull+2)+']'
if site=='哥伦比亚':
    site_xpath='/html/body/main/div/div[3]/div/div[1]/div/div[2]/div/section//div/div/div/div/div/div/ul/li['+str(isFull+3)+']'
if site=='智利':
    site_xpath='/html/body/main/div/div[3]/div/div[1]/div/div[2]/div/section//div/div/div/div/div/div/ul/li['+str(isFull+4)+']'
if site=='阿根廷':
    site_xpath='/html/body/main/div/div[3]/div/div[1]/div/div[2]/div/section//div/div/div/div/div/div/ul/li['+str(isFull+5)+']'
if site=='乌拉圭':
    site_xpath='/html/body/main/div/div[3]/div/div[1]/div/div[2]/div/section//div/div/div/div/div/div/ul/li['+str(isFull+6)+']'
WebDriverWait(driver, 10).until(
    EC.element_to_be_clickable((By.XPATH,
                                site_xpath))).click()


#点击下载邮件/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[3]/div[4]/div[3]/div/a
try:
    WebDriverWait(driver, 10).until(
    EC.element_to_be_clickable((By.XPATH,
                                "/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[4]/div[3]/div/a"))).click()
except Exception as e:
    WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.XPATH,
                                    "/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[3]/div[4]/div[3]/div/a"))).click()
#投诉率/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[1]/div[2]/h3
#或者/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[3]/div[1]/div[2]/h3
data_complain=''
try:
    data_complain=driver.find_element(By.XPATH, '/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[1]/div[2]/h3').text
except Exception as e:
    data_complain=driver.find_element(By.XPATH, '/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[3]/div[1]/div[2]/h3').text

#延误率/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[4]/div[2]/h3
data_delay=''
try:
    data_delay=driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[4]/div[2]/h3').text
except Exception as e:
    data_delay = driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[3]/div[4]/div[2]/h3').text

#声誉/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[1]/p
data_color=''
try:
    data_color=driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[1]/p').text
except Exception as e:
    print(e)

#总单数/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[2]/div/div[1]/p[1]
data_orders=''
try:
    data_orders=driver.find_element(By.XPATH,'/html/body/main/div/div[3]/div/div[1]/div/div[4]/div/div[2]/div/div[1]/p[1]').text
except Exception as e:
    print(e)

#邮件搜索/html/body/div[1]/div/div[1]/div/div[1]/div[2]/div/div/div/div/div/div[1]/div[2]/div/div/div/div/div[1]/div/div[2]/div/input


print(data_complain)
print(data_color)
print(data_delay)
print(data_orders)



time.sleep(30)


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
mail_items1 = driver.find_elements(By.CLASS_NAME, "TtcXM")
print(f"当前可见邮件数量: {len(mail_items1)}")

#点击垃圾邮件/html/body/div[1]/div/div[3]/div/div[2]/div[2]/div/div[1]/div[1]/div/div/div[1]/div/div/div/div/div[1]/div[2]/div/div[2]/div/div/div[2]/div/span[1]
driver.find_element(By.XPATH,'/html/body/div[1]/div/div[3]/div/div[2]/div[2]/div/div[1]/div[1]/div/div/div[1]/div/div/div/div/div[1]/div[2]/div/div[2]/div/div/div[2]/div/span[1]').click()
mail_items2 = driver.find_elements(By.CLASS_NAME, "TtcXM")
print(f"当前可见垃圾邮件数量: {len(mail_items2)}")

mail_items=mail_items2+mail_items1


print(f"当前可见所有邮件数量: {len(mail_items)}")



# 遍历并处理
for item in mail_items:
    # 可以在这里进一步查找标题或点击
    print(item.text)

    if item.text=='Your orders that you shipped with delay report is ready':
        item.click()
        #Go to download report
        # driver.find_element(By.XPATH,'/html/body/div[1]/div/div[2]/div/div[2]/div[2]/div/div[1]/div[1]/div/div/div[3]/div/div/div[3]/div/div/div/div[1]/div/div/div[2]/div/div[2]/div[1]/div/div/div/div/div/div[3]/div[1]/div/div/div/div/div/div/table/tbody/tr/td/table[2]/tbody/tr/td/table/tbody/tr[2]/td/table/tbody/tr[1]/td[2]/table/tbody/tr/td/table/tbody/tr/td/table/tbody/tr[2]/td/table/tbody/tr/td/table/tbody/tr/td/a').click()

        downlod_url=driver.find_element("link text", "Go to download report").get_attribute("href")

        print(downlod_url)
        driver.get(downlod_url)
        # wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'download report')]")))

        # driver.switch_to.window(driver.window_handles[-1]) #切换窗口
        time.sleep(5)
        ##下载文件
        driver.find_element(By.XPATH,
                            '/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[2]/div[2]/div/div/button/span/span').click()

        # # 或者：直接用 JS 点击包含特定文本的 Andes 按钮
        # target_text = "Download"
        # # JS 逻辑：找到所有类名为 TtcXM 或 andes-button__content 的元素，匹配文本并点击
        # js_script = "document.querySelectorAll('.andes-button__content')[1].click();"
        # driver.execute_script(js_script)
        print("已下载延误文件")
        time.sleep(100)


        break

#其他邮件/html/body/div[1]/div/div[3]/div/div[2]/div[2]/div/div[1]/div[1]/div/div/div[1]/div/div/div/div/div[1]/div[2]/div/div[2]/div/div/div[2]/div/span[1]
closeBrowser(window_id)