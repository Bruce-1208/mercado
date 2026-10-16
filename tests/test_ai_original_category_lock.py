"""Manual Mercado category selection and checkpoint editing without a live DB."""
from copy import deepcopy
import json

import pytest

from erp import mercadolibre_collection_store as store


class SnapshotDB:
    def __init__(self, snapshot, category_id="CBT1", category_name="Old category"):
        self.row = {"id": 1, "source_type": "ai_original", "title": "Existing title",
                    "description_text": "Existing description", "category_id": category_id,
                    "category_name": category_name, "source_snapshot_json": json.dumps(snapshot)}
        self.rowcount = 1
        self.committed = False
        self.rolled_back = False

    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, query, params=None):
        if query.startswith("UPDATE"):
            fields = query.split(" SET ")[1].split(" WHERE ")[0].split(", ")
            for index, field in enumerate(fields):
                self.row[field.split("`")[1]] = params[index]
    def fetchone(self): return dict(self.row)
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True
    def close(self): pass


@pytest.fixture
def edit(monkeypatch):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    def save(snapshot, payload, **row_fields):
        db = SnapshotDB(deepcopy(snapshot), **row_fields)
        row = store.update_ai_original_listing(1, payload, connection_factory=lambda: db)
        assert db.committed
        return json.loads(db.row["source_snapshot_json"]), row
    return save


def test_manual_category_lock_syncs_draft_source_and_row_and_keeps_other_platform(edit):
    snapshot = {"original_1688": {"title": "Source", "category_id": "MLM10"},
                "source": {"category_id": "CBT1"}, "ai_original": {"status": "failed"},
                "marketplace_listings": {"amazon": {"title": "Amazon draft"}}}
    saved, row = edit(snapshot, {"category_id": " cbt123 ", "category_name": "Costumes", "category_locked": True})
    for key in ("ai_original", "source", "original_1688"):
        assert saved[key]["category_id"] == "CBT123"
        assert saved[key]["category_name"] == "Costumes"
    assert saved["ai_original"]["category_locked"] is True
    assert saved["marketplace_listings"]["mercadolibre"] == saved["ai_original"]
    assert saved["marketplace_listings"]["amazon"] == snapshot["marketplace_listings"]["amazon"]
    assert row["category_id"] == "CBT123"
    assert row["category_name"] == "Costumes"
    assert saved["ai_original"]["status"] == "failed"


def test_legacy_category_change_creates_invalidated_preparation_checkpoint(edit):
    snapshot = {
        "ai_original": {
            "status": "completed", "category_id": "CBT1",
            "attributes": [{"id": "MATERIAL", "value_name": "Steel"}],
        },
        "source": {"category_id": "CBT1"},
    }

    saved, _ = edit(snapshot, {
        "category_id": "CBT123", "category_name": "Storage",
        "category_locked": True,
    })

    assert saved["ai_original"]["preparation"] == {
        "category_ready": False,
        "variations_ready": False,
        "required_ready": False,
    }


def test_copy_only_edit_preserves_locked_category_and_checkpoint(edit):
    checkpoint = {"image_ready": True, "copy_ready": True, "category_ready": True,
                  "variations_ready": True, "required_ready": False}
    snapshot = {"ai_original": {"category_locked": True, "category_id": "CBT1",
                                 "category_name": "Old category", "preparation": checkpoint},
                "source": {"category_id": "CBT1", "category_name": "Old category"}}
    saved, row = edit(snapshot, {"title_es": "Nuevo título"})
    assert row["category_id"] == "CBT1"
    assert row["category_name"] == "Old category"
    assert saved["ai_original"]["category_locked"] is True
    assert saved["ai_original"]["preparation"] == checkpoint


def test_imported_category_is_not_implicitly_locked(edit):
    saved, row = edit({"original_1688": {"category_id": "CBT1"}}, {"title_es": "Título"})
    assert saved["ai_original"]["category_locked"] is False
    assert row["category_id"] == "CBT1"


def test_clearing_category_unlocks_and_clears_names(edit):
    snapshot = {"ai_original": {"category_locked": True, "category_id": "CBT1", "category_name": "Old"},
                "source": {"category_id": "CBT1", "category_name": "Old"},
                "original_1688": {"category_id": "CBT1", "category_name": "Old"}}
    saved, row = edit(snapshot, {"category_id": "", "category_locked": True})
    assert saved["ai_original"]["category_locked"] is False
    assert row["category_id"] == ""
    assert row["category_name"] == ""
    for key in ("ai_original", "source", "original_1688"):
        assert saved[key]["category_name"] == ""


@pytest.mark.parametrize("payload", [
    {"category_id": "MLM123", "category_locked": True},
    {"category_id": "CBTbad", "category_locked": True},
    {"category_id": "CBT123", "category_locked": "true"},
    {"category_id": "CBT123", "category_locked": 1},
])
def test_invalid_category_lock_rolls_back(monkeypatch, payload):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    db = SnapshotDB({})
    with pytest.raises(ValueError):
        store.update_ai_original_listing(1, payload, connection_factory=lambda: db)
    assert db.rolled_back
    assert not db.committed


def test_manual_source_sku_override_keeps_original_and_unrelated_checkpoint(edit):
    original_variations = [{"seller_sku": "sku1", "price": 19, "available_quantity": 5,
                            "attribute_combinations": [{"name": "←注", "value_name": "皮料"}]}]
    edited_variations = [{**original_variations[0], "attribute_combinations": [{"name": "Material", "value_name": "皮料"}]}]
    checkpoint = {"image_ready": True, "copy_ready": True, "category_ready": True,
                  "variations_ready": True, "required_ready": True}
    snapshot = {"original_1688": {"variations": original_variations},
                "ai_original": {"variations": original_variations, "preparation": checkpoint},
                "marketplace_listings": {"yandex": {"title": "Other draft"}}}
    saved, _ = edit(snapshot, {"variations": edited_variations})
    assert saved["original_1688"]["variations"] == original_variations
    assert saved["ai_original"]["source_variations"] == edited_variations
    assert saved["source"]["variations"] == edited_variations
    assert saved["ai_original"]["preparation"] == {**checkpoint, "variations_ready": False, "required_ready": False}
    assert saved["marketplace_listings"]["yandex"]["title"] == "Other draft"
    assert saved["marketplace_listings"]["yandex"]["review_status"] == "unreviewed"


def test_unchanged_editor_arrays_and_category_preserve_checkpoint(edit):
    checkpoint = {"image_ready": True, "copy_ready": True, "category_ready": True,
                  "variations_ready": True, "required_ready": True}
    variation = {"seller_sku": "sku1", "attribute_combinations": [{"id": "SIZE", "value_name": "S"}]}
    snapshot = {"ai_original": {"category_id": "CBT1", "category_locked": True,
                                "attributes": [], "variations": [variation], "variation_dimensions": [],
                                "preparation": checkpoint}}
    saved, _ = edit(snapshot, {"category_id": "CBT1", "category_locked": True,
                              "attributes": [], "variations": [variation], "variation_dimensions": []})
    assert saved["ai_original"]["preparation"] == checkpoint
    assert "source_variations" not in saved["ai_original"]


def test_manual_attribute_removals_persist_until_explicitly_readded(edit):
    attrs = [{"id": "COLOR", "value_name": "Black"}, {"id": "MODEL", "value_name": "Costume"}]
    snapshot = {"ai_original": {"attributes": attrs}}
    saved, _ = edit(snapshot, {"attributes": attrs[1:]})
    assert saved["ai_original"]["manual_attribute_removed_ids"] == ["COLOR"]
    saved, _ = edit(saved, {"title_es": "Edited copy"})
    assert saved["ai_original"]["manual_attribute_removed_ids"] == ["COLOR"]
    saved, _ = edit(saved, {"attributes": attrs})
    assert saved["ai_original"]["manual_attribute_removed_ids"] == []


def test_each_manual_save_advances_revision_even_when_fields_unchanged(edit):
    saved, _ = edit({"ai_original": {"title_es": "Título"}}, {"title_es": "Título"})
    assert saved["ai_original"]["edit_revision"] == 1
    saved, _ = edit(saved, {"title_es": "Título"})
    assert saved["ai_original"]["edit_revision"] == 2
    assert saved["marketplace_listings"]["mercadolibre"]["edit_revision"] == 2


def test_stale_worker_cannot_overwrite_latest_manual_listing(monkeypatch):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    snapshot = {"ai_original": {"edit_revision": 2, "category_id": "CBT123", "category_locked": True,
                                 "source_variations": [{"seller_sku": "manual-sku"}]},
                "marketplace_listings": {"yandex": {"title": "Current platform draft"}}}
    db = SnapshotDB(snapshot)
    before = deepcopy(db.row)
    with pytest.raises(ValueError, match="刊登资料已被人工修改"):
        store.update_ai_original_product(1, {
            "expected_ai_original_revision": 1,
            "main_image_url": "/stale-worker-image.jpg",
            "source_snapshot_json": {"ai_original": {"edit_revision": 1, "status": "completed"}},
        }, connection_factory=lambda: db)
    assert db.rolled_back
    assert not db.committed
    assert db.row == before


def test_current_worker_revision_is_preserved_and_other_platform_stays_latest(monkeypatch):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    snapshot = {"ai_original": {"edit_revision": 2},
                "marketplace_listings": {"amazon": {"title": "Latest edit"}}}
    db = SnapshotDB(snapshot)
    store.update_ai_original_product(1, {
        "expected_ai_original_revision": 2,
        "source_snapshot_json": {"ai_original": {"status": "completed", "title_es": "Nuevo"},
                                  "marketplace_listings": {"amazon": {"title": "Stale edit"}}},
    }, connection_factory=lambda: db)
    saved = json.loads(db.row["source_snapshot_json"])
    assert db.committed
    assert "expected_ai_original_revision" not in db.row
    assert saved["ai_original"]["edit_revision"] == 2
    assert saved["ai_original"]["title_es"] == "Nuevo"
    assert saved["marketplace_listings"]["mercadolibre"] == saved["ai_original"]
    assert saved["marketplace_listings"]["amazon"]["title"] == "Latest edit"


def test_legacy_worker_revision_defaults_to_zero(monkeypatch):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    db = SnapshotDB({})
    store.update_ai_original_product(1, {"expected_ai_original_revision": 0,
        "source_snapshot_json": {"ai_original": {"status": "processing"}}}, connection_factory=lambda: db)
    assert json.loads(db.row["source_snapshot_json"])["ai_original"]["edit_revision"] == 0


def test_translation_advances_manual_revision(monkeypatch):
    from erp import mercadolibre_translation
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    monkeypatch.setattr(mercadolibre_translation, "translate_texts", lambda *args: ["Organizador"])
    db = SnapshotDB({"original_1688": {"title": "收纳盒"}, "ai_original": {"edit_revision": 3}})
    store.translate_ai_original_listing(1, "es", connection_factory=lambda: db)
    saved = json.loads(db.row["source_snapshot_json"])
    assert saved["ai_original"]["edit_revision"] == 4
    assert saved["marketplace_listings"]["mercadolibre"]["edit_revision"] == 4


def test_manual_bilingual_edits_update_cached_copy_for_resuming_later_steps(edit):
    copy = {"product_type_en": "string lights", "title_es": "Old ES", "title_pt": "Old PT",
            "description_es": "Old ES description", "description_pt": "Old PT description", "attributes": []}
    snapshot = {"ai_original": {**copy, "generated_copy": copy,
                                "preparation": {"image_ready": True, "copy_ready": True}}}
    updates = {"title_es": "Luces LED", "title_pt": "Luzes LED",
               "description_es": "Nueva descripción", "description_pt": "Nova descrição"}
    saved, _ = edit(snapshot, updates)
    cached = saved["ai_original"]["generated_copy"]
    for field, value in updates.items():
        assert cached[field] == value
    assert cached["title"] == updates["title_es"]
    assert cached["description"] == updates["description_es"]
    assert cached["product_type_en"] == "string lights"
    assert saved["ai_original"]["preparation"] == snapshot["ai_original"]["preparation"]


def test_changed_manual_attributes_survive_category_rebuild_but_roundtrip_does_not_add_override(edit):
    attribute = {"id": "MATERIAL", "value_name": "Leather"}
    snapshot = {"ai_original": {"attributes": [attribute], "preparation": {"required_ready": True}}}
    unchanged, _ = edit(snapshot, {"attributes": [attribute]})
    assert "manual_attributes" not in unchanged["ai_original"]
    assert unchanged["ai_original"]["preparation"]["required_ready"] is True
    update = {"id": "MATERIAL", "value_name": "Synthetic leather"}
    changed, _ = edit(snapshot, {"attributes": [update], "category_locked": True, "category_id": "CBT123"})
    assert changed["ai_original"]["manual_attributes"] == [update]
    assert changed["ai_original"]["preparation"]["required_ready"] is False


def test_translation_syncs_cached_bilingual_copy(monkeypatch):
    from erp import mercadolibre_translation
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    monkeypatch.setattr(mercadolibre_translation, "translate_texts", lambda *args: ["Organizador"])
    db = SnapshotDB({"original_1688": {"title": "收纳盒"},
                     "ai_original": {"generated_copy": {"title_es": "Old ES", "title_pt": "Keep PT"}}})
    store.translate_ai_original_listing(1, "es", connection_factory=lambda: db)
    cached = json.loads(db.row["source_snapshot_json"])["ai_original"]["generated_copy"]
    assert cached["title_es"] == "Organizador"
    assert cached["title"] == "Organizador"
    assert cached["title_pt"] == "Keep PT"
