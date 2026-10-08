"""Offline coverage of actual-response accounting and background ownership."""
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from AI_Agent import deepseek
from erp import ai_usage_billing as billing


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from bit import bit_db_api
    monkeypatch.setenv("AI_USAGE_BILLING_PATH", str(tmp_path / "usage.sqlite3"))
    monkeypatch.setattr(bit_db_api, "DB_MODE", "mysql")
    monkeypatch.setattr(billing, "_context_resolver", None)
    with billing.usage_context(organization_key="billing-test", actor_id=7, actor_name="业务员甲"):
        yield lambda: billing.ai_usage_billing({"action": "list", "organization_key": "billing-test"})


def sdk_response(request_id="sdk-one", choices=None):
    return SimpleNamespace(id=request_id, model="qwen3.7-flash",
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=25, total_tokens=125),
        choices=choices if choices is not None else [SimpleNamespace(
            message=SimpleNamespace(content="ok"), finish_reason="stop")])


def mock_client(monkeypatch, response=None, error=None):
    def create(**kwargs):
        if error:
            raise error
        return response
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(deepseek, "_get_client", lambda **kwargs: client)


def test_both_chat_interfaces_count_once_and_failed_choice_still_counts(ledger, monkeypatch):
    mock_client(monkeypatch, sdk_response())
    assert deepseek.chat_deepseek([]) == "ok"
    assert deepseek.chat_deepseek_with_usage([])["usage"]["total_tokens"] == 125
    # The same returned provider request is idempotent across interface/retries.
    assert ledger()["summary"]["total_tokens"] == 125
    mock_client(monkeypatch, sdk_response("sdk-malformed", choices=[]))
    with pytest.raises(IndexError):
        deepseek.chat_deepseek([])
    records = ledger()
    assert records["summary"]["total_tokens"] == 250
    assert records["total"] == 2
    assert {row["actor_id"] for row in records["items"]} == {7}
    assert all("messages" not in row and "api_key" not in row for row in records["items"])


@pytest.mark.parametrize("use_sdk_timeout", [False, True])
def test_sdk_failed_response_usage_is_counted_and_timeout_is_pending(ledger, monkeypatch, use_sdk_timeout):
    error = RuntimeError("gateway refused output")
    error.body = {"id": "failed-billed", "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
    mock_client(monkeypatch, error=error)
    with pytest.raises(RuntimeError):
        deepseek.chat_deepseek([])
    if use_sdk_timeout:
        import httpx
        timeout = deepseek.APITimeoutError(request=httpx.Request("POST", "https://example.com"))
    else:
        timeout = TimeoutError("network timeout")
    mock_client(monkeypatch, error=timeout)
    with pytest.raises(type(timeout)):
        deepseek.chat_deepseek([])
    mock_client(monkeypatch, error=ConnectionError("connection refused"))
    with pytest.raises(ConnectionError):
        deepseek.chat_deepseek([])
    assert ledger()["summary"]["total_tokens"] == 13
    assert ledger()["total"] == 2
    pending = next(row for row in ledger()["items"] if row["status"] == "usage_unknown")
    assert pending["total_tokens"] is None and pending["estimated_cost_cny"] is None
    assert ledger()["summary"]["unknown_usage_count"] == 1


def test_weight_price_timeout_is_pending_without_counting_connection_failures(ledger, monkeypatch):
    from erp.ai_weight_price import models
    import requests
    model = models.Models({"_runtime_api_key": "private-test-key",
        "api_base_url": deepseek.QWEN_BASE_URL, "api_timeout_seconds": 10}, lambda *args: None)
    def fail(error):
        def post(*args, **kwargs):
            raise error
        return post
    for error in (requests.Timeout("read timeout"), requests.ConnectionError("connection refused")):
        monkeypatch.setattr(models.requests, "post", fail(error))
        with pytest.raises(models.ModelServiceError):
            model.call("qwen3-vl-flash", "private prompt")
    assert ledger()["total"] == 1 and ledger()["summary"]["total_tokens"] == 0
    row = ledger()["items"][0]
    assert row["status"] == "usage_unknown" and row["task_type"] == "ai_weight_price"
    assert row["total_tokens"] is None and row["estimated_cost_cny"] is None


@pytest.mark.parametrize("content,finish_reason", [("not json", "stop"), ("{}", "length"), ("", "stop")])
def test_weight_price_usage_survives_invalid_decision(ledger, monkeypatch, content, finish_reason):
    from erp.ai_weight_price import models
    payload = {"id": "weight-call", "model": "qwen3-vl-flash",
               "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
               "choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}
    monkeypatch.setattr(models.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        ok=True, status_code=200, json=lambda: payload))
    model = models.Models({"model": "qwen3-vl-flash", "_runtime_api_key": "private-test-key",
        "api_base_url": deepseek.QWEN_BASE_URL, "api_timeout_seconds": 10}, lambda *args: None)
    with pytest.raises(models.ModelServiceError):
        model.call("qwen3-vl-flash", "private prompt", json_output=True)
    row = ledger()["items"][0]
    assert row["total_tokens"] == 30
    assert row["task_type"] == "ai_weight_price"
    assert "private-test-key" not in str(row) and "private prompt" not in str(row)


def test_image_usage_is_saved_before_missing_image_error(ledger, monkeypatch):
    from erp import ai_original_products as original
    payload = {"id": "image-call", "usage": {"input_tokens": 40, "output_tokens": 80, "total_tokens": 120}}
    monkeypatch.setattr(original.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        ok=True, json=lambda: payload, raise_for_status=lambda: None))
    with pytest.raises(ValueError, match="没有返回图片"):
        original.generate_ai_white_background_image("https://cbu01.alicdn.com/a.jpg", "123",
            api_key="private-key", base_url="https://example.com/v1", model="image-model", source_bytes=b"source")
    row = ledger()["items"][0]
    assert row["total_tokens"] == 120 and row["product_id"] == "123"
    assert row["task_name"] == "AI 原创白底主图"


def test_image_non_json_rejection_preserves_http_error_and_is_not_counted(ledger, monkeypatch):
    from erp import ai_original_products as original
    import requests
    def non_json():
        raise ValueError("not JSON")
    def reject():
        raise requests.HTTPError("HTTP 403")
    monkeypatch.setattr(original.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        ok=False, json=non_json, raise_for_status=reject))
    with pytest.raises(requests.HTTPError, match="403"):
        original.generate_ai_white_background_image("https://cbu01.alicdn.com/a.jpg", "123",
            api_key="private-key", base_url="https://example.com/v1", model="image-model", source_bytes=b"source")
    assert ledger()["total"] == 0


def test_image_timeout_is_pending_without_recording_connection_failure(ledger, monkeypatch):
    from erp import ai_original_products as original
    import requests
    def fail(error):
        def post(*args, **kwargs):
            raise error
        return post
    for error in (requests.Timeout("read timeout"), requests.ConnectionError("connection refused")):
        monkeypatch.setattr(original.requests, "post", fail(error))
        with pytest.raises(type(error)):
            original.generate_ai_white_background_image("https://cbu01.alicdn.com/a.jpg", "123",
                api_key="private-key", base_url="https://example.com/v1", model="image-model", source_bytes=b"source")
    assert ledger()["total"] == 1
    row = ledger()["items"][0]
    assert row["status"] == "usage_unknown" and row["product_id"] == "123"
    assert row["total_tokens"] is None and row["estimated_cost_cny"] is None


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_video_final_usage_keeps_creator_and_is_idempotent(ledger, monkeypatch, status):
    from bit import bit_ai_video as video
    payload = {"id": "remote-video", "status": status,
               "usage": {"completion_tokens": 50, "total_tokens": 50}}
    monkeypatch.setattr(video.requests, "get", lambda *args, **kwargs: SimpleNamespace(
        ok=True, status_code=200, json=lambda: payload))
    monkeypatch.setattr(video, "_provider_headers", lambda *args, **kwargs: {})
    job = {"id": "job-video", "credential_owner_id": 7, "created_by_name": "业务员甲",
           "model": "seedance-model", "ai_usage_context": {"organization_key": "billing-test", "actor_id": 7}}
    with billing.usage_context(organization_key="viewer-org", actor_id=99, actor_name="查看者"):
        for _ in range(2):
            video._query_provider("remote-video", video.SEEDANCE_PROVIDER, job)
    row = ledger()["items"][0]
    assert ledger()["total"] == 1
    assert row["total_tokens"] == 50 and row["actor_id"] == 7
    assert row["task_type"] == "ai_video" and row["task_id"] == "job-video"


def test_thread_bound_identity_survives_parent_context_exit(ledger):
    with billing.usage_context(actor_id=8, actor_name="业务员乙", task_type="ai_original"):
        work = billing.bind_usage(billing.record_usage)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(work, {"total_tokens": 9}, model="custom-model", request_id="thread-call").result()
    row = ledger()["items"][0]
    assert row["actor_id"] == 8 and row["actor_name"] == "业务员乙"
    assert row["task_type"] == "ai_original"


def test_daily_process_worker_restores_serialized_identity_without_leaking_metadata(ledger, monkeypatch):
    from bit import bit_daily_task as daily
    observed = {}
    def appeal(shop, **kwargs):
        observed.update(context=billing.current_usage_context(), shop=shop)
        billing.record_usage({"total_tokens": 17}, model="custom-model", request_id="process-call")
        return {"name": shop["name"]}
    monkeypatch.setattr(daily, "appeal_one_shop", appeal)
    shop = {"name": "店铺一", "_ai_usage_context": {
        "organization_key": "billing-test", "actor_id": 8, "actor_name": "业务员乙"}}
    daily._appeal_one_shop_worker_for_type(shop, daily.APPEAL_TYPE_INFRACTION, 0, "", 0, task_id="daily-one")
    assert observed["context"]["actor_id"] == 8
    assert observed["shop"] == {"name": "店铺一"}
    row = ledger()["items"][0]
    assert row["actor_id"] == 8 and row["task_id"] == "daily-one" and row["task_type"] == "ai_appeal"
