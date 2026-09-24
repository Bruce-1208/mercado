from collections import Counter
from datetime import datetime
import threading
from unittest.mock import Mock

import pytest

from bit import bit_daily_task as daily
from bit import bit_interface


@pytest.fixture
def algorithm_data(monkeypatch):
    # All data is isolated: no real API, database, browser or appeal submission.
    monkeypatch.setattr(daily, "list_mercado_store_tokens", lambda: {"rows": [
        {"id": 1, "display_name": "测试店铺", "site_settings": [
            {"site_id": "MLM", "appeal_enabled": True},
            {"site_id": "MLB", "appeal_enabled": True},
            {"site_id": "MLC", "appeal_enabled": False},
        ]},
    ]})
    today = datetime.now().strftime("%Y-%m-%d")
    collection = Mock(return_value={"data": [
        ["测试店铺", site, f"INF-{site}-{i}", "", today, "", "", "侵权"]
        for site, count in [("墨西哥", 21), ("巴西", 2), ("智利", 100)]
        for i in range(count)
    ]})
    reputation = Mock(return_value={"rows": [
        {"店铺名": "测试店铺", "站点": "墨西哥", "延误率": "10%", "投诉率": "8%", "取消率": "6%"},
        {"店铺名": "测试店铺", "站点": "巴西", "延误率": "1%", "投诉率": "1%", "取消率": "1%"},
        {"店铺名": "测试店铺", "站点": "智利", "延误率": "100%", "投诉率": "100%", "取消率": "100%"},
    ]})
    monkeypatch.setattr(daily.mercado_infraction_sync, "collect_live_detection_infractions", collection)
    monkeypatch.setattr(daily, "get_latest_reputation_info", reputation)
    return collection, reputation


def test_algorithm_combines_four_metrics_and_preserves_authorization(algorithm_data):
    collection, reputation = algorithm_data
    plan = daily.build_appeal_plan("算法模式", top_n=0)
    assert len(plan) == 1
    assert {site["site_code"] for site in plan[0]["sites"]} == {"MX", "BR"}
    assert {site["appeal_type"] for site in plan[0]["sites"]} == {"侵权", "延误", "投诉", "取消率"}
    assert {site["algorithm_weight"] for site in plan[0]["sites"] if site["site_code"] == "MX"} == {5}
    assert {site["algorithm_weight"] for site in plan[0]["sites"] if site["site_code"] == "BR"} == {1}
    collection.assert_called_once()
    reputation.assert_called_once()

    schedule = daily.build_weighted_site_schedule(plan[0]["sites"], "算法模式")
    counts = Counter((site["site_code"], site["appeal_type"]) for site in schedule)
    assert schedule[0]["site_code"] == "MX"
    for kind in ("延误", "投诉", "取消率"):
        assert counts["MX", kind] == 5
        assert counts["BR", kind] == 1
    ids = [item for task in schedule for item in task.get("infraction_ids", [])]
    assert len(ids) == len(set(ids)) == 23
    assert counts["MX", "侵权"] == 3
    assert counts["BR", "侵权"] == 1


def test_algorithm_obeys_all_execution_thresholds(algorithm_data):
    plan = daily.build_appeal_plan(
        "算法模式", top_n=0, min_infraction_count=2, min_delay_rate="1%",
        min_complaint_rate="8%", min_cancellation_rate="6%",
    )
    assert [(s["site_code"], s["appeal_type"]) for s in plan[0]["sites"]] == [
        ("MX", "侵权"), ("MX", "延误"),
    ]
    assert {s["algorithm_weight"] for s in plan[0]["sites"]} == {1}
    assert daily.build_appeal_plan("算法模式", min_infraction_count=100,
                                 min_rate="100%") == []


@pytest.mark.parametrize("kind", ["侵权", "延误", "投诉", "取消率"])
def test_each_metric_independently_prioritizes_shops(monkeypatch, kind):
    def sites(high):
        return [{"site_code": "MX", "count": 10 if high else 1}]
    plans = [{"name": "低风险", "sites": sites(False)}, {"name": "高风险", "sites": sites(True)}]
    monkeypatch.setattr(daily, "build_latest_infraction_appeal_plan",
                        lambda **kwargs: plans if kind == "侵权" else [])
    monkeypatch.setattr(daily, "get_latest_reputation_info", lambda: {})
    monkeypatch.setattr(daily, "build_latest_reputation_appeal_plan",
                        lambda appeal_type, **kwargs: plans if appeal_type == kind else [])
    result = daily.build_appeal_plan("算法模式", top_n=1)
    assert [shop["name"] for shop in result] == ["高风险"]
    assert result[0]["sites"][0]["algorithm_weight"] == 5


def test_algorithm_recalculates_weights_each_round_and_stops_collection(algorithm_data):
    collection, reputation = algorithm_data
    first = daily.build_appeal_plan("算法模式", top_n=0)
    # Remove infractions and reverse all reputation metrics in the next snapshot.
    collection.return_value = {"data": []}
    for row in reputation.return_value["rows"]:
        for field in ("延误率", "投诉率", "取消率"):
            row[field] = "1%" if row["站点"] == "墨西哥" else "20%"
    second = daily.build_appeal_plan("算法模式", top_n=0)
    assert first[0]["sites"][0]["site_code"] == "MX"
    assert second[0]["sites"][0]["site_code"] == "BR"
    stop = threading.Event()
    stop.set()
    assert daily.build_appeal_plan("算法模式", stop_event=stop) == []
    assert collection.call_count == reputation.call_count == 2


@pytest.mark.parametrize("status", ["no_data", "rate_limited", "sent_unknown", "failed"])
def test_algorithm_does_not_use_extra_slots_to_retry_exhausted_task(monkeypatch, status):
    calls = []
    def appeal(name, site, form, message, **kwargs):
        calls.append((site, form))
        return {"execution_status": status if site == "MX" else "sent"}
    monkeypatch.setattr(daily.bit_appeal_ai, "shensu", appeal)
    monkeypatch.setattr(daily, "_resolve_login_anomaly", lambda *args: None)
    monkeypatch.setattr(daily, "_close_ai_appeal_browser", lambda *args: {"success": True})
    daily._appeal_one_shop_locked(
        {"name": "测试店铺", "total": 1, "sites": [
            {"site_code": "MX", "count": 0.1, "appeal_type": "投诉", "algorithm_weight": 5},
            {"site_code": "BR", "count": 0.01, "appeal_type": "取消率", "algorithm_weight": 1},
        ]}, "window", object(), appeal_type="算法模式", site_pause=0, rate_limit_retries=0,
    )
    assert calls == [("MX", "投诉"), ("BR", "取消率")]


def test_algorithm_worker_dispatches_real_types_and_honors_stop(monkeypatch, algorithm_data):
    plan = daily.build_appeal_plan("算法模式", top_n=0)[0]
    calls = []
    stop = threading.Event()
    def appeal(name, site, form, message, **kwargs):
        calls.append((site, form, kwargs))
        if len(calls) == 3:
            stop.set()
        return {"execution_status": "sent"}
    monkeypatch.setattr(daily.bit_appeal_ai, "shensu", appeal)
    monkeypatch.setattr(daily, "_resolve_login_anomaly", lambda *args: None)
    result = daily._appeal_one_shop_locked(
        plan, "window", object(), appeal_type="算法模式", site_pause=0, stop_event=stop,
        appeal_copy_mode="AI话术模式", deepseek_api_key="test-token",
    )
    assert len(calls) == 3
    assert all(form in {"侵权", "延误", "投诉", "取消率"} for _, form, _ in calls)
    assert len(calls[0][2]["infraction_ids"]) == 3
    assert calls[0][2]["ai_script_mode"] is True
    assert result["exit_reason"] == "已停止"


@pytest.mark.parametrize("mode", ["once", "loop"])
def test_algorithm_console_dispatches_single_and_loop_modes(monkeypatch, tmp_path, mode):
    target = "run_ai_appeal_once" if mode == "once" else "loop_ai_appeal"
    runner = Mock(return_value=[])
    monkeypatch.setattr(daily, target, runner)
    params = bit_interface.build_daily_task_params({
        "mode": mode, "appeal_types": ["算法模式", "混合模式", "侵权"],
        "group_names": ["测试组"], "complaint_min_rate": "2%",
    })
    assert params["appeal_types"] == ["算法模式"]
    bit_interface.execute_daily_task(params, Mock(), threading.Event(), "test",
                                     tmp_path / "test.log", {})
    assert runner.call_args.args == ("算法模式",)
    assert runner.call_args.kwargs["group_names"] == ["测试组"]
    assert runner.call_args.kwargs["min_complaint_rate"] == 0.02
