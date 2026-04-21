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
    for message in results:
        print(message)
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
    scheduler = BlockingScheduler()
    scheduler.add_job(get_reputation_info_all, 'cron', hour=6, minute=00)
    scheduler.add_job(get_reputation_info_all, 'cron', hour=10, minute=00)
    scheduler.add_job(download_summary, 'cron', hour=00, minute=00)

    scheduler.start()


