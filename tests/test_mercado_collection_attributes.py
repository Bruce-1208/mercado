import copy

import pytest

from erp.mercadolibre_collection_attributes import check_collection_rows, check_required_attributes


SCHEMA = [
    {"id": "BRAND", "name": "Marca", "tags": {"required": True}},
    {"id": "MODEL", "name": "Modelo", "tags": {"required": True}},
    {"id": "COLOR", "name": "Color", "tags": {"required": True}},
    {"id": "SIZE", "name": "Talla", "tags": {}},
]


def product(**changes):
    return {"id": 91, "category_id": "MLM123", "source": {
        "category_id": "MLM123", "condition": "new",
        "attributes": [{"id": "BRAND", "value_name": "Acme"},
                       {"name": "型号", "value_name": "A1"}],
    }, **changes}


def test_detects_required_field_that_was_never_collected_without_changing_source():
    row = product()
    before = copy.deepcopy(row)
    result = check_required_attributes(row, SCHEMA)
    assert result["status"] == "missing"
    assert result["missing"] == [{"attribute_id": "COLOR", "name": "颜色", "scope": "商品", "sku": ""}]
    assert row == before


def test_each_sku_must_supply_its_own_attributes_and_blank_overrides_parent():
    row = product()
    row["source"]["attributes"].append({"id": "COLOR", "value_name": "Blue"})
    row["source"]["variations"] = [
        {"seller_sku": "blue", "attribute_combinations": [{"id": "COLOR", "value_name": "Blue"}]},
        {"seller_sku": "unknown", "attribute_combinations": [{"id": "COLOR", "value_name": " "}]},
    ]
    result = check_required_attributes(row, SCHEMA)
    assert [(r["scope"], r["sku"], r["attribute_id"]) for r in result["missing"]] == [("规格 2", "unknown", "COLOR")]


@pytest.mark.parametrize("value", ["", " ", None, {"name": "a label"}, {"value_id": "-1"}, {"values": []}])
def test_labels_and_empty_values_do_not_count_as_facts(value):
    row = product(plugin_snapshot={"attributes": {"颜色": value}})
    assert check_required_attributes(row, SCHEMA)["status"] == "missing"


@pytest.mark.parametrize("value", [False, 0, {"value_id": "123"}, {"values": [{"id": "123"}]},
                                    {"value_struct": {"number": 0, "unit": "cm"}}])
def test_alternate_saved_evidence_and_false_zero_values_are_preserved(value):
    row = product(page_snapshot={"specs": {"颜色": value}})
    assert check_required_attributes(row, SCHEMA)["status"] == "complete"


def test_unavailable_rules_are_not_reported_as_complete_but_valid_empty_schema_is():
    assert check_required_attributes(product(), None)["status"] == "unavailable"
    assert check_required_attributes(product(), {"error": "forbidden"})["status"] == "unavailable"
    assert check_required_attributes(product(), []) ["status"] == "complete"
    assert check_required_attributes(product(category_id="../../items"), []) ["status"] == "unavailable"
    assert check_required_attributes({"id": 1}, []) ["status"] == "unavailable"


def test_new_catalog_and_conditional_requirements_are_distinct():
    schema = [
        {"id": "NEW", "tags": {"new_required": True}},
        {"id": "CATALOG", "tags": {"catalog_required": True}},
        {"id": "CONDITIONAL", "tags": {"conditional_required": True}},
        {"id": "READ_ONLY", "tags": {"required": True, "read_only": True}},
    ]
    row = product()
    result = check_required_attributes(row, schema)
    assert [r["attribute_id"] for r in result["missing"]] == ["NEW"]
    assert [r["attribute_id"] for r in result["pending"]] == ["CONDITIONAL"]
    row["source"].update(condition="used", catalog_listing=True)
    assert [r["attribute_id"] for r in check_required_attributes(row, schema)["missing"]] == ["CATALOG"]
    row["source"]["catalog_listing"] = False
    assert check_required_attributes(row, schema)["status"] == "pending"


def test_gtin_exemption_and_incomplete_variants_cannot_claim_completeness():
    row = product()
    row["source"]["attributes"].append({"id": "EMPTY_GTIN_REASON", "value_name": "Handmade"})
    assert check_required_attributes(row, [{"id": "GTIN", "tags": {"required": True}}])["status"] == "pending"
    row["source"]["variation_collection_status"] = "incomplete"
    assert check_required_attributes(row, [])["status"] == "pending"


def test_same_category_is_read_once_and_failures_do_not_hide_other_categories(monkeypatch):
    from erp import mercadolibre_follow_sell as publisher
    calls = []
    def schema(client, category):
        calls.append(category)
        if category == "MLM999":
            raise RuntimeError("private upstream response")
        return SCHEMA
    monkeypatch.setattr(publisher, "_category_attribute_schema", schema)
    results = check_collection_rows([product(), product(id=92), product(id=93, category_id="MLM999")], lambda category: object())
    assert calls == ["MLM123", "MLM999"]
    assert [r["status"] for r in results] == ["missing", "missing", "unavailable"]
    assert "private upstream" not in str(results)


def test_editing_saved_facts_recalculates_missing_fields():
    row = product()
    assert check_required_attributes(row, SCHEMA)["status"] == "missing"
    row["source"]["attributes"].append({"id": "COLOR", "value_name": "Blue"})
    assert check_required_attributes(row, SCHEMA)["status"] == "complete"


@pytest.mark.parametrize('prefix', ['PACKAGE_', 'SELLER_PACKAGE_'])
def test_saved_actual_package_measurements_satisfy_package_attributes(prefix):
    schema = [{"id": key, "tags": {"required": True}} for key in
              (prefix + suffix for suffix in ('WEIGHT', 'LENGTH', 'WIDTH', 'HEIGHT'))]
    row = product(weight_g=120, weight_basis="plugin_actual", package_length_cm=10,
                  package_width_cm=8, package_height_cm=6)
    assert check_required_attributes(row, schema)["status"] == "complete"
    row.update(weight_basis="calculated_volumetric", package_width_cm=0)
    assert {entry["attribute_id"] for entry in check_required_attributes(row, schema)["missing"]} == {
        prefix + 'WEIGHT', prefix + 'WIDTH',
    }


@pytest.mark.parametrize('prefix,target', [('PACKAGE_', 'SELLER_PACKAGE_'), ('SELLER_PACKAGE_', 'PACKAGE_')])
def test_snapshot_package_aliases_and_blank_variant_override(prefix, target):
    row = product()
    row['source']['attributes'] = [{'id': prefix + 'HEIGHT', 'value_name': '6 cm'}]
    schema = [{'id': target + 'HEIGHT', 'tags': {'required': True}}]
    before = copy.deepcopy(row)
    assert check_required_attributes(row, schema)['status'] == 'complete'
    assert row == before
    row['source']['variations'] = [{'attributes': [{'id': prefix + 'HEIGHT', 'value_name': ''}]}]
    assert check_required_attributes(row, schema)['missing'][0]['attribute_id'] == target + 'HEIGHT'
    row['source']['attributes'] = [{'id': 'HEIGHT', 'value_name': '6 cm'}]
    row['source']['variations'] = []
    assert check_required_attributes(row, schema)['status'] == 'missing'


@pytest.mark.parametrize('container', ['source', 'page_snapshot', 'plugin_snapshot'])
@pytest.mark.parametrize('field', ['gtin', 'ean', 'upc'])
def test_existing_identifier_is_reused_without_generating_or_mutating(container, field):
    row = product()
    row.setdefault(container, {})[field] = '4006381333931'
    before = copy.deepcopy(row)
    schema = [{'id': 'GTIN', 'tags': {'conditional_required': True}}]
    assert check_required_attributes(row, schema)['status'] == 'complete'
    assert row == before
    del row[container][field]
    assert check_required_attributes(row, schema)['status'] == 'pending'
