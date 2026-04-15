from DataAnalysis import results
from bit_download import *
from bit_reputation_info import *
from bit_print import *
from bit_summary_delayfile import *


from apscheduler.schedulers.blocking import BlockingScheduler

def print_orders():
     results=print_orders_all()
     for message in results:
         print(message)

def download_summary():
    results=download_relay_mail_all()
    for message in results:
        print(message)
    summary_delayFile()


scheduler = BlockingScheduler()

# 间隔任务
scheduler.add_job(print_orders, 'interval', hour=5)
# Cron 格式任务（每天凌晨 2 点执行）
scheduler.add_job(download_summary(), 'cron', hour=2, minute=0)

scheduler.start()
