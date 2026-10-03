"""Real adapters plus durable job service; every marketplace request is mocked."""
from copy import deepcopy
import json
from urllib.parse import unquote

import pytest
import requests

from erp.ai_original_amazon_publish import AmazonOriginalPublisher
from erp.ai_original_publish import PublicationService, PublicationStore
from test_ai_original_amazon_publish import CREDS, MKT, Transport, row as amazon_row, settings as amazon_settings
from test_ai_original_yandex_publish import fixture as yandex_fixture


OWNER = "operator-1"
ORGANIZATION = "integration-company"


@pytest.fixture
def amazon_stack(tmp_path, monkeypatch):
    monkeypatch.setattr(AmazonOriginalPublisher, "_throttle_preview", lambda self: None)
    store = PublicationStore(tmp_path / "publish.sqlite3", secret_key="integration-encryption-key")
    saved = store.save_amazon_store(ORGANIZATION, {**CREDS, "label": "Integration Amazon US"})
    transport = Transport()
    records = {7: {**amazon_row(), "source_type": "ai_original", "review_status": "safe"}}

    def factory(platform, store_id, organization):
        assert platform == "amazon"
        credentials, label = store.amazon_credentials(organization, store_id)
        return AmazonOriginalPublisher(credentials, request=transport), label

    service = PublicationService(store, lambda ids: [deepcopy(records[value]) for value in ids if value in records],
                                 publisher_factory=factory, start_thread=lambda function: function())
    return service, store, transport, saved["id"], records


def preview_amazon(stack, **changes):
    service, _, _, store_id, _ = stack
    return service.preview({"target_platform": "amazon", "store_id": store_id,
        "items": [{"product_id": 7, "settings": amazon_settings(**changes)}]}, OWNER, ORGANIZATION)


def assert_no_secrets(value):
    encoded = json.dumps(value, ensure_ascii=False)
    for secret in (CREDS["client_secret"], CREDS["refresh_token"], "private-access-token"):
        assert secret not in encoded


def test_amazon_preview_submit_status_and_idempotent_submit(amazon_stack):
    service, store, transport, _, _ = amazon_stack
    preview = preview_amazon(amazon_stack)
    assert preview["valid"] is True
    assert not transport.writes
    assert len(transport.previews) == 1
    job = service.submit(preview["preview_id"], OWNER, ORGANIZATION)
    assert job["state"] == "submitted"
    assert not job["running"] and job["items"][0]["live"] is False
    assert len(transport.writes) == 1
    assert service.submit(preview["preview_id"], OWNER, ORGANIZATION)["id"] == job["id"]
    assert len(transport.writes) == 1
    assert unquote(transport.writes[0][1].rsplit("/", 1)[-1]) == "aip-7"
    assert len(transport.previews) == 2  # fresh official preview immediately before PUT
    transport.current = {"sku": "aip-7", "summaries": [{"marketplaceId": MKT, "asin": "B123456789",
                         "status": ["DISCOVERABLE", "BUYABLE"]}], "issues": []}
    refreshed = service.refresh(job["id"], OWNER, ORGANIZATION)
    assert refreshed["state"] == "live"
    assert refreshed["items"][0]["remote_id"] == "B123456789"
    assert refreshed["items"][0]["submission_unknown"] is False
    assert len(transport.writes) == 1
    for value in (preview, job, refreshed, store.jobs(OWNER, ORGANIZATION, "amazon")):
        assert_no_secrets(value)
    with store.connect() as db:
        persisted = [dict(record) for record in db.execute("SELECT body FROM previews UNION ALL SELECT body FROM jobs")]
        encrypted = db.execute("SELECT credentials FROM amazon_stores").fetchone()["credentials"]
    assert_no_secrets(persisted)
    assert CREDS["client_secret"] not in encrypted and CREDS["refresh_token"] not in encrypted


def test_amazon_actual_rejection_then_retry_reuses_same_sku_and_payload(amazon_stack):
    service, _, transport, _, _ = amazon_stack
    preview = preview_amazon(amazon_stack)
    transport.submission = {"status": "INVALID", "issues": [{"severity": "ERROR", "message": "temporary rejection"}]}
    first = service.submit(preview["preview_id"], OWNER, ORGANIZATION)
    assert first["state"] == "failed"
    assert first["items"][0]["retryable"] is True
    transport.submission = {"status": "ACCEPTED", "issues": [], "submissionId": "retry-accepted"}
    retried = service.retry(first["id"], OWNER, ORGANIZATION)
    assert retried["state"] == "submitted"
    assert retried["items"][0]["sku"] == "aip-7"
    assert len(transport.writes) == 2
    assert transport.writes[0][1] == transport.writes[1][1]
    assert transport.writes[0][2]["json"] == transport.writes[1][2]["json"]
    assert_no_secrets(retried)


def test_amazon_accepted_sku_can_retry_async_failure_using_verified_binding(amazon_stack):
    service, _, transport, _, _ = amazon_stack
    preview = preview_amazon(amazon_stack)
    accepted = service.submit(preview["preview_id"], OWNER, ORGANIZATION)
    transport.current = {"sku": "aip-7", "summaries": [{"marketplaceId": MKT, "asin": "B123456789", "status": []}],
                         "issues": [{"severity": "ERROR", "message": "Temporary processing issue"}]}
    partial = service.refresh(accepted["id"], OWNER, ORGANIZATION)
    assert partial["items"][0]["state"] == "partial"
    assert partial["items"][0]["retryable"] is True
    transport.current["issues"] = []
    retried = service.retry(accepted["id"], OWNER, ORGANIZATION)
    assert retried["state"] == "submitted"
    assert retried["items"][0]["_known_sku"] is True
    assert len(transport.writes) == 2
    assert transport.writes[0][1] == transport.writes[1][1]


def test_failed_unwritten_sku_cannot_claim_a_later_external_listing(amazon_stack):
    service, _, transport, _, _ = amazon_stack
    preview = preview_amazon(amazon_stack)
    transport.submission = {"status": "INVALID", "issues": [{"severity": "ERROR", "message": "rejected"}]}
    failed = service.submit(preview["preview_id"], OWNER, ORGANIZATION)
    transport.current = {"sku": "aip-7", "summaries": [{"marketplaceId": MKT, "asin": "BOTHER12345", "status": ["BUYABLE"]}], "issues": []}
    refreshed = service.refresh(failed["id"], OWNER, ORGANIZATION)
    assert not refreshed["items"][0].get("_owns_sku")
    transport.submission = {"status": "ACCEPTED", "issues": []}
    retried = service.retry(failed["id"], OWNER, ORGANIZATION)
    assert retried["state"] == "failed"
    assert len(transport.writes) == 1


def test_amazon_timeout_then_404_never_enables_blind_retry(amazon_stack):
    service, _, transport, _, _ = amazon_stack
    preview = preview_amazon(amazon_stack)
    transport.submit_exception = requests.Timeout("test only")
    unknown = service.submit(preview["preview_id"], OWNER, ORGANIZATION)
    assert unknown["items"][0]["state"] == "unknown"
    assert unknown["items"][0]["submission_unknown"] is True
    refreshed = service.refresh(unknown["id"], OWNER, ORGANIZATION)
    assert refreshed["items"][0]["submission_unknown"] is True
    assert refreshed["items"][0]["retryable"] is False
    with pytest.raises(ValueError, match="回查|重试"):
        service.retry(unknown["id"], OWNER, ORGANIZATION)
    assert len(transport.writes) == 1


def test_amazon_failed_job_cannot_retry_changed_product_without_new_review(amazon_stack):
    service, _, transport, _, records = amazon_stack
    preview = preview_amazon(amazon_stack)
    transport.submission = {"status": "INVALID", "issues": [{"severity": "ERROR", "message": "rejected"}]}
    failed = service.submit(preview["preview_id"], OWNER, ORGANIZATION)
    records[7]["source_snapshot_json"]["marketplace_listings"]["amazon"]["title"] = "Changed Product Title"
    with pytest.raises(ValueError, match="修改|变化|预检|确认"):
        service.retry(failed["id"], OWNER, ORGANIZATION)
    assert len(transport.writes) == 1


def test_amazon_changed_product_before_first_submit_needs_new_preview(amazon_stack):
    service, _, transport, _, records = amazon_stack
    preview = preview_amazon(amazon_stack)
    records[7]["source_snapshot_json"]["original_1688"]["variations"] = [{"sku": "first"}, {"sku": "second"}]
    with pytest.raises(ValueError, match="已修改"):
        service.submit(preview["preview_id"], OWNER, ORGANIZATION)
    assert not transport.writes


def test_job_owner_and_organization_are_enforced_with_real_adapter(amazon_stack):
    service, store, transport, _, _ = amazon_stack
    preview = preview_amazon(amazon_stack)
    with pytest.raises(KeyError):
        service.submit(preview["preview_id"], "another-owner", ORGANIZATION)
    with pytest.raises(KeyError):
        service.submit(preview["preview_id"], OWNER, "another-company")
    job = service.submit(preview["preview_id"], OWNER, ORGANIZATION)
    with pytest.raises(KeyError):
        service.refresh(job["id"], "another-owner", ORGANIZATION)
    assert store.jobs("another-owner", ORGANIZATION, "amazon") == []
    assert len(transport.writes) == 1


def test_same_sku_cannot_be_reassigned_to_another_source_product(amazon_stack):
    service, _, transport, store_id, records = amazon_stack
    preview = preview_amazon(amazon_stack)
    service.submit(preview["preview_id"], OWNER, ORGANIZATION)
    records[8] = deepcopy(records[7])
    records[8]["id"] = 8
    second = service.preview({"target_platform": "amazon", "store_id": store_id,
        "items": [{"product_id": 8, "settings": amazon_settings(sku="aip-7")}]}, OWNER, ORGANIZATION)
    if second["valid"]:
        with pytest.raises(ValueError, match="另一商品|SKU"):
            service.submit(second["preview_id"], OWNER, ORGANIZATION)
    assert len(transport.writes) == 1


def test_yandex_real_adapter_service_preview_submit_and_status(tmp_path, yandex_fixture):
    publisher, client, source, settings = yandex_fixture
    source["source_type"] = "ai_original"
    store = PublicationStore(tmp_path / "yandex-publish.sqlite3", secret_key="key")
    service = PublicationService(store, lambda ids: [deepcopy(source)] if source["id"] in ids else [],
        publisher_factory=lambda *_: (publisher, "Yandex integration"), start_thread=lambda function: function())
    preview = service.preview({"target_platform": "yandex", "store_id": "22",
        "items": [{"product_id": source["id"], "settings": settings}]}, OWNER, ORGANIZATION)
    assert preview["valid"] is True
    assert not client.writes
    submitted = service.submit(preview["preview_id"], OWNER, ORGANIZATION)
    assert submitted["state"] == "submitted"
    assert submitted["items"][0]["live"] is False
    assert submitted["items"][0]["sku"] == settings["sku"]
    client.remote_status = "PUBLISHED"
    refreshed = service.refresh(submitted["id"], OWNER, ORGANIZATION)
    assert refreshed["state"] == "live"
    assert refreshed["items"][0]["remote_id"] == "789"
    assert "unused" not in json.dumps(refreshed)
