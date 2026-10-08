import json

from scripts.repair_mercado_collection_profitability import stored_actual_weight


def test_legacy_snapshot_recovers_explicit_actual_weight_without_using_volumetric():
    assert stored_actual_weight({"plugin_snapshot_json": json.dumps({
        "ocr_text": "重量：280g丨$2.61低 尺寸：30×30×2（计抛300g）",
        "ocr_confidence": .9847,
    })}) == 280


def test_legacy_snapshot_does_not_substitute_volumetric_or_low_confidence_weight():
    assert stored_actual_weight({"plugin_snapshot_json": json.dumps({
        "ocr_text": "重量： 尺寸：30×30×2（计抛300g）",
        "ocr_confidence": .99,
    })}) is None
    assert stored_actual_weight({"plugin_snapshot_json": json.dumps({
        "ocr_text": "重量：280g", "ocr_confidence": .5,
    })}) is None
