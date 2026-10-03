import json
import multiprocessing
import time
from types import SimpleNamespace

import pytest
import requests

from bit import bit_api as api, bit_browser_api_rate as rate, bit_runtime_lock as locks


@pytest.fixture
def clock(monkeypatch, tmp_path):
    class Clock:
        now = 1000.0

        def sleep(self, delay):
            self.now += delay

    clock = Clock()
    monkeypatch.setattr(locks, "RUNTIME_LOCK_DIR", tmp_path)
    monkeypatch.setattr(rate, "_now", lambda: clock.now)
    monkeypatch.setattr(rate, "_sleep", clock.sleep)
    monkeypatch.delenv("BIT_BROWSER_API_MIN_INTERVAL_SECONDS", raising=False)
    return clock


def test_opener_read_and_cleanup_share_one_request_budget(clock, monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url.rsplit("/", 1)[-1], clock.now))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"success": True, "data": {}}).encode()
        return response

    monkeypatch.setattr(api.requests, "post", post)
    api._post_browser_api("open", {"id": "a"}, 5)
    api.getAllBrowserPids()
    api._post_browser_api("close", {"id": "a"}, 5)
    api.listBrowsers()
    assert [at for _, at in calls] == [1000, 1000.25, 1000.5, 1000.75]


def test_explicit_open_limit_cools_other_windows_and_escalates(clock):
    refusals = {"success": False, "msg": "打开窗口失败，请尝试降低接口请求频率重试"}
    for delay in (10, 20, 40, 60, 60):
        began = clock.now
        rate.record_response("open", refusals)
        rate.wait_for_request("open", 180)
        assert clock.now - began == delay
    rate.record_response("open", {"success": True})
    began = clock.now
    rate.record_response("open", refusals)
    rate.wait_for_request("open", 180)
    assert clock.now - began == 10


def test_open_cooldown_still_allows_pid_verification_and_cleanup(clock):
    rate.record_response("open", {"success": False, "msg": "降低接口请求频率"})
    rate.wait_for_request("pids/alive", 5)
    rate.wait_for_request("close", 5)
    assert clock.now == 1000.25
    rate.wait_for_request("open", 15)
    assert clock.now == 1010


def test_waiting_opener_releases_mutation_slot_for_cleanup(clock, monkeypatch):
    rate.record_response("open", {"success": False, "msg": "降低接口请求频率"})
    monkeypatch.setattr(api, "_BROWSER_OPEN_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(api, "_BROWSER_API_MUTATION_CONCURRENCY", 1)
    calls = []
    monkeypatch.setattr(api, "_send_browser_api", lambda endpoint, *a: calls.append(endpoint) or {"success": True})
    original_sleep = clock.sleep

    def sleep_with_cleanup(delay):
        if not calls:
            api._post_browser_mutation("close", "old", 5, api_lock_timeout=1)
        original_sleep(delay)

    monkeypatch.setattr(rate, "_sleep", sleep_with_cleanup)
    api._post_browser_mutation("open", "new", 5, api_lock_timeout=15)
    assert calls == ["close", "open"]


def test_explicit_429_cools_reads_and_is_not_automatically_replayed(clock, monkeypatch):
    calls = []

    def post(*args, **kwargs):
        calls.append(True)
        response = requests.Response()
        response.status_code = 429
        response._content = b"not json"
        return response

    monkeypatch.setattr(api.requests, "post", post)
    with pytest.raises(requests.HTTPError):
        api._post_browser_api("open", {"id": "a"}, 5)
    assert len(calls) == 1
    before = clock.now
    rate.wait_for_request("pids/all", 15)
    assert clock.now - before == 10


@pytest.mark.parametrize("message", ["Error", "窗口正在打开", "不允许多个账号同时打开"])
def test_ordinary_error_does_not_get_misclassified_as_limit(clock, message):
    rate.record_response("open", {"success": False, "msg": message})
    rate.wait_for_request("open", 0)
    assert clock.now == 1000


def test_cooldown_deadline_prevents_request_without_resetting_shared_state(clock, monkeypatch):
    rate.record_response("open", {"success": False, "msg": "降低接口请求频率"})
    monkeypatch.setattr(api.requests, "post", lambda *a, **kw: pytest.fail("must not send"))
    with pytest.raises(TimeoutError, match="本次未发送"):
        api._post_browser_api("open", {"id": "a"}, 5, api_lock_timeout=1)
    rate.wait_for_request("open", 15)
    assert clock.now == 1010


def _request_worker(directory, start, result):
    from pathlib import Path
    locks.RUNTIME_LOCK_DIR = Path(directory)
    rate.request_interval = lambda: 0.05
    start.wait(10)
    rate.wait_for_request("pids/all", 10)
    result.put(time.time())


def test_independent_processes_share_the_rate_budget(tmp_path):
    context = multiprocessing.get_context("spawn")
    start, result = context.Event(), context.Queue()
    workers = [context.Process(target=_request_worker, args=(str(tmp_path), start, result)) for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        start.set()
        times = sorted(result.get(timeout=15) for _ in workers)
        for worker in workers:
            worker.join(5)
            assert worker.exitcode == 0
        assert all(right - left >= 0.04 for left, right in zip(times, times[1:]))
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        result.close()


def test_concurrent_slots_still_share_open_spacing(clock):
    rate.wait_for_request("open", 5, open_interval=2)
    rate.wait_for_request("open", 5, open_interval=2)
    assert clock.now == 1002


def _cooldown_worker(directory, result):
    from pathlib import Path
    locks.RUNTIME_LOCK_DIR = Path(directory)
    rate._now = lambda: 1000
    result.put(rate.try_request("open"))


def test_one_process_rate_limit_is_seen_by_next_worker(clock, tmp_path):
    rate.record_response("open", {"success": False, "msg": "降低接口请求频率"})
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    worker = context.Process(target=_cooldown_worker, args=(str(tmp_path), result))
    try:
        worker.start()
        assert result.get(timeout=10) == 10
        worker.join(5)
        assert worker.exitcode == 0
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        result.close()


def test_appeal_retries_obey_shared_backoff_and_recover(clock, monkeypatch):
    from bit import bit_appeal_ai as ai
    attempts = []
    monkeypatch.setattr(api, "_BROWSER_OPEN_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(api, "_BROWSER_API_MUTATION_CONCURRENCY", 1)

    def post(*args, **kwargs):
        attempts.append(clock.now)
        result = ({"success": False, "msg": "降低接口请求频率"} if len(attempts) < 3
                  else {"success": True, "data": {"driver": "fake", "http": "localhost:1234"}})
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(result).encode()
        return response

    driver = SimpleNamespace(implicitly_wait=lambda *a: None, set_page_load_timeout=lambda *a: None,
                             set_script_timeout=lambda *a: None)
    monkeypatch.setattr(api.requests, "post", post)
    monkeypatch.setattr(ai, "openBrowser", lambda window: api._post_browser_mutation("open", window, 5))
    monkeypatch.setattr(ai, "time", SimpleNamespace(sleep=clock.sleep))
    monkeypatch.setattr(ai.webdriver, "Chrome", lambda **kw: driver)
    monkeypatch.setattr(ai, "Service", lambda *a: None)
    returned, result = ai.connect_bit_browser("test-window")
    assert returned is driver and result["success"] is True
    assert attempts == [1000, 1010, 1030]
