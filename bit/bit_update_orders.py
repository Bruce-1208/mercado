import time
from ctypes.wintypes import DOUBLE

import pandas as pd
from bit.bit_api import *
from openpyxl import load_workbook
from pathlib import Path
from datetime import datetime
import pandas
from bit.bit_send_mail import *

from bit.bit_utils import *
import sys
from bit.bit_mysql import insert_orders


def update_order_mysql():
    fold = Path(__file__).resolve().parent / "美客多订单"
    print(fold)
    lines = []
    for file in fold.glob("*.xlsx"):
        if file.name.startswith(("~$", ".~")):
            continue
        print(file.absolute())
        wb = load_workbook(file.absolute())
        sheet = wb.active

        headers = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))
        header_index = {name: index for index, name in enumerate(headers) if name}

        def value(row, name):
            index = header_index.get(name)
            if index is None or index >= len(row):
                return None
            return row[index]

        for row in sheet.iter_rows(min_row=2, values_only=True):
            order_id = value(row, "id")
            order_num = value(row, "编号")
            date = value(row, "时间")
            name = value(row, "业务员")
            source = value(row, "来源")
            status = value(row, "状态")
            amount = value(row, "金额")
            charge = value(row, "费用")
            refund = value(row, "退款")
            income = value(row, "人民币收入")
            cost = value(row, "采购成本")
            purchase = value(row, "采购单号")
            logistics = value(row, "采购追踪")
            profit = value(row, "利润")
            product_id = value(row, "产品id")
            classify = value(row, "产品分类")
            title = value(row, "标题")
            img = value(row, "图片")
            num = value(row, "数量")
            freight = value(row, "订单运费")
            remark = value(row, "订单备注")
            site = value(row, "地区")
            buyer = value(row, "买家名称")
            # 更加简洁的写法：直接用列表包裹
            line = []
            line.extend(
                [
                    order_id,
                    order_num,
                    date,
                    name,
                    source,
                    status,
                    amount,
                    charge,
                    refund,
                    income,
                    cost,
                    purchase,
                    logistics,
                    profit,
                    product_id,
                    classify,
                    title,
                    img,
                    num,
                    freight,
                    remark,
                    site,
                    buyer,
                ]
            )
            lines.append(line)

    for item in lines:
        print(item)
    print("行数为",len(lines))
    insert_orders(lines)


if __name__ == "__main__":
    update_order_mysql()
