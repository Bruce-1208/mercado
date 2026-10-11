import pytest

from erp.mercadolibre_collection_quality import collection_quality_issues
from erp.mercadolibre_collection_store import _json_safe_row
from bit.mercado_collection_detail import build_collection_detail


def complete_row():
    return {"id": 91, "task_id": 1, "weight_g": 120, "weight_basis": "plugin_actual",
            "package_length_cm": 10, "package_width_cm": 8, "package_height_cm": 6,
            "sale_price_usd": 20, "shipping_fee_usd": 0,
            "source_json": '{"attributes":[{"id":"MATERIAL","value_name":"Cotton"}]}'}


def test_valid_measurements_attributes_and_zero_shipping_have_no_warning():
    row = complete_row()
    assert collection_quality_issues(row) == []
    assert _json_safe_row(row)["collection_quality_issues"] == []
    assert build_collection_detail(row)["quality_issues"] == []


@pytest.mark.parametrize("value", [None, "", " ", 0, -1, "NaN", "Infinity", "broken"])
def test_invalid_weight_and_partial_dimensions_are_named(value):
    row = {**complete_row(), "weight_g": value, "package_width_cm": value}
    issues = collection_quality_issues(row)
    assert {issue["field"] for issue in issues} == {"weight", "dimensions"}
    assert next(issue["message"] for issue in issues if issue["field"] == "dimensions") == "包装尺寸不全：缺少宽"


def test_volumetric_fallback_does_not_count_as_actual_weight():
    assert collection_quality_issues({**complete_row(), "weight_basis": "calculated_volumetric"}) == [
        {"field": "weight", "message": "缺少有效实际重量"}]


def test_missing_net_names_calculation_inputs_and_preserves_negative_results():
    row = {**complete_row(), "sale_price_usd": None, "shipping_fee_usd": ""}
    assert collection_quality_issues(row) == [{"field": "net", "message": "净收益未计算：待补美元售价、当前运费"}]
    assert collection_quality_issues({**complete_row(), "shipping_fee_usd": 30}) == []


def test_partial_and_variant_attributes_are_reported_and_alternate_evidence_is_used():
    row = {**complete_row(), "source_json": {
        "attributes": [{"id": "COLOR", "name": "颜色", "value_name": ""}],
        "variations": [{"attribute_combinations": [{"id": "SIZE", "value_name": " "}]}],
    }}
    issues = collection_quality_issues(row)
    assert len(issues) == 2
    assert "颜色未填写" in issues[0]["message"]
    assert "规格 1" in issues[1]["message"]
    row["plugin_snapshot_json"] = {"attributes": {"颜色": "Blue"}}
    assert len(collection_quality_issues(row)) == 1


def test_malformed_snapshot_does_not_hide_missing_attributes():
    row = {**complete_row(), "source_json": "bad json"}
    assert collection_quality_issues(row) == [{"field": "attributes", "message": "属性不全：未采集到商品属性"}]


def test_list_and_detail_report_the_same_missing_fields():
    row = {"id": 1, "task_id": 1}
    assert _json_safe_row(row)["collection_quality_issues"] == build_collection_detail(row)["quality_issues"]
