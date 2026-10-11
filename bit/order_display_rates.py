"""Latest published USD/CNY reference rate for order display (hourly cache)."""
from datetime import datetime, timedelta
from threading import Lock
from time import monotonic
from zoneinfo import ZoneInfo
import math

from erp.ecb_exchange_rates import fetch_usd_cny_daily_rates

_lock = Lock()
_snapshot = None
_checked_at = 0.0


def latest_order_display_rate():
    global _snapshot, _checked_at
    with _lock:
        if _snapshot is not None and monotonic() - _checked_at < 3600:
            return dict(_snapshot)
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        rows = fetch_usd_cny_daily_rates(today - timedelta(days=14), today)
        valid = [row for row in rows
                 if (today - timedelta(days=7)).isoformat() <= str(row.get("creation_date", ""))[:10] <= today.isoformat()
                 and math.isfinite(float(row.get("ratio", 0))) and float(row.get("ratio", 0)) > 0]
        if not valid:
            raise ValueError("暂未获取到近期美元兑人民币汇率")
        latest = max(valid, key=lambda row: row["creation_date"])
        _snapshot = {"cny_per_usd": float(latest["ratio"]),
                     "rate_date": latest["creation_date"][:10],
                     "source": "欧洲央行参考汇率"}
        _checked_at = monotonic()
        return dict(_snapshot)
