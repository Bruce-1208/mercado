import time
from ctypes.wintypes import DOUBLE

import pandas as pd
from pandas.io.formats.format import return_docstring
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
from switch_country import *
from openpyxl import load_workbook
from datetime import datetime

from utils import *
from pathlib import Path
from datetime import datetime
import  pandas
from send_mail import *


start = int(time.time())
print(start)
wb = load_workbook(r'D:\比特配置文件测试.xlsx')
sheet = wb.active
reputation_info_sum = []

save_fold=r"D:/BitDownload/"
# 使用 min_row=2 跳过第一行
file_dict={}
for row in sheet.iter_rows(min_row=2, values_only=True):
    id = row[0]
    name = row[1]
    remark = row[2]
    seq=str(row[4])

    if(remark=='忽略'):
        continue
    fold=Path(save_fold+seq)
    print(fold)
    for file in fold.glob('*.csv'):
        print(f"文件名: {file.name}, 绝对路径: {file.absolute()}")
        part=file.name.split("_")
        country=part[10]
        file_time=float((part[12].replace('.csv','')).split(" (")[0])
        file_datetime=datetime.fromtimestamp(file_time/ 1000.0)
        filename=file_dict.get(name+"-"+country)
        if(filename==None):
            file_dict[name+"-"+country]=str(file.absolute())
        else:
            part=filename.split("_")
            filename_time=float((part[12].replace('.csv','')).split(" (")[0])
            if(file_time>filename_time):
                file_dict[name+"-"+country]=str(file.absolute())
print(file_dict)

for key,value in file_dict.items():
    print(key+"|||"+value)

line=[]
for key,filepath in file_dict.items():

    name=key.split("-")[0]
    site=key.split("-")[1]
    try:
     df = pd.read_csv(filepath, header=None, skiprows=1)
     for index, row in df.iterrows():
         # 假设第一列是订单号，第三列是金额（转化为浮点数）

         try:

             print((name, site, row[0], row[1], row[2], row[5], row[6]))
             line.append((name, site, row[0], row[1], row[2], row[5], row[6]))

         except Exception as e:
             print(e)
    except Exception as e:
        print("错误文件为",filepath)



print(line)
df = pd.DataFrame(line, columns=['店铺', '站点', '下单时间', '销售单号', '订单标题', '截止延误时间','实际揽收时间'])
now=datetime.now()
date_str=datetime.now().strftime("%Y-%m-%d-%H")
df.to_excel(r"D:\美客多声誉\武汉泽顺店铺延误信息汇总"+date_str+".xlsx", index=False)

send_info('美客多所有店铺延误信息汇总',"美客多所有店铺延误信息汇总",r"D:\美客多声誉\武汉泽顺店铺延误信息汇总"+date_str+".xlsx",r"武汉泽顺店铺延误信息汇总"+date_str+".xlsx")

