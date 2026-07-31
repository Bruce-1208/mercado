import threading
import time
from concurrent.futures import ThreadPoolExecutor

from bit import bit_api, bit_runtime_lock


def test_browser_api_mutations_can_be_serialized_across_workers(monkeypatch, tmp_path):
    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(bit_api, "_BROWSER_API_MUTATION_CONCURRENCY", 1)

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
