import json
import threading

from bit import bit_interface, bit_reputation_info
import local_agent_worker


def test_agent_open_store_uses_local_window_lookup_and_fixed_reputation_url(monkeypatch):
    calls = []

    def open_store(token_id, **kwargs):
        calls.append((token_id, kwargs))
        return {
            "token_id": token_id,
            "shop_name": "控制台店铺",
            "window_id": "agent-local-window",
            "target_url": bit_reputation_info.REPUTATION_URL,
        }

    monkeypatch.setattr(bit_interface, "_open_mercado_claim_browser", open_store)
    result = local_agent_worker.run_open_store(
        {
            "token_id": 22,
            "token_record": {
                "id": 22,
                "display_name": "控制台店铺",
                "nickname": "SELLER_22",
            },
            "shop_name_hint": "控制台店铺",
        },
        threading.Event(),
    )

    assert result["status"] == "success"
    assert result["shop_name"] == "控制台店铺"
    assert calls == [(
        22,
        {
            "shop_name_hint": "控制台店铺",
            "target_url": bit_reputation_info.REPUTATION_URL,
            "token_record": {
                "id": 22,
                "display_name": "控制台店铺",
                "nickname": "SELLER_22",
            },
        },
    )]


def test_local_agent_resolves_profile_by_authorized_store_alias(monkeypatch):
    looked_up = []
    opened = []
    navigated = []
    released = []
    monkeypatch.setattr(
        bit_interface,
        "getBrowserIdByName",
        lambda name: looked_up.append(name) or "agent-local-window",
    )
    monkeypatch.setattr(
        bit_interface,
        "openBrowser",
        lambda window_id, **kwargs: opened.append((window_id, kwargs)) or {
            "success": True,
            "data": {"http": "127.0.0.1:9222"},
        },
    )
    monkeypatch.setattr(
        bit_interface,
        "_open_bitbrowser_page",
        lambda result, url: navigated.append((result, url)),
    )
    monkeypatch.setattr(
        bit_interface,
        "releaseBrowserLease",
        released.append,
    )
    monkeypatch.setattr(
        bit_interface,
        "list_shop_configs",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("Agent should resolve its own local profile")
        ),
    )

    result = bit_interface._open_mercado_claim_browser(
        22,
        shop_name_hint="控制台店铺",
        target_url=bit_reputation_info.REPUTATION_URL,
        token_record={
            "id": 22,
            "display_name": "控制台店铺",
            "nickname": "SELLER_22",
        },
    )

    assert result["window_id"] == "agent-local-window"
    assert looked_up == ["控制台店铺"]
    assert opened == [(
        "agent-local-window",
        {"api_lock_timeout": 5, "request_timeout": 20},
    )]
    assert navigated == [(
        {"success": True, "data": {"http": "127.0.0.1:9222"}},
        bit_reputation_info.REPUTATION_URL,
    )]
    assert released == ["agent-local-window"]


def test_open_store_worker_saves_actionable_error_result(monkeypatch, tmp_path):
    job_file = tmp_path / "job.json"
    job_file.write_text(
        json.dumps({"job_type": "open_store", "job_id": "open-store-job"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(local_agent_worker, "_watch_cancel", lambda *_args: None)
    monkeypatch.setattr(
        local_agent_worker,
        "run_open_store",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("本机比特浏览器 API 无法连接")
        ),
    )

    result_code = local_agent_worker.main([
        "--job-file",
        str(job_file),
        "--cancel-file",
        str(tmp_path / "cancel.requested"),
    ])

    assert result_code == 1
    assert json.loads(job_file.with_name("result.json").read_text(encoding="utf-8")) == {
        "status": "error",
        "message": "本机比特浏览器 API 无法连接",
    }
