import pytest
from bit import bit_interface, service_split, wipo_routes, wipo_sync

pytestmark = pytest.mark.usefixtures("subscribed_workbench_account")


def test_wipo_public_route_uses_existing_permissions_and_masks_configuration(monkeypatch):
    calls = []
    class Service:
        def settings(self, *, manage):
            calls.append(manage)
            return {"enabled": True, "source_mode": "madrid"}
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setattr(bit_interface, "get_current_workbench_user", lambda: {
        "id": 1, "username": "wipo-viewer", "access_version": 1,
        "permissions": ["infringement_knowledge.view"],
    })
    monkeypatch.setattr(wipo_routes, "get_service", lambda _: Service())
    client = bit_interface.app.test_client()
    response = client.get("/api/infringement-knowledge/wipo/settings")
    assert response.status_code == 200
    assert calls == [False]
    assert client.post("/api/infringement-knowledge/wipo/sync", json={}).status_code == 403


def test_internal_wipo_records_require_token_and_use_blacklist_writer(monkeypatch):
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setenv("BIT_DB_API_TOKEN", "wipo-internal-test")
    written = []
    monkeypatch.setattr(bit_interface, "db_upsert_wipo_infringement_knowledge",
        lambda rows: written.extend(rows) or {"inserted": len(rows)})
    client = bit_interface.app.test_client()
    path = "/api/db/infringement-knowledge/wipo/records"
    options = {"environ_overrides": {"REMOTE_ADDR": "10.0.0.20"}, "json": {"records": [{"brand_name": "Brand"}]}}
    assert client.post(path, **options).status_code == 403
    response = client.post(path, headers={"X-Internal-Token": "wipo-internal-test"}, **options)
    assert response.status_code == 200 and written == [{"brand_name": "Brand"}]


def test_wipo_scheduler_and_routes_use_central_worker_only(monkeypatch, tmp_path):
    calls = []
    class Service:
        path = tmp_path / "wipo.sqlite3"
        def start_scheduler(self):
            calls.append("started")
    monkeypatch.setattr(wipo_sync, "get_service", lambda _: Service())
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    bit_interface.start_wipo_scheduler_bootstrap()
    assert calls == []
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setenv("BIT_BACKGROUND_SERVICES_DISABLED", "1")
    bit_interface.start_wipo_scheduler_bootstrap()
    assert calls == []
    monkeypatch.delenv("BIT_BACKGROUND_SERVICES_DISABLED")
    monkeypatch.setenv("BIT_SERVICE_MODE", "combined")
    bit_interface.start_wipo_scheduler_bootstrap()
    assert calls == ["started"]
    for prefix in ("/api/infringement-knowledge/wipo", "/api/db/infringement-knowledge/wipo"):
        for action in ("settings", "status", "sync"):
            assert service_split.is_worker_path(prefix + "/" + action)
