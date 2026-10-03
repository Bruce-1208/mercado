import json
import threading
from datetime import datetime, timedelta

import pytest
from flask import Flask

from bit import bit_runtime_lock, wipo_routes, wipo_sync
from bit.wipo_sync import LOCAL, WipoSyncService, fetch_feed, normalize_feed_rows, validate_feed_url


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path / "locks")
    return WipoSyncService("test-encryption-key", path=tmp_path / "wipo.sqlite3")


def test_feed_deduplicates_and_queues_review_even_for_expired_marks():
    rows, summary = normalize_feed_rows([
        {"Brand name": "  Example   Mark ", "registration_number": "100", "status": "Expired", "list_type": "whitelist"},
        {"mark": "example mark", "number": "101", "country": "MX"},
        {"Trademark": "中文商标", "Nice class": "25"},
        {"registration_number": "102"},
    ])
    assert summary == {"fetched": 4, "unique": 2, "skipped": 1}
    assert rows[0]["evidence_count"] == 2
    assert all(row["list_type"] == "pending" for row in rows)
    detail = json.loads(rows[0]["source_detail"])
    assert detail["status"] == "Expired"
    assert detail["registration"] == ["100", "101"] and detail["country"] == "MX"


@pytest.mark.parametrize("url", ["http://feed.example/data", "https://127.0.0.1/data",
    "https://localhost/data", "https://user:secret@feed.example/data", "https://branddb.wipo.int/en/",
    "https://feed.example:5000/data"])
def test_feed_addresses_reject_search_pages_and_private_targets(url):
    with pytest.raises(ValueError):
        validate_feed_url(url)


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def iter_content(self, _):
        yield self.body.encode()


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = iter(responses), []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(wipo_sync.socket, "getaddrinfo", lambda *_args, **_kw: [
        (2, 1, 6, "", ("8.8.8.8", 443))])


def test_json_fetch_follows_same_origin_pages_and_reports_complete_count(public_dns):
    session = FakeSession(FakeResponse('{"records":[{"mark":"Alpha"}],"total":2,"next":"?page=2"}'),
                          FakeResponse('{"records":[{"mark":"Beta"}],"total":2}'))
    rows, counts = fetch_feed({"feed_url": "https://feed.example/data", "format": "auto"}, "secret", session=session)
    assert counts["unique"] == 2
    assert [row["brand_name"] for row in rows] == ["Alpha", "Beta"]
    assert session.calls[1][0] == "https://feed.example/data?page=2"
    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer secret"
    assert session.calls[0][1]["allow_redirects"] is False


def test_cross_origin_pagination_never_sends_credentials(public_dns):
    session = FakeSession(FakeResponse('{"records":[{"mark":"Alpha"}],"next":"https://other.example/data"}'))
    with pytest.raises(ValueError, match="同一域名"):
        fetch_feed({"feed_url": "https://feed.example/data", "format": "json"}, "secret", session=session)
    assert len(session.calls) == 1


@pytest.mark.parametrize("body", ["<html>login</html>", '{"records":[{"mark":"A"}],"total":2}',
    '{"records":[{"mark":"A"}],"has_more":true}', '{"error":"bad auth"}',
    '{"records":[]}', "[null]"])
def test_malformed_or_incomplete_payload_cannot_be_imported(public_dns, body):
    with pytest.raises(ValueError):
        fetch_feed({"feed_url": "https://feed.example/data", "format": "auto"}, session=FakeSession(FakeResponse(body)))


def test_csv_fetch_handles_bom_quotes_and_unicode(public_dns):
    rows, _ = fetch_feed({"feed_url": "https://feed.example/data", "format": "csv"},
                        session=FakeSession(FakeResponse('\ufeffBrand name,Number\n"ACME, Inc.",100\n中文商标,101\n')))
    assert [row["brand_name"] for row in rows] == ["ACME, Inc.", "中文商标"]


def test_secret_encrypted_write_only_and_settings_persist(service):
    result = service.save_settings({"source_mode": "feed", "feed_url": "https://feed.example/data", "token": "my-secret"})
    assert result["has_token"] is True
    assert "my-secret" not in service.path.read_bytes().decode(errors="ignore")
    assert "feed_url" not in service.settings(manage=False)
    assert "has_token" not in service.settings(manage=False)
    clone = WipoSyncService("test-encryption-key", path=service.path)
    assert clone.settings() == result
    assert clone.save_settings({"token": ""})["has_token"] is True
    assert clone.save_settings({"clear_token": True})["has_token"] is False


def test_manual_sync_dedupe_lock_success_and_history(service):
    entered, resume = threading.Event(), threading.Event()
    written = []
    service.save_settings({"source_mode": "feed", "feed_url": "https://feed.example/data", "enabled": True})
    def fetcher(*_):
        entered.set()
        assert resume.wait(5)
        return normalize_feed_rows([{"mark": "Alpha"}])
    service.fetcher = fetcher
    service.writer = lambda rows: written.extend(rows) or {
        "inserted": 1, "updated": 0, "unchanged": 0, "restored": 0,
        "skipped_manual": 2, "skipped_deleted": 3,
    }
    assert service.start_sync()["started"] is True
    assert entered.wait(5)
    assert service.start_sync()["started"] is False
    assert service.status()["running"] is True
    resume.set()
    service.thread.join(5)
    state = service.status()
    assert state["status"] == "success" and state["running"] is False
    assert state["summary"]["inserted"] == 1
    assert len(written) == 1 and len(state["history"]) == 1
    assert written[0]["list_type"] == "pending"
    assert "保留人工处置 2" in state["message"]
    assert "跳过已删除 3" in state["message"]
    assert "需人工复核" in state["message"]
    assert state["last_success_at"] and state["next_run_at"]
    assert service.tick() is False  # Persisted next run prevents a second fetch.


def test_failed_fetch_preserves_success_and_retries_in_an_hour(service):
    service.save_settings({"source_mode": "feed", "feed_url": "https://feed.example/data"})
    with service.database() as db:
        service._write(db, "state", {**wipo_sync.DEFAULT_STATE, "last_success_at": "previous-success"})
    def broken(*_):
        raise ValueError("数据源没有可用的文字商标")
    service.fetcher = broken
    service.writer = lambda _: pytest.fail("invalid feed must not be written")
    service.start_sync()
    service.thread.join(5)
    state = service.status()
    assert state["status"] == "error"
    assert state["last_success_at"] == "previous-success"
    assert timedelta() < datetime.fromisoformat(state["next_run_at"]) - datetime.now(LOCAL) < timedelta(hours=1, seconds=1)


def test_disabled_scheduler_and_interrupted_recovery(service):
    service.save_settings({"enabled": False})
    assert service.tick() is False
    with service.database() as db:
        service._write(db, "state", {**wipo_sync.DEFAULT_STATE, "running": True, "status": "running"})
    state = service.status()
    assert state["status"] == "interrupted"
    assert state["running"] is False
    assert state["next_run_at"] == ""


def test_lock_belongs_to_worker_after_request_thread_exits(service):
    resume, entered = threading.Event(), threading.Event()
    service.save_settings({"source_mode": "feed", "feed_url": "https://feed.example/data"})
    def fetcher(*_):
        entered.set()
        assert resume.wait(5)
        return normalize_feed_rows([{"mark": "Alpha"}])
    service.fetcher = fetcher
    service.writer = lambda _: {"inserted": 1}
    request_thread = threading.Thread(target=service.start_sync)
    request_thread.start()
    request_thread.join(5)
    assert entered.wait(5)
    lock = bit_runtime_lock.InterProcessLock(service.lock_key)
    assert lock.read_owner()["thread_id"] == service.thread.ident
    assert lock._is_stale() is False
    assert service.status()["running"] is True
    resume.set()
    service.thread.join(5)
    assert service.status()["status"] == "success"


def test_madrid_cursor_advances_only_after_successful_write(service):
    through = (datetime.now(LOCAL) - timedelta(days=2)).date().isoformat()
    service.fetcher = lambda *_: ([{"brand_name": "A"}], {"fetched": 1, "unique": 1, "madrid_through_date": through})
    service.writer = lambda _: {"inserted": 1}
    service.start_sync()
    service.thread.join(5)
    assert service.status()["madrid_through_date"] == through
    def broken(_):
        raise RuntimeError("database unavailable")
    service.writer = broken
    service.start_sync()
    service.thread.join(5)
    assert service.status()["madrid_through_date"] == through


def test_editing_backfill_date_during_run_resets_saved_cursor(service):
    entered, resume = threading.Event(), threading.Event()
    previous = (datetime.now(LOCAL) - timedelta(days=2)).date().isoformat()
    requested = (datetime.now(LOCAL) - timedelta(days=14)).date().isoformat()
    with service.database() as db:
        service._write(db, "state", {**wipo_sync.DEFAULT_STATE, "madrid_through_date": previous})
    def fetcher(*_):
        entered.set()
        assert resume.wait(5)
        return [], {"fetched": 0, "unique": 0, "madrid_through_date": previous}
    service.fetcher = fetcher
    service.start_sync()
    assert entered.wait(5)
    service.save_settings({"madrid_from_date": requested})
    resume.set()
    service.thread.join(5)
    assert service.status()["madrid_through_date"] == ""


def route_app(service, monkeypatch, permissions, client=False):
    app = Flask(__name__)
    app.secret_key = "test-key"
    monkeypatch.setattr(wipo_routes, "get_service", lambda _: service)
    wipo_routes.register_routes(app, login_required=lambda fn: fn, internal_api_required=lambda fn: fn,
        current_user=lambda: {"permissions": permissions},
        has_permission=lambda user, perm: perm in user["permissions"], is_client=lambda: client,
        reject_client_mode=lambda: ("client blocked", 503) if client else None)
    return app.test_client()


def test_routes_enforce_manage_and_hide_config_from_viewer(service, monkeypatch):
    service.save_settings({"feed_url": "https://feed.example/data", "token": "secret"})
    client = route_app(service, monkeypatch, ["infringement_knowledge.view"])
    data = client.get("/api/infringement-knowledge/wipo/settings").json["data"]
    assert "feed_url" not in data and "has_token" not in data
    assert client.put("/api/infringement-knowledge/wipo/settings", json={"enabled": False}).status_code == 403
    assert client.post("/api/infringement-knowledge/wipo/sync", json={}).status_code == 403


def test_client_routes_forward_to_central_server(service, monkeypatch):
    calls = []
    monkeypatch.setattr(wipo_routes.bit_db_api, "_request",
        lambda method, path, **kw: calls.append((method, path, kw)) or {"running": False})
    client = route_app(service, monkeypatch, ["infringement_knowledge.view", "infringement_knowledge.manage"], client=True)
    assert client.post("/api/infringement-knowledge/wipo/sync", json={}).status_code == 200
    assert calls == [("POST", "/api/db/infringement-knowledge/wipo/sync", {"json": {}})]
    assert client.get("/api/db/infringement-knowledge/wipo/status").status_code == 503
