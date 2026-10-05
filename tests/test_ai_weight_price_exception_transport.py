"""Original business errors must survive Agent persistence and JSON transport."""
import json

import pytest

from erp.ai_weight_price.store import AgentStore, RemoteStore, Store


@pytest.mark.parametrize("backend", ["local", "remote", "agent"])
@pytest.mark.parametrize("keyword", [False, True])
def test_exception_preserves_original_error(tmp_path, monkeypatch, backend, keyword):
    server = Store(tmp_path / "server")
    actor = {"id": 7, "display_name": "seller"}
    server.set_actor(actor)

    def request(method, path, **kwargs):
        # Simulate the actual HTTP JSON boundary, including batch sync.
        body = json.loads(json.dumps(kwargs["json"]))
        with server.actor_scope(body["actor"]):
            if body["method"] == "apply_agent_batch":
                # Replay transported operations independently on SQLite;
                # production batch transactions use the MySQL backend.
                events = body["args"][1]
                for event in events:
                    getattr(server, event["method"])(*event["args"], **event["kwargs"])
                return {"applied": [event["event_id"] for event in events]}
            return getattr(server, body["method"])(*body["args"], **body["kwargs"])

    monkeypatch.setattr("bit.bit_db_api._request", request)
    if backend == "agent":
        store = AgentStore(tmp_path / "agent")
        monkeypatch.setattr(store, "_ensure_sync_thread", lambda: None)
        store.set_actor(actor)
    elif backend == "remote":
        store = RemoteStore(tmp_path / "remote")
        store.set_actor(actor)
    else:
        store = server
    store.add({"erp_goods_id": "g1", "title": "cup"})
    reason = "重量校验误差超出阈值"
    detail = "基准 450g，对照 900g，差值超过允许范围"
    if keyword:
        store.exception(key="g1", reason=reason, detail=ValueError(detail))
    else:
        store.exception("g1", reason, ValueError(detail))

    local = store.get("g1")
    assert local["status"] == "exception"
    assert local["exception_reason"] == reason
    assert local["exception_detail"] == detail
    if backend == "agent":
        assert store.pending_sync_count() == 2
        assert store._sync_once() == 2
        assert store.pending_sync_count() == 0
    saved = server.get("g1")
    assert saved["status"] == "exception"
    assert saved["exception_detail"] == detail
    assert server.logs()[-1]["message"] == reason + "：" + detail
