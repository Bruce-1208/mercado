import json
import os
import re
from datetime import datetime
from pathlib import Path


DEFAULT_CHAT_LOG = Path(__file__).resolve().parent / "ai_chat_records.jsonl"
MAX_CHAT_LOG_BYTES = int(os.getenv("MERCADO_AI_CHAT_LOG_MAX_MB", "50")) * 1024 * 1024
MAX_FIELD_CHARS = int(os.getenv("MERCADO_AI_CHAT_LOG_FIELD_CHARS", "2000"))

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
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rotated_path = log_path.with_name(f"{log_path.stem}_{timestamp}{log_path.suffix}")
    log_path.rename(rotated_path)


def append_chat_log(window, site, event, message="", response="", chat=None, extra=None):
    log_path = Path(os.getenv("MERCADO_AI_CHAT_LOG", str(DEFAULT_CHAT_LOG)))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log_if_needed(log_path)
    record = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": str(window or ""),
        "site": str(site or ""),
        "event": str(event or ""),
        "message": _truncate_value(message or ""),
        "response": _truncate_value(response or ""),
        "chat": _truncate_value(chat or []),
        "extra": _truncate_value(extra or {}),
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return log_path
