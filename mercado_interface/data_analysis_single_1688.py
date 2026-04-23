import time
import os
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
import sys
import logging
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By


# 启用调试日志
# logging.basicConfig(level=logging.DEBUG)


###selenium获取单个商品的属性信息

def scrape_1688_single(url):
    print("正在初始化Chrome选项...")
    # 设置Chrome选项
    options = uc.ChromeOptions()

    # 获取当前脚本所在目录的绝对路径
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # 指定用户数据目录
    chrome_profile = os.path.join(current_dir, "chrome_profile")

    # 确保目录存在
    if not os.path.exists(chrome_profile):
        print(f"创建用户数据目录: {chrome_profile}")
        os.makedirs(chrome_profile)
    else:
        print(f"使用已存在的用户数据目录: {chrome_profile}")

    options.add_argument(f'--user-data-dir={chrome_profile}')

    # 如需使用无头模式，可取消下面一行注释
    # options.headless = True
    # 添加更多反爬虫配置
    options.add_argument('--disable-blink-features=AutomationControlled')  # 关闭自动化标记
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1920,1080')
    options.add_argument(
        'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 ')
    options.add_argument('--user-data-dir=C:\\Users\\Admin\\AppData\\Local\\Google\\Chrome\\User Data\\')
    options.add_argument('--profile-directory=' + 'Profile1')
    item={}
    try:
        print("正在启动Chrome浏览器（首次启动可能需要一些时间）...")
        driver_executable_path = r"C:\Users\Admin\PycharmProjects\MercadoApp\chromedriver.exe"
        driver = uc.Chrome(options=options, driver_executable_path=driver_executable_path)
        # driver = uc.Chrome(options=options)
        print("浏览器启动成功，正在访问目标页面...")
        time.sleep(5)
        driver.get(url)
        # 隐式等待设置（全局生效）
        driver.implicitly_wait(3)
        # items=all_products.find_element(By.CLASS_NAME,'search-offer-wrapper cardui-adOffer').click()
        # search-offer-wrapper cardui-normal search-offer-item major-offer
        # items=all_products.find_elements(By.CLASS_NAME,'search-offer-wrapper.cardui-normal.search-offer-item.major-offer')

        ## 获取标题信息
        title = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.XPATH, '//*[@id="productTitle"]/div/div[1]/h1'))
        ).text
        print(title)
        item['title']=title
        ## 获取最高价格
        prices = WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located(
                (By.CLASS_NAME, 'price-info.currency')))

        high_price = 0.0
        for price in prices:
            text = price.text
            text1 = text.replace("\n", "")
            text2 = text1.replace("¥", "")
            high_price = float(text2)
        print(high_price)
        item['price']=high_price
        ## 获取商品图片链接
        pictures = WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located(
                (By.CLASS_NAME, 'od-gallery-img')))
        pictures_list = []
        i=0
        for picture in pictures:
            if i<5:
                print(picture.get_attribute("src"))
                pictures_list.append(picture.get_attribute("src"))
            i = i + 1

        ## 获取SKU图片
        skus = WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located(
                (By.CLASS_NAME, 'ant-image-img')))
        sku_list = []
        for sku in skus:
            print(sku.get_attribute("src"))
            pictures_list.append(sku.get_attribute("src"))
            sku_list.append(sku.get_attribute("src"))
        item['pictures']=pictures_list
        item['skus']=sku_list
        return item
    except Exception as e:
        print("发生错误:", str(e))
    finally:
        if 'driver' in locals():
            driver.quit()


if __name__ == "__main__":
    scrape_1688_single(
        "https://detail.1688.com/offer/783570784924.html?_t=1756825106818&spm=a2615.7691456/2506.co_0_0_wangpu_score_0_0_0_0_0_0_0000_0.0&kj_agent_plugin=zying")
