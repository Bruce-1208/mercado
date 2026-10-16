"""Original Yandex publishing tests. Every API is an in-memory fake."""
import asyncio
from copy import deepcopy
import json

import pytest

from erp.ai_original_yandex_publish import YandexOriginalPublisher
from yandex.app.yandex_api import StockTarget, YandexApiError


PARAMETERS = [
    {"id": 1, "name": "Цвет", "type": "ENUM", "required": True,
     "values": [{"id": 10, "value": "Белый"}, {"id": 11, "value": "Синий"}]},
    {"id": 2, "name": "Объем", "type": "NUMERIC", "required": True,
     "constraints": {"minValue": 0, "maxValue": 20},
     "unit": {"defaultUnitId": 42, "units": [{"id": 42, "name": "л", "fullName": "литр"}]}},
    {"id": 3, "name": "С крышкой", "type": "BOOLEAN", "required": True},
    {"id": 4, "name": "Материал", "type": "TEXT", "required": False, "constraints": {"maxLength": 20}},
]


class FakeClient:
    def __init__(self):
        self.calls = []
        self.parameters = deepcopy(PARAMETERS)
        self.business = {"onlyDefaultPrice": False, "currency": "CNY"}
        self.scopes = ["offers-and-cards-management", "pricing"]
        self.existing = []
        self.rejected = None
        self.fail_stock = False
        self.timeout_offer = False
        self.remote_status = "CHECKING"
        self.card_errors = []
        self.hidden = []
        self.card_error = None
        self.no_offer = False
        self.no_card = False
        self.stock_timeout = False

    @property
    def writes(self):
        return [call for call in self.calls if call[0] in {"stock", "resume"} or (call[0] == "request" and call[2].endswith(("/update", "/updates")))]

    async def get_category_parameters(self, business_id, category_id):
        self.calls.append(("schema", business_id, category_id))
        return deepcopy(self.parameters)

    async def get_token_info(self):
        self.calls.append(("token",))
        return {"auth_scopes": self.scopes}

    async def get_business_settings(self, business_id):
        self.calls.append(("settings", business_id))
        return deepcopy(self.business)

    async def resolve_stock_target(self, business_id, campaign_id, placement):
        self.calls.append(("target", business_id, campaign_id, placement))
        return StockTarget("business", 33, "指定仓库")

    async def _request(self, method, path, *, json_body=None, attempts=3):
        self.calls.append(("request", method, path, deepcopy(json_body), attempts))
        if path.endswith("/offer-mappings"):
            return {"status": "OK", "result": {"offerMappings": deepcopy(self.existing)}}
        if path.endswith("/offer-mappings/update"):
            if self.timeout_offer:
                raise TimeoutError("read timed out")
            if self.rejected:
                return {"status": "OK", "results": [{"offerId": "test", "errors": [{"code": self.rejected, "message": "参数不正确"}]}]}
            self.existing = deepcopy(json_body["offerMappings"])
            return {"status": "OK", "results": [{"warnings": [{"message": "等待审核"}]}]}
        return {"status": "OK"}

    async def update_offer_stock(self, business_id, campaign_id, sku, quantity, target):
        self.calls.append(("stock", business_id, campaign_id, sku, quantity, target.public_dict()))
        if self.stock_timeout:
            raise TimeoutError("stock write timed out")
        if self.fail_stock:
            raise YandexApiError("库存参数被拒绝", status_code=400)
        return {"status": "OK"}

    async def resume_offer_display(self, campaign_id, sku):
        self.calls.append(("resume", campaign_id, sku))
        return {"status": "OK"}

    async def get_campaign_offers(self, campaign_id, *, offer_ids):
        self.calls.append(("status", campaign_id, offer_ids))
        if self.no_offer:
            return {"offers": []}
        return {"offers": [{"offerId": offer_ids[0], "status": self.remote_status}]}

    async def get_offer_cards(self, business_id, offer_ids):
        self.calls.append(("card", business_id, offer_ids))
        if self.card_error:
            raise self.card_error
        if self.no_card:
            return []
        return [{"offerId": offer_ids[0], "mapping": {"marketSku": 789}, "cardStatus": "HAS_CARD", "errors": self.card_errors}]

    async def get_hidden_offer_ids(self, campaign_id, offer_ids):
        self.calls.append(("hidden", campaign_id, offer_ids))
        return list(self.hidden)


@pytest.fixture
def fixture():
    client = FakeClient()
    publisher = YandexOriginalPublisher({"business_id": 11, "campaign_id": 22, "placement_type": "FBS"}, "unused", client=client)
    row = {"id": 5, "source_item_id": "1688-5", "source_snapshot_json": json.dumps({
        "original_1688": {"title": "收纳盒", "variations": [], "source_url": "https://detail.1688.com/offer/5.html"},
        "marketplace_listings": {"yandex": {"status": "completed", "review_status": "approved", "title": "Белый контейнер для хранения",
            "description": "Контейнер для хранения небольших предметов дома.", "main_image_url": "/local/source.jpg"}},
    })}
    settings = {"sku": "original-5", "category_id": 123, "brand": "Brand", "price": 120, "currency": "CNY", "quantity": 5,
                "condition": "new", "package_length_cm": 10, "package_width_cm": 11, "package_height_cm": 12,
                "weight_g": 250, "image_url": "https://media.example.com/product-original.jpg", "gtin": "4006381333931",
                "additional_attributes": {"1": {"valueId": 10}, "2": 0, "3": False}}
    return publisher, client, row, settings


def test_schema_exposes_required_choices_and_units(fixture):
    publisher, client, _, _ = fixture
    schema = publisher.get_schema("123")
    assert schema["fields"][0] == {"id": "1", "label": "Цвет", "required": True, "type": "enum",
        "choices": [{"value": "10", "label": "Белый"}, {"value": "11", "label": "Синий"}],
        "multivalue": False, "allow_custom_values": False, "units": [], "default_unit_id": None,
        "constraints": {}, "value_restrictions": []}
    assert schema["fields"][1]["default_unit_id"] == 42
    assert not client.writes


def test_prepare_is_read_only_and_creates_original_offer_without_market_sku(fixture):
    publisher, client, row, settings = fixture
    prepared = publisher.prepare(row, settings)
    assert prepared["errors"] == []
    mapping = prepared["payload"]["offerMappings"][0]
    assert "mapping" not in mapping
    assert mapping["offer"]["pictures"] == [settings["image_url"]]
    assert mapping["offer"]["weightDimensions"] == {"length": 10, "width": 11, "height": 12, "weight": 0.25}
    assert mapping["offer"]["parameterValues"] == [
        {"parameterId": 1, "valueId": 10}, {"parameterId": 2, "value": "0", "unitId": 42}, {"parameterId": 3, "value": "false"}]
    assert prepared["price_scope"] == "campaign"
    assert prepared["stock_target"]["warehouse_id"] == 33
    assert not client.writes
    assert "unused" not in json.dumps(prepared)


@pytest.mark.parametrize("changes,expected", [
    ({"price": 10.3}, "售价"), ({"quantity": 0}, "库存"), ({"weight_g": "NaN"}, "重量"),
    ({"package_width_cm": -1}, "包装"), ({"currency": "USD"}, "币种"),
    ({"brand": ""}, "品牌"), ({"condition": "used"}, "新品"),
    ({"gtin": "4006381333932"}, "校验位"), ({"gtin": "fake"}, "条码"),
    ({"image_url": "http://media.example.com/a.jpg"}, "HTTPS"),
    ({"image_url": "https://127.0.0.1/a.jpg"}, "内网"),
    ({"image_url": "https://user:pass@example.com/a.jpg"}, "HTTPS"),
    ({"additional_attributes": {}}, "必填属性"),
    ({"additional_attributes": {"1": 999, "2": 1, "3": True}}, "枚举"),
    ({"additional_attributes": {"1": 10, "2": 999, "3": True}}, "范围"),
    ({"additional_attributes": {"1": 10, "2": 1, "3": "yes"}}, "true"),
    ({"additional_attributes": {"1": 10, "2": 1, "3": True, "99": "unknown"}}, "不属于"),
])
def test_invalid_settings_never_write(fixture, changes, expected):
    publisher, client, row, settings = fixture
    prepared = publisher.prepare(row, {**settings, **changes})
    assert any(expected in error for error in prepared["errors"])
    assert publisher.publish(prepared)["state"] == "failed"
    assert not client.writes


def test_official_parameter_values_and_dependency_validation(fixture):
    publisher, client, row, settings = fixture
    client.parameters[0]["valueRestrictions"] = [{"limitingParameterId": 4, "limitedValues": [{"limitingOptionValueId": 40, "optionValueIds": [11]}]}]
    client.parameters[3] = {"id": 4, "name": "Серия", "type": "ENUM", "values": [{"id": 40, "value": "Новая"}]}
    settings["additional_attributes"] = {"parameterValues": [{"parameterId": 1, "valueId": 10},
        {"parameterId": 2, "value": "2", "unitId": 42}, {"parameterId": 3, "value": "true"}, {"parameterId": 4, "valueId": 40}]}
    assert "不匹配" in publisher.prepare(row, settings)["errors"][0]
    settings["additional_attributes"]["parameterValues"][0]["valueId"] = 11
    assert publisher.prepare(row, settings)["errors"] == []


def test_multi_sku_source_must_be_split_even_with_one_generated_variant(fixture):
    publisher, client, row, settings = fixture
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["original_1688"]["variations"] = [{"sku_id": "a"}, {"sku_id": "b"}]
    snapshot["marketplace_listings"]["yandex"]["source_variations"] = [{"sku_id": "a"}]
    row["source_snapshot_json"] = snapshot
    assert any("多个 SKU" in error for error in publisher.prepare(row, settings)["errors"])
    assert not client.writes


@pytest.mark.parametrize("failure,expected", [("currency", "不一致"), ("scope", "写入权限"), ("warehouse", "平台仓"), ("collision", "不能覆盖")])
def test_account_or_existing_sku_preflight_blocks_submission(fixture, failure, expected):
    publisher, client, row, settings = fixture
    if failure == "currency": client.business["currency"] = "RUR"
    if failure == "scope": client.scopes = ["all-methods:read-only"]
    if failure == "warehouse": publisher.placement_type = "FBY"
    if failure == "collision": client.existing = [{"offer": {"offerId": settings["sku"], "tags": ["someone-else"]}}]
    assert expected in publisher.prepare(row, settings)["errors"][0]
    assert not client.writes


def test_success_is_only_submitted_until_status_is_published(fixture):
    publisher, client, row, settings = fixture
    result = publisher.publish(publisher.prepare(row, settings))
    assert result["state"] == "submitted"
    assert result["live"] is False
    assert result["remote_status"] == "CHECKING"
    assert result["steps"] == ["offer", "price", "stock", "resume"]
    assert client.writes[1][2] == "/v2/campaigns/22/offer-prices/updates"
    assert client.writes[2][1:5] == (11, 22, "original-5", 5)
    assert "等待审核" in result["warnings"]
    client.remote_status = "PUBLISHED"
    status = publisher.status("original-5")
    assert status["live"] is True
    assert status["remote_id"] == "789"


def test_business_pricing_scope_is_preserved_from_preview(fixture):
    publisher, client, row, settings = fixture
    client.business["onlyDefaultPrice"] = True
    prepared = publisher.prepare(row, settings)
    assert any("所有店铺" in warning for warning in prepared["warnings"])
    publisher.publish(prepared)
    assert client.writes[1][2] == "/v2/businesses/11/offer-prices/updates"


def test_nested_per_offer_error_cannot_be_mistaken_for_http_success(fixture):
    publisher, client, row, settings = fixture
    prepared = publisher.prepare(row, settings)
    client.rejected = "INVALID_PARAM"
    result = publisher.publish(prepared)
    assert result["state"] == "failed"
    assert result["steps"] == []
    assert result["failed_stage"] == "offer"
    assert len(client.writes) == 1


def test_partial_failure_keeps_sku_and_can_resume_by_resubmitting_same_offer(fixture):
    publisher, client, row, settings = fixture
    prepared = publisher.prepare(row, settings)
    client.fail_stock = True
    result = publisher.publish(prepared)
    assert result["state"] == "partial"
    assert result["steps"] == ["offer", "price"]
    assert result["failed_stage"] == "stock"
    assert result["submission_unknown"] is False
    assert result["retryable"] is True
    assert not any(call[0] == "resume" for call in client.writes)
    client.fail_stock = False
    retry = publisher.prepare(row, settings)
    assert retry["errors"] == []
    assert retry["sku"] == result["sku"]
    assert publisher.publish(retry)["state"] == "submitted"


def test_timeout_does_not_claim_definite_failure_or_generate_a_new_sku(fixture):
    publisher, client, row, settings = fixture
    prepared = publisher.prepare(row, settings)
    client.timeout_offer = True
    result = publisher.publish(prepared)
    assert result["state"] == "partial"
    assert result["submission_unknown"] is True
    assert result["retryable"] is False
    assert result["sku"] == prepared["sku"]
    assert len(client.writes) == 1


def test_collision_after_preview_and_mismatched_store_or_price_are_rejected(fixture):
    publisher, client, row, settings = fixture
    prepared = publisher.prepare(row, settings)
    wrong = deepcopy(prepared)
    wrong["campaign_id"] = 999
    assert publisher.publish(wrong)["state"] == "failed"
    wrong = deepcopy(prepared)
    wrong["price_payload"]["offers"][0]["offerId"] = "someone-else"
    assert publisher.publish(wrong)["state"] == "failed"
    client.existing = [{"offer": {"offerId": settings["sku"]}}]
    assert publisher.publish(prepared)["failed_stage"] == "preflight"
    assert not client.writes


@pytest.mark.parametrize("failure", ["hidden", "card", "rejected", "read"])
def test_status_never_calls_blocked_or_unverified_offer_live(fixture, failure):
    publisher, client, _, _ = fixture
    client.remote_status = "PUBLISHED"
    if failure == "hidden": client.hidden = ["original-5"]
    if failure == "card": client.card_errors = [{"message": "缺少证书"}]
    if failure == "rejected": client.remote_status = "REJECTED_BY_MARKET"
    if failure == "read": client.card_error = TimeoutError("timeout")
    result = publisher.status("original-5")
    assert result["live"] is False
    assert result["state"] == "partial"
    assert result["issues"]
    assert not client.writes


def test_sync_interface_can_be_used_inside_running_event_loop(fixture):
    publisher, _, _, _ = fixture
    async def call():
        return publisher.get_schema(123)
    assert asyncio.run(call())["category_id"] == 123


def test_network_unknown_after_an_accepted_offer_also_blocks_immediate_retry(fixture):
    publisher, client, row, settings = fixture
    prepared = publisher.prepare(row, settings)
    client.stock_timeout = True
    result = publisher.publish(prepared)
    assert result["submission_unknown"] is True
    assert result["retryable"] is False
    assert result["steps"] == ["offer", "price"]


def test_eventually_consistent_empty_read_keeps_submission_unknown(fixture):
    publisher, client, _, _ = fixture
    client.no_offer = client.no_card = True
    result = publisher.status("original-5")
    assert result["not_found"] is True
    assert result["submission_unknown"] is True
    assert result["retryable"] is False
    assert result["state"] == "submitted"
    assert result["remote_id"] == ""
    client.no_card = False
    result = publisher.status("original-5")
    assert result["submission_unknown"] is False
    assert result["not_found"] is False


def test_eventually_consistent_404_does_not_authorize_a_resend(fixture):
    publisher, client, _, _ = fixture
    client.no_offer = True
    client.card_error = YandexApiError("not found", status_code=404)
    result = publisher.status("original-5")
    assert result["not_found"] is True
    assert result["submission_unknown"] is True
    assert result["retryable"] is False


def test_error_messages_redact_credentials(fixture):
    _, client, row, settings = fixture
    publisher = YandexOriginalPublisher({"business_id": 11, "campaign_id": 22, "placement_type": "FBS",
        "client_secret": "saved-client-secret"}, "real-yandex-token", client=client)
    client.card_error = RuntimeError('Api-Key: real-yandex-token; client_secret="saved-client-secret"; Bearer unrelated-secret; token=unknown-secret')
    result = json.dumps(publisher.status("original-5"), ensure_ascii=False)
    for secret in ("real-yandex-token", "saved-client-secret", "unrelated-secret", "unknown-secret"):
        assert secret not in result
    assert "已隐藏凭证" in result


def test_exactly_one_source_variant_remains_publishable(fixture):
    publisher, _, row, settings = fixture
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["original_1688"]["variations"] = [{"sku_id": "a"}]
    row["source_snapshot_json"] = snapshot
    assert publisher.prepare(row, settings)["errors"] == []
