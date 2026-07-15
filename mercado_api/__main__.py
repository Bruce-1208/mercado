"""``python -m mercado_api`` 命令行入口及 30 分钟定时循环。"""

from __future__ import annotations

import argparse
import logging
import threading

from .client import MercadoLibreClient, TokenStore
from .config import Settings
from .database import MercadoDatabase
from .service import MercadoSyncService


def build_service(settings: Settings) -> MercadoSyncService:
    """根据配置装配客户端、token 存储、数据库和同步服务。"""
    client = MercadoLibreClient(settings.access_token, refresh_token=settings.refresh_token,
        client_id=settings.client_id, client_secret=settings.client_secret,
        token_store=TokenStore(settings.token_file), timeout=settings.request_timeout_seconds)
    return MercadoSyncService(client, MercadoDatabase(settings.database_path),
                              settings.seller_id, settings.listing_user_id)


def main() -> None:
    """解析命令并执行一次性同步或常驻定时任务。"""
    parser = argparse.ArgumentParser(description="Mercado Libre 订单和 Listing 数据同步")
    parser.add_argument("command", choices=("init-db", "sync-orders", "sync-listings", "sync-all", "run"))
    parser.add_argument("--full", action="store_true", help="订单忽略增量游标，重新读取全部")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env(args.env_file)
    service = build_service(settings)

    if args.command == "init-db":
        service.database.initialize()
    elif args.command == "sync-orders":
        service.sync_orders(full=args.full)
    elif args.command == "sync-listings":
        service.sync_listings()
    elif args.command == "sync-all":
        service.sync_orders(full=args.full)
        service.sync_listings()
    else:
        # Event.wait 可被 Ctrl+C 干净地终止，同时避免不可中断的长时间 sleep。
        stop = threading.Event()
        try:
            # Listing 按需求在进程启动时全量读取一次；失败不影响订单定时任务。
            try:
                service.sync_listings()
            except Exception:
                logging.exception("首次 Listing 同步失败；订单定时任务继续运行，可稍后执行 sync-listings 重试")
            while not stop.is_set():
                try:
                    service.sync_orders()
                except Exception:
                    logging.exception("本轮订单同步失败，下个周期会重试")
                # 首轮立即执行，此后按配置的分钟数等待。
                stop.wait(settings.sync_interval_minutes * 60)
        except KeyboardInterrupt:
            stop.set()


if __name__ == "__main__":
    main()
