import os
import sys

# Make this entry file runnable both as:
# 1) python bit/bit_main.py
# 2) python -m bit.bit_main
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from apscheduler.schedulers.blocking import BlockingScheduler

from bit_download import *
from bit_reputation_info import *
from bit_print import *
from bit_summary_delayfile import *

def print_orders():
     results=print_orders_all()
     for message in results:
         print(message)

def download_summary():
    results=download_relay_mail_all()

    summary_delayFile()


# scheduler = BlockingScheduler()
#
# # 间隔任务
# scheduler.add_job(print_orders, 'interval', hours=6)
# # Cron 格式任务（每天凌晨 2 点执行）
# scheduler.add_job(download_summary, 'cron', hour=7, minute=0)
# scheduler.add_job(download_summary, 'cron', hour=12, minute=9)

# scheduler.start()
if __name__ == '__main__':
    print("------------------------------")
    print(CURRENT_DIR)


    scheduler = BlockingScheduler()
    scheduler.add_job(get_reputation_info_all, 'cron', hour=6, minute=00)
    scheduler.add_job(get_reputation_info_all, 'cron', hour=11, minute=00)
    scheduler.add_job(download_summary, 'cron', hour=15, minute=00)
    scheduler.add_job(download_summary, 'cron', hour=00, minute=00)
    scheduler.start()



