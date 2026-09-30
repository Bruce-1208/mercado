"""Public news and policy updates from Mercado Libre's Chinese official site."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import html
import re
import threading
import time
from urllib.parse import urlencode

import requests

SOURCES = (("拉美资讯", "latam"), ("平台功能更新", "platform-updates"), ("政策新规", "policy-updates"))
_lock = threading.Lock()
_cache = None


def _plain(value, limit):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))).strip()[:limit]


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError):
        return 0


def fetch_category(name, slug):
    response = requests.get(
        "https://mercadolibre.cn/api/get_news_list",
        params={"name": name, "offset": 0, "limit": 50}, timeout=15,
    )
    response.raise_for_status()
    rows = response.json()["Data"]["data"]
    if not isinstance(rows, list):
        raise ValueError("官方新闻返回格式错误")
    notices = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("post_guid") or "").strip()
        title = _plain(row.get("title"), 240)
        if not identifier or not title:
            continue
        notices.append({
            "id": identifier, "title": title,
            "description": _plain(row.get("summary"), 300),
            "from_date": str(row.get("publish_time") or ""),
            "category_label": name, "source_name": "美客多中国官网",
            "action_url": "https://mercadolibre.cn/news/info?" + urlencode({"post_guid": identifier, "type": slug}),
            "action_text": "查看官方原文",
        })
    return notices


def get_official_news(*, force_refresh=False):
    global _cache
    with _lock:
        if not force_refresh and _cache and _cache[0] > time.monotonic():
            return _cache[1]
    notices, failures = {}, []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [(name, executor.submit(fetch_category, name, slug)) for name, slug in SOURCES]
        for name, future in futures:
            try:
                for row in future.result():
                    notices.setdefault(row["id"], row)
            except Exception:
                failures.append(name)
    if len(failures) == len(SOURCES):
        raise RuntimeError("官方新闻暂不可用，请稍后刷新")
    data = {
        "notices": sorted(notices.values(), key=lambda row: _timestamp(row["from_date"]), reverse=True)[:12],
        "source_name": "美客多中国官网", "failed_sources": failures,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if not failures:
        with _lock:
            _cache = (time.monotonic() + 900, data)
    return data
