import threading
import time
from concurrent.futures import ThreadPoolExecutor

from bit import bit_api, bit_runtime_lock


def test_browser_api_mutation_default_uses_three_bounded_slots():
    assert bit_api.DEFAULT_BROWSER_API_MUTATION_CONCURRENCY == 3


def test_browser_api_mutations_can_be_serialized_across_workers(monkeypatch, tmp_path):
    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(bit_api, "_BROWSER_API_MUTATION_CONCURRENCY", 1)
    monkeypatch.setattr(bit_api, "_BROWSER_OPEN_COOLDOWN_SECONDS", 0)

    guard = threading.Lock()
    active = 0
    max_active = 0

    class Response:
        def json(self):
            return {"success": True}

    def fake_post(*args, **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return Response()

    monkeypatch.setattr(bit_api.requests, "post", fake_post)

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(
            executor.map(
                lambda index: bit_api._post_browser_mutation(
                    "open",
                    f"window-{index}",
                    5,
                ),
                range(6),
            )
        )

    assert max_active == 1
    assert results == [{"success": True}] * 6


def test_browser_api_slot_order_is_stable_and_covers_every_slot(monkeypatch):
    monkeypatch.setattr(bit_api, "_BROWSER_API_MUTATION_CONCURRENCY", 3)

    first = bit_api._browser_api_slot_order("window-1")
    second = bit_api._browser_api_slot_order("window-1")

    assert first == second
    assert set(first) == {0, 1, 2}


def test_close_browser_forwards_short_api_lock_timeout(monkeypatch):
    calls = []

    class Lease:
        acquired = True

    monkeypatch.setattr(
        bit_api,
        "_post_browser_mutation",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True},
    )

    result = bit_api.closeBrowser(
        "window-1",
        lease=Lease(),
        request_timeout=3,
        api_lock_timeout=5,
    )

    assert result == {"success": True}
    assert calls == [(("close", "window-1", 3), {"api_lock_timeout": 5})]


def test_get_browser_id_by_name_uses_unique_exact_window_name(monkeypatch):
    calls = []

    class Response:
        def json(self):
            return {
                "success": True,
                "data": {
                    "list": [
                        {"id": "partial", "name": "智赢专用窗口-备份"},
                        {"id": "wanted", "name": "智赢专用窗口"},
                    ]
                },
            }

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(bit_api.requests, "post", fake_post)

    assert bit_api.getBrowserIdByName(" 智赢专用窗口 ") == "wanted"
    assert calls[0][0].endswith("/browser/list")


def test_get_browser_id_by_name_rejects_duplicate_exact_names(monkeypatch):
    class Response:
        def json(self):
            return {
                "success": True,
                "data": {
                    "list": [
                        {"id": "one", "name": "同名窗口"},
                        {"id": "two", "name": "同名窗口"},
                    ]
                },
            }

    monkeypatch.setattr(bit_api.requests, "post", lambda *args, **kwargs: Response())

    try:
        bit_api.getBrowserIdByName("同名窗口")
    except RuntimeError as exc:
        assert "同名" in str(exc)
    else:
        raise AssertionError("duplicate browser names must be rejected")


def test_get_browser_id_by_name_accepts_internal_space_difference():
    browsers = [
        {"id": "window-2", "name": "蒋学斌 2"},
        {"id": "other", "name": "其他店铺"},
    ]

    assert bit_api.getBrowserIdByName("蒋学斌2", browsers=browsers) == "window-2"


def test_get_browser_id_by_name_rejects_duplicate_compact_names():
    browsers = [
        {"id": "one", "name": "蒋学斌 2"},
        {"id": "two", "name": "蒋 学斌2"},
    ]

    try:
        bit_api.getBrowserIdByName("蒋学斌2", browsers=browsers)
    except RuntimeError as exc:
        assert "同名" in str(exc)
    else:
        raise AssertionError("duplicate compact browser names must be rejected")
