import json
import os
import re
import sys
import threading
import queue
import time
import uuid
from datetime import datetime
from pathlib import Path
from bit.bit_file_lock import locked_file


DEFAULT_CHAT_LOG = Path(__file__).resolve().parent / "ai_chat_records.jsonl"
MAX_CHAT_LOG_BYTES = int(os.getenv("MERCADO_AI_CHAT_LOG_MAX_MB", "50")) * 1024 * 1024
MAX_FIELD_CHARS = int(os.getenv("MERCADO_AI_CHAT_LOG_FIELD_CHARS", "2000"))
CHAT_DB_ENABLED = os.getenv("MERCADO_AI_CHAT_DB_ENABLED", "1").strip().lower() not in ("0", "false", "no")
_COLLECTOR = threading.local()
_DB_QUEUE = queue.Queue(maxsize=500)
_DB_THREAD = None
_DB_THREAD_LOCK = threading.Lock()

INTERNAL_PROMPT_PATTERNS = (
    r"<desambiguacion_de_tools>.*?</desambiguacion_de_tools>",
    r"<acciones_de_estado_recurrentes>.*?</acciones_de_estado_recurrentes>",
    r"<redactar_respuesta>.*?</redactar_respuesta>",
    r"<preferencia_accion_sobre_informacion>.*?</preferencia_accion_sobre_informacion>",
    r"<politica_contacto_humano>.*?</politica_contacto_humano>",
)
INTERNAL_PROMPT_MARKERS = (
    "acciones_de_estado_recurrentes",
    "desambiguacion_de_tools",
    "redactar_respuesta",
    "preferencia_accion_sobre_informacion",
    "politica_contacto_humano",
    "usa SOLO datos de tools ejecutadas",
    "No te detengas en la información cuando la intención accionable es clara",
)


def _sanitize_text(value):
    text = str(value or "")
    if not text:
        return ""
    if re.search(r"<html\b|<!doctype html|<script\b|<style\b|--andes-", text, re.I):
        return "[filtered html/page source]"
    if any(marker.lower() in text.lower() for marker in INTERNAL_PROMPT_MARKERS):
        for pattern in INTERNAL_PROMPT_PATTERNS:
            text = re.sub(pattern, "[filtered internal instructions]", text, flags=re.I | re.S)
        if any(marker.lower() in text.lower() for marker in INTERNAL_PROMPT_MARKERS):
            return "[filtered internal instructions]"
    return text.strip()


def _truncate_value(value, limit=MAX_FIELD_CHARS):
    if isinstance(value, str):
        value = _sanitize_text(value)
        return value if len(value) <= limit else value[:limit] + "...[truncated]"
    if isinstance(value, list):
        cleaned = [_truncate_value(item, limit) for item in value[-20:]]
        return [item for item in cleaned if item]
    if isinstance(value, dict):
        return {key: _truncate_value(item, limit) for key, item in value.items()}
    return value


def _rotate_log_if_needed(log_path):
    if not log_path.exists() or log_path.stat().st_size < MAX_CHAT_LOG_BYTES:
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8]
    rotated_path = log_path.with_name(f"{log_path.stem}_{timestamp}{log_path.suffix}")
    log_path.rename(rotated_path)


def _write_record_to_db(record):
    if not CHAT_DB_ENABLED:
        return
    try:
        try:
            from bit.bit_db_api import insert_appeal_chat_record
        except ImportError:
            from bit_db_api import insert_appeal_chat_record
        insert_appeal_chat_record(record)
    except Exception as e:
        print(f"AI申诉聊天记录写入数据库失败：{e}", file=sys.stderr)


def start_appeal_log_collection():
    _COLLECTOR.records = []
    _COLLECTOR.run_id = uuid.uuid4().hex


def get_appeal_log_records():
    return list(getattr(_COLLECTOR, "records", []) or [])


def stop_appeal_log_collection():
    if hasattr(_COLLECTOR, "records"):
        delattr(_COLLECTOR, "records")
    if hasattr(_COLLECTOR, "run_id"):
        delattr(_COLLECTOR, "run_id")
    # Best-effort bounded drain. The local journal is already durable before enqueue.
    deadline = time.monotonic() + 2
    while _DB_QUEUE.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.05)


def _db_writer():
    while True:
        record = _DB_QUEUE.get()
        try:
            _write_record_to_db(record)
        finally:
            _DB_QUEUE.task_done()


def _enqueue_db_record(record):
    global _DB_THREAD
    if not CHAT_DB_ENABLED:
        return
    with _DB_THREAD_LOCK:
        if _DB_THREAD is None or not _DB_THREAD.is_alive():
            _DB_THREAD = threading.Thread(target=_db_writer, name="appeal-log-db", daemon=True)
            _DB_THREAD.start()
    try:
        _DB_QUEUE.put_nowait(record)
    except queue.Full:
        print("申诉数据库日志队列已满，记录已保留在本地日志中", file=sys.stderr)


def write_local_record(record, path=None):
    """Journal failures never cause a submitted appeal to be sent again."""
    log_path = Path(path or os.getenv("MERCADO_AI_CHAT_LOG", str(DEFAULT_CHAT_LOG)))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with locked_file(log_path.with_suffix(log_path.suffix + ".lock")):
            _rotate_log_if_needed(log_path)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
    except Exception as exc:
        print(f"申诉日志写入失败：{exc}", file=sys.stderr)
        # A unique recovery file cannot compete with another worker's rotation.
        try:
            recovery = log_path.with_name(f"{log_path.stem}_recovery_{uuid.uuid4().hex}.jsonl")
            with recovery.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except Exception as recovery_exc:
            print(f"申诉日志备份也失败：{recovery_exc}", file=sys.stderr)
    return log_path


def append_chat_log(window, site, event, message="", response="", chat=None, extra=None):
    log_path = Path(os.getenv("MERCADO_AI_CHAT_LOG", str(DEFAULT_CHAT_LOG)))
    record = {
        "event_id": uuid.uuid4().hex,
        "run_id": getattr(_COLLECTOR, "run_id", ""),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": str(window or ""),
        "site": str(site or ""),
        "event": str(event or ""),
        "message": _sanitize_text(message or ""),
        "response": _sanitize_text(response or ""),
        "chat": _truncate_value(chat or []),
        "extra": extra or {},
    }
    records = getattr(_COLLECTOR, "records", None)
    if records is not None:
        records.append(record)
    write_local_record(record, log_path)
    _enqueue_db_record(record)
    return log_path
