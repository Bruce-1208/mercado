from pathlib import Path

import pytest

from bit import bit_interface
from bit import mercado_infraction_sync as sync
from erp.mercadolibre_infraction_store import _build_group_tree


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"filter_subgroup": "BRAND_PROTECTION"}, True),
        ({"reason": "The product could be counterfeit."}, True),
        ({"reason": "The product's brand is not generic."}, True),
        ({"reason": "la publicación usa una marca ilegítimamente"}, True),
        ({"reason": "O produto pode ser falsificado."}, True),
        ({"reason": "The cover photo has watermarks."}, False),
        ({"reason": "Title and photos did not match the category."}, False),
    ],
)
def test_brand_protection_detection_classifier(payload, expected):
    assert sync.is_brand_protection_detection(payload) is expected


def test_case_rows_separates_api_paging_metadata():
    rows, paging = sync._case_rows(
        [
            {"case_id": 371, "item_id": "MLM123", "current_status": "CREATED"},
            {"case_id": 372, "item_id": "MLB456", "current_status": "WAITING_DOCUMENTATION"},
            {"total": 62, "offset": 0, "limit": 50},
        ]
    )

    assert [row["case_id"] for row in rows] == [371, 372]
    assert paging == {"total": 62, "offset": 0, "limit": 50}


def test_legacy_short_date_is_normalized_for_dashboard_filtering():
    assert sync._mysql_datetime("9/3/26") == "2026-09-03 00:00:00"


def test_thumbnail_url_prefers_official_picture_and_https():
    assert sync._thumbnail_url(
        {
            "pictures": [
                {"url": "http://http2.mlstatic.com/D_123-O.jpg"},
            ],
            "thumbnail": "https://example.com/fallback.jpg",
        }
    ) == "https://http2.mlstatic.com/D_123-O.jpg"


def test_detection_pages_use_reason_fallback_and_deduplicate():
    class Client:
        def request(self, _method, _path, *, params):
            assert params["date_created_since"] == "2026-09-01"
            return {
                "infractions": [
                    {
                        "id": "1",
                        "related_item_id": "MLM1",
                        "reason": "The product could be counterfeit.",
                    },
                    {
                        "id": "2",
                        "related_item_id": "MLM2",
                        "reason": "The cover photo has watermarks.",
                    },
                    {
                        "id": "1",
                        "related_item_id": "MLM1",
                        "reason": "The product could be counterfeit.",
                    },
                ],
                "paging": {"offset": 0, "limit": 20, "total": 3},
            }

    rows, scanned, capped = sync._fetch_detection_pages(
        Client(),
        "123",
        date_created_since="2026-09-01",
    )

    assert [row["id"] for row in rows] == ["1"]
    assert scanned == 3
    assert capped is False


def test_live_api_collection_filters_authorized_sites_and_isolates_failures(
    monkeypatch,
):
    monkeypatch.setattr(
        sync,
        "_token_records",
        lambda token_ids: [
            {
                "id": 1,
                "display_name": "正常店铺",
                "meli_user_id": "root-1",
                "access_token": "token-1",
                "site_settings": [],
            },
            {
                "id": 2,
                "display_name": "异常店铺",
                "meli_user_id": "root-2",
                "access_token": "token-2",
                "site_settings": [],
            },
        ],
    )

    def fake_client(record):
        if record["id"] == 2:
            raise RuntimeError("Token 已失效")
        return object(), record

    monkeypatch.setattr(sync, "_client_and_token", fake_client)
    monkeypatch.setattr(
        sync,
        "_marketplace_accounts",
        lambda _client, _root: [
            {"user_id": "seller-mx", "site_id": "MLM"},
            {"user_id": "seller-br", "site_id": "MLB"},
        ],
    )

    def fake_collect(
        _client,
        accounts,
        *,
        date_created_since,
        store_name,
        stop_event,
        deadline,
    ):
        assert accounts == [{"user_id": "seller-mx", "site_id": "MLM"}]
        assert date_created_since
        assert store_name == "正常店铺"
        assert stop_event is None
        assert deadline
        return (
            [
                {
                    "source_id": "infraction-1",
                    "site_id": "MLM",
                    "item_id": "MLM123",
                    "title": "测试商品",
                    "occurred_at": "2026-09-03 12:00:00",
                },
                {
                    "source_id": "infraction-duplicate",
                    "site_id": "MLM",
                    "item_id": "MLM123",
                    "title": "重复商品",
                    "occurred_at": "2026-09-03 12:00:00",
                },
                {
                    "source_id": "not-authorized",
                    "site_id": "MLB",
                    "item_id": "MLB999",
                    "title": "未授权站点",
                    "occurred_at": "2026-09-03 12:00:00",
                },
            ],
            3,
            False,
        )

    monkeypatch.setattr(sync, "_collect_live_detection_records", fake_collect)

    result = sync.collect_live_detection_infractions(
        [
            {"token_id": 1, "name": "正常店铺", "site_ids": ["MLM"]},
            {"token_id": 2, "name": "异常店铺", "site_ids": ["MLM"]},
        ],
        recent_days=100,
        max_workers=2,
    )

    assert [(row["店铺名"], row["站点"], row["编号"]) for row in result["data"]] == [
        ("正常店铺", "MLM", "MLM123")
    ]
    assert result["source"] == "mercado_moderations_api"
    assert result["failed_stores"][0]["store"] == "异常店铺"
    assert "Token 已失效" in result["failed_stores"][0]["message"]


def test_case_pages_follow_metadata_pagination(monkeypatch):
    monkeypatch.setattr(sync, "INFRACTION_MAX_CASE_PAGES", 5)

    class Client:
        def __init__(self):
            self.offsets = []

        def request(self, _method, _path, *, params):
            self.offsets.append(params["offset"])
            offset = params["offset"]
            if offset == 0:
                cases = [
                    {"case_id": index, "item_id": f"MLM{index}"}
                    for index in range(1, 51)
                ]
                return cases + [{"total": 52, "offset": 0, "limit": 50}]
            return [
                {"case_id": 51, "item_id": "MLM51"},
                {"case_id": 52, "item_id": "MLM52"},
                {"total": 52, "offset": 50, "limit": 50},
            ]

    client = Client()
    rows, capped = sync._fetch_case_pages(
        client,
        date_created_since="2026-01-01",
    )

    assert client.offsets == [0, 50]
    assert len(rows) == 52
    assert capped is False


def test_group_tree_nests_account_group_salesperson_and_store():
    tree = _build_group_tree(
        [
            {
                "token_id": 1,
                "store_name": "店铺一",
                "site_id": "MLM",
                "group_name": "精品组",
                "salesperson": "张三",
                "detection_count": 3,
                "rights_holder_count": 1,
            },
            {
                "token_id": 2,
                "store_name": "店铺二",
                "site_id": "MLB",
                "group_name": "精品组",
                "salesperson": "张三",
                "detection_count": 2,
                "rights_holder_count": 0,
                "latest_infraction_at": "2026-09-02 10:00:00",
            },
            {
                "token_id": 1,
                "store_name": "店铺一",
                "site_id": "MLC",
                "group_name": "精品组",
                "salesperson": "张三",
                "detection_count": 1,
                "rights_holder_count": 0,
                "latest_infraction_at": "2026-09-03 10:00:00",
            },
            {
                "token_id": 3,
                "store_name": "店铺三",
                "site_id": "MLA",
                "group_name": "",
                "salesperson": "",
                "detection_count": 0,
                "rights_holder_count": 1,
            },
        ]
    )

    assert [group["group_name"] for group in tree] == ["精品组", "未分组"]
    assert tree[0]["total"] == 7
    assert tree[0]["store_count"] == 2
    assert tree[0]["site_count"] == 3
    assert tree[0]["salespeople"][0]["salesperson"] == "张三"
    assert tree[0]["salespeople"][0]["stores"][0]["store_name"] == "店铺一"
    assert tree[0]["salespeople"][0]["stores"][0]["site_count"] == 2
    assert tree[0]["salespeople"][0]["stores"][0]["total"] == 5
    assert tree[0]["salespeople"][0]["stores"][0]["site_names"] == ["墨西哥", "智利"]
    assert [store["total"] for store in tree[0]["salespeople"][0]["stores"]] == [5, 2]
    assert tree[1]["salespeople"][0]["salesperson"] == "未分配"


def _client(monkeypatch):
    user = {
        "id": 1,
        "username": "tester",
        "display_name": "测试员",
        "access_version": 0,
    }
    monkeypatch.setattr(bit_interface, "get_current_workbench_user", lambda: user)
    client = bit_interface.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = user
    return client


def test_independent_dashboard_page_and_data_api(monkeypatch):
    dashboard = {
        "summary": {"total": 4},
        "account_groups": [],
        "rows": [],
        "filters": {"days": 30, "groups": [], "salespeople": []},
        "total": 0,
        "page": 1,
        "page_size": 100,
        "pages": 1,
    }
    received = {}

    def fake_dashboard(**kwargs):
        received.update(kwargs)
        return dashboard

    monkeypatch.setattr(bit_interface, "list_infraction_dashboard", fake_dashboard)
    client = _client(monkeypatch)

    page_response = client.get("/infringement-dashboard")
    api_response = client.get(
        "/api/official-infractions/dashboard?days=30&detail_token_id=7"
    )

    assert page_response.status_code == 200
    assert "按账户组与业务员查看侵权" in page_response.get_data(as_text=True)
    assert api_response.status_code == 200
    assert api_response.get_json()["data"] == dashboard
    assert received["detail_token_id"] == "7"


def test_dashboard_sync_endpoint_starts_background_job(monkeypatch):
    monkeypatch.setattr(
        bit_interface.mercado_infraction_sync,
        "start_official_infraction_sync",
        lambda token_ids: (
            True,
            {"running": True, "task_id": "task-1", "token_ids": token_ids},
        ),
    )
    response = _client(monkeypatch).post(
        "/api/official-infractions/sync",
        json={"token_ids": [2, 3]},
    )

    assert response.status_code == 202
    assert response.get_json()["data"]["token_ids"] == [2, 3]


def test_console_template_links_to_independent_dashboard():
    source = (
        Path(bit_interface.resolve_template_dir()) / "index.html"
    ).read_text(encoding="utf-8")

    assert "/infringement-dashboard" in source
    assert "独立分组看板" in source


def test_dashboard_template_supports_store_detail_drilldown():
    source = (
        Path(bit_interface.resolve_template_dir()) / "infraction_dashboard.html"
    ).read_text(encoding="utf-8")

    assert "查看明细" in source
    assert "detail_token_id" in source
    assert "查看全部明细" in source
    assert "产品图" in source
    assert "thumbnail_url" in source
