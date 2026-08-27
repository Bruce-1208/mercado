import json

import pytest

from bit import bit_reputation_info as reputation


def _performance_payload(values):
    return {
        "type": "reload",
        "data": {
            "events": [
                {
                    "data": {
                        "bricks": [
                            {
                                "id": "performance_summary_line_chart",
                                "ui_type": "metrics_line_chart",
                                "data": {
                                    "dataset": [
                                        {
                                            "date": f"2026-08-{index + 1:02d}",
                                            "visits": value,
                                            "gross_sales": index,
                                        }
                                        for index, value in enumerate(values)
                                    ],
                                    "line_config": [
                                        {"data_key": "gross_sales"},
                                        {"data_key": "visits", "name": "Visits"},
                                    ],
                                },
                            }
                        ]
                    }
                }
            ],
            "unrelated": {
                "dataset": [
                    {"stage": "unique_visits", "all_sales": 9999},
                ],
                "visits": 9999,
            },
        },
    }


class FakeApiDriver:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def execute_async_script(self, _script, url):
        self.calls.append(url)
        return {"ok": True, "status": 200, "text": json.dumps(self.payload)}


def test_extract_visit_chart_records_selects_daily_line_chart_only():
    payload = _performance_payload([10, 0, 30, 40, 50, 60, 70, 80])

    records = reputation._extract_visit_chart_records(payload, days=8)

    assert [record["visits"] for record in records] == [
        "10",
        "0",
        "30",
        "40",
        "50",
        "60",
        "70",
        "80",
    ]


def test_metrics_api_reads_known_endpoint_and_keeps_zero_days():
    driver = FakeApiDriver(_performance_payload([0, 1, 0, 2, 0, 3, 0, 4]))

    records = reputation._extract_visits_from_metrics_api(driver, days=8)

    assert driver.calls == [reputation.METRICS_PERFORMANCE_DATA_URL]
    assert reputation._to_visit_number_list(records, 8) == [0, 1, 0, 2, 0, 3, 0, 4]


def test_recent_visits_uses_api_without_clicking_or_hovering(monkeypatch):
    expected = _performance_payload([8, 7, 6, 5, 4, 3, 2, 1])
    driver = FakeApiDriver(expected)
    monkeypatch.setattr(reputation, "_open_collection_backend_page", lambda *a, **k: {})
    monkeypatch.setattr(reputation, "_select_country", lambda *a, **k: True)
    monkeypatch.setattr(
        reputation,
        "_click_visits_metric",
        lambda *_args: pytest.fail("稳定接口成功时不应点击图表"),
    )

    result = reputation.get_recent_visits_info(
        driver,
        "window-1",
        "测试店铺",
        "墨西哥",
        days=8,
    )

    assert result == [8, 7, 6, 5, 4, 3, 2, 1]


def test_empty_api_and_fallback_raise_instead_of_silent_empty(monkeypatch):
    class EmptyDriver:
        def save_screenshot(self, _path):
            return True

    driver = EmptyDriver()
    monkeypatch.setattr(reputation, "_open_collection_backend_page", lambda *a, **k: {})
    monkeypatch.setattr(reputation, "_select_country", lambda *a, **k: True)
    monkeypatch.setattr(reputation, "_extract_visits_from_metrics_api", lambda *a: [])
    monkeypatch.setattr(reputation, "_click_visits_metric", lambda *a: None)
    monkeypatch.setattr(reputation, "_extract_visits_from_network", lambda *a: [])
    monkeypatch.setattr(reputation, "_extract_visits_from_dom", lambda *a: [])
    monkeypatch.setattr(reputation, "_extract_visits_by_hover", lambda *a: [])
    monkeypatch.setattr(reputation.time, "sleep", lambda *_args: None)

    with pytest.raises(reputation.MercadoPageStructureError, match="数据不完整：0/8天"):
        reputation.get_recent_visits_info(
            driver,
            "window-1",
            "测试店铺",
            "巴西",
            days=8,
        )


def test_partial_api_and_fallback_raise_instead_of_reporting_success(monkeypatch):
    class PartialDriver:
        def save_screenshot(self, _path):
            return True

    driver = PartialDriver()
    partial = [
        {"date": f"2026-08-{index + 1:02d}", "visits": str(index)}
        for index in range(7)
    ]
    monkeypatch.setattr(reputation, "_open_collection_backend_page", lambda *a, **k: {})
    monkeypatch.setattr(reputation, "_select_country", lambda *a, **k: True)
    monkeypatch.setattr(reputation, "_extract_visits_from_metrics_api", lambda *a: partial)
    monkeypatch.setattr(reputation, "_click_visits_metric", lambda *a: None)
    monkeypatch.setattr(reputation, "_extract_visits_from_network", lambda *a: [])
    monkeypatch.setattr(reputation, "_extract_visits_from_dom", lambda *a: [])
    monkeypatch.setattr(reputation, "_extract_visits_by_hover", lambda *a: [])
    monkeypatch.setattr(reputation.time, "sleep", lambda *_args: None)

    with pytest.raises(reputation.MercadoPageStructureError, match="数据不完整：7/8天"):
        reputation.get_recent_visits_info(
            driver,
            "window-1",
            "测试店铺",
            "墨西哥",
            days=8,
        )


def test_partial_dated_sources_are_merged_without_inventing_days():
    api_records = [
        {"date": f"2026-08-{day:02d}", "visits": str(day)}
        for day in range(1, 8)
    ]
    network_records = [{"date": "2026-08-08", "visits": "8"}]

    merged = reputation._merge_visit_candidates(
        [api_records, network_records],
        days=8,
    )

    assert reputation._to_visit_number_list(merged, 8) == list(range(1, 9))


def test_undated_partial_sources_are_not_concatenated():
    first = [{"date": "", "visits": str(value)} for value in range(5)]
    second = [{"date": "", "visits": str(value)} for value in range(4)]

    merged = reputation._merge_visit_candidates([first, second], days=8)

    assert len(merged) == 5


def test_traffic_failure_status_preserves_specific_reason():
    error = reputation.TrafficCollectionError(
        "测试店铺墨西哥 Visits/访问量数据不完整：3/8天；业务指标接口 HTTP 500"
    )

    status = reputation._failure_status(error)

    assert status.startswith("失败：流量数据读取失败：数据不完整：3/8天")


def test_stuck_bitbrowser_open_has_actionable_failure_status():
    error = reputation.BitBrowserWindowError(
        "打开比特浏览器窗口失败：{'msg': '浏览器正在打开中'}"
    )

    assert reputation._failure_status(error) == "失败：比特浏览器窗口一直处于启动中"


def test_tooltip_lookup_is_single_javascript_call():
    class TooltipDriver:
        def __init__(self):
            self.calls = 0

        def execute_script(self, _script):
            self.calls += 1
            return "Aug. 25\n123"

    driver = TooltipDriver()

    assert reputation._get_tooltip_text(driver) == "Aug. 25\n123"
    assert driver.calls == 1


@pytest.mark.parametrize(
    ("actual", "site"),
    [
        ("Mexico", "墨西哥"),
        ("México", "墨西哥"),
        ("Brasil", "巴西"),
        ("Brazil", "巴西"),
        ("Argentina", "阿根廷"),
    ],
)
def test_country_name_matches_localized_site_names(actual, site):
    assert reputation._country_name_matches(actual, site)
