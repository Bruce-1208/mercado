import datetime
import time
from unittest.mock import DEFAULT
from webbrowser import Error

from apscheduler.schedulers.blocking import BlockingScheduler
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By  # 导入 By 模块
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.wait import WebDriverWait


class shensu(object):

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
                chrome_options.add_argument('--user-data-dir=C:\\Users\\Admin\\AppData\\Local\\Google\\Chrome\\User Data\\')

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
                try:
                    driver.get('https://global-selling.mercadolibre.com/help/chat/v2?hasCreditRestriction=false')
                    time.sleep(10)
                except Exception as e:
                    print("打开客服页面错误",e)
                    driver.find_element(By.XPATH, "/html/body/div[3]/div/div/div[2]/div[3]/div[2]/button/span").click()


                xpth_1 = "/html/body/header/div/div/div[1]/div/div[1]/i"
                driver.find_element(By.XPATH, xpth_1).click()


                mlm = '/html/body/header/div/div/div[1]/div/div[2]/div/div[2]/div[1]'
                mlb = '/html/body/header/div/div/div[1]/div/div[2]/div/div[2]/div[2]'
                mlc = '/html/body/header/div/div/div[1]/div/div[2]/div/div[2]/div[3]'
                mco = '/html/body/header/div/div/div[1]/div/div[2]/div/div[2]/div[4]'

                if user=="黄" or user=="飞" or user=='wxt1' or user=='wxt2' or user=="周周":
                    mlm = '/html/body/header/div/div/div[1]/div/div[2]/div/div[2]/div[2]'
                    mlb = '/html/body/header/div/div/div[1]/div/div[2]/div/div[2]/div[3]'
                    mlc = '/html/body/header/div/div/div[1]/div/div[2]/div/div[2]/div[4]'
                    mco = '/html/body/header/div/div/div[1]/div/div[2]/div/div[2]/div[5]'

                ## 转站点
                if site == 'mlm':
                    driver.find_element(By.XPATH, mlm).click()
                if site == 'mlb':
                    driver.find_element(By.XPATH, mlb).click()
                if site == 'mlc':
                    driver.find_element(By.XPATH, mlc).click()
                if site == 'mco':
                    driver.find_element(By.XPATH, mco).click()

                time.sleep(10)
                xpth_listing = '/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[1]/div[3]/div/div/div[2]/div[1]/div/div/div[2]/ul/li[1]/button/p'
                try:
                    driver.find_element(By.XPATH, xpth_listing).click()
                except Exception as e:
                    # 处理其他所有异常
                    print("客服正在处理中/没有客服")
                    # driver.find_element(By.XPATH, '/html/body/div[3]/div/div/div[2]/div[3]/div[2]/button/span').click()
                    driver.find_elements(By.CLASS_NAME,'andes-button__content')[0].click()

                    print("重新开始会话")
                    driver.close()
                time.sleep(10)

                xpth_issue = '/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[1]/div[5]/div/div/div[2]/div[1]/div/div/div[2]/ul/li[1]/button/p'
                driver.find_element(By.XPATH, xpth_issue).click()
                time.sleep(10)

                xpth_choose = '/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[1]/div[7]/div/div/div[2]/div[1]/div/div/div[2]/ul/li/button/p'
                driver.find_element(By.XPATH, xpth_choose).click()
                time.sleep(10)

                # xpath_chat = '/html/body/div[2]/div/div/div[2]/div[2]/div/div/div/section/div/div/main/article[1]/div[1]'
                xpath_chat='/html/body/div[2]/div/div/div[2]/div[2]/div/div/div/section/div/div/main/article[1]/div[1]'
                driver.find_element(By.XPATH, xpath_chat).click()
                time.sleep(10)

                #xpath_message = '/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[2]/div/div/div[1]/div[1]/p'

                #xpath_send = '/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[2]/div/div/div[2]/div[3]/div/button'
                xpath_message='//*[@id="mlc-content-body"]/p'
                xpath_send='/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[2]/div/div[2]/button'
                if(type(reason)!=list):
                    iframe = driver.find_element(By.XPATH,
                                                 "/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[2]/div/div[1]/div[1]/div[1]/div[1]/iframe")
                    driver.switch_to.frame(iframe)

                    # 现在你可以定位并与iframe内的元素交互了
                    element_in_iframe = driver.find_element(By.XPATH, "/html/body/p").send_keys(reason)

                    # 操作完成后，切换回主文档
                    driver.switch_to.default_content()
                    # driver.find_element(By.ID,'mlc-content-body').send_keys(reason)
                    time.sleep(5)
                    driver.find_element(By.XPATH, xpath_send).click()


                elif(type(reason)==list):
                    for rs in reason:
                        iframe = driver.find_element(By.XPATH,
                                                     "/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[2]/div/div[1]/div[1]/div[1]/div[1]/iframe")
                        driver.switch_to.frame(iframe)

                        # 现在你可以定位并与iframe内的元素交互了
                        element_in_iframe = driver.find_element(By.XPATH, "/html/body/p").send_keys(reason)

                        # 操作完成后，切换回主文档
                        driver.switch_to.default_content()
                        time.sleep(5)
                        driver.find_element(By.XPATH, xpath_send).click()

                time.sleep(10)
                iframe = driver.find_element(By.XPATH,
                                             "/html/body/main/div/div/div/div/div/div/div[4]/div[2]/div/div/div/div/div/div[2]/div/div[2]/div/div[1]/div[1]/div[1]/div[1]/iframe")
                driver.switch_to.frame(iframe)

                # 现在你可以定位并与iframe内的元素交互了
                element_in_iframe = driver.find_element(By.XPATH, "/html/body/p").send_keys("Estimado Servicio de Atención al Cliente, Me llamo Bruce. Me alegro de ser tu amigo,¡espero que puedan ayudarme y espero sus buenas noticias!")
                # 操作完成后，切换回主文档
                driver.switch_to.default_content()
                time.sleep(5)
                driver.find_element(By.XPATH, xpath_send).click()
                driver.close

            except Exception as e:
                print("系统错误，10分钟后再次启动任务",e)
                print(e)
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

