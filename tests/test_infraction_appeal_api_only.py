from pathlib import Path

from bit import bit_appeal_ai, bit_daily_task, mercado_appeal_runner


def test_automatic_ai_plan_delegates_to_live_api_plan(monkeypatch):
    captured = {}
    expected = [
        {
            "name": "店铺",
            "total": 2,
            "sites": [
                {
                    "site_code": "MX",
                    "count": 2,
                    "infraction_ids": ["MLM-1", "MLM-2"],
                }
            ],
        }
    ]

    def build(**kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(bit_daily_task, "build_latest_infraction_appeal_plan", build)

    result = bit_appeal_ai.build_top_infraction_shop_plan(
        top_shops=6,
        recent_days=100,
        max_workers=4,
    )

    assert result == expected
    assert captured == {
        "top_n": 6,
        "recent_days": 100,
        "min_infraction_count": 0,
        "max_workers": 4,
    }


def test_automatic_ai_round_sends_every_site_with_api_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_appeal_ai,
        "shensu",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "完成",
    )
    monkeypatch.setattr(bit_appeal_ai.time, "sleep", lambda _seconds: None)

    result = bit_appeal_ai.run_top_infraction_shop_once(
        {
            "name": "店铺",
            "total": 3,
            "sites": [
                {
                    "site_code": "MX",
                    "count": 2,
                    "infraction_ids": ["MLM-1", "MLM-2"],
                },
                {
                    "site_code": "BR",
                    "count": 1,
                    "infraction_ids": ["MLB-1"],
                },
            ],
        },
        site_pause=0,
    )

    assert [args[1] for args, _kwargs in calls] == ["MX", "BR"]
    assert [kwargs["infraction_ids"] for _args, kwargs in calls] == [
        ["MLM-1", "MLM-2"],
        ["MLB-1"],
    ]
    assert len(result["results"]) == 2


def test_each_automatic_ai_round_rebuilds_api_plan(monkeypatch):
    builds = []

    def build(**kwargs):
        builds.append(kwargs)
        return []

    monkeypatch.setattr(bit_appeal_ai, "build_top_infraction_shop_plan", build)

    bit_appeal_ai.run_top_infraction_appeal_round(
        max_windows=3,
        top_shops=0,
        recent_days=100,
        site_pause=0,
    )
    bit_appeal_ai.run_top_infraction_appeal_round(
        max_windows=3,
        top_shops=0,
        recent_days=100,
        site_pause=0,
    )

    assert len(builds) == 2
    assert all(call["max_workers"] == 3 for call in builds)


def test_standalone_runner_collects_api_ids_without_cdp(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mercado_appeal_runner.bit_appeal_ai,
        "get_infraction_orders",
        lambda window_id, name, site: calls.append((window_id, name, site))
        or ["MLM-1", "MLM-2"],
    )

    summary, ids = mercado_appeal_runner.collect_infractions(
        "must-not-be-used",
        "MX",
        "店铺",
    )

    assert calls == [("", "店铺", "MX")]
    assert ids == ["MLM-1", "MLM-2"]
    assert summary["source"] == "mercado_moderations_api"


def test_all_infraction_appeal_modules_are_api_only_and_group_by_ten():
    module_paths = [
        Path(bit_daily_task.__file__),
        Path(bit_appeal_ai.__file__),
        Path(mercado_appeal_runner.__file__),
    ]
    sources = "\n".join(path.read_text(encoding="utf-8") for path in module_paths)

    assert "/noindex/pppi/infractions" not in sources
    assert "get_infractions_info(" not in sources
    assert "get_latest_infraction_info(" not in Path(
        bit_appeal_ai.__file__
    ).read_text(encoding="utf-8")
    assert [len(group) for group in mercado_appeal_runner.chunks(list(range(21)), 10)] == [
        10,
        10,
        1,
    ]
