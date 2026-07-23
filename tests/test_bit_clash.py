from concurrent.futures import ThreadPoolExecutor

from bit import bit_clash


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _configure_fake_clash(monkeypatch, tmp_path):
    monkeypatch.setattr(bit_clash.bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path)
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


def test_hongkong_ip_switch_has_no_time_cooldown(
    monkeypatch,
    tmp_path,
):
    calls = _configure_fake_clash(monkeypatch, tmp_path)

    first = bit_clash.switch_random_hongkong_node()
    second = bit_clash.switch_random_hongkong_node()

    assert first["switched"] is True
    assert second["switched"] is True
    assert first["remaining_seconds"] == 0
    assert second["remaining_seconds"] == 0
    assert calls == {"get": 2, "put": 2}


def test_concurrent_callers_are_serialized_without_time_cooldown(monkeypatch, tmp_path):
    calls = _configure_fake_clash(monkeypatch, tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: bit_clash.switch_random_hongkong_node(),
                range(8),
            )
        )

    assert sum(result["switched"] for result in results) == 8
    assert calls == {"get": 8, "put": 8}


def test_resolve_clash_api_uses_running_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "external-controller: '127.0.0.1:62180'\nsecret: ''\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CLASH_API_URL", raising=False)
    monkeypatch.setattr(
        bit_clash,
        "_running_clash_config_paths",
        lambda: [config_path],
    )

    assert bit_clash._resolve_clash_api_settings() == (
        "http://127.0.0.1:62180",
        "",
    )
