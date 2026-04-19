import time
from ctypes.wintypes import DOUBLE

import pandas as pd
from bit_api import *
from openpyxl import load_workbook
from pathlib import Path
from datetime import datetime
import pandas
from send_mail import *

from utils import *
import sys
from bit_mysql import *


def update_order_mysql():
    fold = Path(__file__).resolve().parent /"美客多订单"
    print(fold)
    lines=[]
    for file in fold.glob('*.xlsx'):
        print(file.absolute())
        wb = load_workbook(file.absolute())
        sheet = wb.active

        # 使用 min_row=2 跳过第一行
        file_dict = {}
        for row in sheet.iter_rows(min_row=2, values_only=True):
            order_id=row[0]
            order_num=row[1]
            date=row[2]
            name=row[3]
            source=row[4]
            status=row[9]
            amount=row[13]
            charge=row[14]
            refund=row[16]
            income=row[17]
            cost=row[18]
            purchase=row[19]
            logistics=row[20]
            profit=row[21]
            product_id=row[23]
            classify=row[24]
            title=row[27]
            img=row[31]
            num=row[34]
            freight=row[42]
            remark=row[43]
            site=row[45]
            buyer=row[46]
            # 更加简洁的写法：直接用列表包裹
            line=[]
            line.extend([
                order_id, order_num, date, name, source, status, amount,
                charge, refund, income, cost, purchase, logistics, profit,
                product_id, classify, title, img, num, freight, remark, site, buyer
            ])
            lines.append(line)
    for item in lines[:10]:
        print(item)
    insert_orders(lines)

if __name__ == '__main__':
    update_order_mysql()