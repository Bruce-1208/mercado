from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json
import sqlite3

import pytest

from erp import ai_usage_billing as billing


OFFICIAL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_USAGE_BILLING_PATH", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.delenv("AI_USAGE_BILLING_RATES_JSON", raising=False)
    monkeypatch.delenv("AI_USAGE_BILLING_RATES_PATH", raising=False)
    monkeypatch.delenv("AI_BILLING_ACTOR_ID", raising=False)
    monkeypatch.delenv("AI_BILLING_ACTOR_NAME", raising=False)
    monkeypatch.delenv("AI_BILLING_ORGANIZATION_KEY", raising=False)
    monkeypatch.delenv("BIT_EXECUTION_JOB_ID", raising=False)
    monkeypatch.setattr(billing, "_context_resolver", None)
    monkeypatch.setattr(billing, "_start_outbox_worker", lambda: None)
    from bit import bit_db_api
    monkeypatch.setattr(bit_db_api, "DB_MODE", "mysql")
    return lambda action="list", **fields: billing.ai_usage_billing({
        "action": action, "organization_key": "alpha", **fields})


def record(ledger, **fields):
    return ledger("record", actor_id=7, actor_name="李明", task_type="risk_check",
                  model="qwen3.7-flash", provider="dashscope", base_url=OFFICIAL,
                  usage={"prompt_tokens": 1000, "completion_tokens": 100,
                         "prompt_tokens_details": {"cached_tokens": 200}}, **fields)


def test_provider_usage_objects_missing_zero_and_inconsistent():
    sdk = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3,
                          total_tokens=15, prompt_tokens_details={"cached_tokens": 4}))
    assert billing.parse_usage(sdk) == {"input_tokens": 12, "output_tokens": 3,
        "cached_tokens": 4, "total_tokens": 15, "usage_known": True, "usage_consistent": True}
    assert billing.parse_usage({"prompt_eval_count": 9, "eval_count": 2})["total_tokens"] == 11
    assert billing.parse_usage({"prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 6,
                                "completion_tokens": 3})["input_tokens"] == 10
    assert billing.parse_usage({})["total_tokens"] is None
    assert billing.parse_usage({})["usage_known"] is False
    invalid = billing.parse_usage({"prompt_tokens": 12, "completion_tokens": 3,
                                   "total_tokens": 14, "cached_tokens": 13})
    assert invalid["total_tokens"] == 14  # Retain the original provider observation.
    assert invalid["usage_known"] is False
    assert billing.estimate_cost(invalid, model="qwen3.7-flash", base_url=OFFICIAL)["estimated_cost_cny"] is None
    assert billing.parse_usage({"input_tokens": True, "output_tokens": -1})["usage_known"] is False


@pytest.mark.parametrize("model,input_count,input_price,cached_price,output_price", [
    ("qwen3.7-flash", 32000, .2, .04, .8),
    ("qwen3.7-flash-2026-10-07", 32001, .6, .12, 2.4),
    ("qwen3-vl-flash", 128001, .6, .12, 6),
    ("qwen3-vl-plus", 128000, 1.5, .3, 15),
])
def test_beijing_price_tiers_and_snapshots(ledger, model, input_count, input_price, cached_price, output_price):
    usage = billing.parse_usage({"input_tokens": input_count, "output_tokens": 1000, "cached_tokens": 100})
    price = billing.estimate_cost(usage, model=model, base_url=OFFICIAL)
    assert price["estimated_cost_cny"] == pytest.approx(((input_count - 100) * input_price +
                                                        100 * cached_price + 1000 * output_price) / 1000000)
    assert price["rate_metadata"]["version"] == billing.RATE_VERSION
    assert price["rate_metadata"]["input_rate"] == input_price


@pytest.mark.parametrize("model,url", [
    ("unknown", OFFICIAL), ("qwen3.7-flash", "https://proxy.example/v1"),
    ("qwen3.7-flash", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    ("qwen3.7-flash-custom", OFFICIAL),
])
def test_unknown_or_non_beijing_model_never_becomes_free(ledger, model, url):
    price = billing.estimate_cost(billing.parse_usage({"input_tokens": 5, "output_tokens": 2}),
                                  model=model, base_url=url)
    assert price["estimated_cost_cny"] is None
    assert price["cost_known"] is False


def test_custom_rate_requires_matching_host(ledger, monkeypatch):
    monkeypatch.setenv("AI_USAGE_BILLING_RATES_JSON", json.dumps({"custom": {
        "host": "provider.example", "version": "contract-1", "tiers": [[99999, 2, 1, 4]]}}))
    usage = billing.parse_usage({"input_tokens": 1000, "output_tokens": 1000})
    assert billing.estimate_cost(usage, model="custom", base_url="https://provider.example/v1")["estimated_cost_cny"] == .006
    assert billing.estimate_cost(usage, model="custom", base_url="https://other.example/v1")["estimated_cost_cny"] is None


def test_scope_and_options_never_leak_same_name_other_company(ledger):
    record(ledger, request_id="a", created_at="2026-10-07T10:00:00+08:00")
    ledger("record", actor_id=8, actor_name="王华", task_type="ai_appeal", model="unknown", total_tokens=9)
    ledger("record", actor_id=0, actor_name="系统任务", total_tokens=2)
    ledger("record", organization_key="beta", actor_id=7, actor_name="李明", total_tokens=123456,
           model="private-beta-model")
    own = ledger(scope_user_ids=[7])
    assert own["total"] == 1
    assert own["summary"]["total_tokens"] == 1100
    assert own["options"]["models"] == ["qwen3.7-flash"]
    assert own["options"]["salespersons"] == [{"actor_id": 7, "actor_name": "李明", "salesperson": "李明"}]
    assert ledger(scope_user_ids=[])["total"] == 0
    assert ledger()["total"] == 3
    with pytest.raises(ValueError, match="企业"):
        billing.ai_usage_billing({"action": "list", "scope_user_ids": [7]})
    with pytest.raises(ValueError, match="范围"):
        ledger(scope_user_ids=[-1])


def test_date_filters_use_china_calendar_and_filtered_summary(ledger):
    record(ledger, created_at="2026-10-07T00:00:00+08:00", request_id="midnight")
    record(ledger, created_at="2026-10-07T23:59:59+08:00", request_id="night")
    record(ledger, created_at="2026-10-08T00:00:00+08:00", request_id="next")
    result = ledger(start_date="2026-10-07", end_date="2026-10-07", actor_id=7,
                    task_type="risk_check", model="qwen3.7-flash", keyword="侵权", page_size=1, page=2)
    assert result["total"] == 2
    assert len(result["items"]) == 1
    assert result["items"][0]["request_id"] == "midnight"
    assert result["summary"]["total_tokens"] == 2200
    assert result["summary"]["estimated_cost_cny"] == pytest.approx(.000496)


def test_missing_partial_unknown_price_and_zero_usage(ledger):
    assert ledger("record", total_tokens=0)["skipped"] is True
    ledger("record", task_id="missing")
    ledger("record", input_tokens=10, task_id="partial")
    ledger("record", total_tokens=12, task_id="unknown-model", model="unknown")
    result = ledger()
    assert result["total"] == 3
    assert result["summary"]["unknown_usage_count"] == 2
    assert result["summary"]["unpriced_record_count"] == 3
    assert all(item["estimated_cost_cny"] is None for item in result["items"])


def test_provider_request_replays_update_usage_without_double_billing(ledger):
    initial = ledger("record", actor_id=7, provider="dashscope", request_id="provider-1",
                     input_tokens=100, model="qwen3.7-flash", base_url=OFFICIAL)
    completed = ledger("record", actor_id=7, provider="dashscope", request_id="provider-1",
                       input_tokens=100, output_tokens=50, model="qwen3.7-flash", base_url=OFFICIAL)
    assert completed["duplicate"] is True
    assert completed["record_id"] == initial["record_id"]
    assert ledger()["summary"]["total_tokens"] == 150
    assert ledger()["total"] == 1
    ledger("record", organization_key="beta", provider="dashscope", request_id="provider-1", total_tokens=300)
    assert ledger(organization_key="beta")["total"] == 1
    with pytest.raises(ValueError, match="编号冲突"):
        ledger("record", organization_key="beta", record_id=initial["record_id"], total_tokens=1)


def test_context_capture_threads_generators_and_agent_environment(ledger, monkeypatch):
    monkeypatch.setenv("AI_BILLING_ACTOR_ID", "15")
    monkeypatch.setenv("AI_BILLING_ACTOR_NAME", "Agent 业务员")
    monkeypatch.setenv("AI_BILLING_ORGANIZATION_KEY", "agent-org")
    monkeypatch.setenv("BIT_EXECUTION_JOB_ID", "agent-task")
    assert billing.current_usage_context()["actor_id"] == "15"
    assert billing.current_usage_context()["agent_job_id"] == "agent-task"
    billing.set_usage_context_resolver(lambda: {"actor_id": 7, "actor_name": "请求业务员", "organization_key": "alpha"})
    with billing.usage_context(task_type="risk_check", task_id="product-1"):
        run = billing.bind_usage(lambda: billing.current_usage_context())
        def stream():
            yield billing.current_usage_context()
        generate = billing.bind_usage(stream)
    billing.set_usage_context_resolver(None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert pool.submit(run).result()["actor_id"] == 7
    assert next(generate())["task_id"] == "product-1"
    assert next(generate())["agent_job_id"] == "agent-task"
    assert billing.current_usage_context()["task_id"] == "agent-task"


def test_agent_job_kept_when_product_task_changes(ledger, monkeypatch):
    monkeypatch.setenv("BIT_EXECUTION_JOB_ID", "finished-agent-job")
    with billing.usage_context(actor_id=7, organization_key="alpha", task_id="product-33"):
        billing.record_usage({"input_tokens": 1, "output_tokens": 2}, model="unknown")
    stored = ledger()["items"][0]
    assert stored["task_id"] == "product-33"
    assert stored["agent_job_id"] == "finished-agent-job"


def test_record_helper_uses_context_and_drops_provider_content(ledger):
    with billing.usage_context(actor_id=7, actor_name="李明", organization_key="alpha", task_id="product-2"):
        result = billing.record_usage({"id": "completion-1", "usage": {"input_tokens": 10, "output_tokens": 2},
                                       "content": "private product text", "api_key": "never-store"},
                                      model="qwen3.7-flash", base_url=OFFICIAL, task_type="risk_check")
    assert result["record"]["actor_id"] == 7
    assert result["record"]["task_id"] == "product-2"
    raw = billing._path().read_bytes()
    assert b"never-store" not in raw
    assert b"private product text" not in raw


def test_durable_outbox_recovers_unknown_write_outcome(ledger, monkeypatch):
    from bit import bit_db_api
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    with billing.usage_context(actor_id=7, actor_name="李明", organization_key="alpha"):
        result = billing.record_usage({"input_tokens": 100, "output_tokens": 50},
                                      model="qwen3.7-flash", provider="dashscope", base_url=OFFICIAL)
    assert result["queued"] is True
    calls = []
    def uncertain_write(payload):
        calls.append(payload)
        billing.ai_usage_billing(payload)
        raise RuntimeError("response lost after central commit")
    monkeypatch.setattr(bit_db_api, "ai_usage_billing", uncertain_write)
    assert billing.flush_outbox()["pending"] == 1
    assert ledger()["total"] == 1
    monkeypatch.setattr(bit_db_api, "ai_usage_billing", billing.ai_usage_billing)
    assert billing.flush_outbox() == {"delivered": 1, "pending": 0}
    assert ledger()["total"] == 1
    assert ledger()["summary"]["total_tokens"] == 150
    with sqlite3.connect(billing._path()) as db:
        assert db.execute("SELECT COUNT(*) FROM ai_usage_outbox").fetchone()[0] == 0


def test_parallel_records_do_not_lose_usage(ledger):
    def write(index):
        return ledger("record", actor_id=7, input_tokens=1, output_tokens=1, request_id=str(index))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(32)))
    assert ledger()["total"] == 32
    assert ledger()["summary"]["total_tokens"] == 64


def test_central_ledger_write_failure_preserves_durable_outbox(ledger, monkeypatch):
    original = billing._record
    monkeypatch.setattr(billing, "_record", lambda payload: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with billing.usage_context(actor_id=7, organization_key="alpha"):
        result = billing.record_usage({"input_tokens": 2, "output_tokens": 3})
    assert result["queued"] is True
    with sqlite3.connect(billing._path()) as db:
        assert db.execute("SELECT COUNT(*) FROM ai_usage_outbox").fetchone()[0] == 1
    monkeypatch.setattr(billing, "_record", original)
    assert billing.flush_outbox()["delivered"] == 1
    assert ledger()["summary"]["total_tokens"] == 5


def test_same_provider_request_concurrent_replays_remain_one_record(ledger):
    def write(index):
        return ledger("record", actor_id=7, input_tokens=1, output_tokens=1, provider="dashscope", request_id="shared")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(32)))
    assert sum(not value["duplicate"] for value in results) == 1
    assert ledger()["total"] == 1


def test_inconsistent_provider_counts_are_visible_but_excluded_from_confirmed_totals(ledger):
    ledger("record", input_tokens=100, output_tokens=50, total_tokens=140, cached_tokens=110)
    ledger("record", input_tokens=2, output_tokens=3, cached_tokens=1)
    result = ledger()
    unknown = next(item for item in result["items"] if not item["usage_known"])
    assert unknown["input_tokens"] == 100
    assert unknown["cached_tokens"] == 110
    assert unknown["total_tokens"] == 140
    assert unknown["estimated_cost_cny"] is None
    assert result["summary"]["unknown_usage_count"] == 1
    assert result["summary"]["total_tokens"] == 5
    assert result["summary"]["input_tokens"] == 2
    assert result["summary"]["output_tokens"] == 3
    assert result["summary"]["cached_tokens"] == 1
