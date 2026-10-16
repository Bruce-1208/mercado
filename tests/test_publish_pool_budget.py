"""Offline regressions for publication with the isolated worker's DB budget."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from bit.db_pool import PoolAcquireTimeout
from erp import mercadolibre_batch_publish as publish
from test_erp_mercadolibre_batch_publish import _rows


@pytest.mark.parametrize("limit,reserved,expected", [(4, 1, 2), (12, 1, 10), (1, 1, 1), (4, 0, 3)])
def test_budget_accounts_for_auth_and_background(monkeypatch, limit, reserved, expected):
    monkeypatch.setenv("MYSQL_POOL_MAX_CONNECTIONS", str(limit))
    monkeypatch.setenv("MYSQL_POOL_AUTH_RESERVED", str(reserved))
    assert publish.publication_worker_limit() == expected


def test_concurrent_batches_share_budget_and_do_not_republish_on_state_timeout(monkeypatch):
    monkeypatch.setenv("MYSQL_POOL_MAX_CONNECTIONS", "4")
    monkeypatch.setenv("MYSQL_POOL_AUTH_RESERVED", "1")
    monkeypatch.setattr(publish, "_token_record", lambda _: {"access_token": "offline"})
    guard = threading.Lock()
    active = peak = 0
    remote_calls = []
    saved = set()
    timed_out = set()

    def remote(_client, url, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            remote_calls.append(url)
        time.sleep(.02)
        with guard:
            active -= 1
        return {"result": {"id": "CBT1"}}

    def save(product_id, **changes):
        if changes["status"] == "published":
            with guard:
                if product_id not in timed_out:
                    timed_out.add(product_id)
                    raise PoolAcquireTimeout("busy")
                saved.add(product_id)

    monkeypatch.setattr(publish, "follow_sell", remote)

    def batch(offset):
        rows = [{**_rows()[0], "id": offset + i,
                 "source_url": f"https://example/MLM{offset + i}"} for i in range(8)]
        return publish.publish_product_batch(rows, token_id=1, workers=16,
                                             client=object(), update_state=save)

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(batch, (100, 200, 300)))
    assert all(result["worker_count"] == 2 for result in results)
    assert all(result["published_count"] == 8 for result in results)
    assert 1 < peak <= 2
    assert len(remote_calls) == len(set(remote_calls)) == len(saved) == 24
    assert publish._active_publishers == 0


def test_slot_released_even_when_progress_callback_fails(monkeypatch):
    monkeypatch.setenv("MYSQL_POOL_MAX_CONNECTIONS", "4")
    monkeypatch.setattr(publish, "_token_record", lambda _: {"access_token": "offline"})
    monkeypatch.setattr(publish, "follow_sell", lambda *a, **k: {"result": {"id": "CBT1"}})
    def fail(_):
        raise RuntimeError("progress failed")
    with pytest.raises(RuntimeError, match="progress failed"):
        publish.publish_product_batch(_rows(), token_id=1, client=object(),
                                      update_state=lambda *a, **k: None, on_progress=fail)
    assert publish._active_publishers == 0


@pytest.mark.parametrize("error,expected", [(PoolAcquireTimeout("busy"), 3), (RuntimeError("SQL failed"), 1)])
def test_state_retry_is_bounded_and_only_for_admission_failure(monkeypatch, error, expected):
    calls = []
    monkeypatch.setattr(publish.time, "sleep", lambda _: None)
    def save(*args, **kwargs):
        calls.append(1)
        raise error
    with pytest.raises(type(error)):
        publish._save_product_state(save, 1, status="failed")
    assert len(calls) == expected
