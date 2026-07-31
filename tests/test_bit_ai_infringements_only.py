from bit import bit_appeal_ai
from bit_playwright import bit_infractions_info


def test_ai_infraction_orders_requests_infringements_only(monkeypatch):
    captured = {}

    def get_infos(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return [
            ["店铺", "墨西哥", "INF-1", "title", "", "", "", "侵权"],
            ["店铺", "墨西哥", "REPORT-1", "title", "", "", "", "权利人"],
            ["店铺", "墨西哥", "REPORT-2", "title", "", "", "", "reports"],
            ["店铺", "墨西哥", "INF-2"],
        ]

    monkeypatch.setattr(bit_appeal_ai, "get_infractions_info", get_infos)

    result = bit_appeal_ai.get_infraction_orders("window", "店铺", "墨西哥")

    assert result == ["INF-1", "INF-2"]
    assert captured["args"] == ("window", "店铺", "墨西哥", 0)
    assert captured["kwargs"] == {"include_rights_holder": False}


def test_ai_random_infraction_orders_requests_infringements_only(monkeypatch):
    captured = {}

    def get_infos(*args, **kwargs):
        captured["kwargs"] = kwargs
        return [["店铺", "巴西", "INF-1", "title", "", "", "", "infringements"]]

    monkeypatch.setattr(bit_appeal_ai, "get_infractions_info", get_infos)

    result = bit_appeal_ai.get_infraction_orders_random("window", "店铺", "巴西", 10)

    assert "INF-1" in result
    assert captured["kwargs"] == {"include_rights_holder": False}


def test_collector_skips_reports_tab_when_disabled(monkeypatch):
    collected_types = []

    monkeypatch.setattr(bit_infractions_info, "_safe_goto_infractions", lambda *args: None)
    monkeypatch.setattr(bit_infractions_info.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(bit_infractions_info, "_validate_infractions_page", lambda page: None)
    monkeypatch.setattr(bit_infractions_info, "_switch_site_if_needed", lambda *args: None)
    monkeypatch.setattr(bit_infractions_info, "_current_infraction_type", lambda page: "侵权")

    def collect(page, name, site, infraction_type, already_collected):
        collected_types.append(infraction_type)
        already_collected.add(infraction_type)
        return [[name, site, "INF-1", "title", "", "", "", infraction_type]]

    monkeypatch.setattr(bit_infractions_info, "_collect_type_once", collect)
    monkeypatch.setattr(
        bit_infractions_info,
        "_infraction_type_total",
        lambda page, infraction_type: (_ for _ in ()).throw(
            AssertionError(f"不应读取 {infraction_type} 统计")
        ),
    )

    result = bit_infractions_info._collect_site_infractions(
        object(),
        "店铺",
        "墨西哥",
        switch_site=False,
        include_rights_holder=False,
    )

    assert collected_types == ["侵权"]
    assert result[0][2] == "INF-1"
