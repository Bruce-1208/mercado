"""Manual review can be changed and is confirmed before a transaction commits."""
from copy import deepcopy
from decimal import Decimal

import pytest

from erp import mercadolibre_collection_store as store
from test_ai_original_net_persistence import ProductDB, existing_product


@pytest.fixture(autouse=True)
def no_schema_ddl(monkeypatch):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)


@pytest.mark.parametrize("previous", ["unreviewed", "failed", "suspected", "infringing", "risk", "rejected"])
def test_review_can_be_approved_again_with_saved_net_and_missing_dimensions(previous):
    db = ProductDB(existing_product(status="completed", net=Decimal("5"), review_status=previous))
    saved = store.update_ai_original_product(7, {
        "weight_g": 250, "net_proceeds_usd": 14.75, "review_status": "approved",
    }, connection_factory=lambda: db)
    assert saved["net_proceeds_usd"] == 14.75
    assert saved["review_status"] == "approved"
    assert db.committed and not db.rolled_back
    assert "FOR UPDATE" in db.queries[0][0]


@pytest.mark.parametrize("status", ["unreviewed", "failed", "suspected", "infringing", "risk"])
def test_nonapproval_reviews_do_not_require_successful_generation(status):
    db = ProductDB(existing_product(status="failed", review_status="approved"))
    saved = store.update_ai_original_product(7, {"review_status": status}, connection_factory=lambda: db)
    assert saved["review_status"] == status
    assert db.committed


@pytest.mark.parametrize("field,wrong", [("net_proceeds_usd", Decimal("99")),
    ("weight_g", Decimal("10")), ("review_status", "unreviewed"), ("category_id", "CBT999")])
def test_wrong_database_readback_rolls_back_instead_of_reporting_success(field, wrong):
    class StaleDB(ProductDB):
        def fetchone(self):
            result = deepcopy(self.row)
            if self.queries[-1][0].startswith("SELECT *"):
                result[field] = wrong
            return result
    db = StaleDB(existing_product(status="completed", net=Decimal("5")))
    with pytest.raises(ValueError, match="回读"):
        store.update_ai_original_product(7, {
            "weight_g": 250, "net_proceeds_usd": 14.75,
            "review_status": "approved", "category_id": "CBT123",
        }, connection_factory=lambda: db)
    assert not db.committed and db.rolled_back


def test_approval_still_requires_generation_without_changing_failed_task():
    db = ProductDB(existing_product(status="failed"))
    with pytest.raises(ValueError, match="原创任务完成"):
        store.update_ai_original_product(7, {
            "weight_g": 250, "net_proceeds_usd": 14.75, "review_status": "approved",
        }, connection_factory=lambda: db)
    assert db.rolled_back and not db.committed
    assert all(not sql.startswith("UPDATE") for sql, _ in db.queries)
