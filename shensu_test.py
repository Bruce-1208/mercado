import datetime
import time
from unittest.mock import DEFAULT
from webbrowser import Error

from apscheduler.schedulers.blocking import BlockingScheduler
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By  # 导入 By 模块
from selenium.webdriver.chrome.service import Service


class shensu_test(object):

    def main(self,user,site,reason,chromedriver):

        def zhaokefu():
            try:
                print("开始启动程序:" + str(datetime.datetime.now()))
                chrome_options = Options()

                chrome_options.binary_location=chromedriver

                # 这里可以添加任意数量的用户配置项，例如禁用某些插件，启用某些特性等
                # 例如，禁用自动填充功能

                chrome_options.add_argument("disable-features=AutofillEnableAutocompleteName,AutofillEnableProfileName")
                # 如果有配置文件的路径，可以通过user-data-dir参数指定
                chrome_options.add_argument(
                    '--user-data-dir=C:\\Users\\Admin\\AppData\\Local\\Google\\Chrome\\User Data')

                profile = 'Default'
                if user == '龙':
                    profile = "Profile 1"
                elif user == '腾':
                    profile = "Profile 2"
                elif  user == '虎':
                    profile = "Profile 3"
                elif  user == '跃':
                    profile = "Profile 4"
                elif  user == 'vngbjkk':
                    profile = "Default"
                elif  user =="rijindoujin":
                    profile = "Profile 7"
                elif  user =="黄":
                    profile = "Profile 9"
                elif user =="wxt1":
                    profile = "Profile 11"
                elif  user =="wxt2":
                    profile = "Profile 6"
                elif  user =="花开富贵":
                    profile = "Profile 6"
                elif  user =="周周":
                    profile = "Profile 12"
                elif  user =="lzw":
                    profile = "Profile 5"

                print(profile)
                chrome_options.add_argument('--profile-directory=' + profile)

                ser = Service()
                ser.executable_path = chromedriver  # 指定 ChromeDriver 的路径

                # 启动Chrome浏览器
                driver = webdriver.Chrome(service=ser, options=chrome_options)

                # 打开网页
                driver.get('https://global-selling.mercadolibre.com/help/chat/v2?hasCreditRestriction=false&chat_id=cc9d0ec7-9857-4bf4-9e43-cf5dd9711b86&case_id=362994633')

                time.sleep(10)
                #xpath_message = '/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p'

                #xpath_send = '/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[2]/div/div/div[2]/div[3]/div/button'
                xpath_message='//*[@id="mlc-content-body"]/p'
                xpath_send='/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[2]/div/div[2]/button'
                if(type(reason)!=list):
                    # 定位iframe并切换到它
                    iframe = driver.find_element(By.XPATH, "/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[2]/div/div[1]/div[1]/div[1]/div[1]/iframe")
                    driver.switch_to.frame(iframe)

                    # 现在你可以定位并与iframe内的元素交互了
                    element_in_iframe = driver.find_element(By.XPATH, "/html/body/p").send_keys(reason)

                    # 操作完成后，切换回主文档
                    driver.switch_to.default_content()
                    time.sleep(3)
                    driver.find_element(By.XPATH, xpath_send).click()


                elif(type(reason)==list):
                    for rs in reason:
                        driver.find_element(By.XPATH, xpath_message).send_keys(rs)
                        time.sleep(3)
                        driver.find_element(By.XPATH, xpath_send).click()

                time.sleep(10)
                driver.find_element(By.XPATH, xpath_message).send_keys(
                    "Estimado Servicio de Atención al Cliente, ¡espero que puedan ayudarme y espero sus buenas noticias!")
                time.sleep(3)
                driver.find_element(By.XPATH, xpath_send).click()
                driver.close

            except Exception as e:
                print("系统错误，10分钟后再次启动任务",e)
                # driver.close()
                return

        zhaokefu()
        # 其他操作...
        # 创建调度器

        # scheduler = BlockingScheduler()
        # # 添加任务，每10分钟执行一次
        # scheduler.add_job(zhaokefu, 'interval', seconds=600, next_run_time=datetime.datetime.now())
        # print("--------------------------------------------")
        # # 启动定时任务
        # try:
        #  scheduler.start()
        # except Exception as e:
        #  scheduler.shutdown()
        #  print(e)

