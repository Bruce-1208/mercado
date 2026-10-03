from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bit import bit_db_api, clearance_alerts as jobs
from bit.service_split import is_worker_path
from erp.mercadolibre_clearance_store import ClearanceStore


NOW = datetime(2026, 9, 30, 4, tzinfo=timezone.utc)
SCOPES = {(1, "MLM"): {"token_id": 1, "site_id": "MLM", "store_name": "A", "group_name": "半托管"}}


def test_api_clients_forward_empty_scope_without_global_expansion(monkeypatch):
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    calls = []
    monkeypatch.setattr(bit_db_api, "_request", lambda *a, **kw: calls.append((a, kw)))
    bit_db_api.list_clearance_alerts(allowed_token_ids=set(), page=1)
    assert calls[-1][1]["params"]["allowed_token_ids"] == [""]
    bit_db_api.refresh_clearance_alerts(allowed_token_ids=set())
    assert calls[-1][1]["json"]["allowed_token_ids"] == []
    bit_db_api.save_clearance_facts(5, {"sku_key": "variation:2"}, allowed_token_ids={1})
    assert calls[-1][1]["json"] == {"facts": {"sku_key": "variation:2"}, "allowed_token_ids": [1]}


def test_worker_owns_all_stateful_reads_and_writes():
    for path in ("/api/clearance-alerts", "/api/clearance-alerts/refresh", "/api/clearance-alerts/5/facts",
                 "/api/db/clearance-alerts", "/api/db/clearance-alerts/refresh", "/api/db/clearance-alerts/5/facts"):
        assert is_worker_path(path)


def test_list_retains_store_options_and_blocks_forbidden_token(monkeypatch, tmp_path):
    store = ClearanceStore(tmp_path / "alerts.sqlite")
    scopes = {**SCOPES, (2, "MLB"): {"token_id": 2, "site_id": "MLB", "store_name": "B", "group_name": "半托管"}}
    monkeypatch.setattr(jobs, "current_scopes", lambda *a, **kw: ([], scopes))
    monkeypatch.setattr(jobs, "ClearanceStore", lambda: store)
    result = jobs.list_alerts(allowed_token_ids={1, 2}, token_id=1)
    assert {s["token_id"] for s in result["stores"]} == {1, 2}
    with pytest.raises(PermissionError):
        jobs.list_alerts(allowed_token_ids={1}, token_id=2)


def test_empty_scope_does_not_start_scan(monkeypatch):
    monkeypatch.setattr(jobs, "current_scopes", lambda *a, **kw: ([], {}))
    monkeypatch.setattr(jobs, "InterProcessLock", lambda *a, **kw: pytest.fail("must not acquire lock"))
    assert jobs.start_refresh(allowed_token_ids=set())["started"] is False


def test_scan_exhausts_generator_before_commit_and_marks_scoped_failure(monkeypatch):
    from bit import clearance_sources
    calls = []
    def partial(*a, **kw):
        yield {"link_id": 1}
        raise RuntimeError("partial read")
    monkeypatch.setattr(clearance_sources, "collect_store", partial)
    monkeypatch.setattr(jobs, "run_capacity_store", lambda kind, token, call: call())
    fake_store = SimpleNamespace(replace_store=lambda *a, **kw: pytest.fail("no partial commit"),
        mark_failed=lambda *a, **kw: calls.append((a, kw)))
    released = []
    jobs._running_tokens.add(1)
    jobs._scan([{"id": 1}], SCOPES, fake_store, SimpleNamespace(release=lambda: released.append(True)))
    assert calls[0][1]["site_ids"] == {"MLM"}
    assert released == [True]
    assert jobs._running_tokens == set()


def test_scan_passes_only_requested_sites_to_replace(monkeypatch):
    from bit import clearance_sources
    calls = []
    monkeypatch.setattr(clearance_sources, "collect_store", lambda *a, **kw: iter([]))
    monkeypatch.setattr(jobs, "run_capacity_store", lambda kind, token, call: call())
    fake_store = SimpleNamespace(replace_store=lambda *a, **kw: calls.append((a, kw)),
        mark_failed=lambda *a, **kw: pytest.fail("unexpected failure"))
    jobs._scan([{"id": 1}], SCOPES, fake_store, SimpleNamespace(release=lambda: None))
    assert calls[0][1]["site_ids"] == {"MLM"}
