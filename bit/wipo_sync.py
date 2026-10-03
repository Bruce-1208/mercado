"""Pull authorized trademark feeds into the knowledge review queue.

Uses the official Madrid daily update files by default; the complete Global
Brand Database requires a separate authorized JSON/CSV feed.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import ipaddress
import json
import logging
import os
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from bit.bit_runtime_lock import InterProcessLock

LOCAL = timezone(timedelta(hours=8))
MAX_BYTES = 50 * 1024 * 1024
MAX_ROWS = 250_000
MAX_PAGES = 1000
SOURCE = "WIPO Global Brand Database"
DISCLAIMER = ("The World Intellectual Property Organization (WIPO) bears no responsibility "
              "for the integrity or accuracy of the data contained herein, in particular due, "
              "but not limited, to any deletion, manipulation, or reformatting of data that "
              "may have occurred beyond its control.")
DEFAULT_SETTINGS = {"enabled": True, "source_mode": "madrid", "madrid_from_date": "",
                    "feed_url": "", "format": "auto", "interval_hours": 24, "token_encrypted": ""}
DEFAULT_STATE = {"running": False, "status": "idle", "message": "等待 WIPO 马德里商标每日增量同步",
                 "started_at": "", "finished_at": "", "last_success_at": "", "next_run_at": "",
                 "summary": {}, "logs": [], "madrid_through_date": ""}


def now_text():
    return datetime.now(LOCAL).isoformat(timespec="seconds")


def _field(value):
    return "".join(ch for ch in str(value).casefold() if ch.isalnum())


def _value(row, *names):
    fields = {_field(k): v for k, v in row.items()}
    return next((fields[_field(k)] for k in names
                 if _field(k) in fields and fields[_field(k)] not in (None, "")), "")


def normalize_feed_rows(rows):
    """Deduplicate names, preserving registration evidence without trusting list type."""
    unique, details = {}, {}
    fetched = skipped = 0
    for row in rows:
        fetched += 1
        if fetched > MAX_ROWS:
            raise ValueError(f"单次数据源超过 {MAX_ROWS} 条，请提供分批数据源")
        if not isinstance(row, dict):
            raise ValueError("数据源记录须为 JSON 对象")
        raw_name = _value(row, "brand_name", "brand name", "mark_name", "mark", "trademark",
                          "verbal_element", "brand", "name", "品牌名称", "商标名称", "商标")
        if not isinstance(raw_name, str) or not raw_name.strip():
            skipped += 1  # Figurative-only registrations have no title keyword.
            continue
        brand = " ".join(raw_name.split())
        if len(brand) > 255:
            raise ValueError("数据源包含超过 255 个字符的品牌名称")
        detail = {"source": SOURCE}
        for key, names in {
            "registration": ("registration_number", "registration", "number", "application_number", "id"),
            "country": ("country", "office", "jurisdiction", "designation country"),
            "status": ("status", "mark_status"),
            "nice_classes": ("nice_classes", "nice class", "classes"),
        }.items():
            value = _value(row, *names)
            if value not in (None, ""):
                detail[key] = str(value)[:200]
        key = brand.casefold()
        if key in unique:
            unique[key]["evidence_count"] += 1
            merged = details[key]
            for field, value in detail.items():
                if field not in merged:
                    merged[field] = value
                elif merged[field] != value:
                    values = merged[field] if isinstance(merged[field], list) else [merged[field]]
                    if value not in values:
                        if len(values) < 5:
                            values.append(value)
                        else:
                            merged["detail_truncated"] = True
                    merged[field] = values
            continue
        details[key] = detail
        unique[key] = {"brand_name": brand, "list_type": "pending", "source_type": "wipo",
                       "notes": "WIPO 商标数据同步，仅作为待复核线索，需人工确认处置。",
                       "evidence_count": 1}
    if not unique:
        raise ValueError("数据源没有可用的文字商标；请核对品牌字段和文件格式")
    for key, record in unique.items():
        record["source_detail"] = json.dumps(details[key], ensure_ascii=False)
    return list(unique.values()), {"fetched": fetched, "unique": len(unique), "skipped": skipped}


def validate_feed_url(value, *, resolve=False):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("数据源地址无效")
    url = value.strip()
    parsed = urlsplit(url)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("数据源端口无效") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or port != 443):
        raise ValueError("请使用 HTTPS 数据源地址，凭证填写到 Token 字段")
    if parsed.hostname.lower() in {"branddb.wipo.int", "api.branddb.wipo.int", "legacy.branddb.wipo.int"}:
        raise ValueError("WIPO 公开检索站不是自动下载接口，请填写授权数据源地址")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("数据源须为公开的 HTTPS 地址")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("数据源须为公开的 HTTPS 地址")
    if resolve:
        try:
            addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("无法解析数据源域名") from exc
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("数据源不能指向本机或内网地址")
    return url


def fetch_feed(settings, token="", *, session=None):
    """Fetch bounded CSV or JSON pages; credentials stay on the original origin."""
    transport = session or requests.Session()
    if session is None:
        from bit.wipo_http import PublicHTTPSAdapter
        transport.trust_env = False
        transport.mount("https://", PublicHTTPSAdapter())
    url = validate_feed_url(settings["feed_url"], resolve=True)
    origin = urlsplit(url).netloc.lower()
    headers = {"Accept": "application/json, text/csv", "User-Agent": "Mercado-WIPO-Feed-Sync/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    rows, visited, total_bytes = [], set(), 0
    deadline = time.monotonic() + 600
    try:
        while url:
            if url in visited or len(visited) >= MAX_PAGES or time.monotonic() > deadline:
                raise ValueError("数据源分页重复或超过同步范围，请核对分页接口")
            visited.add(url)
            validate_feed_url(url, resolve=True)
            with transport.get(url, headers=headers, timeout=(10, 60), stream=True,
                               allow_redirects=False) as response:
                if response.status_code != 200:
                    raise ValueError(f"数据源返回 HTTP {response.status_code}，请核对地址与认证")
                chunks = []
                for chunk in response.iter_content(64 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_BYTES or time.monotonic() > deadline:
                        raise ValueError("数据源超过 50 MB 或同步超时，请提供分批数据源")
                    chunks.append(chunk)
                try:
                    content = b"".join(chunks).decode("utf-8-sig")
                except UnicodeError as exc:
                    raise ValueError("数据文件须使用 UTF-8 编码") from exc
                kind = settings["format"]
                if kind == "auto":
                    kind = "json" if content.lstrip().startswith(("[", "{")) else "csv"
                next_url = ""
                if kind == "json":
                    try:
                        payload = json.loads(content)
                    except ValueError as exc:
                        raise ValueError("数据源不是有效的 JSON 文件") from exc
                    if isinstance(payload, list):
                        page_rows = payload
                    elif isinstance(payload, dict):
                        page_rows = next((payload[k] for k in ("records", "results", "data", "items")
                                          if isinstance(payload.get(k), list)), None)
                        next_url = payload.get("next") or payload.get("next_url") or ""
                        if not isinstance(next_url, str):
                            raise ValueError("JSON 分页 next 须为地址字符串")
                        if any(payload.get(k) for k in ("has_more", "hasMore", "next_cursor")) and not next_url:
                            raise ValueError("数据源还有下一页，但未提供 next 地址")
                        if payload.get("total") is not None:
                            try:
                                total = int(payload["total"])
                            except (ValueError, TypeError) as exc:
                                raise ValueError("JSON total 字段无效") from exc
                            if total > len(rows) + len(page_rows or []) and not next_url:
                                raise ValueError("数据源尚未完整返回，请提供 next 分页地址")
                    else:
                        page_rows = None
                    if not isinstance(page_rows, list):
                        raise ValueError("JSON 须为记录数组或包含 records/results/data/items 数组")
                else:
                    try:
                        reader = csv.DictReader(io.StringIO(content), strict=True)
                        if not reader.fieldnames or not any(_field(name) in {
                            _field(key) for key in ("brand_name", "brand name", "mark_name", "mark", "trademark",
                                                    "verbal_element", "brand", "name", "品牌名称", "商标名称", "商标")
                        } for name in reader.fieldnames):
                            raise ValueError("CSV 缺少 brand_name / mark / trademark 品牌列")
                        page_rows = list(reader)
                    except csv.Error as exc:
                        raise ValueError("CSV 数据格式无效") from exc
                rows.extend(page_rows)
                if len(rows) > MAX_ROWS:
                    raise ValueError(f"单次数据源超过 {MAX_ROWS} 条，请提供分批数据源")
                url = urljoin(url, next_url) if next_url else ""
                if url and urlsplit(url).netloc.lower() != origin:
                    raise ValueError("分页地址须与数据源处于同一域名")
        return normalize_feed_rows(rows)
    except requests.RequestException as exc:
        # Requests exception strings can contain URLs and credentials.
        raise ValueError("数据源连接失败或超时，请核对接口及网络后重试") from exc
    finally:
        if session is None:
            transport.close()


class WipoSyncService:
    def __init__(self, secret_key, *, path=None, writer=None, fetcher=fetch_feed):
        self.path = Path(path or os.environ.get("BIT_WIPO_SYNC_PATH") or
                         Path(__file__).resolve().parents[1] / ".data" / "wipo-sync.sqlite3")
        self.secret_key, self.writer, self.fetcher = secret_key, writer, fetcher
        self.guard = threading.RLock()
        self.thread = self.scheduler = None
        self.stop = threading.Event()
        self.lock_key = "wipo_feed_sync_" + hashlib.sha256(str(self.path.resolve()).encode()).hexdigest()[:16]

    @contextmanager
    def database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            os.chmod(self.path, 0o600)
            db.execute("CREATE TABLE IF NOT EXISTS wipo_values (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS wipo_history (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _read(self, db, key, default):
        row = db.execute("SELECT data FROM wipo_values WHERE key=?", (key,)).fetchone()
        data = {**default, **(json.loads(row[0]) if row else {})}
        if key == "settings" and not data["madrid_from_date"]:
            # Fix the starting date when settings are first persisted so a
            # process restart cannot silently move the bootstrap window.
            data["madrid_from_date"] = (datetime.now(LOCAL) - timedelta(days=7)).date().isoformat()
            self._write(db, key, data)
        return data

    def _write(self, db, key, value):
        db.execute("INSERT INTO wipo_values(key,data) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                   (key, json.dumps(value, ensure_ascii=False)))

    def _cipher(self):
        from cryptography.fernet import Fernet
        key = hashlib.sha256(b"mercado-wipo-feed\0" + str(self.secret_key).encode()).digest()
        return Fernet(base64.urlsafe_b64encode(key))

    def settings(self, *, manage=True):
        with self.database() as db:
            data = self._read(db, "settings", DEFAULT_SETTINGS)
        encrypted = data.pop("token_encrypted", "")
        data["has_token"] = bool(encrypted)
        if not manage:
            data.pop("feed_url", None)
            data.pop("has_token", None)
        return data

    def save_settings(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("同步设置须为 JSON 对象")
        with self.guard, self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self._read(db, "settings", DEFAULT_SETTINGS)
            data = {**existing, **{k: payload[k] for k in DEFAULT_SETTINGS if k in payload and k != "token_encrypted"}}
            if type(data["enabled"]) is not bool:
                raise ValueError("自动同步开关须为布尔值")
            if type(data["interval_hours"]) is not int or not 1 <= data["interval_hours"] <= 720:
                raise ValueError("同步间隔须为 1–720 小时的整数")
            if data["format"] not in {"auto", "json", "csv"}:
                raise ValueError("数据格式仅支持 auto/json/csv")
            if data["source_mode"] not in {"madrid", "feed"}:
                raise ValueError("数据源模式仅支持 madrid/feed")
            try:
                date = datetime.strptime(data["madrid_from_date"], "%Y-%m-%d").date()
            except (ValueError, TypeError) as exc:
                raise ValueError("增量起始日期须为 YYYY-MM-DD") from exc
            if date > datetime.now(LOCAL).date():
                raise ValueError("增量起始日期不能晚于今天")
            data["madrid_from_date"] = date.isoformat()
            if not isinstance(data["feed_url"], str):
                raise ValueError("数据源地址无效")
            data["feed_url"] = data["feed_url"].strip()
            if data["feed_url"]:
                validate_feed_url(data["feed_url"])
            elif data["enabled"] and data["source_mode"] == "feed":
                raise ValueError("启用自动同步前请填写数据源地址")
            token = payload.get("token", "")
            if not isinstance(token, str) or len(token) > 4096 or "\n" in token or "\r" in token:
                raise ValueError("Token 格式无效")
            if token.strip():
                data["token_encrypted"] = self._cipher().encrypt(token.strip().encode()).decode()
            if payload.get("clear_token") is True:
                data["token_encrypted"] = ""
            self._write(db, "settings", data)
            state = self._read(db, "state", DEFAULT_STATE)
            if existing["madrid_from_date"] != data["madrid_from_date"]:
                state["madrid_through_date"] = ""
            if not state["running"]:
                state["next_run_at"] = now_text() if data["enabled"] else ""
            self._write(db, "state", state)
        return self.settings()

    def status(self):
        with self.guard, self.database() as db:
            state = self._read(db, "state", DEFAULT_STATE)
            if state["running"]:
                lock = InterProcessLock(self.lock_key, owner="wipo-recovery")
                if lock.acquire(timeout=0):
                    try:
                        settings = self._read(db, "settings", DEFAULT_SETTINGS)
                        state.update(running=False, status="interrupted", finished_at=now_text(),
                                     message="上次同步被中断，可以重新同步",
                                     next_run_at=((datetime.now(LOCAL) + timedelta(hours=1)).isoformat(timespec="seconds")
                                                  if settings["enabled"] else ""))
                        self._write(db, "state", state)
                        db.execute("INSERT INTO wipo_history(data) VALUES (?)", (json.dumps(state, ensure_ascii=False),))
                    finally:
                        lock.release()
            state["history"] = [json.loads(row[0]) for row in db.execute(
                "SELECT data FROM wipo_history ORDER BY id DESC LIMIT 10")]
            settings = self._read(db, "settings", DEFAULT_SETTINGS)
        state["source"] = "WIPO Madrid Monitor" if settings["source_mode"] == "madrid" else SOURCE
        state["disclaimer"] = DISCLAIMER
        return state

    def _log(self, message):
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._read(db, "state", DEFAULT_STATE)
            state["message"] = message
            state["logs"] = [*state["logs"], f"[{now_text()}] {message}"][-100:]
            self._write(db, "state", state)

    def start_sync(self):
        with self.guard:
            settings = self.settings()
            if settings["source_mode"] == "feed" and not settings["feed_url"]:
                raise ValueError("请先配置 WIPO 授权数据源地址")
            lock = InterProcessLock(self.lock_key, owner="wipo-sync")
            if not lock.acquire(timeout=0):
                return {**self.status(), "started": False, "running": True, "message": "WIPO 同步正在执行"}
            try:
                with self.database() as db:
                    state = self._read(db, "state", DEFAULT_STATE)
                    state.update(running=True, status="running", started_at=now_text(), finished_at="",
                                 message="正在拉取 WIPO 商标数据", summary={}, logs=[])
                    self._write(db, "state", state)
                    private_settings = self._read(db, "settings", DEFAULT_SETTINGS)
                    private_settings["configured_madrid_from_date"] = private_settings["madrid_from_date"]
                    if state["madrid_through_date"]:
                        date = datetime.strptime(state["madrid_through_date"], "%Y-%m-%d").date()
                        private_settings["madrid_from_date"] = (date + timedelta(days=1)).isoformat()
                ready = threading.Event()
                self.thread = threading.Thread(target=self._run, args=(private_settings, lock, ready),
                                               name="wipo-brand-feed-sync", daemon=True)
                self.thread.start()
                # Keep the acquiring thread alive until the worker owns the
                # lease, so status readers cannot mistake it for a stale lock.
                if not ready.wait(5):
                    raise RuntimeError("WIPO 同步线程未就绪")
            except BaseException:
                lock.release()
                with self.database() as db:
                    state.update(running=False, status="error", finished_at=now_text(), message="同步线程启动失败")
                    self._write(db, "state", state)
                raise
            return {**state, "started": True}

    def _run(self, settings, lock, ready=None):
        summary, outcome, message = {}, "error", "WIPO 同步失败"
        try:
            lock.transfer_to_current_thread()
            if ready is not None:
                ready.set()
            self._log("正在读取数据源，新增商标线索进入待复核，不覆盖人工处置")
            token = ""
            if settings["source_mode"] == "feed" and settings["token_encrypted"]:
                try:
                    token = self._cipher().decrypt(settings["token_encrypted"].encode()).decode()
                except Exception as exc:
                    raise ValueError("数据源 Token 无法解密，请重新保存 Token") from exc
            fetcher = self.fetcher
            if settings["source_mode"] == "madrid" and fetcher is fetch_feed:
                from bit.wipo_madrid import fetch_madrid_updates
                fetcher = fetch_madrid_updates
            rows, summary = fetcher(settings, token)
            self._log(f"已拉取 {summary['fetched']} 条记录，去重后 {summary['unique']} 个品牌；正在写入待复核线索")
            writer = self.writer
            if writer is None:
                from bit.bit_mysql import upsert_wipo_infringement_knowledge
                writer = upsert_wipo_infringement_knowledge
            if rows:
                summary.update(writer(rows))
            else:
                summary.update(inserted=0, updated=0, unchanged=0, restored=0,
                               skipped_manual=0, skipped_deleted=0)
            outcome = "success"
            message = (f"同步完成：新增 {summary.get('inserted', 0)}，更新 {summary.get('updated', 0)}，"
                       f"保留人工处置 {summary.get('skipped_manual', 0)}，"
                       f"跳过已删除 {summary.get('skipped_deleted', 0)}，"
                       f"未变化 {summary.get('unchanged', 0)}；新增线索需人工复核")
            if summary.get("remaining_packages"):
                message += f"；尚有 {summary['remaining_packages']} 个日期包，自动同步将在下一轮继续"
        except ValueError as exc:
            # Only controlled parser/validation messages are exposed.
            message = str(exc)
        except Exception:
            logging.error("WIPO 数据同步失败，请检查数据源或数据库连接")
            message = "WIPO 同步失败，请检查数据源或数据库连接后重试"
        finally:
            if ready is not None:
                ready.set()
            try:
                self._log(message)
                with self.database() as db:
                    db.execute("BEGIN IMMEDIATE")
                    state = self._read(db, "state", DEFAULT_STATE)
                    current = self._read(db, "settings", DEFAULT_SETTINGS)
                    state.update(running=False, status=outcome, finished_at=now_text(), message=message, summary=summary)
                    if outcome == "success":
                        state["last_success_at"] = state["finished_at"]
                        if (settings["source_mode"] == "madrid"
                                and current["madrid_from_date"] == settings.get("configured_madrid_from_date", settings["madrid_from_date"])):
                            state["madrid_through_date"] = summary.get("madrid_through_date") or state["madrid_through_date"]
                    delay = current["interval_hours"] if outcome == "success" else 1
                    wait = timedelta(minutes=1) if outcome == "success" and summary.get("remaining_packages") else timedelta(hours=delay)
                    state["next_run_at"] = ((datetime.now(LOCAL) + wait).isoformat(timespec="seconds")
                                            if current["enabled"] else "")
                    self._write(db, "state", state)
                    db.execute("INSERT INTO wipo_history(data) VALUES (?)", (json.dumps(state, ensure_ascii=False),))
                    db.execute("DELETE FROM wipo_history WHERE id NOT IN (SELECT id FROM wipo_history ORDER BY id DESC LIMIT 50)")
            finally:
                lock.release()

    def tick(self):
        settings, state = self.settings(), self.status()
        if not settings["enabled"] or state["running"]:
            return False
        due = state["next_run_at"]
        if due and datetime.fromisoformat(due) > datetime.now(LOCAL):
            return False
        return self.start_sync().get("started", False)

    def start_scheduler(self):
        with self.guard:
            if self.scheduler and self.scheduler.is_alive():
                return self.scheduler
            self.stop.clear()
            def run():
                while not self.stop.is_set():
                    try:
                        self.tick()
                    except Exception:
                        logging.error("WIPO 自动同步调度失败，将在下一轮重试")
                    self.stop.wait(60)
            self.scheduler = threading.Thread(target=run, name="wipo-feed-scheduler", daemon=True)
            self.scheduler.start()
            return self.scheduler


_service = None
_service_guard = threading.Lock()


def get_service(secret_key):
    global _service
    with _service_guard:
        if _service is None:
            _service = WipoSyncService(secret_key)
        return _service
