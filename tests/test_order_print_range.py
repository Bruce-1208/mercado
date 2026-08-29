from datetime import datetime

import pytest

from bit import bit_interface


def _options():
    return {
        "shops": [
            {
                "token_id": 7,
                "shop_name": "店铺甲",
                "sites": ["墨西哥"],
            }
        ],
        "sites": ["墨西哥"],
    }


def test_order_print_params_accept_selected_time_range(monkeypatch):
    monkeypatch.setattr(bit_interface, "_order_print_config_options", _options)

    params = bit_interface.build_order_print_params(
        {
            "shops": ["店铺甲"],
            "sites": ["墨西哥"],
            "date_from": "2026-08-27T08:30",
            "date_to": "2026-08-28T08:30",
        }
    )

    assert params["date_from"] == "2026-08-27T08:30"
    assert params["date_to"] == "2026-08-28T08:30"
    assert datetime.fromisoformat(params["end_at"]) - datetime.fromisoformat(
        params["start_at"]
    ) == bit_interface.timedelta(days=1)


def test_order_print_params_reject_range_over_31_days(monkeypatch):
    monkeypatch.setattr(bit_interface, "_order_print_config_options", _options)

    with pytest.raises(ValueError, match="不能超过 31 天"):
        bit_interface.build_order_print_params(
            {
                "shops": ["店铺甲"],
                "sites": ["墨西哥"],
                "date_from": "2026-06-01T08:30",
                "date_to": "2026-08-01T08:30",
            }
        )
