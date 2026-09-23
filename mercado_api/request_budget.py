"""Bound in-flight API calls across clients without holding slots during retries."""
from contextlib import contextmanager
import hashlib
import os
import threading


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
    def slot(self, token):
        # Never retain OAuth credentials in budget keys or diagnostics.
        key = hashlib.sha256(str(token).encode()).digest()
        with self.condition:
            while self.active >= self.total or self.accounts.get(key, 0) >= self.per_account:
                self.condition.wait()
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
