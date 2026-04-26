import time
from bit.bit_summary_delayfile import *
from bit.bit_email_info import *
import traceback
from pathlib import Path
from bit.bit_mysql import *
from bit.bit_clash import *
from bit.bit_summary_delayfile import *


def download_relay_mail(window_id, site):
    # /browser/open 接口会返回 selenium使用的http地址，以及webdriver的path，直接使用即可
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
    wait = WebDriverWait(driver, 30)
    ## 进入声誉页面点击下载
    click=False
    i=0
    while (i<3):
        i=i+1
        try:
            click = click_download(driver, site)
        except Exception as e:
            print("点击下载失败", e)
            traceback.print_exc()
            switch_random_hongkong_node()
            get_public_ip()

    if (click == True):

        ##循环扫描邮箱
        i=0
        while (i<3):
            i=i+1
            time.sleep(60)
            mail_item=()
            try:
                mail_item = scan_email(driver, 1)
                if(mail_item=="读取邮件失败"):
                    break
            except Exception as e:
                print("扫描邮件信息失败", e)
                traceback.print_exc()
            flag = False
            ##下载文件
            if (mail_item != None):

                try:

                    flag = download_excel(driver, mail_item)
                except Exception as e:
                    print("下载延误邮件失败", e)
                    traceback.print_exc()

                # print("下载文件失败")
                if (flag == True):
                    return "下载文件成功"
                    break
    else:
        return "没有需要下载的文件"


def click_download(driver, site):
    driver.get("https://global-selling.mercadolibre.com/reputation")
    driver.refresh()
    time.sleep(10)

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
    time.sleep(3)

    # 点击下载邮件/html/body/main/div/div[3]/div/div[1]/div/div[5]/div[3]/div[4]/div[3]/div/a
    xpath = "//a[contains(text(), 'Download affected orders') and not(../descendant::*[contains(text(), 'Review in Metrics') or contains(text(), 'Review')])]"
    # xpath = "//*[contains(text(), 'Non-compliant shipments')]/following-sibling::*[2][self::a]"
    # 逻辑：先精准找到那个文本 span/div，再找它后面的同级链接 a

    try:
        driver.find_element(By.XPATH,xpath).click()
        print("点击下载成功")
        return True
    except Exception as e:
        return False



##判断最近五分钟是否下载邮件
# isAll判断是读取全部还是当前的邮箱
def scan_email(driver, isAll):
    email_infos = []


    if isAll == 1:
        email_infos = read_email_info_all(driver)

    else:
        email_infos = get_mail_info(driver, '普通邮件')
    if (email_infos==None):
        print("读取邮件失败")
        return "读取邮件失败"

    email_infos_sorted = sorted(list(email_infos), key=lambda x: x[1], reverse=True)

    for subject, time, element, text in email_infos_sorted:
        if subject != 'Your orders that you shipped with delay report is ready':
            continue
        else:
            now = datetime.now()
            diff = now - time
            print("相差时间为:", diff)
            if (diff.total_seconds() > 3600.0):
                return None
            else:
                return (subject, time, element, text)


def download_excel(driver, mail_item):
    wait = WebDriverWait(driver, 30)
    print("扫描到的邮件为",mail_item)
    subject = mail_item[0]
    mail_time = mail_item[1]
    element = mail_item[2]
    text = mail_item[3]
    if (text == '垃圾邮件'):
        element.click()
        print("在垃圾邮件里找到")
    else:
        folder = wait.until(EC.element_to_be_clickable(
            (By.XPATH,
             "//div[contains(@title, '收件箱') or contains(@title, '收件匣')]")
        ))
        folder.click()
        mail_item_2 = scan_email(driver, 0)
        mail_item_2[2].click()
        print("在普通邮件里找到")

    print("点击时间为的邮件:", str(mail_time))
    # Go to download report
    time.sleep(10)
    contain = wait.until(EC.presence_of_element_located((By.CLASS_NAME, 'wide-content-host')))

    element_download = wait.until(EC.presence_of_element_located(("link text", "Go to download report")))
    downlod_url = contain.find_element("link text", "Go to download report").get_attribute("href")

    print(downlod_url)
    driver.get(downlod_url)

    driver.switch_to.window(driver.window_handles[-1])  # 切换窗口
    ##下载文件
    wait.until(EC.element_to_be_clickable((By.XPATH, "//span[text()='Download']/ancestor::button"))).click()
    # wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'download report')]")))

    print("已下载延误文件")
    return True


def download_relay_mail_all():


    # download_relay_mail('df2d33b20d0b4d72949fc490f7ff075a','墨西哥')
    #
    # time.sleep(100000)
    root_path = Path(__file__).resolve().parent
    # file_path = root_path / "比特配置文件.xlsx"
    file_path = root_path / "比特配置文件.xlsx"


    start = int(time.time())
    print(start)
    wb = load_workbook(file_path)
    sheet = wb.active
    reputation_info_sum = []
    # 使用 min_row=2 跳过第一行
    result = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        print(row)  # row 是一个元组，包含该行所有数据
        id = row[0]
        name = row[1]
        remark = row[2]
        if remark == '忽略':
            continue
        print(get_now_time()+"开始打开窗口:", name)
        site_list = row[3].split("，")
        for site in site_list:
            try:
                print(get_now_time()+"执行任务:", name + site)
                message = download_relay_mail(id, site)
                print(get_now_time()+name + site + message)
                result.append(('下载延误表格', name, site, "成功", get_now_time()))
            except Exception as e:
                print(get_now_time()+name + site + "执行失败", e)
                result.append(('下载延误表格', name, site, "失败", get_now_time()))

        print(get_now_time()+"结束，正在关闭窗口",name)
        try:
            closeBrowser(str(id))
        except Exception as e:
            continue
            # print("关闭窗口失败",e)
        print(get_now_time()+"已经关闭窗口")
        time.sleep(5)


    end = int(time.time())
    print(get_now_time()+"总花费", end - start)
    for i in result:
        print(i)
    insert_task_record(result)
    return result


if __name__ == '__main__':
    results=download_relay_mail_all()
    for i in results:
        print(i)

    bit_summary_delayfile()