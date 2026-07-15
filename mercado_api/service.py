"""编排 API 读取、增量规则和数据库写入的同步服务。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .client import MercadoLibreClient
from .database import MercadoDatabase

LOGGER = logging.getLogger(__name__)


class MercadoSyncService:
    """协调订单与 Listing 同步，不负责定时调度。"""

    def __init__(self, client: MercadoLibreClient, database: MercadoDatabase,
                 seller_id: str, listing_user_id: str | None = None):
        """绑定 API 客户端、数据库和需要同步的账号 ID。"""
        self.client = client
        self.database = database
        self.seller_id = seller_id
        self.listing_user_id = listing_user_id or seller_id

    def sync_orders(self, full: bool = False) -> int:
        """同步订单详情并返回处理数量。

        首次运行或 ``full=True`` 时读取全部订单；其他情况从上次成功同步时间
        开始增量读取。只有全部数据成功落库后才推进游标，失败时下个周期会
        从旧游标重试。
        """
        self.database.initialize()
        state_key = f"orders:{self.seller_id}:last_updated"
        filters: dict[str, str] = {"sort": "updated_asc"}
        previous = None if full else self.database.get_state(state_key)
        if previous:
            # 向前重叠 5 分钟，防止接口延迟或时间边界造成漏单；UPSERT 会消除重复。
            start = datetime.fromisoformat(previous.replace("Z", "+00:00")) - timedelta(minutes=5)
            filters["last_updated.from"] = start.isoformat(timespec="milliseconds")
        started_at = datetime.now(timezone.utc).isoformat()
        count, batch = 0, []
        for order_id in self.client.iter_order_ids(self.seller_id, **filters):
            batch.append(self.client.get_order(order_id))
            # 分批提交，避免全量同步时把所有订单详情同时留在内存中。
            if len(batch) == 50:
                count += self.database.upsert_orders(self.seller_id, batch)
                batch = []
        if batch:
            count += self.database.upsert_orders(self.seller_id, batch)
        self.database.set_state(state_key, started_at)
        LOGGER.info("订单同步完成：%s 条", count)
        return count

    def sync_listings(self) -> int:
        """读取账号下的全部 Listing 详情，保存后记录本次完成时间。"""
        self.database.initialize()
        item_ids = self.client.iter_listing_ids(self.listing_user_id)
        count = self.database.upsert_listings(self.listing_user_id, self.client.get_listings(item_ids))
        self.database.set_state(f"listings:{self.listing_user_id}:last_sync", datetime.now(timezone.utc).isoformat())
        LOGGER.info("Listing 同步完成：%s 条", count)
        return count
