import time
import os
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
import sys
import logging
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

from data_analysis_mercado import post_mercado
from data_analysis_single_1688 import scrape_1688_single


# 启用调试日志
# logging.basicConfig(level=logging.DEBUG)

def scrape_1688(url):
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

    try:
        print("正在启动Chrome浏览器（首次启动可能需要一些时间）...")
        driver_executable_path = r"C:\Users\Admin\PycharmProjects\MercadoApp\chromedriver.exe"
        driver = uc.Chrome(options=options, driver_executable_path=driver_executable_path)
        # driver = uc.Chrome(options=options)
        print("浏览器启动成功，正在访问目标页面...")
        time.sleep(5)
        driver.get(url)
        #隐式等待设置（全局生效）
        driver.implicitly_wait(3)
        #模拟浏览器划到最底部
        driver.execute_script("window.scroll(0, document.body.scrollHeight);")
        time.sleep(5)

        all_products=driver.find_element(By.XPATH,'//*[@id="app"]/div/div/div[2]/div[5]/div[1]/div/div/div')
        #items=all_products.find_element(By.CLASS_NAME,'search-offer-wrapper cardui-adOffer').click()
        #search-offer-wrapper cardui-normal search-offer-item major-offer
        # items=all_products.find_elements(By.CLASS_NAME,'search-offer-wrapper.cardui-normal.search-offer-item.major-offer')
        # items = WebDriverWait(driver, 10).until(
        #     EC.presence_of_all_elements_located(
        #         (By.CLASS_NAME, "search-offer-wrapper.cardui-normal.search-offer-item.major-offer"))
        # )
        items=driver.find_elements(By.CLASS_NAME, "search-offer-wrapper.cardui-normal.search-offer-item.major-offer")

        url_list=[]
        for item in items:
            print("爬取到的商品链接有:",item.get_attribute("href"))
            url_list.append(item.get_attribute("href"))


        print("商品数为",item.size)

        return url_list

    except Exception as e:
        print("发生错误:", str(e))
    finally:
        if 'driver' in locals():
            driver.quit()

if __name__ == "__main__":
    # url_list=scrape_1688("https://s.1688.com/selloffer/offer_search.htm?keywords=%BF%E7%BE%B3%B5%C6%BE%DF&spm=a260k.home2025.searchbox.0&priceEnd=100&beginPage=1")
    # for url in url_list:
    #
    #     dict=scrape_1688_single(url)
    #     post_mercado(dict)
    #     time.sleep(3)
    url='https://detail.1688.com/offer/758320532242.html?spm=a26352.13672862.offerlist.114.5c281e62s6WzWx&kj_agent_plugin=zying'
    dict = scrape_1688_single(url)
    post_mercado(dict)
