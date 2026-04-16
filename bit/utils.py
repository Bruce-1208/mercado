from opencc import OpenCC
from pathlib import Path

from openpyxl import load_workbook
from datetime import datetime, timedelta

from pydantic.v1.datetime_parse import parse_date
from datetime import datetime
import pytz
import random
import time

def parser_delay_date(time_str):
    date = time_str.split("-")[0]
    date_format = "%A %d %b %Y "

    return datetime.strptime(date, date_format)


def convert_text(text):
    # 初始化OpenCC对象，可以选择不同的转换配置文件
    # 't2s' 代表繁体转简体，'s2t' 代表简体转繁体
    cc = OpenCC('t2s')  # 繁体转简体
    # cc = OpenCC('s2t')  # 简体转繁体
    converted_text = cc.convert(text)
    return converted_text # 输出简体化的文本

def get_latest_modified_file(folder_path):
    # 获取文件夹内所有文件的Path对象列表
    files = Path(folder_path).glob('*.xlsx')
    latest_file = None
    latest_modified_time = 0

    for file in files:
        # 确保是文件而不是文件夹
        if file.is_file():
            # 获取文件的修改时间戳
            modified_time = file.stat().st_mtime
            # 如果找到更晚修改的文件，更新变量
            if modified_time > latest_modified_time:
                latest_modified_time = modified_time
                latest_file = file

    return latest_file.name if latest_file else None




def get_bit_path():
    return Path(__file__).resolve().parent


if __name__ == '__main__':
    date="Monday 6 Apr 2026 - 21:31 hs"
    print(parser_date(date))
