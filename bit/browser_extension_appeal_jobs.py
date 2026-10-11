"""Durable, idempotent preparation receipts; browser sends remain client-owned."""
import hashlib
import json
import logging
import os
import secrets
import threading
import time

from bit.bit_runtime_lock import InterProcessLock, _pid_is_running
from bit.browser_extension_appeal import AppealError


_SLOTS = threading.BoundedSemaphore(4)
MAX_SECONDS = 600


def _path(service, user, request_id):
    if not isinstance(request_id, str) or not request_id or len(request_id) > 100:
        raise AppealError("申诉准备请求编号无效")
    scope = [int(user.get("id") or 0), user.get("organization_key"), request_id]
    key = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
    return service.root / "preparations" / (key + ".json")


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix("." + secrets.token_hex(8) + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False)
    os.replace(temporary, path)


def _public(value):
    if value["state"] == "preparing" and not _pid_is_running(value["pid"]):
        raise AppealError("申诉资料准备因服务重启而中断，请重新开始；本次准备阶段不会发送申诉", 503)
    if value["state"] == "preparing" and time.time() - value["created_at"] > MAX_SECONDS:
        raise AppealError("申诉资料准备超时或服务已重启，请重新开始；本次准备阶段不会发送申诉", 504)
    if value["state"] == "failed":
        raise AppealError(value["message"], value["status"])
    return {key: value[key] for key in ("request_id", "state", "message", "plan") if key in value}


def status(service, user, payload):
    path = _path(service, user, payload.get("request_id"))
    if not path.is_file():
        raise AppealError("申诉准备请求不存在", 404)
    value = json.loads(path.read_text(encoding="utf-8"))
    if value["state"] == "ready":
        # Recheck current shop and site permissions before releasing the plan.
        service._load(user, value["plan"]["task_id"])
    return _public(value)


def start(service, user, payload, wrap=lambda fn: fn):
    request_id = payload.get("request_id")
    path = _path(service, user, request_id)
    params = {key: value for key, value in payload.items() if key != "request_id"}
    fingerprint = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    lease = InterProcessLock("appeal_prepare_" + path.stem)
    if not lease.acquire(timeout=0):
        raise AppealError("申诉准备请求正在登记，请稍后查询进度", 409)
    try:
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if value["fingerprint"] != fingerprint:
                raise AppealError("申诉准备请求参数已改变，请重新开始", 409)
            return status(service, user, payload)
        if not _SLOTS.acquire(blocking=False):
            raise AppealError("申诉资料准备繁忙，请稍后重试", 503)
        value = {"request_id": request_id, "fingerprint": fingerprint, "state": "preparing",
                 "message": "正在核对店铺授权并读取申诉资料…", "created_at": time.time(), "pid": os.getpid()}
        def prepare():
            try:
                plan = service.plan(user, params)
                completed = {**value, "state": "ready", "message": "申诉资料已准备完成", "plan": plan}
            except AppealError as exc:
                completed = {**value, "state": "failed", "message": str(exc), "status": exc.status}
            except Exception:
                logging.exception("插件申诉资料后台准备失败")
                completed = {**value, "state": "failed", "message": "申诉资料准备失败，请重试", "status": 503}
            try:
                _write(path, completed)
            finally:
                _SLOTS.release()
        try:
            _write(path, value)
            threading.Thread(target=wrap(prepare), name="extension-appeal-prepare", daemon=True).start()
        except Exception:
            _SLOTS.release()
            _write(path, {**value, "state": "failed", "message": "申诉资料准备未能启动，请重试", "status": 503})
            raise
        return _public(value)
    finally:
        lease.release()
