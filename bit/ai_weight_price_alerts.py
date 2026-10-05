"""Server-side pause mail monitor; independent of an open extension/popup."""
import json
import logging
import threading
import time

from bit.bit_runtime_lock import InterProcessLock
from bit.browser_extension_mail import send_alert

PAUSE_SECONDS = 300
_thread = None
_guard = threading.Lock()


def check_pauses(store, secret_key, *, now=None, sender=send_alert):
    now = time.time() if now is None else now
    with store.connect() as db:
        rows = db.execute("SELECT key, value FROM state WHERE key LIKE ?", ("user:%:circuit",)).fetchall()
    for row in rows:
        try:
            key, raw = row[0], row[1]
            pause = json.loads(raw)
            if not isinstance(pause, dict) or not pause.get("reason"):
                continue
            user_id = int(key.split(":")[1])
            if not pause.get("at") or now - float(pause["at"]) < PAUSE_SECONDS:
                continue
            lock = InterProcessLock(f"awp_pause_mail_{user_id}")
            if not lock.acquire():
                continue
            try:
                with store.actor_scope({"id": user_id}, view_all=False):
                    # Recheck after acquiring the lock: the user may have resumed.
                    if store.state("circuit") != pause:
                        continue
                    run = store.state("run", {}) or {}
                    incident = "|".join(str(v) for v in (run.get("run_id", ""), pause.get("kind", ""), pause["at"], pause["reason"]))
                    notice = store.state("pause_email", {}) or {}
                    if notice.get("incident") == incident and (notice.get("sent") or now - notice.get("attempted_at", 0) < 60):
                        continue
                    notice = {"incident": incident, "attempted_at": now, "sent": False}
                    store.set_state("pause_email", notice)
                    try:
                        result = sender(user_id, {"event_type": "task_blocked", "source": "AI核重核价",
                                                  "message": pause["reason"], "incident_id": incident}, secret_key)
                        notice["sent"] = bool(result.get("sent") or result.get("deduplicated"))
                        if notice["sent"]:
                            notice["error"] = ""
                        elif result.get("pending"):
                            notice["error"] = "同一事件的邮件正在由其他进程处理，稍后确认发送结果"
                        elif result.get("unconfigured"):
                            notice["error"] = f"账号 {user_id} 的邮件通知尚未完成配置，请在集成与凭证设置中保存邮箱配置"
                        elif result.get("disabled"):
                            notice["error"] = f"账号 {user_id} 未启用邮件提醒，请在集成与凭证设置中启用并保存"
                        else:
                            notice["error"] = "邮件服务未确认发送结果，将重试"
                    except Exception as exc:
                        notice["error"] = str(exc)[:500]
                    store.set_state("pause_email", notice)
                    if not notice["sent"]:
                        store.log("人工验证超时邮件发送失败，将在一分钟后重试：" + notice["error"], level="ERROR")
            finally:
                lock.release()
        except Exception:
            logging.exception("核重核价暂停邮件检查失败")


def start_scheduler(store, secret_key):
    global _thread
    with _guard:
        if _thread and _thread.is_alive():
            return _thread
        def monitor():
            while True:
                try:
                    check_pauses(store, secret_key)
                except Exception:
                    logging.exception("核重核价邮件监控失败，将重试")
                time.sleep(30)
        _thread = threading.Thread(target=monitor, name="awp-pause-email", daemon=True)
        _thread.start()
        return _thread
