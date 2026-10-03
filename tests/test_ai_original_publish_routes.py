"""Publication HTTP authorization contracts, with no network or database access."""
from copy import deepcopy
from functools import wraps
from types import SimpleNamespace

from flask import Flask, jsonify
import pytest

from bit.ai_original_publish_routes import register_routes
from bit import service_split


BASE = "/api/ai-original-products/publish"
ENDPOINTS = [
    ("get", BASE + "/stores?target_platform=amazon"),
    ("post", BASE + "/amazon-store"),
    ("get", BASE + "/schema?target_platform=amazon&store_id=store-a&category_id=PRODUCT"),
    ("post", BASE + "/preview"),
    ("post", BASE),
    ("get", BASE + "/jobs?target_platform=amazon"),
    ("get", BASE + "/jobs/job-a"),
    ("post", BASE + "/jobs/job-a/refresh"),
    ("post", BASE + "/jobs/job-a/retry"),
]


class ScopedService:
    """Service spy whose resources exist only for a fixed owner/organization."""
    def __init__(self):
        self.calls = []
        self.store = SimpleNamespace(save_amazon_store=self.save_store, jobs=self.jobs)
        self.schema_error = None

    def record(self, *args): self.calls.append(deepcopy(args))

    @staticmethod
    def check(owner, organization):
        if (owner, organization) != ("7", "org-a"):
            raise KeyError("private-other-tenant-job-content")

    def stores(self, platform, organization):
        self.record("stores", platform, organization)
        return {"rows": [{"id": "store-a"}]} if organization == "org-a" else {"rows": []}

    def save_store(self, organization, body):
        self.record("save-store", organization, body)
        return {"id": "saved-store", "configured": True}

    def publisher_factory(self, platform, store_id, organization):
        self.record("publisher", platform, store_id, organization)
        if self.schema_error:
            raise self.schema_error
        if (store_id, organization) != ("store-a", "org-a"):
            raise KeyError("private-store-secret")
        return SimpleNamespace(get_schema=lambda category: {"category_id": category, "fields": []}), "Store A"

    def preview(self, body, owner, organization):
        self.record("preview", body, owner, organization)
        return {"preview_id": "preview-a", "valid": True, "items": []}

    def submit(self, preview_id, owner, organization):
        self.record("submit", preview_id, owner, organization)
        self.check(owner, organization)
        if preview_id != "preview-a":
            raise KeyError("private-preview-content")
        return {"id": "job-a", "state": "running", "running": True}

    def jobs(self, owner, organization, platform):
        self.record("jobs", owner, organization, platform)
        return [{"id": "job-a"}] if (owner, organization) == ("7", "org-a") else []

    def get_job(self, job_id, owner, organization):
        self.record("job", job_id, owner, organization)
        self.check(owner, organization)
        if job_id != "job-a":
            raise KeyError("private-other-job-content")
        return {"id": job_id, "state": "submitted", "items": [{"sku": "test-sku", "live": False}]}

    def refresh(self, job_id, owner, organization):
        self.record("refresh", job_id, owner, organization)
        return self.get_job(job_id, owner, organization)

    def retry(self, job_id, owner, organization):
        self.record("retry", job_id, owner, organization)
        return self.get_job(job_id, owner, organization)


@pytest.fixture
def context():
    context = SimpleNamespace(user={"id": 7, "organization_key": "org-a", "role_key": "operator",
                                  "permissions": {"tasks.view", "tasks.execute"}},
                              checks=[], logins=[], service=ScopedService())
    app = Flask(__name__)
    app.secret_key = "test-only-signing-key"
    app.config["TESTING"] = True

    def login_required(function):
        @wraps(function)
        def authenticated(*args, **kwargs):
            context.logins.append(function.__name__)
            if not context.user:
                return jsonify(status="error", message="login required"), 401
            return function(*args, **kwargs)
        return authenticated

    def has_permission(user, permission):
        context.checks.append((user["id"], permission))
        return permission in user.get("permissions", set())

    register_routes(app, login_required=login_required, current_user=lambda: context.user,
                    has_permission=has_permission, load_rows=lambda ids: pytest.fail("no DB calls allowed"),
                    own_store_only=lambda user: bool(user.get("own_store_only")), service=context.service)
    context.app, context.client = app, app.test_client()
    return context


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_every_publication_endpoint_requires_login_before_service_access(context, method, path):
    context.user = None
    response = getattr(context.client, method)(path, json={"preview_id": "preview-a"})
    assert response.status_code == 401
    assert context.logins
    assert context.service.calls == []


def test_stale_session_without_current_database_identity_is_denied(context):
    context.user = {"role_key": "super_admin"}
    response = context.client.get(BASE + "/stores?target_platform=amazon")
    assert response.status_code == 403
    assert context.service.calls == []


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_task_permissions_and_admin_gate_fail_closed(context, method, path):
    context.user["permissions"] = set()
    response = getattr(context.client, method)(path, json={"preview_id": "preview-a"})
    assert response.status_code == 403
    assert context.service.calls == []


def test_read_permission_does_not_grant_execute_or_authorization_management(context):
    context.user["permissions"] = {"tasks.view"}
    assert context.client.get(BASE + "/jobs?target_platform=amazon").status_code == 200
    assert context.client.get(BASE + "/jobs/job-a").status_code == 200
    assert context.client.post(BASE + "/preview", json={}).status_code == 403
    assert context.client.post(BASE, json={"preview_id": "preview-a"}).status_code == 403
    assert context.client.post(BASE + "/amazon-store", json={}).status_code == 403
    assert (7, "tasks.view") in context.checks
    assert (7, "tasks.execute") in context.checks


def test_execute_permission_does_not_implicitly_grant_view(context):
    context.user["permissions"] = {"tasks.execute"}
    assert context.client.post(BASE + "/preview", json={"target_platform": "amazon"}).status_code == 200
    assert context.client.get(BASE + "/jobs?target_platform=amazon").status_code == 403


@pytest.mark.parametrize("role", ["super_admin", "enterprise_admin", "admin"])
def test_only_admin_can_save_authorization_under_current_organization(context, role):
    context.user.update(role_key=role, permissions=set())
    credentials = {"seller_id": "SELLER", "client_secret": "never-return-this-secret", "refresh_token": "private-refresh",
                   "organization": "another-org", "owner": "another-user"}
    response = context.client.post(BASE + "/amazon-store", json=credentials)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert context.service.calls == [("save-store", "org-a", credentials)]
    assert "never-return-this-secret" not in response.get_data(as_text=True)
    assert "private-refresh" not in response.get_data(as_text=True)


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_own_store_only_cannot_access_unassigned_platform_stores(context, method, path):
    context.user["own_store_only"] = True
    response = getattr(context.client, method)(path, json={"preview_id": "preview-a"})
    assert response.status_code == 403
    assert context.service.calls == []


@pytest.mark.parametrize("organization", ["../org-a", "ORG A", "x", "a" * 65])
def test_invalid_current_organization_is_rejected_before_backend(context, organization):
    context.user["organization_key"] = organization
    assert context.client.get(BASE + "/stores?target_platform=amazon").status_code == 403
    assert context.service.calls == []


@pytest.mark.parametrize("organization", [None, "", "default"])
def test_legacy_organization_maps_to_fixed_default(context, organization):
    context.user["organization_key"] = organization
    response = context.client.get(BASE + "/stores?target_platform=amazon")
    assert response.status_code == 200
    assert context.service.calls == [("stores", "amazon", "wuhan-zeshun")]


def test_request_cannot_spoof_owner_organization_or_permission_context(context):
    body = {"target_platform": "amazon", "store_id": "store-a", "items": [],
            "owner": "999", "organization": "org-b", "is_admin": True}
    response = context.client.post(BASE + "/preview?organization_key=org-b&owner=999", json=body)
    assert response.status_code == 200
    assert context.service.calls[-1] == ("preview", body, "7", "org-a")
    assert response.headers["Cache-Control"] == "no-store"
    response = context.client.get(BASE + "/schema?target_platform=amazon&store_id=store-a&category_id=SHIRT&organization_key=org-b")
    assert response.get_json()["data"]["category_id"] == "SHIRT"
    assert context.service.calls[-1] == ("publisher", "amazon", "store-a", "org-a")


@pytest.mark.parametrize("change", [{"id": 8}, {"organization_key": "org-b"}])
def test_other_owner_or_organization_cannot_read_submit_refresh_or_retry_resources(context, change):
    context.user.update(change)
    for method, path, body in [
        ("get", BASE + "/jobs/job-a?owner=7&organization_key=org-a", None),
        ("post", BASE, {"preview_id": "preview-a", "owner": "7", "organization": "org-a"}),
        ("post", BASE + "/jobs/job-a/refresh", {}),
        ("post", BASE + "/jobs/job-a/retry", {}),
    ]:
        response = getattr(context.client, method)(path, json=body)
        assert response.status_code == 404
        assert "private" not in response.get_data(as_text=True)
    response = context.client.get(BASE + "/jobs?target_platform=amazon&owner=7&organization_key=org-a")
    assert response.get_json()["data"]["rows"] == []


def test_store_and_schema_resolution_are_organization_scoped(context):
    context.user["organization_key"] = "org-b"
    response = context.client.get(BASE + "/stores?target_platform=amazon&organization_key=org-a")
    assert response.get_json()["data"]["rows"] == []
    response = context.client.get(BASE + "/schema?target_platform=amazon&store_id=store-a&category_id=SHIRT&organization_key=org-a")
    assert response.status_code == 404
    assert "private-store-secret" not in response.get_data(as_text=True)


def test_preview_submit_jobs_and_followups_preserve_http_contract(context):
    assert context.client.post(BASE + "/preview", json={"target_platform": "amazon"}).get_json()["data"]["preview_id"] == "preview-a"
    response = context.client.post(BASE, json={"preview_id": "preview-a"})
    assert response.status_code == 202
    assert response.get_json()["data"]["state"] == "running"
    response = context.client.get(BASE + "/jobs?target_platform=amazon")
    assert response.get_json()["data"]["rows"][0]["id"] == "job-a"
    assert response.headers["Cache-Control"] == "no-store"
    assert context.client.get(BASE + "/jobs/job-a").get_json()["data"]["items"][0]["live"] is False
    assert context.client.post(BASE + "/jobs/job-a/refresh", json={}).status_code == 200
    assert context.client.post(BASE + "/jobs/job-a/retry", json={}).status_code == 202
    assert ("submit", "preview-a", "7", "org-a") in context.service.calls
    assert ("jobs", "7", "org-a", "amazon") in context.service.calls


@pytest.mark.parametrize("body", [None, [], "not-an-object", 1])
def test_mutating_payload_requires_json_object(context, body):
    response = context.client.post(BASE + "/preview", json=body)
    assert response.status_code == 400
    assert context.service.calls == []


def test_oversized_preview_is_rejected_without_service_work(context):
    response = context.client.post(BASE + "/preview", json={"padding": "x" * 2_000_001})
    assert response.status_code == 400
    assert context.service.calls == []


@pytest.mark.parametrize("exception", [RuntimeError, ValueError, TypeError, PermissionError, KeyError])
def test_exception_responses_and_logs_never_disclose_request_credentials(context, caplog, exception):
    secret = "Api-Key: example-api-secret; client_secret=example-client-secret; refresh_token=example-refresh-secret"
    context.service.schema_error = exception(secret)
    response = context.client.get(BASE + "/schema?target_platform=amazon&store_id=store-a&category_id=SHIRT")
    assert response.status_code in {400, 403, 404, 502}
    rendered = response.get_data(as_text=True) + caplog.text
    for sensitive in ("example-api-secret", "example-client-secret", "example-refresh-secret"):
        assert sensitive not in rendered


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_all_publication_paths_are_owned_by_stateful_worker(method, path):
    assert service_split.is_worker_path(path.split("?", 1)[0])
    assert not service_split.is_worker_path("/api/ai-original-products/profiles")


def test_web_proxy_never_retries_ambiguous_publish_post(context, monkeypatch, caplog):
    monkeypatch.setenv("BIT_SERVICE_MODE", "web")
    sends = []
    class UnavailableWorker:
        trust_env = True
        def request(self, *args, **kwargs):
            sends.append((args, kwargs))
            assert self.trust_env is False
            raise service_split.requests.ReadTimeout("client_secret=proxy-secret")
        def close(self): pass
    monkeypatch.setattr(service_split.requests, "Session", UnavailableWorker)
    service_split.install_service_routing(context.app)
    response = context.client.post(BASE, json={"preview_id": "preview-a"})
    assert response.status_code == 503
    assert len(sends) == 1
    assert context.service.calls == []
    assert "proxy-secret" not in response.get_data(as_text=True) + caplog.text
