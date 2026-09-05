from pathlib import Path

import pytest

from bit import bit_interface
from bit import mercado_infraction_sync as sync
from erp.mercadolibre_infraction_store import _build_group_tree
from erp import mercadolibre_infraction_store as infraction_store


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


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("The product's brand is not generic.", False),
        ("  THE PRODUCT'S BRAND IS NOT GENERIC.  ", False),
        ("The product could be counterfeit.", True),
        ("", True),
    ],
)
def test_auto_appeal_excludes_non_generic_brand_reason(reason, expected):
    assert sync.is_auto_appeal_eligible_detection({"reason": reason}) is expected


def test_prohibited_product_is_classified_for_infringement_appeal():
    payload = {"reason": " The product is prohibited. "}

    assert sync.is_prohibited_detection(payload) is True
    assert sync.is_auto_appeal_eligible_detection(payload) is True


def test_prohibited_snapshot_is_normalized_and_respects_recent_window(monkeypatch):
    monkeypatch.setattr(
        sync,
        "get_prohibited_sync_context",
        lambda _token_id: {
            "rows": [
                {
                    "item_id": "MLM123",
                    "site_id": "MLM",
                    "seller_id": "seller-mx",
                    "infraction_id": "prohibited-1",
                    "infraction_reason": "The product is prohibited.",
                    "infraction_date": "2026-09-03 10:20:30",
                    "title": "禁限售商品",
                    "thumbnail_url": "http://http2.mlstatic.com/test.jpg",
                    "permalink": "https://articulo.mercadolibre.com.mx/MLM123",
                    "status": "under_review",
                    "remedy": "Check our policies.",
                }
            ]
        },
    )
    record = {
        "id": 7,
        "meli_user_id": "root-seller",
        "site_settings": [
            {"site_id": "MLM", "salesperson": "张三", "group_name": "一组"}
        ],
    }

    rows = sync._prohibited_snapshot_records(
        record,
        date_created_since="2026-09-01",
    )

    assert len(rows) == 1
    assert rows[0]["source_id"] == "prohibited-1"
    assert rows[0]["item_id"] == "MLM123"
    assert rows[0]["reason_code"] == sync.PROHIBITED_REASON_CODE
    assert rows[0]["reason"] == sync.PROHIBITED_REASON
    assert rows[0]["thumbnail_url"].startswith("https://")
    assert rows[0]["salesperson"] == "张三"
    assert sync._prohibited_snapshot_records(
        record,
        date_created_since="2026-09-04",
    ) == []


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


def test_full_detection_snapshot_omits_empty_date_filter():
    class Client:
        def request(self, _method, _path, *, params):
            assert "date_created_since" not in params
            return {"infractions": [], "paging": {"total": 0}}

    rows, scanned, capped = sync._fetch_detection_pages(
        Client(),
        "123",
        date_created_since="",
    )

    assert rows == []
    assert scanned == 0
    assert capped is False


def test_live_appeal_collection_excludes_non_generic_brand_reason(monkeypatch):
    monkeypatch.setattr(
        sync,
        "_fetch_detection_pages",
        lambda *_args, **_kwargs: (
            [
                {
                    "id": "excluded",
                    "related_item_id": "MLM1",
                    "date_created": "2026-09-04",
                    "reason": "The product's brand is not generic.",
                },
                {
                    "id": "eligible",
                    "related_item_id": "MLM2",
                    "date_created": "2026-09-04",
                    "reason": "The product could be counterfeit.",
                },
            ],
            2,
            False,
        ),
    )

    class Client:
        access_token = "token"
        timeout = 10

    records, scanned, capped, errors, successful = sync._collect_live_detection_records(
        Client(),
        [{"user_id": "seller", "site_id": "MLM"}],
        date_created_since="2026-09-01",
        store_name="店铺",
    )

    assert [row["item_id"] for row in records] == ["MLM2"]
    assert records[0]["reason"] == "The product could be counterfeit."
    assert (scanned, capped, errors, successful) == (2, False, [], 1)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("DOCUMENTATION_APPROVED", (False, "appeal_success")),
        ("MEMBER_NOT_RESPOND", (False, "appeal_success")),
        ("ROLLBACK", (False, "appeal_success")),
        ("DOCUMENTATION_NOT_APPROVED", (False, "appeal_failed")),
        ("WAITING_DOCUMENTATION", (True, "current")),
    ],
)
def test_rights_holder_status_maps_to_dashboard_state(status, expected):
    state = sync._rights_holder_state(status)

    assert (state["is_current"], state["resolution_status"]) == expected


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

    def fake_client(record, **_kwargs):
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
            [],
            1,
        )

    monkeypatch.setattr(sync, "_collect_live_detection_records", fake_collect)
    monkeypatch.setattr(
        sync,
        "_prohibited_snapshot_records",
        lambda record, **_kwargs: (
            [
                {
                    "source_id": "prohibited-1",
                    "site_id": "MLM",
                    "item_id": "MLM-PROHIBITED",
                    "title": "禁限售商品",
                    "occurred_at": "2026-09-03 13:00:00",
                    "reason": "The product is prohibited.",
                }
            ]
            if record["id"] == 1
            else []
        ),
    )

    result = sync.collect_live_detection_infractions(
        [
            {"token_id": 1, "name": "正常店铺", "site_ids": ["MLM"]},
            {"token_id": 2, "name": "异常店铺", "site_ids": ["MLM"]},
        ],
        recent_days=100,
        max_workers=2,
    )

    assert [(row["店铺名"], row["站点"], row["编号"]) for row in result["data"]] == [
        ("正常店铺", "MLM", "MLM123"),
        ("正常店铺", "MLM", "MLM-PROHIBITED"),
    ]
    assert result["data"][1]["侵权原因"] == "The product is prohibited."
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


def test_current_count_snapshot_flattens_same_dashboard_tree(monkeypatch):
    monkeypatch.setattr(
        infraction_store,
        "list_infraction_dashboard",
        lambda **kwargs: {
            "summary": {"last_synced_at": "2026-09-04 09:00:00"},
            "account_groups": [{
                "salespeople": [{
                    "stores": [{
                        "token_id": 8,
                        "sites": [{
                            "site_id": "MLM",
                            "detection_count": 5,
                            "rights_holder_count": 3,
                        }],
                    }],
                }],
            }],
        },
    )

    result = infraction_store.current_infraction_counts_by_token_site(100)

    assert result["counts"][(8, "MLM")] == {
        "infraction_count": 5,
        "rights_holder_count": 3,
        "latest_infraction_at": None,
    }
    assert result["last_synced_at"] == "2026-09-04 09:00:00"


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
    embedded_response = client.get("/infringement-dashboard?embedded=1")
    api_response = client.get(
        "/api/official-infractions/dashboard?days=30&view_mode=history&category=counterfeit&detail_token_id=7"
    )

    assert page_response.status_code == 200
    assert "按账户组与业务员查看违规商品" in page_response.get_data(as_text=True)
    assert '<body class="embedded">' in embedded_response.get_data(as_text=True)
    assert api_response.status_code == 200
    assert api_response.get_json()["data"] == dashboard
    assert received["detail_token_id"] == "7"
    assert received["view_mode"] == "history"
    assert received["category"] == "counterfeit"


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
    assert '<span class="nav-label">违规商品总览</span>' in source
    assert 'data-src="/infringement-dashboard?embedded=1"' in source
    assert 'id="infraction-dashboard-frame"' in source
    assert 'window.location.assign("/infringement-dashboard")' not in source


def test_dashboard_template_supports_store_detail_drilldown():
    source = (
        Path(bit_interface.resolve_template_dir()) / "infraction_dashboard.html"
    ).read_text(encoding="utf-8")

    assert "查看明细" in source
    assert "detail_token_id" in source
    assert "查看全部明细" in source
    assert "产品图" in source
    assert "thumbnail_url" in source
    assert "当前违规商品" in source
    assert "全部历史（去重）" in source
    assert "申诉成功" in source
    assert "违规类型" in source
    assert "category-select" in source
