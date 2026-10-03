import json
from copy import deepcopy

import pytest
import requests

from erp.ai_original_amazon_publish import (
    AmazonOriginalPublisher, AmazonPublishError, US_MARKETPLACE_ID,
)


CREDS = {"seller_id": "SELLER123", "client_id": "client-id", "client_secret": "private-client-secret",
         "refresh_token": "private-refresh-token", "marketplace_id": US_MARKETPLACE_ID}
MKT = US_MARKETPLACE_ID


def scalar_field(value_type="string", **extra):
    return {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["value"],
            "properties": {"value": {"type": value_type, **extra}, "marketplace_id": {"const": MKT},
                           "language_tag": {"const": "en_US"}}}}


def schema():
    return {
        "$schema": "https://schemas.amazon.com/selling-partners/definitions/product-types/meta-schema/v1",
        "type": "object", "required": ["item_name", "brand", "material", "condition_type", "purchasable_offer"],
        "properties": {
            "item_name": scalar_field(maxLength=75), "product_description": scalar_field(),
            "item_highlights": scalar_field(maxLength=125), "bullet_point": scalar_field(),
            "brand": scalar_field(), "material": {"title": "Material", **scalar_field(enum=["Plastic", "Metal"])},
            "condition_type": scalar_field(enum=["new_new", "used_very_good"]),
            "supplier_declared_has_product_identifier_exemption": scalar_field("boolean"),
            "externally_assigned_product_identifier": {"type": "array"},
            "main_product_image_locator": {"type": "array"},
            "purchasable_offer": {"type": "array"}, "fulfillment_availability": {"type": "array"},
            "battery_required": scalar_field("boolean"), "battery_type": scalar_field(),
        },
        "allOf": [{"if": {"required": ["battery_required"], "properties": {"battery_required": {
            "contains": {"properties": {"value": {"const": True}}, "required": ["value"]}}}},
            "then": {"required": ["battery_type"]}}],
    }


def row():
    return {"id": 7, "source_snapshot_json": {"original_1688": {"source_1688_item_id": "123456789"},
        "marketplace_listings": {"amazon": {"title": "Desktop Storage Box", "description": "A box for desktop accessories.",
        "bullet_points": ["Stores stationery and small accessories"], "item_highlights": "Desktop storage",
        "main_image_url": "/api/ai-original-products/images/123.jpg", "attributes": [], "status": "completed"}}}}


def settings(**updates):
    return {"sku": "aip-7", "category_id": "STORAGE_BOX", "brand": "Acme", "gtin": "012345678905",
            "gtin_exemption": False, "condition": "new_new", "image_url": "https://cdn.example.com/box.jpg",
            "price": "12.99", "currency": "USD", "quantity": 4,
            "additional_attributes": {"material": [{"value": "Plastic", "language_tag": "en_US", "marketplace_id": MKT}]},
            **updates}


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
    def json(self):
        return deepcopy(self.body)


class Transport:
    def __init__(self):
        self.calls = []
        self.schema = schema()
        self.preview = {"status": "VALID", "issues": [], "submissionId": "preview-id"}
        self.submission = {"status": "ACCEPTED", "issues": [], "submissionId": "submission-id"}
        self.current = Response({"errors": [{"code": "NOT_FOUND"}]}, 404)
        self.lwa_response = Response({"access_token": "private-access-token", "expires_in": 3600})
        self.submit_exception = None
        self.submit_status = 200
        self.schema_link = "https://schemas.s3.amazonaws.com/storage-box.json?signature=public-download"
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        assert kwargs["allow_redirects"] is False
        if url == "https://api.amazon.com/auth/o2/token":
            return self.lwa_response
        if "/definitions/" in url:
            return Response({"schema": {"link": {"resource": self.schema_link}},
                             "productTypeVersion": {"version": "schema-v1"}})
        if "schemas.s3.amazonaws.com" in url:
            assert "x-amz-access-token" not in kwargs["headers"]
            return Response(self.schema)
        if method == "PUT" and kwargs["params"].get("mode") == "VALIDATION_PREVIEW":
            return Response(self.preview)
        if method == "PUT":
            if self.submit_exception:
                raise self.submit_exception
            return Response(self.submission, self.submit_status)
        if method == "GET":
            return self.current if isinstance(self.current, Response) else Response(self.current)
        pytest.fail(f"unexpected mocked API call: {method} {url}")
    @property
    def writes(self):
        return [call for call in self.calls if call[0] == "PUT" and call[2]["params"].get("mode") != "VALIDATION_PREVIEW"]
    @property
    def previews(self):
        return [call for call in self.calls if call[0] == "PUT" and call[2]["params"].get("mode") == "VALIDATION_PREVIEW"]


@pytest.fixture
def service(monkeypatch):
    transport = Transport()
    publisher = AmazonOriginalPublisher(CREDS, request=transport)
    monkeypatch.setattr(publisher, "_throttle_preview", lambda: None)
    return publisher, transport


def test_schema_uses_seller_specific_current_standalone_requirements(service):
    publisher, transport = service
    data = publisher.get_schema("storage_box")
    field = next(field for field in data["fields"] if field["id"] == "material")
    assert field["required"] is True
    assert field["choices"] == [{"value": "Plastic", "label": "Plastic"}, {"value": "Metal", "label": "Metal"}]
    ptd = next(call for call in transport.calls if "/definitions/" in call[1])
    assert ptd[2]["params"] == {"sellerId": "SELLER123", "marketplaceIds": MKT, "requirements": "LISTING",
        "requirementsEnforced": "ENFORCED", "locale": "en_US", "productTypeVersion": "LATEST", "parentageLevel": "NONE"}
    assert ptd[2]["headers"]["x-amz-access-token"] == "private-access-token"
    assert "Authorization" not in ptd[2]["headers"]
    assert "private-access-token" not in json.dumps(data)
    assert "private-client-secret" not in json.dumps(data)


def test_prepare_is_read_only_preview_and_maps_real_payload(service):
    publisher, transport = service
    source = row()
    prepared = publisher.prepare(source, settings())
    assert not prepared["errors"]
    assert not transport.writes
    assert len(transport.previews) == 1
    payload = prepared["payload"]
    assert payload["productType"] == "STORAGE_BOX"
    assert payload["requirements"] == "LISTING"
    attrs = payload["attributes"]
    assert attrs["brand"][0]["value"] == "Acme"
    assert attrs["externally_assigned_product_identifier"][0] == {"value": "012345678905", "type": "upc", "marketplace_id": MKT}
    assert attrs["purchasable_offer"][0]["our_price"] == [{"schedule": [{"value_with_tax": 12.99}]}]
    assert attrs["fulfillment_availability"] == [{"fulfillment_channel_code": "DEFAULT", "quantity": 4}]
    assert attrs["main_product_image_locator"][0]["media_location"] == "https://cdn.example.com/box.jpg"
    assert attrs["item_highlights"][0]["value"] == "Desktop storage"
    assert source == row()


@pytest.mark.parametrize("changes,expected", [
    ({"brand": ""}, "真实品牌"), ({"condition": ""}, "商品状况"),
    ({"gtin": ""}, "真实 GTIN"), ({"gtin": "012345678906"}, "校验位"),
    ({"gtin_exemption": True}, "不可同时"), ({"gtin_exemption": "true"}, "布尔值"),
    ({"price": "NaN"}, "美元售价"), ({"price": "0"}, "美元售价"),
    ({"price": "1e10000"}, "美元售价"),
    ({"quantity": "2.2"}, "整数库存"), ({"quantity": True}, "整数库存"),
    ({"currency": "CNY"}, "USD"), ({"image_url": "http://cdn.example.com/a.jpg"}, "HTTPS"),
    ({"image_url": "https://127.0.0.1/a.jpg"}, "内网"), ({"image_url": "https://10.0.0.1/a.jpg"}, "内网"),
    ({"sku": ""}, "SKU"), ({"additional_attributes": []}, "JSON 对象"),
])
def test_bad_seller_inputs_never_call_external_api(service, changes, expected):
    publisher, transport = service
    result = publisher.prepare(row(), settings(**changes))
    assert expected in " ".join(result["errors"])
    assert transport.calls == []


def test_explicit_approved_exemption_never_invents_barcode(service):
    publisher, _ = service
    result = publisher.prepare(row(), settings(gtin="", gtin_exemption=True))
    assert not result["errors"]
    attrs = result["payload"]["attributes"]
    assert "externally_assigned_product_identifier" not in attrs
    assert attrs["supplier_declared_has_product_identifier_exemption"][0]["value"] is True
    assert any("不会申请" in warning for warning in result["warnings"])


def test_unexpanded_supplier_variations_block_publish(service):
    publisher, transport = service
    source = row()
    source["source_snapshot_json"]["original_1688"]["variations"] = [{"sku_id": "red"}, {"sku_id": "blue"}]
    result = publisher.prepare(source, settings())
    assert "单 SKU" in " ".join(result["errors"])
    assert not transport.calls


def test_dynamic_required_enum_and_conditional_fields_block_before_preview(service):
    publisher, transport = service
    missing = publisher.prepare(row(), settings(additional_attributes={}))
    assert "material" in " ".join(missing["errors"])
    bad_enum = publisher.prepare(row(), settings(additional_attributes={"material": [{"value": "Wood"}]}))
    assert "Wood" in " ".join(bad_enum["errors"])
    extras = deepcopy(settings()["additional_attributes"])
    extras["battery_required"] = [{"value": True}]
    conditional = publisher.prepare(row(), settings(additional_attributes=extras))
    assert "battery_type" in " ".join(conditional["errors"])
    assert not transport.previews and not transport.writes


def test_extra_attributes_cannot_override_price_or_create_variations(service):
    publisher, transport = service
    result = publisher.prepare(row(), settings(additional_attributes={"purchasable_offer": [], "variation_theme": []}))
    assert "不能覆盖" in " ".join(result["errors"])
    assert not transport.calls


def test_official_invalid_preview_blocks_actual_submission(service):
    publisher, transport = service
    transport.preview = {"status": "INVALID", "issues": [{"severity": "ERROR", "code": "90220", "message": "model_name is required"}]}
    prepared = publisher.prepare(row(), settings())
    assert prepared["errors"] == ["model_name is required"]
    result = publisher.publish(prepared)
    assert result["state"] == "failed"
    assert not transport.writes


def test_preview_must_return_valid_not_accepted(service):
    publisher, transport = service
    transport.preview = {"status": "ACCEPTED", "issues": []}
    prepared = publisher.prepare(row(), settings())
    assert "VALID" in " ".join(prepared["errors"])
    assert not transport.writes


def test_publish_rechecks_schema_and_preview_then_reports_accepted_not_live(service):
    publisher, transport = service
    prepared = publisher.prepare(row(), settings())
    result = publisher.publish(prepared)
    assert result["state"] == "submitted"
    assert result["submission_id"] == "submission-id"
    assert result["live"] is False
    assert result["buyable"] is False
    assert len(transport.previews) == 2
    assert len(transport.writes) == 1
    assert sum("/definitions/" in call[1] for call in transport.calls) == 2
    assert sum("auth/o2/token" in call[1] for call in transport.calls) == 1
    assert transport.previews[-1][2]["json"] == transport.writes[0][2]["json"]


def test_changed_schema_prevents_submission_after_old_preview(service):
    publisher, transport = service
    prepared = publisher.prepare(row(), settings())
    transport.schema["required"].append("new_required_compliance_attribute")
    result = publisher.publish(prepared)
    assert result["state"] == "failed"
    assert "new_required_compliance_attribute" in result["message"]
    assert not transport.writes


def test_current_preview_failure_prevents_submission_after_old_preview(service):
    publisher, transport = service
    prepared = publisher.prepare(row(), settings())
    transport.preview = {"status": "INVALID", "issues": [{"severity": "ERROR", "message": "restricted product"}]}
    result = publisher.publish(prepared)
    assert result["state"] == "failed"
    assert not transport.writes


def test_real_put_timeout_has_unknown_result_and_is_not_retried(service):
    publisher, transport = service
    prepared = publisher.prepare(row(), settings())
    transport.submit_exception = requests.Timeout("private-access-token hidden")
    result = publisher.publish(prepared)
    assert result["state"] == "partial"
    assert result["submission_unknown"] is True
    assert len(transport.writes) == 1
    assert "private-access-token" not in json.dumps(result)


def test_error_responses_redact_tokens(service):
    publisher, transport = service
    prepared = publisher.prepare(row(), settings())
    transport.submit_status = 403
    transport.submission = {"errors": [{"message": "private-access-token private-refresh-token private-client-secret"}]}
    result = publisher.publish(prepared)
    assert result["state"] == "failed"
    assert "[redacted]" in result["message"]
    assert all(secret not in json.dumps(result) for secret in (CREDS["client_secret"], CREDS["refresh_token"], "private-access-token"))


def test_lwa_auth_failure_does_not_expose_body(service):
    publisher, transport = service
    transport.lwa_response = Response({"error_description": "private-refresh-token"}, 400)
    prepared = publisher.prepare(row(), settings())
    assert "LWA" in " ".join(prepared["errors"])
    assert "private-refresh-token" not in json.dumps(prepared)
    assert not transport.previews


def test_schema_download_does_not_follow_untrusted_url(service):
    publisher, transport = service
    transport.schema_link = "https://127.0.0.1/private"
    prepared = publisher.prepare(row(), settings())
    assert "schema 下载地址" in " ".join(prepared["errors"])
    assert not any("127.0.0.1" in call[1] for call in transport.calls)


def test_remote_schema_references_fail_closed(service):
    publisher, transport = service
    transport.schema["allOf"].append({"$ref": "https://untrusted.example.com/schema"})
    result = publisher.prepare(row(), settings())
    assert "无法完整校验" in " ".join(result["errors"])
    assert not transport.previews


def test_status_requires_buyable_and_surfaces_async_issues(service):
    publisher, transport = service
    transport.current = {"sku": "aip-7", "summaries": [{"marketplaceId": MKT, "asin": "B123456789",
                         "status": ["DISCOVERABLE"]}], "issues": []}
    result = publisher.status("aip-7")
    assert result["remote_id"] == "B123456789"
    assert result["discoverable"] is True and result["live"] is False
    transport.current["summaries"][0]["status"].append("BUYABLE")
    assert publisher.status("aip-7")["live"] is True
    transport.current["summaries"][0]["status"] = []
    transport.current["issues"] = [{"severity": "ERROR", "code": "90220", "message": "Missing compliance documentation"}]
    blocked = publisher.status("aip-7")
    assert blocked["state"] == "partial"
    assert blocked["live"] is False
    assert blocked["errors"] == ["Missing compliance documentation"]


def test_status_not_found_preserves_unknown_submission(service):
    publisher, transport = service
    transport.current = Response({"errors": [{"code": "NOT_FOUND"}]}, 404)
    result = publisher.status("aip-7")
    assert result["state"] == "partial" and result["not_found"] is True
    assert result["submission_unknown"] is True and result["retryable"] is False


def test_only_us_marketplace_and_complete_auth_supported():
    with pytest.raises(AmazonPublishError, match="美国站"):
        AmazonOriginalPublisher({**CREDS, "marketplace_id": "other"})
    with pytest.raises(AmazonPublishError, match="凭证不完整"):
        AmazonOriginalPublisher({**CREDS, "refresh_token": ""})


def test_existing_unbound_sku_is_never_overwritten(service):
    publisher, transport = service
    transport.current = {"sku": "aip-7", "summaries": [{"marketplaceId": MKT, "asin": "B123456789"}]}
    result = publisher.prepare(row(), settings())
    assert "避免覆盖" in " ".join(result["errors"])
    assert not transport.previews and not transport.writes


def test_existing_bound_sku_can_be_retried_only_with_server_binding(service):
    publisher, transport = service
    transport.current = {"sku": "aip-7", "summaries": [{"marketplaceId": MKT, "asin": "B123456789"}]}
    result = publisher.prepare(row(), settings(_known_sku=True))
    assert not result["errors"]
    assert publisher.publish(result)["state"] == "submitted"
    assert len(transport.writes) == 1


def test_sku_created_externally_after_preview_is_not_overwritten(service):
    publisher, transport = service
    prepared = publisher.prepare(row(), settings())
    transport.current = {"sku": "aip-7", "summaries": [{"marketplaceId": MKT, "asin": "B123456789"}]}
    result = publisher.publish(prepared)
    assert result["state"] == "failed"
    assert "避免覆盖" in result["message"]
    assert not transport.writes


def test_server_error_during_real_put_is_uncertain_not_automatically_retried(service):
    publisher, transport = service
    prepared = publisher.prepare(row(), settings())
    transport.submit_status = 503
    transport.submission = {"errors": [{"message": "Temporary failure"}]}
    result = publisher.publish(prepared)
    assert result["state"] == "partial" and result["submission_unknown"] is True
    assert len(transport.writes) == 1


def test_unmapped_item_highlights_are_explicitly_reported(service):
    publisher, transport = service
    del transport.schema["properties"]["item_highlights"]
    result = publisher.prepare(row(), settings())
    assert not result["errors"]
    assert "item_highlights" not in result["payload"]["attributes"]
    assert any("未上传" in item for item in result["warnings"])


def test_user_supplied_schema_attributes_must_be_finite_json(service):
    publisher, transport = service
    result = publisher.prepare(row(), settings(additional_attributes={"weight": float("nan")}))
    assert "NaN" in " ".join(result["errors"])
    assert not transport.calls


def test_prepared_payload_cannot_be_submitted_to_a_different_seller(service):
    publisher, transport = service
    prepared = publisher.prepare(row(), settings())
    other = AmazonOriginalPublisher({**CREDS, "seller_id": "OTHER_SELLER"}, request=transport)
    result = other.publish(prepared)
    assert result["state"] == "failed"
    assert not transport.writes


def test_failed_generation_is_not_published_using_leftover_copy(service):
    publisher, transport = service
    source = row()
    source["source_snapshot_json"]["marketplace_listings"]["amazon"]["status"] = "failed"
    result = publisher.prepare(source, settings())
    assert "生成失败" in " ".join(result["errors"])
    assert not transport.calls


def test_single_supplier_sku_does_not_require_variant_expansion(service):
    publisher, _ = service
    source = row()
    source["source_snapshot_json"]["original_1688"]["variations"] = [{"sku_id": "only-sku"}]
    result = publisher.prepare(source, settings())
    assert not result["errors"]
