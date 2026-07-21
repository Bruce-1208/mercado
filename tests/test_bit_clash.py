import json
from concurrent.futures import ThreadPoolExecutor

from bit import bit_clash


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _configure_fake_clash(monkeypatch, tmp_path, now):
    monkeypatch.setattr(bit_clash.bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path)
    monkeypatch.setattr(bit_clash.time, "time", lambda: now[0])
    monkeypatch.setattr(bit_clash.random, "choice", lambda nodes: nodes[0])

    calls = {"get": 0, "put": 0}

    def fake_get(*args, **kwargs):
        calls["get"] += 1
        return FakeResponse(
            200,
            {"now": "香港 A", "all": ["香港 A", "香港 B", "日本 A"]},
        )

    def fake_put(*args, **kwargs):
        calls["put"] += 1
        return FakeResponse(204)

    monkeypatch.setattr(bit_clash.requests, "get", fake_get)
    monkeypatch.setattr(bit_clash.requests, "put", fake_put)
    return calls


def test_hongkong_ip_switch_has_shared_twenty_minute_cooldown(
    monkeypatch,
    tmp_path,
):
    now = [1_000.0]
    calls = _configure_fake_clash(monkeypatch, tmp_path, now)

    first = bit_clash.switch_random_hongkong_node()
    second = bit_clash.switch_random_hongkong_node()

    assert first["switched"] is True
    assert second["switched"] is False
    assert second["reason"] == "cooldown"
    assert second["remaining_seconds"] == 20 * 60
    assert calls == {"get": 1, "put": 1}

    now[0] += 20 * 60
    third = bit_clash.switch_random_hongkong_node()

    assert third["switched"] is True
    assert calls == {"get": 2, "put": 2}


def test_concurrent_callers_only_switch_once(monkeypatch, tmp_path):
    now = [2_000.0]
    calls = _configure_fake_clash(monkeypatch, tmp_path, now)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: bit_clash.switch_random_hongkong_node(),
                range(8),
            )
        )

    assert sum(result["switched"] for result in results) == 1
    assert calls == {"get": 1, "put": 1}

    state_path = tmp_path / bit_clash.HONGKONG_IP_SWITCH_STATE_FILE
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["switch_succeeded"] is True
    assert state["new_node"] == "香港 B"
