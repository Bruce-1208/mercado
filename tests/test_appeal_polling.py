import pytest

from bit import bit_interface
from bit.local_agent_hub import LocalAgentStore


@pytest.fixture
def polling(monkeypatch, tmp_path):
    user = {"id": 7, "permissions": ["appeal.view", "appeal.execute"], "access_version": 1}
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    store.heartbeat("polling-agent", name="测试执行电脑", capabilities=["appeal"])
    monkeypatch.setattr(bit_interface.app, "testing", True)
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setattr(bit_interface, "get_current_workbench_user", lambda: user)
    monkeypatch.setattr(bit_interface, "get_local_agent_store", lambda: store)
    monkeypatch.setattr(bit_interface, "current_local_agent_bundle", lambda: {"version": "test"})
    monkeypatch.setattr(bit_interface, "validate_authorized_appeal_sites", lambda name, sites: tuple(sites))
    monkeypatch.setattr(bit_interface, "_appeal_poll_maintenance_at", 0.0)
    return user, store, bit_interface.app.test_client()


def submit(client, task_id="polling-job"):
    return client.post("/api/run_shensu", json={
        "name": "测试店铺", "sites": ["墨西哥"], "forms": ["侵权"],
        "mode": "AI客服", "loop_count": 10, "execution_target": "agent",
        "agent_id": "polling-agent", "task_id": task_id, "log_transport": "poll",
    })


def test_pending_jobs_release_response_without_streaming(polling, monkeypatch):
    user, store, client = polling
    def forbidden_stream(*args):
        pytest.fail("Polling must not start a streaming response")
    monkeypatch.setattr(bit_interface, "stream_local_agent_job", forbidden_stream)
    for index in range(100):
        task_id = f"polling-job-{index}"
        response = submit(client, task_id)
        assert response.status_code == 200
        assert response.get_json()["data"]["log_transport"] == "poll"
        assert store.get_job(task_id)["status"] == "queued"
        result = client.get(f"/api/run_shensu/{task_id}/events").get_json()["data"]
        assert result["done"] is False
    assert len(store.list_jobs(limit=200)) == 100


def test_terminal_logs_drain_all_pages_and_do_not_expose_payload(polling):
    _, store, client = polling
    assert submit(client).status_code == 200
    store.claim_job("polling-agent")
    for index in range(205):
        store.append_event("polling-job", "polling-agent", content=f"line-{index}\n")
    store.append_event("polling-job", "polling-agent", content="final-line\n", status="error", message="执行失败")
    first = client.get("/api/run_shensu/polling-job/events").get_json()["data"]
    assert first["status"] == "error"
    assert first["has_more"] and not first["done"]
    second = client.get(f'/api/run_shensu/polling-job/events?after={first["next_after"]}').get_json()["data"]
    assert second["done"] and not second["has_more"]
    assert "final-line" in second["log"]
    assert "line-0\n" not in second["log"]
    assert "payload" not in second
    empty = client.get(f'/api/run_shensu/polling-job/events?after={second["next_after"]}').get_json()["data"]
    assert empty["log"] == ""
    assert empty["done"]


def test_poll_requires_permission_and_owner(polling):
    user, store, client = polling
    submit(client)
    user["id"] = 8
    assert client.get("/api/run_shensu/polling-job/events").status_code == 404
    user["is_platform_admin"] = True
    assert client.get("/api/run_shensu/polling-job/events").status_code == 200
    user["permissions"] = []
    assert client.get("/api/run_shensu/polling-job/events").status_code == 403


@pytest.mark.parametrize("after", ["-1", "oops", "9223372036854775808"])
def test_poll_rejects_invalid_cursor(polling, after):
    _, _, client = polling
    submit(client)
    assert client.get(f"/api/run_shensu/polling-job/events?after={after}").status_code == 400


def test_queue_maintenance_is_shared_between_viewers(polling, monkeypatch):
    _, store, client = polling
    submit(client)
    calls = []
    monkeypatch.setattr(store, "reap_expired_jobs", lambda: calls.append(True))
    for _ in range(10):
        assert client.get("/api/run_shensu/polling-job/events").status_code == 200
    assert calls == [True]


def test_poll_reports_stopping_until_agent_acknowledges(polling):
    _, store, client = polling
    submit(client)
    store.claim_job("polling-agent")
    assert client.post("/api/run_shensu/stop", json={"task_id": "polling-job"}).status_code == 200
    state = client.get("/api/run_shensu/polling-job/events").get_json()["data"]
    assert state["cancel_requested"] and not state["done"]
    store.append_event("polling-job", "polling-agent", status="stopped", message="停止完成")
    state = client.get("/api/run_shensu/polling-job/events").get_json()["data"]
    assert state["done"] and state["status"] == "stopped"


def test_large_log_events_are_split_by_byte_budget(polling):
    _, store, client = polling
    submit(client)
    store.claim_job("polling-agent")
    for marker in ("A", "B", "C"):
        store.append_event("polling-job", "polling-agent", content=marker * 300000)
    store.append_event("polling-job", "polling-agent", status="success")
    after = 0
    for index, marker in enumerate(("A", "B", "C")):
        data = client.get(f"/api/run_shensu/polling-job/events?after={after}").get_json()["data"]
        assert marker * 300000 in data["log"]
        assert len(data["log"].encode()) < 512 * 1024
        assert data["done"] is (index == 2)
        after = data["next_after"]


def test_hundred_http_polls_complete_with_four_wsgi_workers(polling):
    """An isolated HTTP check, never connecting to production jobs or agents."""
    import json
    import threading
    import time
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor
    from waitress import create_server

    _, store, client = polling
    for index in range(100):
        assert submit(client, f"http-job-{index}").status_code == 200
    server_map = {}
    server = create_server(bit_interface.app, host="127.0.0.1", port=0,
                           threads=4, map=server_map)
    stopping = threading.Event()
    def run_server():
        while not stopping.is_set():
            server.asyncore.loop(timeout=0.02, count=1, map=server_map)
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.effective_port}"

    def poll(index):
        started = time.monotonic()
        with urllib.request.urlopen(f"{base}/api/run_shensu/http-job-{index}/events", timeout=10) as response:
            data = json.load(response)["data"]
        assert data["status"] == "queued" and not data["done"]
        return time.monotonic() - started

    try:
        with ThreadPoolExecutor(max_workers=100) as workers:
            timings = sorted(workers.map(poll, range(100)))
        print(f"Isolated log polling: 100 requests, 4 WSGI workers, p95={timings[94]:.3f}s, max={max(timings):.3f}s")
        assert len(timings) == 100
    finally:
        stopping.set()
        thread.join(timeout=2)
        server.task_dispatcher.shutdown()
        server.close()
        # The test owns this private asyncore map and every channel in it.
        for channel in list(server_map.values()):
            channel.close()
