"""Publication lifecycle tests: all marketplace I/O is injected and mocked."""
from copy import deepcopy
import json
import threading

import pytest

from erp.ai_original_publish import PublicationService, PublicationStore, public_image_url


OWNER, ORG = "operator-1", "wuhan-zeshun"


class Publisher:
    def __init__(self):
        self.prepared, self.published = [], []
        self.result = {"state": "submitted", "live": False, "amazon_status": "ACCEPTED", "submission_id": "remote-request"}
        self.status_result = {"state": "submitted", "live": True, "submission_unknown": False}

    def prepare(self, row, settings):
        self.prepared.append(deepcopy(settings))
        return {"sku": settings["sku"], "platform": "amazon", "_known_sku": settings["_known_sku"],
                "payload": {"title": row["source_snapshot_json"]["marketplace_listings"]["amazon"]["title"]},
                "errors": [], "warnings": []}

    def publish(self, item):
        self.published.append(deepcopy(item))
        if isinstance(self.result, Exception):
            raise self.result
        return deepcopy(self.result)

    def status(self, sku):
        if isinstance(self.status_result, Exception):
            raise self.status_result
        return deepcopy(self.status_result)


@pytest.fixture
def setup(tmp_path):
    rows = [{"id": i, "source_type": "ai_original", "review_status": "approved", "source_snapshot_json": {
        "original_1688": {"id": str(i)}, "marketplace_listings": {"amazon": {
            "title": "Product", "status": "completed", "main_image_url": "https://cdn.example.com/one.jpg"}}}}
        for i in (1, 2)]
    publisher = Publisher()
    store = PublicationStore(tmp_path / "publish.sqlite3", "test-secret")
    service = PublicationService(store, lambda ids: [row for row in rows if row["id"] in ids],
                                 lambda *args: (publisher, "Mock store"), start_thread=lambda function: function())
    return service, publisher, rows


def preview(service, ids=(1,), **settings):
    return service.preview({"target_platform": "amazon", "store_id": "store-1", "items": [
        {"product_id": i, "settings": settings} for i in ids]}, OWNER, ORG)


def submit(service, **settings):
    return service.submit(preview(service, **settings)["preview_id"], OWNER, ORG)


def test_credentials_encrypted_isolated_and_no_secret_return(setup):
    service, _, _ = setup
    data = {"seller_id": "SELLER123", "label": "US", "client_id": "cid", "client_secret": "private-secret",
            "refresh_token": "private-refresh"}
    saved = service.store.save_amazon_store(ORG, data)
    assert "secret" not in json.dumps(saved)
    assert b"private-secret" not in service.store.path.read_bytes()
    assert service.store.amazon_credentials(ORG, saved["id"])[0]["client_secret"] == "private-secret"
    assert service.store.amazon_stores("another-org") == []
    with pytest.raises(KeyError):
        service.store.amazon_credentials("another-org", saved["id"])
    assert service.store.save_amazon_store(ORG, data)["id"] == saved["id"]
    assert service.store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("url", ["http://cdn.example.com/a.jpg", "https://localhost/a", "https://a.internal./a",
    "https://127.0.0.1/a", "https://10.0.0.1/a", "https://[::1]/a", "https://host:bad/a",
    "https://user:secret@example.com/a", "https://cdn.example.com:8443/a", "javascript:alert(1)"])
def test_public_images_reject_private_or_invalid_urls(url):
    with pytest.raises(ValueError):
        public_image_url(url)


def test_relative_images_require_public_origin(monkeypatch):
    monkeypatch.delenv("AI_ORIGINAL_IMAGE_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="公网"):
        public_image_url("/api/ai-original-products/images/a.jpg")
    assert public_image_url("/api/ai-original-products/images/a.jpg", "https://erp.example.com") == "https://erp.example.com/api/ai-original-products/images/a.jpg"


def test_preview_is_read_only_and_cannot_spoof_sku_ownership(setup):
    service, publisher, _ = setup
    result = preview(service, _known_sku=True)
    assert result["valid"]
    assert publisher.prepared[0]["_known_sku"] is False
    assert not publisher.published
    assert service.store.jobs(OWNER, ORG, "amazon") == []


@pytest.mark.parametrize("change", ["draft", "risk", "image"])
def test_invalid_item_cannot_publish(setup, change):
    service, publisher, rows = setup
    if change == "draft": rows[0]["source_snapshot_json"]["marketplace_listings"]["amazon"]["status"] = "running"
    if change == "risk": rows[0]["review_status"] = "risk"
    args = {"image_url": "http://localhost/a"} if change == "image" else {}
    result = preview(service, **args)
    assert not result["valid"] and result["items"][0]["errors"]
    with pytest.raises(ValueError): service.submit(result["preview_id"], OWNER, ORG)
    assert not publisher.published


def test_duplicate_product_and_sku_blocked(setup):
    service, _, _ = setup
    with pytest.raises(ValueError): preview(service, ids=(1, 1))
    assert not preview(service, ids=(1, 2), sku="same")["valid"]


def test_submit_is_idempotent_and_accepted_is_not_live(setup):
    service, publisher, _ = setup
    prepared = preview(service)
    job = service.submit(prepared["preview_id"], OWNER, ORG)
    assert job["state"] == "submitted" and not job["items"][0]["live"]
    assert service.submit(prepared["preview_id"], OWNER, ORG)["id"] == job["id"]
    assert len(publisher.published) == 1
    assert service.refresh(job["id"], OWNER, ORG)["state"] == "live"
    with pytest.raises(KeyError): service.get_job(job["id"], "another-user", ORG)
    with pytest.raises(KeyError): service.get_job(job["id"], OWNER, "another-org")


def test_atomic_preview_claim_across_workers(setup):
    service, publisher, _ = setup
    prepared = preview(service)
    results = []
    def claim(): results.append(service.store.claim(prepared["preview_id"], OWNER, ORG))
    threads = [threading.Thread(target=claim) for _ in range(5)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(results) == 5 and sum(result[1] for result in results) == 1
    assert len({result[0]["id"] for result in results}) == 1


def test_expired_or_modified_draft_needs_new_confirmation(setup):
    service, publisher, rows = setup
    prepared = preview(service)
    with service.store.connect() as db:
        db.execute("UPDATE previews SET expires=0 WHERE id=?", (prepared["preview_id"],))
    with pytest.raises(ValueError, match="过期"): service.submit(prepared["preview_id"], OWNER, ORG)
    prepared = preview(service)
    rows[0]["source_snapshot_json"]["marketplace_listings"]["amazon"]["title"] = "Edited"
    with pytest.raises(ValueError, match="修改"): service.submit(prepared["preview_id"], OWNER, ORG)
    assert not publisher.published


@pytest.mark.parametrize("result", [RuntimeError("secret request detail"),
    {"state": "partial", "live": False, "submission_unknown": True, "retryable": True}])
def test_unknown_submission_and_later_not_found_cannot_retry(setup, result):
    service, publisher, _ = setup
    publisher.result = result
    job = submit(service)
    assert job["items"][0]["state"] == "unknown"
    assert "secret request detail" not in json.dumps(job)
    publisher.status_result = {"state": "partial", "submission_unknown": True, "not_found": True, "live": False}
    refreshed = service.refresh(job["id"], OWNER, ORG)
    assert refreshed["items"][0]["state"] == "unknown"
    with pytest.raises(ValueError, match="回查"): service.retry(job["id"], OWNER, ORG)
    publisher.status_result = {"state": "submitted", "submission_unknown": False, "live": False}
    assert service.refresh(job["id"], OWNER, ORG)["state"] == "submitted"


def test_definitive_failure_retries_same_sku_without_claiming_remote_ownership(setup):
    service, publisher, _ = setup
    publisher.result = {"state": "failed", "live": False, "retryable": True}
    job = submit(service)
    # A later foreign listing cannot turn this pre-creation failure into our live SKU.
    assert service.refresh(job["id"], OWNER, ORG)["state"] == "failed"
    publisher.result = {"state": "submitted", "amazon_status": "ACCEPTED", "live": False}
    retried = service.retry(job["id"], OWNER, ORG)
    assert retried["state"] == "submitted"
    assert [item["sku"] for item in publisher.published] == ["aip-1", "aip-1"]
    assert publisher.published[-1]["_known_sku"] is False


def test_partial_creation_keeps_ownership_when_retried(setup):
    service, publisher, _ = setup
    publisher.result = {"state": "partial", "steps": ["offer"], "live": False, "retryable": True}
    job = submit(service)
    publisher.status_result = {"state": "submitted", "live": False, "submission_unknown": False}
    refreshed = service.refresh(job["id"], OWNER, ORG)
    assert refreshed["items"][0]["retryable"]
    service.retry(job["id"], OWNER, ORG)
    assert publisher.published[-1]["_known_sku"] is True
    assert publisher.published[-1]["settings"]["_known_sku"] is True


def test_changed_review_blocks_retry(setup):
    service, publisher, rows = setup
    publisher.result = {"state": "failed", "live": False}
    job = submit(service)
    rows[0]["review_status"] = "risk"
    with pytest.raises(ValueError, match="修改"): service.retry(job["id"], OWNER, ORG)
    assert len(publisher.published) == 1


def test_new_preview_can_replace_failed_job_but_old_job_cannot_retry(setup):
    service, publisher, _ = setup
    publisher.result = {"state": "failed", "live": False}
    old = submit(service)
    new = submit(service, price="updated")
    assert old["id"] != new["id"]
    with pytest.raises(ValueError, match="最新"): service.retry(old["id"], OWNER, ORG)
    other = preview(service, ids=(2,), sku="aip-1")
    with pytest.raises(ValueError, match="另一商品"): service.submit(other["preview_id"], OWNER, ORG)


def test_restart_distinguishes_unstarted_from_possibly_submitted(setup):
    service, _, _ = setup
    prepared = preview(service, ids=(1, 2))
    job, _ = service.store.claim(prepared["preview_id"], OWNER, ORG)
    job["items"][1]["state"] = "publishing"
    service.store.update_job(job)
    recovered = service.get_job(job["id"], OWNER, ORG)
    assert not recovered["running"]
    assert recovered["items"][0]["retryable"]
    assert recovered["items"][1]["state"] == "unknown"
    assert not recovered["items"][1]["retryable"]


def test_thread_launch_failure_is_durable_and_retryable(setup):
    service, _, _ = setup
    def fail(function): raise RuntimeError("thread unavailable")
    service.start_thread = fail
    with pytest.raises(ValueError, match="线程"): submit(service)
    job = service.store.jobs(OWNER, ORG, "amazon")[0]
    assert job["state"] == "failed" and job["items"][0]["retryable"]
