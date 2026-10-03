"""Durable, reviewed publication jobs for AI-original marketplace listings.

Credentials never enter previews/jobs. Consuming a preview is atomic; stable
SKU bindings prevent another source product from overwriting a previous offer.
Remote acceptance is recorded separately from the marketplace's live status.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def timestamp(value=None):
    return datetime.fromtimestamp(value or time.time(), timezone.utc).isoformat(timespec="seconds")


def platform_id(value):
    if value not in {"yandex", "amazon"}:
        raise ValueError("自动上架平台须为 Yandex 或亚马逊美国站")
    return value


def listing_for(row, platform):
    snapshot = row.get("source_snapshot_json") or {}
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    return ((snapshot.get("marketplace_listings") or {}).get(platform)
            or (row.get("marketplace_listings") or {}).get(platform) or {})


def fingerprint(row, platform):
    # A later edit or generation requires a new review, including source SKU facts.
    raw = row.get("source_snapshot_json") or {}
    snapshot = json.loads(raw) if isinstance(raw, str) else raw
    return hashlib.sha256(dumps({"listing": listing_for(row, platform), "review_status": row.get("review_status"),
        "original": snapshot.get("original_1688") or row.get("original_1688"),
        "package": {key: row.get(key) for key in (
            "weight_g", "package_length_cm", "package_width_cm", "package_height_cm")}}).encode()).hexdigest()


def public_image_url(value, base_url=""):
    value = str(value or "").strip()
    if value.startswith("/api/ai-original-products/images/"):
        base = str(base_url or os.getenv("AI_ORIGINAL_IMAGE_BASE_URL") or "").rstrip("/")
        if not base:
            raise ValueError("主图尚无公网地址，请配置 AI_ORIGINAL_IMAGE_BASE_URL 或填写公网 HTTPS 主图")
        value = base + value
    try:
        parsed = urlsplit(value)
        host = str(parsed.hostname or "").lower().rstrip(".")
        invalid = (parsed.scheme != "https" or not host or parsed.username or parsed.password
                   or parsed.fragment or parsed.port not in (None, 443) or len(value) > 2000
                   or host in {"localhost", "localhost.localdomain"}
                   or host.endswith((".local", ".internal", ".localhost")) or "." not in host)
        try:
            invalid = invalid or not ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        if invalid:
            raise ValueError()
    except ValueError as exc:
        raise ValueError("商品主图必须是无需登录即可访问的公网 HTTPS 地址") from exc
    return value


class PublicationStore:
    def __init__(self, path=None, secret_key=""):
        self.path = Path(path or os.getenv("AI_ORIGINAL_PUBLISH_DB") or
                         Path(__file__).resolve().parents[1] / ".data" / "ai-original-publish.sqlite3")
        self.secret_key = secret_key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS amazon_stores (
                    id TEXT PRIMARY KEY, organization TEXT NOT NULL, seller_id TEXT NOT NULL,
                    label TEXT NOT NULL, credentials TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(organization, seller_id));
                CREATE TABLE IF NOT EXISTS previews (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, organization TEXT NOT NULL,
                    platform TEXT NOT NULL, store_id TEXT NOT NULL, valid INTEGER NOT NULL,
                    expires REAL NOT NULL, body TEXT NOT NULL, job_id TEXT);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, organization TEXT NOT NULL,
                    platform TEXT NOT NULL, store_id TEXT NOT NULL, state TEXT NOT NULL,
                    updated REAL NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sku_bindings (
                    organization TEXT NOT NULL, platform TEXT NOT NULL, store_id TEXT NOT NULL,
                    sku TEXT NOT NULL, product_id INTEGER NOT NULL, job_id TEXT NOT NULL,
                    PRIMARY KEY(organization, platform, store_id, sku));
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def cipher(self):
        from cryptography.fernet import Fernet
        if not self.secret_key:
            raise ValueError("服务端缺少稳定加密密钥，不能保存店铺授权")
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(
            ("ai-original-publish\0" + str(self.secret_key)).encode()).digest()))

    def save_amazon_store(self, organization, payload):
        fields = ("seller_id", "client_id", "client_secret", "refresh_token")
        credentials = {key: str(payload.get(key) or "").strip() for key in fields}
        if any(not value or len(value) > 8192 for value in credentials.values()):
            raise ValueError("请完整填写 Seller ID、LWA Client ID、Client Secret 和 Refresh Token")
        if not re.fullmatch(r"[A-Za-z0-9]{5,64}", credentials["seller_id"]):
            raise ValueError("Seller ID 格式无效")
        credentials["marketplace_id"] = "ATVPDKIKX0DER"
        label = str(payload.get("label") or credentials["seller_id"]).strip()[:120]
        encrypted = self.cipher().encrypt(dumps(credentials).encode()).decode()
        with self.connect() as db:
            existing = db.execute("SELECT id FROM amazon_stores WHERE organization=? AND seller_id=?",
                                  (organization, credentials["seller_id"])).fetchone()
            store_id = existing["id"] if existing else uuid.uuid4().hex
            db.execute("INSERT INTO amazon_stores VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "label=excluded.label,credentials=excluded.credentials,updated_at=excluded.updated_at",
                       (store_id, organization, credentials["seller_id"], label, encrypted, timestamp()))
        return {"id": store_id, "label": label, "seller_id": credentials["seller_id"], "configured": True}

    def amazon_stores(self, organization):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,label,seller_id,updated_at FROM amazon_stores "
                                                   "WHERE organization=? ORDER BY label", (organization,))]

    def amazon_credentials(self, organization, store_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM amazon_stores WHERE organization=? AND id=?", (organization, store_id)).fetchone()
        if not row:
            raise KeyError("店铺不存在")
        try:
            return json.loads(self.cipher().decrypt(row["credentials"].encode())), row["label"]
        except Exception as exc:
            raise ValueError("亚马逊店铺授权无法解密，请管理员重新保存授权") from exc

    def save_preview(self, body, owner, organization):
        body = {**body, "preview_id": uuid.uuid4().hex, "expires_at": timestamp(time.time() + 900)}
        with self.connect() as db:
            db.execute("DELETE FROM previews WHERE expires < ? AND job_id IS NULL", (time.time() - 86400,))
            db.execute("INSERT INTO previews VALUES (?,?,?,?,?,?,?,?,NULL)",
                (body["preview_id"], owner, organization, body["platform"], body["store_id"],
                 int(body["valid"]), time.time() + 900, dumps(body)))
        return body

    def preview(self, preview_id, owner, organization):
        with self.connect() as db:
            row = db.execute("SELECT * FROM previews WHERE id=? AND owner=? AND organization=?",
                             (preview_id, owner, organization)).fetchone()
        if not row:
            raise KeyError("预览不存在")
        return dict(row)

    def job(self, job_id, owner, organization):
        with self.connect() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=? AND owner=? AND organization=?",
                             (job_id, owner, organization)).fetchone()
        if not row:
            raise KeyError("发布记录不存在")
        return json.loads(row["body"])

    def jobs(self, owner, organization, platform):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM jobs WHERE owner=? AND organization=? AND platform=? "
                              "ORDER BY updated DESC LIMIT 30", (owner, organization, platform)).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def update_job(self, job):
        job["updated_at"] = timestamp()
        with self.connect() as db:
            db.execute("UPDATE jobs SET body=?,state=?,updated=? WHERE id=?",
                       (dumps(job), job["state"], time.time(), job["id"]))

    def binding(self, organization, platform, store_id, sku):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sku_bindings WHERE organization=? AND platform=? AND store_id=? AND sku=?",
                             (organization, platform, store_id, sku)).fetchone()
        return dict(row) if row else None

    def claim(self, preview_id, owner, organization):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            preview = db.execute("SELECT * FROM previews WHERE id=? AND owner=? AND organization=?",
                                 (preview_id, owner, organization)).fetchone()
            if not preview:
                raise KeyError("预览不存在")
            if preview["job_id"]:
                return self.job(preview["job_id"], owner, organization), False
            if not preview["valid"] or preview["expires"] < time.time():
                raise ValueError("预览无效或已过期，请重新检查上架资料")
            body = json.loads(preview["body"])
            job = {**body, "id": uuid.uuid4().hex, "state": "running", "running": True,
                   "created_at": timestamp(), "updated_at": timestamp(), "runner_pid": os.getpid()}
            for item in job["items"]:
                binding = db.execute("SELECT * FROM sku_bindings WHERE organization=? AND platform=? AND store_id=? AND sku=?",
                    (organization, job["platform"], job["store_id"], item["sku"])).fetchone()
                if binding:
                    if binding["product_id"] != item["product_id"]:
                        raise ValueError(f"SKU {item['sku']} 已属于另一商品，请更换 SKU")
                    previous = db.execute("SELECT body FROM jobs WHERE id=? AND owner=?", (binding["job_id"], owner)).fetchone()
                    previous = json.loads(previous["body"]) if previous else {}
                    prior = next((row for row in previous.get("items", []) if row["sku"] == item["sku"]), {})
                    if previous.get("running") or not prior.get("retryable") or prior.get("state") not in {"failed", "partial"}:
                        raise ValueError(f"SKU {item['sku']} 已有发布记录，请回查原任务")
                db.execute("INSERT INTO sku_bindings VALUES (?,?,?,?,?,?) ON CONFLICT(organization,platform,store_id,sku) "
                           "DO UPDATE SET job_id=excluded.job_id",
                    (organization, job["platform"], job["store_id"], item["sku"], item["product_id"], job["id"]))
                item.update(state="pending", message="等待提交", live=False)
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)", (job["id"], owner, organization,
                job["platform"], job["store_id"], job["state"], time.time(), dumps(job)))
            db.execute("UPDATE previews SET job_id=? WHERE id=?", (job["id"], preview_id))
        return job, True


class PublicationService:
    def __init__(self, store, load_rows, publisher_factory=None, start_thread=None):
        self.store, self.load_rows = store, load_rows
        self.publisher_factory = publisher_factory or self.publisher
        self.start_thread = start_thread or self._start
        self._lock = threading.RLock()
        self._active = set()

    @staticmethod
    def _start(function):
        threading.Thread(target=function, name="ai-original-publish", daemon=True).start()

    def publisher(self, platform, store_id, organization):
        if platform == "amazon":
            from erp.ai_original_amazon_publish import AmazonOriginalPublisher
            credentials, label = self.store.amazon_credentials(organization, store_id)
            return AmazonOriginalPublisher(credentials), label
        self._yandex_scope(organization)
        from yandex.app.central_authorization import CentralAuthorizationStore
        from erp.ai_original_yandex_publish import YandexOriginalPublisher
        saved = CentralAuthorizationStore().get_store(int(store_id), include_secret=True)
        if not saved:
            raise KeyError("Yandex 店铺不存在")
        return YandexOriginalPublisher(saved, saved["access_token"]), saved.get("alias") or saved.get("store_name") or store_id

    @staticmethod
    def _yandex_scope(organization):
        if organization != os.getenv("AI_ORIGINAL_YANDEX_ORGANIZATION", "wuhan-zeshun"):
            raise PermissionError("当前企业未绑定 Yandex 中央授权库，请管理员配置企业归属")

    def stores(self, platform, organization):
        platform_id(platform)
        if platform == "amazon":
            rows = self.store.amazon_stores(organization)
        else:
            self._yandex_scope(organization)
            from yandex.app.central_authorization import CentralAuthorizationStore
            rows = [{"id": str(row["id"]), "label": row.get("alias") or row.get("store_name") or str(row["id"]),
                     "campaign_id": row.get("campaign_id"), "business_id": row.get("business_id")}
                    for row in CentralAuthorizationStore().list_stores()]
        return {"rows": rows, "configuration": {"amazon_configured": bool(self.store.amazon_stores(organization))},
                "image_base_url_configured": bool(os.getenv("AI_ORIGINAL_IMAGE_BASE_URL"))}

    def preview(self, payload, owner, organization):
        platform = platform_id(payload.get("target_platform"))
        store_id = str(payload.get("store_id") or "").strip()
        items = payload.get("items")
        if not store_id or not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValueError("请选择店铺与 1–20 件商品")
        if any(not isinstance(item, dict) or type(item.get("product_id")) is not int or item["product_id"] <= 0
               or not isinstance(item.get("settings"), dict) for item in items):
            raise ValueError("每件商品须包含有效 product_id 和 settings")
        ids = [item["product_id"] for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError("同一商品不能在一批中重复提交")
        rows = {int(row["id"]): row for row in self.load_rows(ids)}
        if len(rows) != len(ids) or any(row.get("source_type") != "ai_original" for row in rows.values()):
            raise ValueError("部分记录不是 AI 原创商品，请刷新列表")
        publisher, label = self.publisher_factory(platform, store_id, organization)
        results, skus = [], set()
        for item in items:
            row = rows[item["product_id"]]
            draft = listing_for(row, platform)
            settings = deepcopy(item["settings"])
            settings["sku"] = str(settings.get("sku") or f"aip-{row['id']}").strip()
            binding = self.store.binding(organization, platform, store_id, settings["sku"])
            known = False
            if binding and binding["product_id"] == row["id"]:
                try:
                    previous = self.store.job(binding["job_id"], owner, organization)
                    known = any(old["sku"] == settings["sku"] and old.get("_owns_sku")
                                for old in previous["items"])
                except KeyError:
                    pass
            settings["_known_sku"] = known
            entry = {"product_id": row["id"], "sku": settings["sku"], "title": draft.get("title") or "",
                     "settings": settings, "_owns_sku": known, "fingerprint": fingerprint(row, platform), "errors": [], "warnings": []}
            try:
                if settings["sku"] in skus:
                    raise ValueError("本批商品 SKU 重复")
                skus.add(settings["sku"])
                if draft.get("status") != "completed":
                    raise ValueError("请先完成当前平台的 AI 草稿生成")
                if row.get("review_status") in {"suspected", "infringing", "risk"}:
                    raise ValueError("商品仍标记为侵权风险或待复核，请完成审核后再上架")
                settings["image_url"] = public_image_url(settings.get("image_url") or draft.get("main_image_url"))
                prepared = publisher.prepare(row, settings)
                entry.update({key: value for key, value in prepared.items() if key not in {
                    "product_id", "settings", "fingerprint", "title"}})
                entry["settings"] = settings
            except ValueError as exc:
                entry["errors"] = [str(exc)]
            except Exception:
                entry["errors"] = ["平台预检暂时失败，请检查店铺授权、网络和类目后重试"]
            results.append(entry)
        return self.store.save_preview({"platform": platform, "store_id": store_id, "store_label": label,
            "items": results, "valid": all(not row.get("errors") for row in results)}, owner, organization)

    def submit(self, preview_id, owner, organization):
        record = self.store.preview(preview_id, owner, organization)
        if record["job_id"]:
            return self.store.job(record["job_id"], owner, organization)
        body = json.loads(record["body"])
        rows = {int(row["id"]): row for row in self.load_rows([item["product_id"] for item in body["items"]])}
        if any(item["product_id"] not in rows or fingerprint(rows[item["product_id"]], body["platform"]) != item["fingerprint"]
               for item in body["items"]):
            raise ValueError("商品草稿或货源在预览后已修改，请重新预检并确认")
        # Resolve current authorization before atomically consuming the preview.
        self.publisher_factory(body["platform"], body["store_id"], organization)
        with self._lock:
            job, created = self.store.claim(preview_id, owner, organization)
            if created:
                self._launch(job, owner, organization)
        return self.store.job(job["id"], owner, organization)

    @staticmethod
    def summarize(job):
        states = [item["state"] for item in job["items"]]
        job["running"] = any(state in {"pending", "publishing"} for state in states)
        job["state"] = ("running" if job["running"] else "live" if all(item.get("live") for item in job["items"])
                        else "submitted" if all(state == "submitted" for state in states)
                        else "failed" if all(state == "failed" for state in states) else "partial")

    def _launch(self, job, owner, organization):
        with self._lock:
            self._active.add(job["id"])
        try:
            self.start_thread(lambda: self._run(job["id"], owner, organization))
        except Exception:
            with self._lock:
                self._active.discard(job["id"])
            for item in job["items"]:
                if item["state"] == "pending":
                    item.update(state="failed", message="发布线程未启动，可重试", retryable=True)
            self.summarize(job)
            self.store.update_job(job)
            raise ValueError("发布线程未启动，请在发布记录中重试")

    def _run(self, job_id, owner, organization):
        job = self.store.job(job_id, owner, organization)
        try:
            publisher, _ = self.publisher_factory(job["platform"], job["store_id"], organization)
            for item in job["items"]:
                if item["state"] != "pending":
                    continue
                item.update(state="publishing", message="正在提交")
                self.store.update_job(job)
                try:
                    result = publisher.publish(deepcopy(item))
                    item.update(result)
                    if result.get("amazon_status") == "ACCEPTED" or "offer" in result.get("steps", []):
                        item["_owns_sku"] = True
                    if item.get("submission_unknown"):
                        item["state"] = "unknown"
                        item["retryable"] = False
                    else:
                        item["retryable"] = bool(result.get("retryable", item["state"] in {"failed", "partial"}))
                except Exception:
                    item.update(state="unknown", submission_unknown=True, retryable=False,
                                message="提交结果未确认，请先回查平台状态；不会自动重复提交")
                self.store.update_job(job)
        except Exception:
            for item in job["items"]:
                if item["state"] == "pending":
                    item.update(state="failed", retryable=True, message="店铺授权读取失败，未提交")
        finally:
            self.summarize(job)
            self.store.update_job(job)
            with self._lock:
                self._active.discard(job_id)

    def get_job(self, job_id, owner, organization):
        with self._lock:
            job = self.store.job(job_id, owner, organization)
            active = job_id in self._active
        if job.get("running") and not active:
            # Stateful routes have one worker owner. A restarted owner never
            # silently replays a potentially accepted external request.
            for item in job["items"]:
                if item["state"] == "pending":
                    item.update(state="failed", retryable=True, submission_unknown=False,
                                message="服务已中断，此项尚未提交，可重试")
                elif item["state"] == "publishing":
                    item.update(state="unknown", retryable=False, submission_unknown=True,
                                message="服务已中断，请回查平台结果后再操作")
            self.summarize(job)
            self.store.update_job(job)
        return job

    def refresh(self, job_id, owner, organization):
        with self._lock:
            job = self.get_job(job_id, owner, organization)
            if job["running"] or job_id in self._active:
                return job
            self._active.add(job_id)
        try:
            publisher, _ = self.publisher_factory(job["platform"], job["store_id"], organization)
            for item in job["items"]:
                # An item definitively rejected before creation cannot claim a
                # later, independently created remote SKU by merely reading it.
                if item["state"] == "failed" and not item.get("_owns_sku") and not item.get("submission_unknown"):
                    continue
                try:
                    result = publisher.status(item["sku"])
                    # A successful read after uncertain submission is safer than
                    # inferring success from the original HTTP response alone.
                    was_partial = item["state"] == "partial" and item.get("retryable")
                    item.update(result)
                    if result.get("submission_unknown"):
                        item.update(state="unknown", retryable=False)
                    elif result.get("submission_unknown") is False or result.get("state") == "submitted":
                        item["submission_unknown"] = False
                        if not result.get("not_found"):
                            item["_owns_sku"] = True
                    if item.get("submission_unknown"):
                        item.update(state="unknown", retryable=False)
                    elif was_partial and not result.get("live") and not result.get("not_found"):
                        item.update(state="partial", retryable=True,
                                    message="商品已创建，上次提交仍有待处理项；请核对平台问题后修正或重试")
                    else:
                        item["retryable"] = bool(result.get("retryable", item["state"] in {"failed", "partial"}))
                    if item.get("live"):
                        item.update(state="submitted", submission_unknown=False, retryable=False)
                except Exception:
                    item["message"] = "平台状态暂时无法读取，原提交结果保持不变"
            self.summarize(job)
            self.store.update_job(job)
        finally:
            with self._lock:
                self._active.discard(job_id)
        return job

    def retry(self, job_id, owner, organization):
        with self._lock:
            job = self.get_job(job_id, owner, organization)
            if job["running"] or job_id in self._active:
                return job
            selected = [item for item in job["items"] if item.get("retryable") and item["state"] in {"failed", "partial"}]
            if not selected:
                raise ValueError("没有可重试项；结果不明的商品须先回查平台状态")
            rows = {int(row["id"]): row for row in self.load_rows([item["product_id"] for item in selected])}
            if any(item["product_id"] not in rows or fingerprint(rows[item["product_id"]], job["platform"]) != item["fingerprint"]
                   for item in selected):
                raise ValueError("商品草稿或货源已修改，请重新预检并确认，不能重试旧资料")
            for item in selected:
                binding = self.store.binding(organization, job["platform"], job["store_id"], item["sku"])
                if not binding or binding["job_id"] != job_id:
                    raise ValueError("该 SKU 已有更新的发布预览/任务，请使用最新发布记录")
                item["_known_sku"] = item["settings"]["_known_sku"] = bool(item.get("_owns_sku"))
                # Preparation succeeded before this immutable payload was
                # approved. Clear diagnostics from the previous attempt; the
                # adapter still validates the exact payload again on publish.
                item.update(state="pending", message="等待重试", retryable=False,
                            errors=[], issues=[], submission_unknown=False, not_found=False)
            self.summarize(job)
            self.store.update_job(job)
            self._launch(job, owner, organization)
        return self.store.job(job_id, owner, organization)
