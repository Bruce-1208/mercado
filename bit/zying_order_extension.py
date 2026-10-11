"""User-scoped, durable order sync driven by the browser extension."""
import json
import os
import secrets
import sqlite3
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from bit.zying_order_sync import AgentSync, SYNC_BATCH_SIZE, clean, snapshot_orders

PREPARATION_OWNER = secrets.token_hex(16)
PREPARATION_TIMEOUT = 600


class ExtensionOrderSync:
    def __init__(self, path=None):
        self.path = path

    @contextmanager
    def connection(self):
        path = Path(self.path or os.environ.get("BIT_ZYING_EXTENSION_SYNC_PATH") or
                    Path(__file__).resolve().parents[1] / ".data/zying-extension-sync.sqlite3")
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS runs (owner TEXT PRIMARY KEY, job TEXT NOT NULL)")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def load(connection, owner):
        saved = connection.execute("SELECT job FROM runs WHERE owner=?", (str(owner),)).fetchone()
        return json.loads(saved[0]) if saved else None

    @staticmethod
    def save(connection, owner, job):
        connection.execute("INSERT OR REPLACE INTO runs VALUES (?, ?)",
                           (str(owner), json.dumps(job, ensure_ascii=False, default=str)))

    @staticmethod
    def public(job):
        if not job:
            return {"phase": "idle", "running": False, "results": []}
        return {**job["result"], "results": job["result"].get("results", [])[-100:], "request_id": job["job_id"],
                "date_start": job["date_start"], "date_end": job["date_end"], "order_status": job.get("order_status", "")}

    def status(self, owner):
        with self.connection() as connection:
            job = self.load(connection, owner)
            self.expire_preparation(connection, owner, job)
            return self.public(job)

    def expire_preparation(self, connection, owner, job):
        if job and job["result"].get("running") and job["result"].get("phase") == "loading":
            if (job.get("preparation_owner") != PREPARATION_OWNER or
                    time.time() - job.get("preparation_updated_at", 0) > PREPARATION_TIMEOUT):
                job["result"].update(phase="error", running=False,
                                     message="订单准备已中断或超时，请重新启动同步")
                self.save(connection, owner, job)

    def start(self, user, filters, reader):
        owner = user["id"]
        # Reserve before reading all pages so concurrent starts cannot race.
        with self.connection() as connection:
            old = self.load(connection, owner)
            self.expire_preparation(connection, owner, old)
            if old and old["result"].get("running"):
                raise ValueError("智赢订单同步正在运行，请先结束任务")
            job = {"job_id": secrets.token_hex(16), "agent_id": "extension", "status": "running",
                   "created_by_id": owner, "created_by_name": user.get("display_name") or user.get("username") or "",
                   "date_start": filters["start_date"], "date_end": filters["end_date"],
                   "order_status": filters.get("status", ""),
                   "preparation_owner": PREPARATION_OWNER, "preparation_updated_at": time.time(),
                   "payload": {"rows": []}, "result": {
                       "phase": "loading", "running": True, "message": "正在读取日期范围内的全部订单…",
                       "total": 0, "completed": 0, "updated": 0, "skipped": 0, "failed": 0, "results": []}}
            self.save(connection, owner, job)
        state = self.public(job)
        # Return the reservation immediately: reading hundreds of pages must not
        # hold the HTTP response (and the extension's login tab) hostage.
        threading.Thread(target=self.prepare, args=(owner, job, dict(filters), reader), daemon=True,
                         name="zying-extension-orders").start()
        return state

    def prepare(self, owner, job, filters, reader):
        loaded = 0
        def cancelled():
            state = self.status(owner)
            return state.get("request_id") != job["job_id"] or not state.get("running")

        def read_page(**params):
            nonlocal loaded
            data = reader(**params)
            loaded += len(data.get("rows") or [])
            with self.connection() as connection:
                current = self.load(connection, owner)
                self.expire_preparation(connection, owner, current)
                if not current or current["job_id"] != job["job_id"] or not current["result"].get("running"):
                    raise InterruptedError("任务已结束")
                current["preparation_updated_at"] = time.time()
                current["result"].update(prepared=loaded, total=int(data.get("total") or 0),
                    message=f"正在准备订单，已读取 {loaded} / {int(data.get('total') or 0)} 单；可先登录智赢…")
                self.save(connection, owner, current)
            return data

        try:
            rows = snapshot_orders(read_page, filters, cancelled,
                                   page_size=2000 if filters.get("sync_identifiers_only") else 200)
            rows = [{key: row.get(key) for key in ("id", "order_number", "pack_id")}
                    for row in rows if not clean(row.get("id")).startswith("yandex:")]
        except InterruptedError:
            return
        except Exception:
            logging.exception("插件智赢同步准备订单失败")
            with self.connection() as connection:
                current = self.load(connection, owner)
                if current and current["job_id"] == job["job_id"] and current["result"].get("running"):
                    current["result"].update(phase="error", running=False, message="读取订单失败，请重新启动")
                    self.save(connection, owner, current)
            return
        with self.connection() as connection:
            current = self.load(connection, owner)
            self.expire_preparation(connection, owner, current)
            if not current or current["job_id"] != job["job_id"] or not current["result"].get("running"):
                return
            job = current
            job["payload"]["rows"] = rows
            job["result"].update(total=len(rows), phase="waiting_login" if rows else "completed",
                                 running=bool(rows), message="请在插件打开的智赢网页登录，然后点击确定登录"
                                 if rows else "所选日期范围内没有可同步的订单")
            self.save(connection, owner, job)

    def action(self, owner, request_id, action, data=None, writer=None):
        data = data or {}
        with self.connection() as connection:
            job = self.load(connection, owner)
            self.expire_preparation(connection, owner, job)
            if not job or job["job_id"] != request_id:
                raise ValueError("智赢订单同步任务不存在或已过期")
            state = job["result"]
            if action == "stop":
                if state.get("running"):
                    state.update(phase="stopped", running=False, message="任务已结束，已同步的数据保留")
            elif action == "fail":
                if state.get("running"):
                    state.update(phase="error", running=False, message=str(data.get("message") or "插件同步失败")[:300])
            elif action == "login_required":
                if state.get("running"):
                    state.update(phase="waiting_login", message="智赢登录已失效，请登录后再次点击确定登录")
            elif action == "confirm":
                if state.get("phase") != "waiting_login":
                    raise ValueError("当前任务不在等待登录状态")
                state.update(phase="syncing", message="正在同步智赢采购信息…")
            elif action == "batch":
                if state.get("phase") != "syncing" or not state.get("running"):
                    return {**self.public(job), "rows": []}
                done = {item["order_id"] for item in state["results"]}
                pending = [row for row in job["payload"]["rows"] if clean(row["id"]) not in done]
                return {**self.public(job), "rows": pending[:SYNC_BATCH_SIZE]}
            elif action == "results":
                if not state.get("running") or state.get("phase") != "syncing":
                    raise ValueError("任务已结束或尚未确认登录")
                class Adapter:
                    def append_event(self, *args, result, **kwargs):
                        job["result"] = result
                # Share the Agent's validation, non-empty writeback, deduplication
                # and matched-row check instead of reimplementing purchase rules.
                state = AgentSync().step(Adapter(), job, {"action": "rows", "rows": data.get("rows")}, writer)
                if state["completed"] == state["total"]:
                    state.update(phase="completed", running=False, message="同步完成")
                job["result"] = state
            else:
                raise ValueError("不支持的同步操作")
            self.save(connection, owner, job)
            return self.public(job)


extension_order_sync = ExtensionOrderSync()
