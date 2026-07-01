from apscheduler.schedulers.blocking import BlockingScheduler

from playwright.bit_download import download_relay_mail_all
from playwright.bit_infractions_info import get_infractions_info_all
from playwright.bit_print import print_orders_all
from playwright.bit_summary_info import get_reputation_info_all
from bit.bit_summary_delayfile import summary_delayFile


def print_orders():
    for message in print_orders_all():
        print(message)


def download_summary():
    download_relay_mail_all()
    summary_delayFile()


def main():
    scheduler = BlockingScheduler()
    scheduler.add_job(get_reputation_info_all, "cron", hour=6, minute=0)
    scheduler.add_job(get_reputation_info_all, "cron", hour=11, minute=0)
    scheduler.add_job(get_infractions_info_all, "cron", hour=15, minute=0)
    scheduler.start()


if __name__ == "__main__":
    main()
