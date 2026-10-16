"""Manual review can be changed and is confirmed before a transaction commits."""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from bit import product_access
from erp import mercadolibre_collection_store as store
from test_ai_original_net_persistence import ProductDB, existing_product


def completed_product(**changes):
    row = existing_product(status="completed", net=Decimal("5"), category_id="CBT123",
        main_image_url="/test-ai-white.jpg", package_length_cm=10, package_width_cm=10,
        package_height_cm=10, **changes)
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["original_1688"]["variations"] = []
    snapshot["ai_original"].update(category_id=row["category_id"], title_es="Caja", title_pt="Caixa", description_es="Descripcion",
        description_pt="Descricao", image_generation_method="local_background_removal",
        attributes=[{"id": "MATERIAL", "value_name": "Steel"}],
        validation={"errors": [], "warnings": []})
    row["source_snapshot_json"] = json.dumps(snapshot)
    return row


@pytest.fixture(autouse=True)
def no_schema_ddl(monkeypatch):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)


@pytest.mark.parametrize("previous", ["unreviewed", "failed", "suspected", "infringing", "risk", "rejected"])
def test_complete_listing_can_be_manually_approved_again(previous):
    db = ProductDB(completed_product(review_status=previous))
    prepared = store.update_ai_original_product(7, {
        "weight_g": 250, "net_proceeds_usd": 14.75,
    }, connection_factory=lambda: db)
    assert prepared["review_status"] == "unreviewed"
    saved = store.update_ai_original_product(
        7, {"review_status": "approved"}, connection_factory=lambda: db,
    )
    assert saved["net_proceeds_usd"] == 14.75
    assert saved["review_status"] == "approved"
    assert db.committed and not db.rolled_back
    assert "FOR UPDATE" in db.queries[0][0]


def test_manual_parameter_change_returns_approved_product_to_review():
    row = completed_product(review_status="approved")
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["marketplace_listings"] = {
        "amazon": {"status": "completed", "review_status": "approved"},
        "yandex": {"status": "completed", "review_status": "approved"},
    }
    row["source_snapshot_json"] = json.dumps(snapshot)
    db = ProductDB(row)
    saved = store.update_ai_original_product(
        7, {"weight_g": 300}, connection_factory=lambda: db,
    )
    assert saved["weight_g"] == 300
    assert saved["review_status"] == "unreviewed"
    assert {saved["marketplace_listings"][name]["review_status"] for name in ("amazon", "yandex")} == {"unreviewed"}
    assert db.committed and not db.rolled_back


def test_legacy_manual_only_failure_can_be_approved_after_fields_are_complete():
    row = completed_product(review_status="unreviewed")
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["ai_original"].update(status="failed", failed_step="manual_fields")
    row["source_snapshot_json"] = json.dumps(snapshot)
    db = ProductDB(row)

    saved = store.update_ai_original_product(
        7, {"review_status": "approved"}, connection_factory=lambda: db,
    )

    assert saved["review_status"] == "approved"
    assert db.committed and not db.rolled_back


def test_manual_review_rejects_product_outside_actor_scope():
    row = completed_product(
        review_status="unreviewed", organization_key="wuhan-zeshun",
        owner_user_id=99, owner_name="别人", visibility="enterprise",
    )
    db = ProductDB(row)
    actor = {
        "access_version": 1, "id": 7, "display_name": "当前用户",
        "role_key": "member", "organization_key": "wuhan-zeshun",
    }

    with product_access.scope(actor), pytest.raises(PermissionError, match="自己的产品"):
        store.update_ai_original_product(
            7, {"review_status": "approved"}, connection_factory=lambda: db,
        )

    assert not db.committed
    assert all(not sql.startswith("UPDATE") for sql, _ in db.queries)


@pytest.mark.parametrize("status", ["unreviewed", "failed", "suspected", "infringing", "risk"])
def test_nonapproval_reviews_do_not_require_successful_generation(status):
    db = ProductDB(existing_product(status="failed", review_status="approved"))
    saved = store.update_ai_original_product(7, {"review_status": status}, connection_factory=lambda: db)
    assert saved["review_status"] == status
    assert db.committed


@pytest.mark.parametrize("field,wrong,changes", [
    ("net_proceeds_usd", Decimal("99"), {"net_proceeds_usd": 14.75}),
    ("weight_g", Decimal("10"), {"weight_g": 300}),
    ("review_status", "unreviewed", {"review_status": "approved"}),
    ("category_id", "CBT999", {"category_id": "CBT456"}),
])
def test_wrong_database_readback_rolls_back_instead_of_reporting_success(field, wrong, changes):
    class StaleDB(ProductDB):
        def fetchone(self):
            result = deepcopy(self.row)
            if self.queries[-1][0].startswith("SELECT *"):
                result[field] = wrong
            return result
    db = StaleDB(completed_product())
    with pytest.raises(ValueError, match="回读"):
        store.update_ai_original_product(7, changes, connection_factory=lambda: db)
    assert not db.committed and db.rolled_back


def test_approval_still_requires_generation_without_changing_failed_task():
    db = ProductDB(existing_product(status="failed"))
    with pytest.raises(ValueError, match="原创任务完成"):
        store.update_ai_original_product(
            7, {"review_status": "approved"}, connection_factory=lambda: db,
        )
    assert db.rolled_back and not db.committed
    assert all(not sql.startswith("UPDATE") for sql, _ in db.queries)


@pytest.mark.parametrize("field", ["package_length_cm", "package_width_cm", "package_height_cm", "category_id", "main_image_url"])
def test_manual_approval_rejects_missing_required_listing_fact(field):
    row = completed_product()
    row[field] = None
    db = ProductDB(row)
    with pytest.raises(ValueError, match="需补齐所有必填项"):
        store.update_ai_original_product(7, {"review_status": "approved"}, connection_factory=lambda: db)
    assert not db.committed and db.rolled_back


@pytest.mark.parametrize("change", ["title_es", "description_pt", "attributes", "missing_required_attributes", "variations_ready", "required_ready", "validation", "validation_missing"])
def test_manual_approval_rejects_missing_generated_fact(change):
    row = completed_product()
    snapshot = json.loads(row["source_snapshot_json"])
    draft = snapshot["ai_original"]
    if change == "missing_required_attributes":
        draft[change] = [{"id": "MODEL"}]
    elif change in {"variations_ready", "required_ready"}:
        draft["preparation"] = {change: False}
    elif change == "validation":
        draft["validation"] = {"errors": ["标题语言需修正"], "warnings": []}
    elif change == "validation_missing":
        draft.pop("validation", None)
    else:
        draft[change] = [] if change == "attributes" else ""
    row["source_snapshot_json"] = json.dumps(snapshot)
    db = ProductDB(row)
    with pytest.raises(ValueError, match="需补齐所有必填项"):
        store.update_ai_original_product(7, {"review_status": "approved"}, connection_factory=lambda: db)
    assert not db.committed and db.rolled_back


def test_manual_approval_rejects_obviously_english_bilingual_titles():
    row = completed_product(review_status="unreviewed")
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["ai_original"].update(
        title_es="Beautiful Wooden Toy Train For Small Children",
        title_pt="Beautiful Wooden Toy Train For Small Children",
    )
    row["source_snapshot_json"] = json.dumps(snapshot)
    db = ProductDB(row)

    with pytest.raises(ValueError, match="疑似整段英文"):
        store.update_ai_original_product(
            7, {"review_status": "approved"}, connection_factory=lambda: db,
        )

    assert not db.committed and db.rolled_back


@pytest.mark.parametrize("draft_category", [None, "CBT999"])
def test_manual_approval_rejects_missing_or_stale_generated_category(draft_category):
    row = completed_product(review_status="unreviewed")
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["ai_original"]["category_id"] = draft_category
    row["source_snapshot_json"] = json.dumps(snapshot)
    db = ProductDB(row)

    with pytest.raises(ValueError, match="AI 生成资料不一致"):
        store.update_ai_original_product(
            7, {"review_status": "approved"}, connection_factory=lambda: db,
        )

    assert not db.committed and db.rolled_back


def test_approval_validates_incoming_snapshot_instead_of_stale_saved_copy():
    row = completed_product(review_status="unreviewed")
    incoming = json.loads(row["source_snapshot_json"])
    incoming["ai_original"]["title_es"] = ""
    db = ProductDB(row)

    with pytest.raises(ValueError, match="必须单独保存"):
        store.update_ai_original_product(
            7,
            {"source_snapshot_json": incoming, "review_status": "approved"},
            connection_factory=lambda: db,
        )

    assert not db.committed and db.rolled_back
    assert all(not sql.startswith("UPDATE") for sql, _ in db.queries)


def test_content_and_approval_must_be_saved_as_two_manual_actions():
    db = ProductDB(completed_product(review_status="unreviewed"))

    with pytest.raises(ValueError, match="必须单独保存"):
        store.update_ai_original_product(
            7, {"category_id": "CBT456", "review_status": "approved"},
            connection_factory=lambda: db,
        )

    assert not db.committed
    assert all(not sql.startswith("UPDATE") for sql, _ in db.queries)


def test_manual_category_change_invalidates_generated_category_work():
    db = ProductDB(completed_product(review_status="approved"))

    saved = store.update_ai_original_product(
        7, {"category_id": "CBT456"}, connection_factory=lambda: db,
    )

    assert saved["review_status"] == "unreviewed"
    assert saved["ai_original"]["status"] == "failed"
    assert saved["ai_original"]["failed_step"] == "category"
    assert saved["ai_original"]["preparation"]["required_ready"] is False
    with pytest.raises(ValueError, match="原创任务完成"):
        store.update_ai_original_product(
            7, {"review_status": "approved"}, connection_factory=lambda: db,
        )
