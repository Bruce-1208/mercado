"""Bound in-flight API calls across clients without holding slots during retries."""
from contextlib import contextmanager
import hashlib
import os
import threading
import time


class AdmissionTimeout(TimeoutError):
    """An interactive request could not acquire an API slot in time."""


def _limit(name, default):
    try:
        return max(1, min(128, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


class RequestBudget:
    def __init__(self, total=32, per_account=8):
        self.total = max(1, total)
        self.per_account = max(1, min(per_account, self.total))
        self.condition = threading.Condition()
        self.active = 0
        self.accounts = {}

    @contextmanager
    def slot(self, token, timeout=None):
        # Never retain OAuth credentials in budget keys or diagnostics.
        key = hashlib.sha256(str(token).encode()).digest()
        deadline = None if timeout is None else time.monotonic() + max(0, float(timeout))
        with self.condition:
            while self.active >= self.total or self.accounts.get(key, 0) >= self.per_account:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise AdmissionTimeout("API 请求队列繁忙，请稍后重试")
                self.condition.wait(remaining)
            self.active += 1
            self.accounts[key] = self.accounts.get(key, 0) + 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.accounts[key] -= 1
                if not self.accounts[key]:
                    del self.accounts[key]
                self.condition.notify_all()


_budget = RequestBudget(_limit("MERCADO_API_INFLIGHT_LIMIT", 32),
                        _limit("MERCADO_API_ACCOUNT_INFLIGHT_LIMIT", 8))
admission = _budget.slot
