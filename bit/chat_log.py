import json
import os
from datetime import datetime
from pathlib import Path


DEFAULT_CHAT_LOG = Path(__file__).resolve().parent / "ai_chat_records.jsonl"


def append_chat_log(window, site, event, message="", response="", chat=None, extra=None):
    log_path = Path(os.getenv("MERCADO_AI_CHAT_LOG", str(DEFAULT_CHAT_LOG)))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": str(window or ""),
        "site": str(site or ""),
        "event": str(event or ""),
        "message": message or "",
        "response": response or "",
        "chat": chat or [],
        "extra": extra or {},
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return log_path
