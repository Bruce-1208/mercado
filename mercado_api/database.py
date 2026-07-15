"""SQLite 表结构、事务管理和 Mercado Libre 数据持久化。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


def _json(value: Any) -> str:
    """把 API 中的对象压缩为 UTF-8 JSON，保留非 ASCII 字符。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class MercadoDatabase:
    """Mercado Libre 本地 SQLite 数据库。

    常用业务字段单独建列以便查询，同时将完整响应写入 ``raw_json``，保证
    API 后续新增字段时无需立即迁移表结构也不会丢失数据。
    """

    def __init__(self, path: str | Path):
        """记录数据库路径；真正的连接按事务创建并及时关闭。"""
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """提供自动提交/回滚的数据库连接上下文。

        WAL 模式允许读取和定时写入更好地并行；外键约束保证删除订单时不会
        留下孤立的订单商品行。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """幂等创建订单、订单商品、Listing 和同步游标表及常用索引。"""
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, buyer_id TEXT,
                    status TEXT, status_detail TEXT, date_created TEXT, date_closed TEXT,
                    last_updated TEXT, currency_id TEXT, total_amount REAL, shipping_id TEXT,
                    raw_json TEXT NOT NULL, synced_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_seller_updated ON orders(seller_id, last_updated);
                CREATE TABLE IF NOT EXISTS order_items (
                    order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
                    line_number INTEGER NOT NULL, item_id TEXT, title TEXT, quantity INTEGER,
                    unit_price REAL, currency_id TEXT, raw_json TEXT NOT NULL,
                    PRIMARY KEY(order_id, line_number)
                );
                CREATE TABLE IF NOT EXISTS listings (
                    item_id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, site_id TEXT, title TEXT,
                    category_id TEXT, status TEXT, sub_status TEXT, price REAL, currency_id TEXT,
                    available_quantity INTEGER, sold_quantity INTEGER, seller_custom_field TEXT,
                    permalink TEXT, date_created TEXT, last_updated TEXT, raw_json TEXT NOT NULL,
                    synced_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_listings_seller_status ON listings(seller_id, status);
                CREATE TABLE IF NOT EXISTS sync_state (
                    sync_key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)

    def upsert_orders(self, seller_id: str, orders: Iterable[dict[str, Any]]) -> int:
        """新增或更新一批订单，并返回本批处理的订单数量。

        订单主表使用 ``order_id`` 做 UPSERT。订单商品属于订单详情快照，因此
        每次更新先删除旧行再写入新行，避免商品变更后残留过期数据。
        """
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        with self.connect() as db:
            for order in orders:
                order_id = str(order["id"])
                db.execute("""INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(order_id) DO UPDATE SET
                    seller_id=excluded.seller_id,buyer_id=excluded.buyer_id,status=excluded.status,
                    status_detail=excluded.status_detail,date_created=excluded.date_created,date_closed=excluded.date_closed,
                    last_updated=excluded.last_updated,currency_id=excluded.currency_id,total_amount=excluded.total_amount,
                    shipping_id=excluded.shipping_id,raw_json=excluded.raw_json,synced_at=excluded.synced_at""",
                    (order_id, seller_id, str((order.get("buyer") or {}).get("id") or "") or None,
                     order.get("status"), _json(order.get("status_detail")), order.get("date_created"),
                     order.get("date_closed"), order.get("last_updated"), order.get("currency_id"),
                     order.get("total_amount"), str((order.get("shipping") or {}).get("id") or "") or None,
                     _json(order), now))
                db.execute("DELETE FROM order_items WHERE order_id=?", (order_id,))
                for line_number, item in enumerate(order.get("order_items") or []):
                    detail = item.get("item") or {}
                    db.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?,?,?)",
                        (order_id, line_number, detail.get("id"), detail.get("title"), item.get("quantity"),
                         item.get("unit_price"), item.get("currency_id"), _json(item)))
                count += 1
        return count

    def upsert_listings(self, seller_id: str, listings: Iterable[dict[str, Any]]) -> int:
        """按 ``item_id`` 新增或更新 Listing，并返回处理数量。"""
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        with self.connect() as db:
            for item in listings:
                db.execute("""INSERT INTO listings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(item_id) DO UPDATE SET
                    seller_id=excluded.seller_id,site_id=excluded.site_id,title=excluded.title,category_id=excluded.category_id,
                    status=excluded.status,sub_status=excluded.sub_status,price=excluded.price,currency_id=excluded.currency_id,
                    available_quantity=excluded.available_quantity,sold_quantity=excluded.sold_quantity,
                    seller_custom_field=excluded.seller_custom_field,permalink=excluded.permalink,
                    date_created=excluded.date_created,last_updated=excluded.last_updated,
                    raw_json=excluded.raw_json,synced_at=excluded.synced_at""",
                    (str(item["id"]), seller_id, item.get("site_id"), item.get("title"), item.get("category_id"),
                     item.get("status"), _json(item.get("sub_status")), item.get("price"), item.get("currency_id"),
                     item.get("available_quantity"), item.get("sold_quantity"), item.get("seller_custom_field"),
                     item.get("permalink"), item.get("date_created"), item.get("last_updated"), _json(item), now))
                count += 1
        return count

    def get_state(self, key: str) -> str | None:
        """读取某类同步任务最后一次成功完成时保存的游标。"""
        with self.connect() as db:
            row = db.execute("SELECT value FROM sync_state WHERE sync_key=?", (key,)).fetchone()
            return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        """原子新增或更新同步游标。"""
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("""INSERT INTO sync_state VALUES(?,?,?)
                ON CONFLICT(sync_key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (key, value, now))
