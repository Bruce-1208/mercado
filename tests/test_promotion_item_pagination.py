from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from bit import mercado_promotion_routes as routes
from erp.mercadolibre_promotion_store import PromotionStore


def campaign(store, *, token_id=1, promotion_id="P-1"):
    return store.upsert_promotion(
        token_id=token_id, store_name=f"店铺 {token_id}", application_id="app",
        seller_id=str(token_id), site_id="MLM",
        row={"id": promotion_id, "type": "DEAL", "name": "活动", "status": "started"},
    )


def finished_job(store, promotion_id, results, *, action="enroll"):
    preview_id = store.create_preview(
        actor="operator", action=action, promotion_fk=promotion_id,
        payload={"rows": []},
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    job_id = store.create_job(
        preview_id=preview_id, actor="operator", action=action, total=len(results),
    )
    store.finish_job(job_id, results=results)
    return job_id


def products(count):
    return [{"id": f"MLM{index:04}", "status": "candidate", "price": 20} for index in range(count)]


@pytest.fixture
def store(tmp_path):
    return PromotionStore(tmp_path / "promotions.sqlite3")


def test_page_fetch_is_bounded_before_product_decoding_and_outcome_lookup(store, monkeypatch):
    promotion_id = campaign(store)
    store.replace_items(promotion_id, products(125))
    finished_job(store, promotion_id, [
        {"item_id": row["id"], "status": "succeeded"} for row in products(125)
    ])
    other_id = campaign(store, token_id=2, promotion_id="P-2")
    store.replace_items(other_id, products(200))
    statements, decoded = [], []
    connect, decode = store._connect, store._row

    def traced_connection():
        db = connect()
        db.set_trace_callback(statements.append)
        return db

    def traced_decode(row):
        decoded.append(dict(row))
        return decode(row)

    def forbid_unbounded(*args, **kwargs):
        pytest.fail("Product pages must not fetch the entire collection or job history")

    monkeypatch.setattr(store, "_connect", traced_connection)
    monkeypatch.setattr(store, "_row", traced_decode)
    monkeypatch.setattr(store, "list_items", forbid_unbounded)
    monkeypatch.setattr(store, "_enrollment_results_by_promotion", forbid_unbounded)
    result = store.list_items_page(promotion_id, page=3, page_size=50)

    assert {key: result[key] for key in ("page", "page_size", "total", "pages")} == {
        "page": 3, "page_size": 50, "total": 125, "pages": 3,
    }
    assert [row["item_id"] for row in result["rows"]] == [f"MLM{index:04}" for index in range(100, 125)]
    assert len(decoded) == 25
    assert all(row["enrollment_result"]["status"] == "succeeded" for row in result["rows"])
    assert any("LIMIT 50 OFFSET 100" in sql for sql in statements)
    lookup = next(sql for sql in statements if "WITH ranked AS" in sql)
    assert "'MLM0100'" in lookup and "'MLM0124'" in lookup
    assert "'MLM0000'" not in lookup


@pytest.mark.parametrize("page_size", [20, 50, 100])
def test_page_sizes_cover_every_product_once_in_existing_order(store, page_size):
    promotion_id = campaign(store)
    store.replace_items(promotion_id, list(reversed(products(123))))
    paged = []
    first = store.list_items_page(promotion_id, page_size=page_size)
    for page in range(1, first["pages"] + 1):
        result = store.list_items_page(promotion_id, page=page, page_size=page_size)
        assert len(result["rows"]) <= page_size
        paged.extend(row["id"] for row in result["rows"])
    assert paged == [row["id"] for row in store.list_items(promotion_id)]
    assert len(set(paged)) == 123


def test_invalid_and_stale_pages_are_clamped_and_empty_activity_has_one_page(store):
    promotion_id = campaign(store)
    assert store.list_items_page(promotion_id, page=99) == {
        "rows": [], "page": 1, "page_size": 50, "total": 0, "pages": 1,
    }
    store.replace_items(promotion_id, products(125))
    result = store.list_items_page(promotion_id, page="bad", page_size="bad")
    assert result["page"] == 1 and result["page_size"] == 50
    result = store.list_items_page(promotion_id, page=999, page_size=10000)
    assert result["page"] == 2 and result["page_size"] == 100
    assert len(result["rows"]) == 25
    result = store.list_items_page(promotion_id, page=-10, page_size=-10)
    assert result["page"] == 1 and result["page_size"] == 1


def test_linked_product_page_prefers_first_candidate_without_changing_sort(store):
    promotion_id = campaign(store)
    rows = [{"id": f"MLM{index:04}", "status": "active", "offer_id": f"active-{index}"} for index in range(45)]
    rows += products(80)
    rows += [
        {"id": "MLM0065", "status": "active", "offer_id": "active-offer"},
        {"id": "MLM0065", "status": "candidate", "candidate_id": "second-candidate"},
    ]
    store.replace_items(promotion_id, rows)
    ordered = store.list_items(promotion_id)
    candidate = next(row for row in ordered if row["item_id"] == "MLM0065" and row["status_raw"] == "candidate")
    expected_page = next(index for index, row in enumerate(ordered) if row["id"] == candidate["id"]) // 20 + 1

    result = store.list_items_page(promotion_id, page=1, page_size=20, target_item_id="mlm0065")
    assert result["page"] == expected_page
    assert candidate["id"] in {row["id"] for row in result["rows"]}
    assert [row["id"] for row in result["rows"]] == [
        row["id"] for row in ordered[(expected_page - 1) * 20:expected_page * 20]
    ]
    fallback = store.list_items_page(promotion_id, page=6, page_size=20, target_item_id="MLM0000")
    assert fallback["page"] == 3  # Active rows precede candidate rows.
    missing = store.list_items_page(promotion_id, page=2, page_size=20, target_item_id="missing")
    assert missing["page"] == 2


def test_linked_product_without_candidate_uses_first_matching_row(store):
    promotion_id = campaign(store)
    rows = [{"id": f"MLM{index:04}", "status": "active"} for index in range(45)]
    rows.append({"id": "MLM0030", "status": "pending", "offer_id": "pending-offer"})
    store.replace_items(promotion_id, rows)
    result = store.list_items_page(promotion_id, page=3, page_size=20, target_item_id="MLM0030")
    assert result["page"] == 2
    assert any(row["item_id"] == "MLM0030" and row["status_raw"] == "active" for row in result["rows"])


def test_filtered_outcomes_and_sql_summary_keep_latest_enrollment_semantics(store, monkeypatch):
    promotion_id = campaign(store)
    first = finished_job(store, promotion_id, [
        {"item_id": "MLM1", "status": "succeeded"},
        {"item_id": "MLM2", "status": "unknown"},
        {"item_id": "MLM3", "status": "failed"},
    ])
    latest = finished_job(store, promotion_id, [
        {"item_id": "MLM1", "status": "failed"},
        {"item_id": "MLM1", "status": "succeeded", "message": "last result wins"},
        {"item_id": "MLM2", "status": "unexpected"},
    ])
    finished_job(store, promotion_id, [{"item_id": "MLM1", "status": "failed"}], action="withdraw")
    expected = store._enrollment_results_by_promotion([promotion_id])[promotion_id]

    def forbid_unbounded(*args, **kwargs):
        pytest.fail("Summary and filtered result lookup must stay in SQLite")

    monkeypatch.setattr(store, "_enrollment_results_by_promotion", forbid_unbounded)
    assert store.enrollment_results(promotion_id) == expected
    assert store.enrollment_results(promotion_id, item_ids=[]) == []
    filtered = store.enrollment_results(promotion_id, item_ids=["MLM1", "MLM3"])
    assert {row["item_id"]: row["job_id"] for row in filtered} == {"MLM1": latest, "MLM3": first}
    assert store.get_promotion(promotion_id)["enrollment_summary"] == {
        "succeeded": 1, "failed": 1, "unknown": 1, "total": 3,
    }
    assert len(store.enrollment_results(promotion_id, limit=1)) == 1


def test_bounded_latest_job_keeps_full_counters_before_result_decoding(store, monkeypatch):
    promotion_id = campaign(store)
    results = [{"item_id": f"MLM{index:04}", "status": ("succeeded", "failed", "unknown")[index % 3]}
               for index in range(150)]
    job_id = finished_job(store, promotion_id, results)
    decode = store._row
    decoded_result_sizes = []

    def verify_bounded(row):
        parsed = decode(row)
        if "results" in parsed:
            decoded_result_sizes.append(len(parsed["results"]))
        return parsed

    monkeypatch.setattr(store, "_row", verify_bounded)
    bounded = store.latest_job(promotion_id, result_limit=50)
    assert bounded["job_id"] == job_id
    assert bounded["results"] == results[:50]
    assert bounded["results_total"] == bounded["total_count"] == 150
    assert bounded["results_truncated"] is True
    assert bounded["success_count"] == bounded["failed_count"] == bounded["unknown_count"] == 50
    assert decoded_result_sizes == [50]
    assert len(store.latest_job(promotion_id)["results"]) == 150


def route_client(store, monkeypatch, enrich):
    monkeypatch.setattr(routes, "PromotionStore", lambda: store)
    monkeypatch.setattr(routes, "enrich_item_products", enrich)
    app = Flask(__name__)
    app.register_blueprint(routes.create_promotions_blueprint(
        login_required=lambda fn: fn,
        current_user=lambda: {"username": "operator", "organization_key": "enterprise-a"},
        authorized_token_ids=lambda _: [1],
    ))
    return app.test_client()


def test_route_defaults_and_page_changes_only_enrich_visible_rows(store, monkeypatch):
    promotion_id = campaign(store)
    store.replace_items(promotion_id, products(125))
    finished_job(store, promotion_id, [{"item_id": row["id"], "status": "succeeded"} for row in products(125)])
    enriched = []

    def enrich(promotion, rows):
        assert promotion["token_id"] == 1 and promotion["site_id"] == "MLM"
        enriched.append([row["item_id"] for row in rows])
        return rows

    client = route_client(store, monkeypatch, enrich)
    default = client.get(f"/api/promotions/{promotion_id}/items").get_json()["data"]
    assert default["page"] == 1 and default["page_size"] == 50
    assert len(default["rows"]) == len(default["enrollment_results"]) == 50
    assert default["enrollment_results_total"] == 125 and default["enrollment_results_truncated"] is True
    assert default["latest_job"]["total_count"] == 125
    assert len(default["latest_job"]["results"]) == 50
    assert default["latest_job"]["results_truncated"] is True
    target = client.get(f"/api/promotions/{promotion_id}/items?page=1&page_size=20&target_item_id=MLM0105").get_json()["data"]
    assert target["page"] == 6
    assert any(row["item_id"] == "MLM0105" for row in target["rows"])
    assert [len(rows) for rows in enriched] == [50, 20]
    invalid = client.get(f"/api/promotions/{promotion_id}/items?page=bad&page_size=bad")
    assert invalid.status_code == 200
    assert invalid.get_json()["data"]["page_size"] == 50


def test_route_enforces_store_access_before_loading_or_enriching_products(store, monkeypatch):
    forbidden_id = campaign(store, token_id=2, promotion_id="P-2")

    def forbid_loading(*args, **kwargs):
        pytest.fail("Unauthorized product pages must not be loaded")

    client = route_client(store, monkeypatch, forbid_loading)
    monkeypatch.setattr(store, "list_items_page", forbid_loading)
    assert client.get(f"/api/promotions/{forbidden_id}/items?page=2").status_code == 403
    assert client.get("/api/promotions/99999/items").status_code == 404
