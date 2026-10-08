"""1688 dimension evidence and writeback regressions, without live services."""
import pytest

from bit.ai_weight_price_client import detail, start
from erp.collection_ai_check import measurements
from erp.ai_weight_price.models import parse_dimensions_evidence, supplier_dimensions_evidence
from erp.ai_weight_price.service import Service
from test_ai_weight_price_client import _service
from test_ai_weight_price_image_first import ImageModel, make_service


@pytest.mark.parametrize("claim, expected", [
    ("包装尺寸：300×200×100毫米", "30x20x10"),
    ("包装尺寸（厘米）：30＊20＊10", "30x20x10"),
    ("包装尺寸(cm): 30 x 20 x 10", "30x20x10"),
    ("30厘米×20厘米×10厘米", "30x20x10"),
    ("30cmx20cmx10cm", "30x20x10"),
    ("蓝色 / 30x20x10cm / 2只", "30x20x10"),
    ("0.3m×200mm×10cm", "30x20x10"),
    ("长30厘米，宽20厘米，高10厘米", "30x20x10"),
    ("包装长：30cm，包装宽：20cm，包装高：10cm", "30x20x10"),
    ("长度(mm):300 宽度(mm):200 高度(mm):100", "30x20x10"),
    ("长30，宽20，高10cm", "30x20x10"),
    ("30x20x10cm（300×200×100mm）", "30x20x10"),
    ("30×20×10", None),
    ("30-40x20x10cm", None),
    ("30 - 40x20x10cm", None),
    ("30–40x20x10cm", None),
    ("30到40x20x10cm", None),
    ("30/40x20x10cm", None),
    ("30x20x10cm/15cm", None),
    ("-30x20x10cm", None),
    ("10x30x20x10cm", None),
    ("30x20x10-15cm", None),
    ("30x20x10cm+5cm", None),
    ("30x20x0cm", None),
    ("30x20x10cm；40x20x10cm", None),
    ("长30cm，宽20cm", None),
])
def test_explicit_dimensions_units_and_ambiguity(claim, expected):
    assert parse_dimensions_evidence(claim) == expected


COMMON = {"package_length_cm": 30, "package_width_cm": 20, "package_height_cm": 10}


@pytest.mark.parametrize("sku, expected", [
    ({}, "30x20x10"),
    ({"label": "蓝色"}, "30x20x10"),
    ({"label": "蓝色 100×80×40毫米"}, "10x8x4"),
    ({"package_length_cm": 10, "package_width_cm": 8, "package_height_cm": 4}, "10x8x4"),
    ({"package_length_cm": 10}, None),
    ({"raw_dimensions": "包装尺寸未提供"}, None),
    ({"dimension_evidence_present": True}, None),
    ({"label": "长30cm，宽20cm"}, None),
])
def test_sku_size_precedence_never_mixes_axes(sku, expected):
    assert supplier_dimensions_evidence(sku, COMMON) == expected
    result = measurements({"weight_g": 450, **COMMON}, sku)
    assert result["weight_g"] == 450
    actual = [result.get(field) for field in COMMON]
    assert actual == ([float(side) for side in expected.split("x")] if expected else [None] * 3)


def test_legacy_page_evidence_includes_structured_package_size():
    text = Service.page_text({"selected_sku": {"label": "蓝色", **COMMON}})
    assert "包装尺寸：30x20x10 cm" in text


@pytest.mark.parametrize("sku_size, expected", [(COMMON, "30x20x10"), ({"package_length_cm": 10}, "12x12x12")])
def test_agent_highest_price_structured_size_is_saved_or_original_is_retained(tmp_path, monkeypatch, sku_size, expected):
    service, browser, config = make_service(tmp_path, monkeypatch)
    service.store.add({"erp_goods_id": "1", "title": "测试商品", "main_image_url": "https://img.example/1.jpg"})
    browser.records["1"] = {"weight_g": "430", "net_income_usd": "9.5", "review_status": "待审核",
                            "dimensions_cm": "12x12x12"}
    browser.read_offer = lambda _task, candidate: {**candidate, **COMMON, "skus": [
        {"id": "cheap", "label": "小号", "price": "10", "raw_dimensions": "1x2x3cm"},
        {"id": "highest", "label": "大号", "price": "22", "raw_weight": "450g", **sku_size},
    ]}
    service.process(service.store.get("1"), browser, ImageModel(), config)
    saved = service.store.get("1")
    assert saved["supplier_sku_id"] == "highest"
    assert saved["erp_after"]["dimensions_cm"] == expected
    assert saved["write_verified"] is True


@pytest.mark.parametrize("sku_size, expected", [({"label": "蓝色 300×200×100毫米"}, "30x20x10"),
                                               ({"dimension_evidence_present": True}, None)])
def test_extension_server_accepts_label_size_and_blocks_invalid_sku_fallback(tmp_path, sku_size, expected):
    service = _service(tmp_path)
    config = service.config.load()
    config["writeback_enabled"] = True
    service.config.save(config)
    start(service, {"selection": {"start_product_id": ""}, "max_items": 1, "products": [{"erp_goods_id": "1001", "title": "商品",
                                                "main_image_url": "https://example.test/1.jpg"}]})
    result = detail(service, {"task_id": "1001", "detail": {**COMMON, "skus": [
        {"id": "one", "label": "蓝色", "price": "20", "raw_weight": "200g", **sku_size}]}})
    assert result["action"] == "writeback"
    assert result["changes"].get("dimensions_cm") == expected
    assert bool(service.store.get("1001")["dimensions_notice"]) is (expected is None)
