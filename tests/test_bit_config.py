from pathlib import Path

import pytest

from bit import bit_config, bit_db_api, bit_mysql


def _token_data():
    return {
        "rows": [
            {
                "id": 7,
                "display_name": "授权店铺",
                "nickname": "MELI_ALIAS",
                "site_settings": [
                    {
                        "site_id": "MLM",
                        "salesperson": "张三",
                        "visit_stats_enabled": True,
                        "appeal_enabled": False,
                    },
                    {
                        "site_id": "MLB",
                        "salesperson": "李四",
                        "visit_stats_enabled": False,
                        "appeal_enabled": True,
                    },
                ],
            }
        ]
    }


def test_split_config_sites_supports_all_existing_separators():
    assert bit_config.split_config_sites("墨西哥，巴西/智利；阿根廷|乌拉圭") == [
        "墨西哥",
        "巴西",
        "智利",
        "阿根廷",
        "乌拉圭",
    ]


def test_authorization_switches_independently_define_browser_task_rows():
    browsers = [{"id": "window-1", "name": "MELI_ALIAS"}]

    stats_rows = bit_config.list_config_rows(
        authorization_flag="visit_stats_enabled",
        token_data=_token_data(),
        browsers=browsers,
    )
    appeal_rows = bit_config.list_config_rows(
        authorization_flag="appeal_enabled",
        token_data=_token_data(),
        browsers=browsers,
    )

    assert stats_rows == [
        ("window-1", "授权店铺", "", "墨西哥", "", "张三", "")
    ]
    assert appeal_rows == [
        ("window-1", "授权店铺", "", "巴西", "", "李四", "")
    ]


def test_window_lookup_accepts_authorization_nickname(monkeypatch):
    monkeypatch.setattr(
        bit_config.bit_db_api,
        "list_mercado_store_tokens",
        _token_data,
    )
    monkeypatch.setattr(
        bit_config,
        "listBrowsers",
        lambda: [{"id": "window-1", "name": "MELI_ALIAS"}],
    )

    assert bit_config.get_window_id_by_shop_name(
        "授权店铺",
        authorization_flag="appeal_enabled",
    ) == "window-1"
    assert bit_config.require_shop_config(
        shop_name="MELI_ALIAS",
        authorization_flag="appeal_enabled",
    )["shop_name"] == "授权店铺"


def test_missing_browser_window_is_reported_without_restoring_old_table(monkeypatch):
    monkeypatch.setattr(
        bit_config.bit_db_api,
        "list_mercado_store_tokens",
        _token_data,
    )
    monkeypatch.setattr(bit_config, "listBrowsers", lambda: [])

    rows = bit_config.list_shop_configs(authorization_flag="appeal_enabled")

    assert rows[0]["window_id"] == ""
    assert "未匹配比特浏览器窗口" in rows[0]["status"]
    with pytest.raises(RuntimeError, match="未匹配比特浏览器窗口"):
        bit_config.get_window_id_by_shop_name(
            "授权店铺",
            authorization_flag="appeal_enabled",
        )


def test_retired_browser_config_read_api_is_disabled():
    with pytest.raises(RuntimeError, match="已停用"):
        bit_db_api.list_bit_browser_configs()
    with pytest.raises(RuntimeError, match="已停用"):
        bit_db_api.get_bit_browser_config(shop_name="授权店铺")


def test_runtime_config_adapter_does_not_reference_legacy_read_methods():
    source = Path(bit_config.__file__).read_text(encoding="utf-8")
    assert "list_bit_browser_configs(" not in source
    assert "get_bit_browser_config(" not in source


def test_database_snapshot_scope_uses_visit_stats_switch(monkeypatch):
    monkeypatch.setattr(bit_mysql, "list_mercado_store_tokens", _token_data)

    assert bit_mysql._load_authorized_shop_sites("visit_stats_enabled") == [
        {
            "token_id": 7,
            "店铺名": "授权店铺",
            "站点": "墨西哥",
            "site_id": "MLM",
            "业务员": "张三",
            "店铺组": "",
        }
    ]
    assert bit_mysql._load_authorized_shop_sites("appeal_enabled")[0]["站点"] == "巴西"
